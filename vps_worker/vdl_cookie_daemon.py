#!/usr/bin/env python3
"""VDL Cookie 守护进程（VPS 常驻）。

职责：
  1) 后台线程每 VDL_COOKIE_PUSH_INTERVAL 秒（默认 120）循环推送 B站 Cookie 到网页版公共池；
  2) 提供轻量 HTTP 接口，供 Railway 在「容器重建/重启后公共池被清空」时主动召唤立即补推：
       POST /v1/push?token=<VDL_COOKIE_API_TOKEN>  -> 立即推送一次并返回结果
       GET  /healthz                               -> 200 健康检查
  3) 提供多平台 Playwright 解析接口（绕开已失效的 yt-dlp 提取器）：
       GET /v1/resolve?token=<token>&platform=<douyin|kuaishou>&url=<链接>
       （兼容旧路径 GET /v1/douyin/resolve、/v1/kuaishou/resolve）
  3.5) GET /v1/pull-cookie?token=<token> 返回本地缓存的 B站 Cookie，供 Railway 经
       反向隧道主动拉取（替代 VPS 主动推送，规避 VPS→Railway 跨境链路抖动）。

配置来自 .cookie_sync.env：VDL_COOKIE_SYNC_URL / VDL_COOKIE_SYNC_TOKEN / VDL_BILI_SESSDATA
  + VDL_COOKIE_API_TOKEN（Railway 调用 /v1/push、/v1/resolve 的令牌，务必与
    Railway 的 VDL_COOKIE_REFILL_TOKEN 一致）
  + VDL_COOKIE_API_PORT（默认 18731）/ VDL_COOKIE_PUSH_INTERVAL（默认 120）
"""
import os
import sys
import time
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bilibili_ecs_cookie import push_once, _collect_cookies, _push_to_cloud  # noqa: E402
from douyin_resolve import resolve as douyin_resolve  # noqa: E402
from kuaishou_resolve import resolve as kuaishou_resolve  # noqa: E402
from weibo_resolve import resolve as weibo_resolve  # noqa: E402
from ximalaya_album_resolve import resolve_album as ximalaya_album_resolve  # noqa: E402
from iqiyi_resolve import resolve as iqiyi_resolve  # noqa: E402
from douyu_resolve import resolve as douyu_resolve  # noqa: E402
from ysp_resolve import resolve as ysp_resolve  # noqa: E402
from m1905_resolve import resolve as m1905_resolve  # noqa: E402
from fun_resolve import resolve as fun_resolve  # noqa: E402
from bestv_resolve import resolve as bestv_resolve  # noqa: E402


def _load_env_file(path: str) -> None:
    """启动时自加载同目录 .cookie_sync.env（VAR=value 或 VAR='value' 格式）。

    不覆盖已存在的环境变量（显式注入的优先）。这样无论 daemon 由 systemd /
    nohup / 手动哪种方式启动，配置都可靠加载，不会因外部 source 失效而丢 token。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
    except Exception:
        pass


_load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cookie_sync.env"))

API_TOKEN = os.environ.get("VDL_COOKIE_API_TOKEN", "")
API_PORT = int(os.environ.get("VDL_COOKIE_API_PORT", "18731"))
SYNC_URL = os.environ.get("VDL_COOKIE_SYNC_URL", "https://hanyuxz.top")
SYNC_TOKEN = os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
SESSDATA = os.environ.get("VDL_BILI_SESSDATA", "")
INTERVAL = int(os.environ.get("VDL_COOKIE_PUSH_INTERVAL", "120"))

_lock = threading.Lock()
# 推送锁：仅串行化「云端 Cookie 推送」自身（推送不启动 Chromium）。
_push_lock = threading.Lock()
# 解析锁：串行化各平台 Playwright 解析，确保任意时刻只有一个 Chromium 实例
# （VPS 内存吃紧，并发会卡死）。注意：解析与推送各用各的锁，互不阻塞——
# 否则推送因网络抖动（urlopen 无 timeout）长期占锁会把所有 /v1/resolve 饿死。
_resolve_lock = threading.Lock()
_state = {"ts": 0, "ok": None, "msg": ""}
# 主动推送开关：默认开启；设为 false 时仅收集并缓存 Cookie（供 Railway 经隧道拉取），
# 不再直连 Railway 推送——规避 VPS→Railway 跨境公网链路抖动导致的推送失败。
PUSH_ENABLED = os.environ.get("VDL_COOKIE_PUSH_ENABLED", "true").lower() in ("1", "true", "yes")
# 本地缓存有效期（秒）：/v1/pull-cookie 在有效期内直接返回缓存，避免每次起 Chromium。
COOKIE_TTL = int(os.environ.get("VDL_COOKIE_CACHE_TTL", "1800"))
# 最近一次收集到的 B站 Cookie 缓存（供 Railway 经隧道 /v1/pull-cookie 拉取）。
_cached = {"cookie": "", "cookies": [], "ts": 0, "passed": False, "hit_slider": False}

# 平台 → Playwright 解析函数映射（新增平台在此注册即可）
_RESOLVERS = {
    "douyin": douyin_resolve,
    "kuaishou": kuaishou_resolve,
    "weibo": weibo_resolve,
    "ximalaya_album": ximalaya_album_resolve,
    "iqiyi": iqiyi_resolve,
    "douyu": douyu_resolve,
    "yangshipin": ysp_resolve,
    "1905": m1905_resolve,
    "funshion": fun_resolve,
    "bestv": bestv_resolve,
}


PUSH_RETRIES = int(os.environ.get("VDL_COOKIE_PUSH_RETRIES", "2"))
PUSH_RETRY_BACKOFF = int(os.environ.get("VDL_COOKIE_PUSH_BACKOFF", "15"))


def _collect_and_cache():
    """串行起 Chromium 收集一次 B站 Cookie 并写入 _cached（不推送）。

    用 _resolve_lock 串行化，避免与 /v1/resolve 并发起多个 Chromium 撑爆 VPS 内存。
    """
    if not _resolve_lock.acquire(blocking=False):
        _resolve_lock.acquire()
    try:
        header, passed, hit_slider, cookies = _collect_cookies(SESSDATA)
        with _lock:
            _cached.update(cookie=header, cookies=cookies, ts=int(time.time()),
                           passed=passed, hit_slider=hit_slider)
        return header
    finally:
        _resolve_lock.release()


def _push_once_to_cloud(header):
    """推送一次到云端，返回 (ok, msg)。仅封装底层，不含重试。"""
    try:
        resp = _push_to_cloud(SYNC_URL, SYNC_TOKEN, header)
        return bool(resp and resp.get("ok")), str(resp)[:200]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:200]


def do_push(wait: bool = True):
    """收集并（可选）推送一次 B站 Cookie。

    - 收集始终执行并写入 _cached（Railway 可经隧道拉取，独立于推送成败）。
    - 推送受 VDL_COOKIE_PUSH_ENABLED 开关控制：默认开启，采用「重试 + 退避」，
      且推送与解析已拆锁（见 _resolve_lock），推送抖动不再饿死 /v1/resolve。
    - 推送失败仅更新状态、不刷屏（避免 VPS→Railway 跨境链路抖动时日志刷屏）。
    - 关闭主动推送（PUSH_ENABLED=false）时仅维持缓存供 Railway 拉取。
    """
    if not PUSH_ENABLED:
        try:
            _collect_and_cache()
        except Exception as e:  # noqa: BLE001
            with _lock:
                _state.update(ts=int(time.time()), ok=False,
                              msg="collect failed: %s" % str(e)[:120])
        return False, {"error": "push disabled (Railway pulls instead)"}
    if not _push_lock.acquire(blocking=False):
        if not wait:
            return False, {"error": "push already in progress"}
        _push_lock.acquire()  # 阻塞等待上一轮完成
    try:
        try:
            header = _collect_and_cache()
        except Exception as e:  # noqa: BLE001
            with _lock:
                _state.update(ts=int(time.time()), ok=False,
                              msg="collect failed: %s" % str(e)[:120])
            return False, {"error": "collect failed: %s" % str(e)[:120]}
        last_err = None
        for attempt in range(1, PUSH_RETRIES + 1):
            ok, msg = _push_once_to_cloud(header)
            if ok:
                with _lock:
                    _state.update(ts=int(time.time()), ok=True, msg="pushed ok")
                return True, {"ok": True, "msg": msg}
            last_err = msg
            if attempt < PUSH_RETRIES:
                time.sleep(PUSH_RETRY_BACKOFF)
        with _lock:
            _state.update(ts=int(time.time()), ok=False,
                          msg="push failed after %d tries: %s" % (PUSH_RETRIES, last_err))
        return False, {"error": "push failed after %d tries: %s" % (PUSH_RETRIES, last_err)}
    finally:
        _push_lock.release()


def do_resolve(platform, url):
    """用 Playwright 解析指定平台视频。用 _resolve_lock 串行化 Chromium，
    与推送锁独立，避免被云端推送的网络抖动饿死。"""
    resolver = _RESOLVERS.get(platform)
    if resolver is None:
        return False, "不支持的平台: %s" % platform
    if not _resolve_lock.acquire(blocking=False):
        _resolve_lock.acquire()  # 阻塞等待上一轮 Chromium 完成
    try:
        return True, resolver(url)
    except Exception as e:
        return False, str(e)[:300]
    finally:
        _resolve_lock.release()


def _loop():
    while True:
        do_push()
        time.sleep(INTERVAL)


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_token(self, q):
        token = q.get("token", [""])[0]
        if not token:
            return False
        # 接受两种 token：本机 API token（VDL_COOKIE_API_TOKEN），或共享的同步
        # token（VDL_COOKIE_SYNC_TOKEN）。Railway 经隧道拉取时复用共享 sync token 即可，
        # 无需在两端另配独立密钥。
        if API_TOKEN and token == API_TOKEN:
            return True
        if SYNC_TOKEN and token == SYNC_TOKEN:
            return True
        return False

    def do_GET(self):
        p = urlparse(self.path)
        path = p.path
        q = parse_qs(p.query)
        if path == "/healthz":
            self._send(200, {"ok": True, "ts": int(time.time())})
            return
        # 供 Railway 经反向隧道主动拉取本地缓存的 B站 Cookie（替代 VPS 主动推送，
        # 规避 VPS→Railway 跨境公网链路抖动）。Railway 经本机隧道代理 127.0.0.1:18889
        # 转发到本机 127.0.0.1:18731 即达此端点。
        if path == "/v1/pull-cookie":
            if not self._check_token(q):
                self._send(403, {"error": "forbidden"})
                return
            with _lock:
                fresh = _cached.get("cookie") and (time.time() - _cached["ts"]) < COOKIE_TTL
            if not fresh:
                try:
                    _collect_and_cache()
                except Exception:  # noqa: BLE001
                    pass  # 收集失败也尽量返回已有缓存
            with _lock:
                c = dict(_cached)
            self._send(200, {
                "ok": True,
                "domain": "bilibili.com",
                "cookie": c.get("cookie", ""),
                "cookies": c.get("cookies", []),
                "ts": c.get("ts", 0),
                "passed": c.get("passed", False),
                "hit_slider": c.get("hit_slider", False),
            })
            return
        # 通用 resolve 端点：/v1/resolve?platform=xx&url=yy
        # 兼容旧路径 /v1/<platform>/resolve?url=yy
        platform = None
        if path == "/v1/resolve":
            platform = q.get("platform", [""])[0]
        elif path.startswith("/v1/") and path.endswith("/resolve"):
            platform = path[len("/v1/"):-len("/resolve")]
        if platform is not None:
            if not self._check_token(q):
                self._send(403, {"error": "forbidden"})
                return
            url = q.get("url", [""])[0]
            if not url:
                self._send(400, {"error": "missing url"})
                return
            ok, result = do_resolve(platform, url)
            if not ok:
                # 业务失败（链接无效/需登录/视频删除/未开播等）返回 200 + ok:false，
                # 让 Railway 端 _call_vps_worker 透传真实业务原因；只有解析器
                # 内部异常才返回 5xx。
                self._send(200, {"ok": False, "error": result})
                return
            # 成功：透传 worker 返回的 dict（worker 内部自带 ok:true）
            self._send(200, result)
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == "/v1/push":
            q = parse_qs(p.query)
            if not self._check_token(q):
                self._send(403, {"error": "forbidden"})
                return
            ok, resp = do_push(wait=False)
            with _lock:
                st = dict(_state)
            self._send(200, {"ok": ok, "result": resp, "last": st})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def main():
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", API_PORT), _Handler)
    print(f"✅ cookie daemon up: loop={INTERVAL}s api=0.0.0.0:{API_PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
