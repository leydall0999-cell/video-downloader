#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VDL 扫码分享服务（share node）。

用途：桌面端「视频工坊」选本地文件 → 上传到本节点 → 得到一个短链与二维码 →
      任何人（微信 / QQ / 浏览器）扫码即可直接打开查看或下载。

设计约束（均来自真实部署环境，勿轻易改动）
------------------------------------------
* 阿里云安全组**只放行 8888**，因此本服务必须只听 127.0.0.1，
  公网入口由 nginx 在 8888 上按域名（share.<域>）分流进来。
* VPS 仅 1.6G 内存 → 上传必须**流式落盘**，严禁把整份文件读进内存。
* 视频要在微信里能播 → 文件字节由 **nginx 直出**（支持 Range / 断点续传），
  本服务的 /f/ 路由只是直连调试时的兜底实现。
* 微信/QQ 内打开要求 HTTPS → 由 Cloudflare 边缘提供（回源到 8888）。

零第三方依赖：仅 Python 3.10 标准库。

接口
----
POST   /api/upload             raw body 上传文件（流式落盘）
       Header X-Filename       原文件名（URL 编码）
       Header X-Auth           token（与 share_token 文件一致）
       Header X-Expire         可选，过期秒数；0/缺省=永久
GET    /api/meta/<sid>         元信息 JSON
GET    /s/<sid>                分享页（H5，按类型渲染）
GET    /f/<sid>                文件字节（支持单区间 Range）
GET    /api/download/<sid>     强制下载（Content-Disposition: attachment）
DELETE /api/share/<sid>        删除（需 X-Auth）
GET    /healthz                健康检查
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 配置

BASE_DIR = os.environ.get("VDL_SHARE_DIR", "/opt/vdl-share")
DATA_DIR = os.path.join(BASE_DIR, "data")
TOKEN_FILE = os.path.join(BASE_DIR, "share_token")
LISTEN_HOST = os.environ.get("VDL_SHARE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("VDL_SHARE_PORT", "8901"))
# 对外的公网地址前缀。必须配置：大文件走 IP 直连通道上传时 Host 头是 IP，
# 靠 Host 反推会生成 `http://8.138.223.3/s/xxx` 这种不能用的链接。
PUBLIC_BASE = os.environ.get("VDL_SHARE_PUBLIC_BASE", "").rstrip("/")

MAX_FILE_BYTES = int(os.environ.get("VDL_SHARE_MAX_FILE", str(2 * 1024 ** 3)))    # 单文件 2GB
MAX_TOTAL_BYTES = int(os.environ.get("VDL_SHARE_MAX_TOTAL", str(20 * 1024 ** 3)))  # 总量 20GB
DEFAULT_TTL = int(os.environ.get("VDL_SHARE_TTL", "0"))                            # 0 = 永久
MIN_FREE_BYTES = int(os.environ.get("VDL_SHARE_MIN_FREE", str(512 * 1024 ** 2)))   # 保底留 512MB

SID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


def _is_public_host(host: str) -> bool:
    """Host 头是否可以当作可用的公网域名来反推分享链接。

    【为什么要判断】两类请求的 Host 可信程度完全不同：
    * 经域名访问（正式域名 / cloudflared 临时隧道）→ Host 就是唯一可用的公网域名，应当采用；
    * 走 IP 直连上传通道 `http://<IP>:8888/su` → Host 是裸 IP，反推出来的是
      `http://8.138.223.3/s/xxx` 这种打不开的链接，必须回退 PUBLIC_BASE。
    2026-09-20 起采用「域名优先」，这样 CF 未配置期间用临时隧道也能拿到正确链接，
    正式域名配好后无需再改代码。
    """
    if not host:
        return False
    h = host.strip().lower()
    if h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False
    if ":" in h:                       # IPv6 字面量
        return False
    if re.fullmatch(r"[\d.]+", h):     # IPv4 字面量
        return False
    return "." in h

# 浏览器/微信可直接播放的容器；其余视频格式只给下载（提示可能不支持在线播放）
PLAYABLE_VIDEO = {"mp4", "m4v", "webm", "ogv", "mov"}
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif", "avif", "svg"}
AUDIO_EXT = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "amr"}
PDF_EXT = {"pdf"}
TEXT_EXT = {"txt", "md", "log", "csv", "json", "xml", "yaml", "yml"}

LOG_LOCK = threading.Lock()


def log(*args: object) -> None:
    msg = " ".join(str(a) for a in args)
    with LOG_LOCK:
        sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        sys.stderr.flush()


# ---------------------------------------------------------------- 工具

def human_size(n: int) -> str:
    if n is None or n < 0:
        return "-"
    if n < 1024:
        return "%d B" % n
    if n < 1024 ** 2:
        return "%.1f KB" % (n / 1024)
    if n < 1024 ** 3:
        return "%.2f MB" % (n / 1024 ** 2)
    return "%.2f GB" % (n / 1024 ** 3)


def safe_filename(name: str) -> str:
    """去掉路径成分与危险字符，保留可读的中文名。"""
    name = (name or "").strip().replace("\\", "/")
    name = name.split("/")[-1]
    name = name.replace("\x00", "")
    name = re.sub(r'[<>:"|?*\r\n\t]', "_", name)
    name = name.strip(" .")
    if not name:
        name = "file"
    # 落盘名另做长度限制（保留扩展名）
    root, ext = os.path.splitext(name)
    if len(root) > 80:
        root = root[:80]
    if len(ext) > 12:
        ext = ext[:12]
    return root + ext


def ext_of(name: str) -> str:
    return os.path.splitext(name)[1].lstrip(".").lower()


def kind_of(name: str) -> str:
    e = ext_of(name)
    if e in IMAGE_EXT:
        return "image"
    if e in AUDIO_EXT:
        return "audio"
    if e in PDF_EXT:
        return "pdf"
    if e in TEXT_EXT:
        return "text"
    # 视频判断放最后：避免 mp4 之类被误判
    if e in PLAYABLE_VIDEO or e in {"mkv", "avi", "wmv", "flv", "ts", "m2ts", "3gp", "rmvb"}:
        return "video"
    return "file"


def new_sid() -> str:
    """16 字符 URL 安全随机串（不可枚举）。"""
    return secrets.token_urlsafe(12)


def read_token() -> str:
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def meta_path(sid: str) -> str:
    return os.path.join(DATA_DIR, sid, "meta.json")


def load_meta(sid: str):
    """返回 (meta, file_path) 或 (None, None)。"""
    if not SID_RE.match(sid):
        return None, None
    d = os.path.join(DATA_DIR, sid)
    mp = os.path.join(d, "meta.json")
    try:
        with open(mp, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None, None
    fp = os.path.join(d, meta.get("store_name") or "")
    if not os.path.isfile(fp):
        return None, None
    exp = int(meta.get("expire_at") or 0)
    if exp and exp < time.time():
        return None, None
    return meta, fp


def save_meta(sid: str, meta: dict) -> None:
    mp = meta_path(sid)
    tmp = mp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, mp)


def dir_total_bytes() -> int:
    total = 0
    try:
        for name in os.listdir(DATA_DIR):
            d = os.path.join(DATA_DIR, name)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                try:
                    total += os.path.getsize(os.path.join(d, fn))
                except OSError:
                    pass
    except OSError:
        pass
    return total


# ---------------------------------------------------------------- 分享页

PAGE_CSS = """
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0}
body{background:#0b0d12;color:#e8ecf3;font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue","Hiragino Sans GB","Microsoft YaHei",sans-serif;
display:flex;flex-direction:column;min-height:100vh}
.hd{padding:14px 16px;background:rgba(255,255,255,.04);border-bottom:1px solid rgba(255,255,255,.07)}
.hd h1{margin:0;font-size:16px;font-weight:600;word-break:break-all;line-height:1.4}
.hd .sub{margin-top:5px;font-size:12.5px;color:#8e99ad}
.tag{display:inline-block;padding:1px 7px;border-radius:5px;background:rgba(94,158,255,.16);color:#8fbaff;font-size:11.5px;margin-right:6px}
.main{flex:1;display:flex;align-items:center;justify-content:center;padding:14px}
.main img{max-width:100%;max-height:76vh;border-radius:10px;display:block;box-shadow:0 6px 28px rgba(0,0,0,.5)}
.main video,.main audio{width:100%;max-width:100%;border-radius:10px;background:#000}
.main video{max-height:76vh}
.main iframe{width:100%;height:76vh;border:0;border-radius:10px;background:#fff}
.card{background:rgba(255,255,255,.045);border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:22px 18px;text-align:center;max-width:420px;width:100%}
.card .big{font-size:34px;line-height:1;margin-bottom:12px}
.card .name{font-size:14px;word-break:break-all;color:#c7d0de;margin-bottom:4px}
.card .size{font-size:12.5px;color:#8e99ad}
.ft{padding:12px 16px 22px;display:flex;flex-direction:column;gap:9px}
.btn{display:block;text-align:center;padding:12px 14px;border-radius:11px;font-size:15px;font-weight:600;text-decoration:none;border:0}
.btn-p{background:#2b6cff;color:#fff}
.btn-s{background:rgba(255,255,255,.08);color:#dbe3ef}
.tip{font-size:12px;color:#78839a;text-align:center;margin-top:2px}
.note{font-size:12.5px;color:#a5b0c4;background:rgba(255,190,80,.1);border:1px solid rgba(255,190,80,.22);border-radius:10px;padding:10px 12px;margin:10px 0 0}
@media (min-width:720px){.main img{max-height:82vh}.main iframe{height:82vh}}
"""


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def render_page(sid: str, meta: dict) -> str:
    name = meta.get("name") or "文件"
    size = human_size(int(meta.get("size") or 0))
    kind = meta.get("kind") or "file"
    # 带文件名：nginx 才能用 alias 直出（Range / 大文件零 Python 开销）
    f_url = "/f/%s/%s" % (sid, urllib.parse.quote(meta.get("store_name") or name))
    d_url = "/api/download/" + sid
    f_esc = esc(f_url)

    if kind == "image":
        body = '<img src="%s" alt="%s">' % (f_esc, esc(name))
        extra = ""
    elif kind == "video":
        if ext_of(name) in PLAYABLE_VIDEO:
            body = ('<video src="%s" controls playsinline webkit-playsinline '
                    'preload="metadata" x5-video-player-type="h5"></video>') % f_esc
            extra = ""
        else:
            body = ('<div class="card"><div class="big">🎬</div><div class="name">%s</div>'
                    '<div class="size">%s</div></div>') % (esc(name), size)
            extra = ('<div class="note">该格式（.%s）浏览器通常无法直接播放，'
                     '请点下方「下载到本地」后用播放器打开。</div>') % esc(ext_of(name))
    elif kind == "audio":
        body = '<audio src="%s" controls preload="metadata"></audio>' % f_esc
        extra = ""
    elif kind == "pdf":
        body = '<iframe src="%s#view=FitH" title="%s"></iframe>' % (f_esc, esc(name))
        extra = ('<div class="note">若上方空白（部分安卓浏览器不支持内嵌 PDF），'
                 '请点「下载到本地」查看。</div>')
    else:
        body = ('<div class="card"><div class="big">📄</div><div class="name">%s</div>'
                '<div class="size">%s</div></div>') % (esc(name), size)
        extra = ""

    views = int(meta.get("views") or 0)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(meta.get("time") or 0)))
    return """<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d12">
<meta name="referrer" content="no-referrer">
<title>%s</title>
<style>%s</style>
</head><body>
<div class="hd">
  <h1>%s</h1>
  <div class="sub"><span class="tag">%s</span>%s · 上传于 %s · 浏览 %d 次</div>
</div>
<div class="main">%s</div>
<div class="ft">
  <a class="btn btn-p" href="%s">下载到本地（%s）</a>
  <div class="tip">本页由「视频工坊」生成 · 扫码即可查看</div>
  %s
</div>
</body></html>""" % (
        esc(name), PAGE_CSS, esc(name), esc(kind_label(kind)), size, esc(when), views,
        body, esc(d_url), size, extra,
    )


def kind_label(kind: str) -> str:
    return {"image": "图片", "video": "视频", "audio": "音频",
            "pdf": "PDF", "text": "文本", "file": "文件"}.get(kind, "文件")


def render_error(msg: str, code: int = 404) -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>链接不可用</title><style>%s</style></head>
<body><div class="main"><div class="card">
<div class="big">🔍</div><div class="name">%s</div>
<div class="size">错误码 %d</div></div></div></body></html>""" % (PAGE_CSS, esc(msg), code)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "VDLShare/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 通用响应 ----------

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None,
              head_only: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-Filename,X-Auth,X-Expire,Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def _json(self, code: int, obj: dict, head_only: bool = False) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", head_only=head_only)

    def _html(self, code: int, html: str, head_only: bool = False) -> None:
        self._send(code, html.encode("utf-8"), "text/html; charset=utf-8", head_only=head_only)

    # ---------- 入口 ----------

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET(head_only=True)

    def do_GET(self, head_only: bool = False) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/healthz":
                return self._json(200, {"ok": True, "service": "vdl-share",
                                        "files": count_shares(), "bytes": dir_total_bytes()})
            if path.startswith("/s/"):
                return self._page(path[3:], head_only)
            if path.startswith("/f/"):
                return self.guard_file(path[3:], head_only, force_dl=False)
            if path.startswith("/api/download/"):
                return self.guard_file(path[len("/api/download/"):], head_only, force_dl=True)
            if path.startswith("/api/meta/"):
                meta, fp = load_meta(path[len("/api/meta/"):])
                if not meta:
                    return self._json(404, {"ok": False, "error": "not_found"})
                return self._json(200, {"ok": True, "meta": meta, "url": self.public_url(meta["sid"])})
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/":
                return self._html(200, render_error("请使用完整的分享链接", 404))
        except BrokenPipeError:
            return
        except Exception as exc:  # pragma: no cover - 兜底
            log("GET 处理异常:", repr(exc))
            try:
                self._json(500, {"ok": False, "error": "internal"})
            except Exception:
                pass
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/upload":
            return self.handle_upload()
        self._json(404, {"ok": False, "error": "not_found"})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/share/"):
            if not self.auth_ok():
                return self._json(403, {"ok": False, "error": "bad_token"})
            sid = parsed.path[len("/api/share/"):]
            return self.delete_share(sid)
        self._json(404, {"ok": False, "error": "not_found"})

    # ---------- 鉴权 / 地址 ----------

    def auth_ok(self) -> bool:
        want = read_token()
        if not want:
            return True  # 未配置 token 则不校验（仅限直连调试）
        got = (self.headers.get("X-Auth") or "").strip()
        return secrets.compare_digest(got, want)

    def public_url(self, sid: str) -> str:
        """生成分享短链。优先级：请求域名（可信时）> PUBLIC_BASE > Host 兜底。

        域名优先让「正式域名」与「cloudflared 临时隧道域名」都能自动产出正确链接；
        IP 直连上传通道的 Host 是裸 IP，会被 _is_public_host 挡掉并回退 PUBLIC_BASE。
        """
        host = (self.headers.get("Host") or "").split(":")[0].strip()
        if _is_public_host(host):
            # 公网入口对外一律 https：正式域名走 CF 橙云（强制 https），临时隧道本身就是 https。
            # ⚠️ 不要读 X-Forwarded-Proto —— nginx 只监听明文 8888（无 TLS），$scheme 恒为 http，
            #    它反映的是「nginx→上游」的协议而非客户端协议。2026-09-20 实测踩过：
            #    读它会生成 http://<隧道域名>/s/xxx，而隧道强制 https，链接直接不可用。
            return "https://%s/s/%s" % (host, sid)
        if PUBLIC_BASE:
            return "%s/s/%s" % (PUBLIC_BASE, sid)
        if host:
            return "http://%s/s/%s" % (host, sid)
        return "/s/%s" % sid

    # ---------- 路由实现 ----------

    def _page(self, sid: str, head_only: bool) -> None:
        meta, _fp = load_meta(sid)
        if not meta:
            return self._html(404, render_error("链接不存在或已过期"), head_only)
        try:
            meta["views"] = int(meta.get("views") or 0) + 1
            save_meta(sid, meta)
        except OSError:
            pass
        return self._html(200, render_page(sid, meta), head_only)

    def guard_file(self, sid: str, head_only: bool, force_dl: bool) -> None:
        meta, fp = load_meta(sid)
        if not meta:
            return self._json(404, {"ok": False, "error": "not_found"})
        return self.serve_bytes(fp, meta.get("name") or "file", head_only, force_dl)

    def serve_bytes(self, fp: str, name: str, head_only: bool, force_dl: bool) -> None:
        """带 Range 支持的字节分发（生产环境主要由 nginx 直出，这里是兜底）。"""
        try:
            size = os.path.getsize(fp)
        except OSError:
            return self._json(404, {"ok": False, "error": "not_found"})

        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        code = 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:  # bytes=-N 最后 N 字节
                    start = max(0, size - int(e))
                    end = size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206

        disp = "attachment" if force_dl else "inline"
        quoted = urllib.parse.quote(name)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         "%s; filename*=UTF-8''%s" % (disp, quoted))
        self.send_header("Cache-Control", "public, max-age=604800")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head_only:
            return
        remaining = end - start + 1
        with open(fp, "rb") as f:
            f.seek(start)
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    # ---------- 上传 ----------

    def handle_upload(self) -> None:
        if not self.auth_ok():
            return self._json(403, {"ok": False, "error": "bad_token"})

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._json(400, {"ok": False, "error": "empty_body"})
        if length > MAX_FILE_BYTES:
            return self._json(413, {"ok": False, "error": "too_large",
                                    "max": MAX_FILE_BYTES, "got": length})

        # 磁盘水位与总配额保护
        try:
            free = shutil.disk_usage(DATA_DIR).free
            if free - length < MIN_FREE_BYTES:
                return self._json(507, {"ok": False, "error": "disk_low"})
            if dir_total_bytes() + length > MAX_TOTAL_BYTES:
                return self._json(507, {"ok": False, "error": "quota_exceeded"})
        except OSError:
            pass

        raw_name = self.headers.get("X-Filename") or ""
        try:
            raw_name = urllib.parse.unquote(raw_name)
        except Exception:
            pass
        name = safe_filename(raw_name) or "file"
        try:
            ttl = int(self.headers.get("X-Expire") or DEFAULT_TTL)
        except ValueError:
            ttl = DEFAULT_TTL

        sid = new_sid()
        d = os.path.join(DATA_DIR, sid)
        os.makedirs(d, exist_ok=True)
        store_name = name
        tmp = os.path.join(d, ".uploading")

        written = 0
        try:
            with open(tmp, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            shutil.rmtree(d, ignore_errors=True)
            log("上传中断，已清理:", sid)
            return
        except OSError as exc:
            shutil.rmtree(d, ignore_errors=True)
            log("上传写盘失败:", repr(exc))
            return self._json(500, {"ok": False, "error": "write_failed"})

        if written != length:
            shutil.rmtree(d, ignore_errors=True)
            return self._json(400, {"ok": False, "error": "incomplete",
                                    "expect": length, "got": written})

        final = os.path.join(d, store_name)
        try:
            os.replace(tmp, final)
        except OSError as exc:
            shutil.rmtree(d, ignore_errors=True)
            log("改名失败:", repr(exc))
            return self._json(500, {"ok": False, "error": "rename_failed"})

        now = int(time.time())
        meta = {
            "sid": sid,
            "name": name,
            "store_name": store_name,
            "size": written,
            "kind": kind_of(name),
            "time": now,
            "expire_at": (now + ttl) if ttl > 0 else 0,
            "views": 0,
            "ip": self.headers.get("X-Real-IP") or self.client_address[0],
        }
        save_meta(sid, meta)
        url = self.public_url(sid)
        log("上传完成: %s (%s, %s)" % (sid, name, human_size(written)))
        return self._json(200, {"ok": True, "sid": sid, "url": url,
                                "name": name, "size": written, "kind": meta["kind"],
                                "expire_at": meta["expire_at"]})

    # ---------- 删除 ----------

    def delete_share(self, sid: str) -> None:
        if not SID_RE.match(sid):
            return self._json(400, {"ok": False, "error": "bad_sid"})
        d = os.path.join(DATA_DIR, sid)
        if not os.path.isdir(d):
            return self._json(404, {"ok": False, "error": "not_found"})
        shutil.rmtree(d, ignore_errors=True)
        log("已删除:", sid)
        return self._json(200, {"ok": True, "sid": sid})

    # ---------- 日志（静音默认 access log，由 nginx 记录） ----------

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        return


def count_shares() -> int:
    try:
        return sum(1 for n in os.listdir(DATA_DIR) if os.path.isdir(os.path.join(DATA_DIR, n)))
    except OSError:
        return 0


# ---------------------------------------------------------------- 后台清理

def cleaner_loop(stop_evt: threading.Event) -> None:
    """每小时清理：过期分享、超过 24h 的未完成上传残留。"""
    while not stop_evt.wait(3600):
        now = time.time()
        removed = 0
        try:
            for sid in os.listdir(DATA_DIR):
                d = os.path.join(DATA_DIR, sid)
                if not os.path.isdir(d):
                    continue
                mp = os.path.join(d, "meta.json")
                if os.path.isfile(mp):
                    try:
                        with open(mp, "r", encoding="utf-8") as f:
                            meta = json.load(f)
                        exp = int(meta.get("expire_at") or 0)
                        if exp and exp < now:
                            shutil.rmtree(d, ignore_errors=True)
                            removed += 1
                    except (OSError, ValueError):
                        pass
                else:
                    # 没有 meta 的目录 = 中断的上传残留
                    try:
                        if now - os.path.getmtime(d) > 86400:
                            shutil.rmtree(d, ignore_errors=True)
                            removed += 1
                    except OSError:
                        pass
            if removed:
                log("清理过期分享 %d 个" % removed)
        except OSError as exc:
            log("清理任务异常:", repr(exc))


# ---------------------------------------------------------------- main

def main() -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.isfile(TOKEN_FILE):
        tok = secrets.token_urlsafe(24)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(tok)
        os.chmod(TOKEN_FILE, 0o600)
        log("已生成 share_token:", TOKEN_FILE)

    stop_evt = threading.Event()
    threading.Thread(target=cleaner_loop, args=(stop_evt,), daemon=True).start()

    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    srv.daemon_threads = True
    log("vdl-share 启动: http://%s:%d  数据目录=%s  单文件上限=%s"
        % (LISTEN_HOST, LISTEN_PORT, DATA_DIR, human_size(MAX_FILE_BYTES)))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
