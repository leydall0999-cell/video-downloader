"""server/routers/share.py — 扫码分享（本机文件 → 短链 + 二维码）。

用途
----
把本机文件（图片 / 视频 / PDF / 任意格式）上传到自建分享节点，换取短链与二维码；
任何人用微信、QQ 或浏览器扫码即可直接查看或下载，无需安装任何 App。

组成
----
1. 本文件：配置下发 + 二维码生成 + 上传转发（含进度）
2. 分享节点：VPS `/opt/vdl-share/share_server.py`（systemd `vdl-share`；
   nginx 在 8888 端口按域名 `share.<域>` 分流 —— 安全组只放行 8888，见 nginx 注释）

为什么上传要走本地后端转发，而不是前端直传
------------------------------------------
桌面端原生文件框只返回**本地路径**而非 File 对象，前端拿不到内容；
本地后端与 App 同机（localhost），流式读文件再转发不产生额外网络开销，
还能顺带做进度上报与失败自动切换通道。

两条上传通道
------------
* base（https://share.<域>）：经 Cloudflare，加密，但**请求体上限 100MB**（免费版）
* direct（http://<VPS_IP>:8888/su）：直连源站绕开 CF，无体积限制，但为明文 HTTP
  选路：≤100MB 优先走 base；超过或 base 不可用时自动改走 direct。

配置来源（优先级由高到低）
--------------------------
1. 环境变量 VDL_SHARE_BASE / VDL_SHARE_TOKEN / VDL_SHARE_DIRECT
2. ~/.videodownloader/share.json  {"base": "...", "token": "...", "direct": "..."}
3. 下方内置默认值

注意：token 内置在客户端 = 客户端持有上传凭证，仅适用于自用场景；
若日后对外分发 App，应改为按用户签发（节点侧已有总量配额与磁盘水位保护兜底）。
"""
from __future__ import annotations

import atomic_io
import http.client
import json
import os
import ssl
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, File, UploadFile
from fastapi.responses import JSONResponse, Response

router = APIRouter()

# ---- 内置默认（2026-09-21 起分享服务迁至香港节点 47.82.101.79，公网入口 hanyuxz.top）----
# 运行时以 ~/.videodownloader/share.json 为准，这里是它缺失（换机/重装）时的兜底。
# 为什么弃用旧的 share.hanyuxz.top：该子域从来没有 DNS 记录，链路必然回退明文直连。
DEFAULT_BASE = "https://hanyuxz.top"
DEFAULT_DIRECT = "http://47.82.101.79/api/upload"
DEFAULT_TOKEN = "_hI50c3L0HYZ2kK_jMXa5tzKY7BnS_3b"

CF_BODY_LIMIT = 100 * 1024 * 1024      # Cloudflare 免费版请求体上限
CHUNK = 256 * 1024                     # 流式块大小
TASK_TTL = 3600                        # 已完成任务记录保留秒数
MAX_UPLOAD = 2 * 1024 ** 3             # 与节点 VDL_SHARE_MAX_FILE 一致
CONF_PATH = Path.home() / ".videodownloader" / "share.json"

_TASKS: dict = {}
_TASKS_LOCK = threading.Lock()

# ---- 分享历史（「我的分享」列表，2026-09-21 新增）----
# 为什么落盘：前端 shState 原先是**纯内存**，切走页面或重开 App 记录全丢 ——
# 已发出的链接与二维码再也找不回来（实测确认）。这里只记「本机发出过什么」，
# 不含任何服务器凭据；删除动作仍需节点 token，token 不写进历史文件。
HIST_PATH = CONF_PATH.parent / "share_history.json"
HIST_MAX = 300                      # 列表上限，超出裁掉最旧的
# ⚠️ 不要在这里另起一把私有锁：统一走 atomic_io.mutation（按绝对路径共享的可重入锁），
#    否则「这个入口用私有锁、那个入口用路径锁」等于没锁。

# 可选有效期（秒）。0 = 永久。须与节点 X-Expire 语义一致（share_server.py:554）
EXPIRE_CHOICES = (0, 86400, 7 * 86400, 30 * 86400)
EXPIRE_MAX = 366 * 86400


# ---------------------------------------------------------------- 配置

def _load_conf() -> dict:
    """合并配置：内置默认 ← share.json ← 环境变量。"""
    conf = {"base": DEFAULT_BASE, "token": DEFAULT_TOKEN, "direct": DEFAULT_DIRECT}
    try:
        if CONF_PATH.is_file():
            with CONF_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in ("base", "token", "direct"):
                    v = data.get(k)
                    if isinstance(v, str) and v.strip():
                        conf[k] = v.strip()
    except Exception:
        pass  # 配置损坏退回默认，不影响主流程
    for k, env in (("base", "VDL_SHARE_BASE"), ("token", "VDL_SHARE_TOKEN"),
                   ("direct", "VDL_SHARE_DIRECT")):
        v = (os.environ.get(env) or "").strip()
        if v:
            conf[k] = v
    for k in conf:
        conf[k] = conf[k].rstrip("/")
    return conf


@router.get("/api/share/config")
def share_config():
    """下发分享节点配置（前端据此提示体积限制、生成二维码）。"""
    conf = _load_conf()
    return {
        "ok": True,
        "base": conf["base"],
        "token": conf["token"],
        "direct": conf["direct"],
        "cf_body_limit": CF_BODY_LIMIT,
        "max_upload": MAX_UPLOAD,
        "enabled": bool(conf["base"] and conf["token"]),
    }


# ---------------------------------------------------------------- 节点直连

def _node_roots(conf: dict) -> list:
    """可用的节点根地址（按优先级）：base 域名 → direct 源站根。

    direct 配置形如 `http://IP:8888/su`，那条路径**只能上传**（nginx 精确匹配）。
    去掉通道后缀得到源站根，删除 / 探活这类接口必须用它 —— 2026-09-21 实测：
    正式域名 share.<域> 无 DNS 时，删除接口在 base 通道完全不可达，只有源站根能通
    （nginx default_server 已放行 `DELETE /api/share/<sid>`）。
    """
    roots = []
    base = (conf.get("base") or "").rstrip("/")
    if base:
        roots.append(base)
    direct = (conf.get("direct") or "").rstrip("/")
    for suffix in ("/su", "/api/upload"):
        if direct.endswith(suffix):
            direct = direct[: -len(suffix)]
            break
    direct = direct.rstrip("/")
    if direct and direct not in roots:
        roots.append(direct)
    return roots


def _node_req(root: str, method: str, path: str, headers: dict, timeout: int = 20):
    """对节点根发一次请求，返回 (status, body_bytes)；网络异常向上抛。"""
    p = urllib.parse.urlparse(root)
    if not p.hostname:
        raise ValueError("bad node root: %s" % root)
    port = p.port or (443 if p.scheme == "https" else 80)
    prefix = (p.path or "").rstrip("/")
    if p.scheme == "https":
        conn = http.client.HTTPSConnection(p.hostname, port, timeout=timeout,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(p.hostname, port, timeout=timeout)
    try:
        conn.request(method, prefix + path, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _node_delete(sid: str) -> tuple:
    """删除节点上的分享。返回 (ok, detail)；404 视为「本来就不存在」= 成功。"""
    conf = _load_conf()
    last = "no_root"
    for root in _node_roots(conf):
        try:
            code, body = _node_req(root, "DELETE", "/api/share/" + urllib.parse.quote(sid),
                                   {"X-Auth": conf["token"]}, timeout=25)
        except Exception as exc:
            last = "%s: %s" % (root, exc)
            continue
        if code in (200, 404):
            return True, body[:200].decode("utf-8", "replace")
        last = "%s: HTTP %s %s" % (root, code, body[:160].decode("utf-8", "replace"))
    return False, last


def _node_alive(sid: str):
    """探活：分享页是否还在。True / False；两条通道都连不上返回 None（未知）。"""
    conf = _load_conf()
    for root in _node_roots(conf):
        try:
            code, _ = _node_req(root, "HEAD", "/s/" + urllib.parse.quote(sid), {}, timeout=8)
        except Exception:
            continue
        return code == 200
    return None


# ---------------------------------------------------------------- 历史记录

def _hist_load() -> list:
    try:
        if HIST_PATH.is_file():
            with HIST_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items") if isinstance(data, dict) else data
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict) and x.get("sid")]
    except Exception:
        pass  # 历史损坏不该影响上传主流程
    return []


def _hist_save(items: list) -> None:
    """原子落盘（唯一临时名 + os.replace + fsync）。

    ⚠️ 必须走 atomic_io，**不能**自己拼固定名 `.tmp`：并发写者会互相截断（仓库里有
    可复现反证），而且棘轮守卫 test_config_atomic_write.py 会直接把构建拦下
    —— 2026-09-21 实测踩到（构建自验证 45 通过 / 1 失败正是这条）。
    """
    try:
        atomic_io.atomic_write_json(HIST_PATH, {"items": items[:HIST_MAX]})
    except Exception:
        pass


def _hist_add(rec: dict) -> None:
    """上传成功后落一条（同 sid 去重，新的排最前）。"""
    if not rec.get("sid"):
        return
    with atomic_io.mutation(HIST_PATH):     # 必须包住「读」——只锁落盘等于没锁
        items = [x for x in _hist_load() if x.get("sid") != rec.get("sid")]
        items.insert(0, rec)
        _hist_save(items)


# ---------------------------------------------------------------- 二维码

@router.get("/api/share/qr")
def share_qr(text: str = "", size: int = 640, margin: int = 4):
    """生成二维码 PNG。

    用包内已有的 OpenCV（cv2.QRCodeEncoder）生成，不引入任何新依赖；
    文本越短二维码越稀疏、越好扫，因此前端只用它编码 `.../s/<sid>` 短链。
    """
    text = (text or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "empty_text"}, status_code=400)
    if len(text) > 1200:
        return JSONResponse({"ok": False, "error": "text_too_long"}, status_code=400)
    try:
        import cv2
        import numpy as np
    except Exception:
        return JSONResponse({"ok": False, "error": "cv2_unavailable"}, status_code=503)

    size = max(160, min(int(size or 640), 1600))
    # 静区（quiet zone）按「模块数」计：QR 规范要求 ≥4 个模块，不足会显著降低扫码成功率
    quiet_mods = max(2, min(int(margin or 4), 8))
    try:
        enc = cv2.QRCodeEncoder_create()
        qr = np.asarray(enc.encode(text))
        if qr.ndim != 2:
            raise ValueError("unexpected qr shape: %r" % (qr.shape,))
        qr = qr.astype(np.uint8)
        # ⚠️ OpenCV 返回的已是方向正确的灰度图：0 = 深色模块，255 = 浅色背景。
        #    实测：任何形式的反色/阈值翻转都会让二维码彻底扫不出来（2026-09-20 踩坑）。
        if qr.max() <= 1:
            qr = (qr * 255).astype(np.uint8)
        n = max(1, int(qr.shape[0]))
        mod_px = max(2, size // (n + quiet_mods * 2))     # 整数倍模块宽，边缘才锐利
        inner = n * mod_px
        pad = quiet_mods * mod_px
        qr = cv2.resize(qr, (inner, inner), interpolation=cv2.INTER_NEAREST)
        qr = cv2.copyMakeBorder(qr, pad, pad, pad, pad,
                                cv2.BORDER_CONSTANT, value=255)
        ok, buf = cv2.imencode(".png", qr, [int(cv2.IMWRITE_PNG_COMPRESSION), 6])
        if not ok:
            raise RuntimeError("png encode failed")
        png = buf.tobytes()
    except Exception as exc:  # pragma: no cover
        return JSONResponse({"ok": False, "error": "encode_failed", "detail": str(exc)},
                            status_code=500)

    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store",
                             "Content-Disposition": 'inline; filename="qr.png"'})


# ---------------------------------------------------------------- 上传任务

def _new_task(name: str, total: int) -> str:
    tid = uuid.uuid4().hex[:16]
    now = time.time()
    with _TASKS_LOCK:
        _TASKS[tid] = {"id": tid, "name": name, "total": total, "sent": 0,
                       "status": "pending", "sid": "", "url": "", "error": "",
                       "channel": "", "started": now}
        for k, v in list(_TASKS.items()):
            if v["status"] in ("done", "failed") and now - v["started"] > TASK_TTL:
                _TASKS.pop(k, None)
    return tid


def _patch(tid: str, **kw) -> None:
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        if t:
            t.update(kw)


def _bump(tid: str, n: int) -> None:
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        if t:
            t["sent"] += n


def _http_post_stream(url: str, headers: dict, reader, total: int, tid: str):
    """用标准库 http.client 做流式 POST（可控 Content-Length，避免 chunked）。

    reader() 每次返回一段 bytes，读尽返回 b''。
    """
    p = urllib.parse.urlparse(url)
    host = p.hostname
    if not host:
        raise ValueError("bad upload url: %s" % url)
    port = p.port or (443 if p.scheme == "https" else 80)
    path = (p.path or "/") + (("?" + p.query) if p.query else "")
    if p.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=1800,
                                           context=ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(host, port, timeout=1800)
    try:
        conn.putrequest("POST", path, skip_accept_encoding=True)
        conn.putheader("Content-Length", str(total))
        conn.putheader("Content-Type", "application/octet-stream")
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        while True:
            chunk = reader()
            if not chunk:
                break
            conn.send(chunk)
            _bump(tid, len(chunk))
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, body
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _run_upload(tid: str, make_reader, total: int, filename: str, conf: dict,
                expire: int = 0) -> None:
    """后台线程：按体积选通道上传，失败自动换另一条通道。

    expire 为有效期秒数（0 = 永久），经 X-Expire 交给节点计算 expire_at；
    节点在响应里回传真实 expire_at，据此落进「我的分享」历史。
    """
    quoted = urllib.parse.quote(filename)
    base_ep = conf["base"] + "/api/upload"
    direct_ep = conf["direct"]
    if total <= CF_BODY_LIMIT:
        channels = [("base", base_ep), ("direct", direct_ep)]
    else:
        channels = [("direct", direct_ep), ("base", base_ep)]

    headers = {"X-Filename": quoted, "X-Auth": conf["token"],
               "X-Expire": str(int(expire or 0))}

    last_err = ""
    for chan, ep in channels:
        if not ep or (chan == "base" and not conf["base"]):
            continue
        # 每条通道都从文件头重新读
        reader = make_reader()
        _patch(tid, status="uploading", channel=chan, sent=0)
        try:
            code, body = _http_post_stream(ep, headers, reader, total, tid)
        except Exception as exc:
            last_err = "%s: %s" % (chan, exc)
            continue
        if code == 200:
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except Exception:
                data = {}
            if data.get("ok") and data.get("url"):
                now = int(time.time())
                sid = data.get("sid", "")
                _patch(tid, status="done", sid=sid,
                       url=data.get("url", ""), channel=chan, sent=total)
                _hist_add({
                    "sid": sid,
                    "name": filename,
                    "size": total,
                    "url": data.get("url", ""),
                    "time": now,
                    "expire_at": int(data.get("expire_at") or (now + expire if expire else 0)),
                    "kind": data.get("kind", ""),
                    "channel": chan,
                })
                return
            last_err = "%s: bad response" % chan
        else:
            last_err = "%s: HTTP %s %s" % (chan, code, body[:180].decode("utf-8", "replace"))

    _patch(tid, status="failed", error=last_err or "上传失败")


def _norm_expire(raw) -> int:
    """把前端传来的有效期夹到合法范围（0 = 永久，上限 366 天）。"""
    try:
        v = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(v, EXPIRE_MAX))


def _start_upload(tid: str, make_reader, total: int, filename: str,
                  expire: int = 0, cleanup_path: str = "") -> None:
    """起后台上传线程；cleanup_path 非空时，无论成败都删掉它。

    为什么把清理放在这一层：`_run_upload` 内部有多条 return（成功即返回），
    在调用处包一层 try/finally 比给它整体加缩进更不容易改错。
    """
    conf = _load_conf()

    def _worker() -> None:
        try:
            _run_upload(tid, make_reader, total, filename, conf, expire)
        finally:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except OSError:
                    pass

    th = threading.Thread(target=_worker,
                          name="share-upload-%s" % tid, daemon=True)
    th.start()


@router.post("/api/share/upload_path")
def share_upload_path(payload: dict = Body(...)):
    """桌面端入口：传本机绝对路径，由后端流式读取并转发到分享节点。"""
    path = (payload or {}).get("path") or ""
    path = os.path.expanduser(str(path).strip())
    if not path or not os.path.isfile(path):
        return JSONResponse({"ok": False, "error": "file_not_found", "path": path},
                            status_code=400)
    try:
        total = os.path.getsize(path)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": "stat_failed", "detail": str(exc)},
                            status_code=400)
    if total <= 0:
        return JSONResponse({"ok": False, "error": "empty_file"}, status_code=400)
    if total > MAX_UPLOAD:
        return JSONResponse({"ok": False, "error": "too_large",
                             "max": MAX_UPLOAD, "size": total}, status_code=413)

    name = os.path.basename(path)
    expire = _norm_expire((payload or {}).get("expire"))

    def make_reader():
        def _read():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    yield chunk
        gen = _read()
        return lambda: next(gen, b"")

    tid = _new_task(name, total)
    _start_upload(tid, make_reader, total, name, expire)
    return {"ok": True, "task_id": tid, "name": name, "size": total, "expire": expire}


def _spool_to_tempfile(f) -> str:
    """把上传流分块落到临时文件，返回路径（失败时清掉半截文件）。

    🔴 为什么必须落盘而不是直接读 `UploadFile.file`：
    Starlette 在**请求处理返回后**关闭 `UploadFile.file`，而真正读它的
    `_run_upload` 跑在**后台线程**里 —— 直接读会报
    `I/O operation on closed file`（2026-09-21 实测：桌面端走 upload_path 正常，
    浏览器入口 upload_file 必失败）。落盘一次，后台线程就有稳定的数据源。
    分块而非 `read()` 整份进内存：单文件上限 2GB，整份进内存会打爆小内存机器。
    """
    fd, path = tempfile.mkstemp(prefix="vdl_share_up_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


@router.post("/api/share/upload_file")
def share_upload_file(file: UploadFile = File(...), expire: int = 0):
    """浏览器入口：multipart 上传（Web 版没有本地路径权限）。

    expire 走 query 参数（`?expire=86400`），语义与桌面端一致：0 = 永久。

    ⚠️ 本函数刻意是**同步 def**：改成 async def 后 Starlette 会在响应返回时关闭
    `UploadFile.file`，而后台上传线程仍在读它（见 `_spool_to_tempfile` 的说明）。
    同步 def 由 FastAPI 丢进线程池执行，`file.file` 在本函数返回前一直可用。
    """
    f = file.file
    try:
        f.seek(0, os.SEEK_END)
        total = f.tell()
        f.seek(0)
    except Exception:
        return JSONResponse({"ok": False, "error": "size_unknown"}, status_code=400)
    if total <= 0:
        return JSONResponse({"ok": False, "error": "empty_file"}, status_code=400)
    if total > MAX_UPLOAD:
        return JSONResponse({"ok": False, "error": "too_large",
                             "max": MAX_UPLOAD, "size": total}, status_code=413)

    name = os.path.basename(file.filename or "file")
    expire = _norm_expire(expire)

    try:
        spool = _spool_to_tempfile(f)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": "spool_failed", "detail": str(exc)},
                            status_code=500)

    def make_reader():
        def _read():
            with open(spool, "rb") as fh:
                while True:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    yield chunk
        gen = _read()
        return lambda: next(gen, b"")

    tid = _new_task(name, total)
    _start_upload(tid, make_reader, total, name, expire, cleanup_path=spool)
    return {"ok": True, "task_id": tid, "name": name, "size": total}


@router.get("/api/share/task/{tid}")
def share_task(tid: str):
    """查询上传进度（前端轮询）。"""
    with _TASKS_LOCK:
        t = _TASKS.get(tid)
        if not t:
            return JSONResponse({"ok": False, "error": "task_not_found"}, status_code=404)
        return {"ok": True, **t}


# ---------------------------------------------------------------- 我的分享

@router.get("/api/share/history")
def share_history(probe: int = 0, limit: int = 100):
    """「我的分享」列表（本地持久化，重开 App 仍在）。

    probe=1 时顺带探活（HEAD /s/<sid>）把已被删除的记录标出来；探活在线程池里
    并发做，避免十条记录串行等十个超时。
    """
    now = int(time.time())
    # ⚠️ 别写 `limit or 100`：那会把显式传入的 0 也变成默认 100。0 的语义是「最少一条」。
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 100
    lim = max(1, min(lim, HIST_MAX))
    out = []
    for it in _hist_load()[:lim]:
        exp = int(it.get("expire_at") or 0)
        out.append({
            "sid": it.get("sid", ""),
            "name": it.get("name", ""),
            "size": it.get("size", 0),
            "url": it.get("url", ""),
            "time": it.get("time", 0),
            "expire_at": exp,
            "kind": it.get("kind", ""),
            "channel": it.get("channel", ""),
            "expired": bool(exp and exp <= now),
        })
    if probe:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=6) as ex:
                alive = list(ex.map(lambda r: _node_alive(r["sid"]), out))
            for row, a in zip(out, alive):
                row["alive"] = a          # True / False / None(未知)
        except Exception:
            pass
    return {"ok": True, "items": out, "now": now}


@router.delete("/api/share/history/{sid}")
def share_history_delete(sid: str):
    """删除一条分享：先删节点上的文件，成功后再摘掉本地记录。

    ⚠️ 只允许删**本机历史里**的 sid，否则这个端点会变成「知道 sid 就能删」的
    任意删除代理 —— token 内置在客户端，不能拿它当通用删除入口。
    """
    sid = (sid or "").strip()
    # 读不加锁：写走 os.replace，读到的必是完整文件
    if not any(x.get("sid") == sid for x in _hist_load()):
        return JSONResponse({"ok": False, "error": "not_in_history"}, status_code=404)
    ok, detail = _node_delete(sid)
    if not ok:
        return JSONResponse({"ok": False, "error": "node_delete_failed", "detail": detail},
                            status_code=502)
    with atomic_io.mutation(HIST_PATH):
        _hist_save([x for x in _hist_load() if x.get("sid") != sid])
    return {"ok": True, "sid": sid, "node": detail}
