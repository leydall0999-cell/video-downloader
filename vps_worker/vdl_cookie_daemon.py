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
from bilibili_ecs_cookie import push_once  # noqa: E402
from douyin_resolve import resolve as douyin_resolve  # noqa: E402
from kuaishou_resolve import resolve as kuaishou_resolve  # noqa: E402
from weibo_resolve import resolve as weibo_resolve  # noqa: E402
from ximalaya_album_resolve import resolve_album as ximalaya_album_resolve  # noqa: E402

API_TOKEN = os.environ.get("VDL_COOKIE_API_TOKEN", "")
API_PORT = int(os.environ.get("VDL_COOKIE_API_PORT", "18731"))
SYNC_URL = os.environ.get("VDL_COOKIE_SYNC_URL", "https://hanyuxz.top")
SYNC_TOKEN = os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
SESSDATA = os.environ.get("VDL_BILI_SESSDATA", "")
INTERVAL = int(os.environ.get("VDL_COOKIE_PUSH_INTERVAL", "120"))

_lock = threading.Lock()
# 全局 Playwright 串行锁：cookie 推送与各平台解析都跑无头浏览器，
# 共用一个锁保证任何时刻只有一个 Chromium 实例（否则并发会卡死 VPS）。
_push_lock = threading.Lock()
_state = {"ts": 0, "ok": None, "msg": ""}

# 平台 → Playwright 解析函数映射（新增平台在此注册即可）
_RESOLVERS = {
    "douyin": douyin_resolve,
    "kuaishou": kuaishou_resolve,
    "weibo": weibo_resolve,
    "ximalaya_album": ximalaya_album_resolve,
}


def do_push(wait: bool = True):
    """推送一次。wait=True 时若已有推送在进行则阻塞等待；wait=False 时立即返回 busy。"""
    if not _push_lock.acquire(blocking=False):
        if not wait:
            return False, {"error": "push already in progress"}
        _push_lock.acquire()  # 阻塞等待上一轮完成
    try:
        header, resp = push_once(SYNC_URL, SYNC_TOKEN, SESSDATA)
        ok = bool(resp and resp.get("ok"))
        with _lock:
            _state.update(ts=int(time.time()), ok=ok, msg=str(resp)[:200])
        return ok, resp
    except Exception as e:
        with _lock:
            _state.update(ts=int(time.time()), ok=False, msg=str(e)[:200])
        return False, {"error": str(e)[:200]}
    finally:
        _push_lock.release()


def do_resolve(platform, url):
    """用 Playwright 解析指定平台视频。复用 _push_lock 保证全局串行。"""
    resolver = _RESOLVERS.get(platform)
    if resolver is None:
        return False, "不支持的平台: %s" % platform
    if not _push_lock.acquire(blocking=False):
        _push_lock.acquire()  # 阻塞等待上一轮 Playwright 完成
    try:
        return True, resolver(url)
    except Exception as e:
        return False, str(e)[:300]
    finally:
        _push_lock.release()


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
        return bool(API_TOKEN) and token == API_TOKEN

    def do_GET(self):
        p = urlparse(self.path)
        path = p.path
        q = parse_qs(p.query)
        if path == "/healthz":
            self._send(200, {"ok": True, "ts": int(time.time())})
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
            self._send(200 if ok else 502, result if ok else {"error": result})
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
