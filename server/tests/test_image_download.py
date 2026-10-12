"""server/image_download.py + routers/image.py 回归测试（2026-10-12 图片下载 P0）。

背景：图片直链下载（「无水印图片」方案 L1）新增了整条链路 —— image_download 核心传输 /
SSRF 重定向护栏 / 魔数识别 / 文件名清洗 / kind=image 任务分流（tasks.py + routers/core.py）
/ HTTP 入口 routers/image.py / 前端 mode-switch 接线。此前这块**零测试**，而它直接决定
「用户粘贴的图片能不能落盘、内网地址会不会被偷渡、HTML 错误页会不会被当成图片存下来」。

覆盖：
  · sniff_image_ext          —— 魔数表 / ftyp 盒 / SVG / content-type 兜底
  · normalize_image_url      —— scheme 拒绝清单（data:/javascript:/file:…）、裸域名补全
  · assert_public_url        —— 私网/环回/链路本地/CGNAT/元数据地址拒绝、公网放行、DNS 失败 fail-closed
  · _SafeRedirectHandler     —— 重定向每一跳过 SSRF（私网目标直接抛错）
  · fetch_image              —— 成功/非图片早拒/重定向私网拦截/重定向公网放行/体积上限/取消/空响应
  · derive_filename          —— Content-Disposition 优先级 / 扩展名以魔数为准 / 同名去重 / 穿越清洗
  · run_image_download       —— 成功落终态（含 filepath）/ 非图片 / 取消 / 404 / 停滞看门狗
  · tasks.py kind            —— 创建/持久化/公开字典三处同源
  · routers/image.py HTTP    —— 挂载/参数校验/SSRF 真实拦截/端到端落盘+file 媒体类型/暂停拒绝/重试分流
  · 前端 mode-switch         —— app.js / index.html / styles.css 接线棘轮

运行：
    cd server && python tests/test_image_download.py
    cd server && python -m pytest tests/test_image_download.py -v
"""
import atexit
import contextlib
import gzip as gzip_module
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from pathlib import Path
from unittest.mock import patch

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 🔴 隔离必须发生在 import app 之前：membership / tasks / auth_store 都吃 VDL_DATA_DIR，
# 不隔离会去读写真实 ~/.video-downloader（2026-09-30 事故同款）。
if not os.environ.get("VDL_DATA_DIR"):
    os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_img_dl_")
# 本文件测的是功能内部逻辑，登录门禁的拦截/放行由 tests/test_login_gate.py 单独覆盖
os.environ.setdefault("VDL_LOGIN_GATE", "0")
# 沙盒的 http_proxy 会拦截 127.0.0.1 —— 本地被测服务器必须直连
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

import app as server_app  # noqa: E402
import image_download  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from platforms import LinkError  # noqa: E402
from tasks import TaskStore  # noqa: E402

try:
    from PIL import Image as _PILImage  # noqa: E402,F401  （仅探测可用性）
    _HAS_PIL = True
except Exception:  # noqa: BLE001
    _HAS_PIL = False


# --------------------------------------------------------------------------- #
# 断言辅助
# --------------------------------------------------------------------------- #
def check(desc, cond, extra=""):
    assert cond, f"{desc}" + (f" | {extra}" if extra else "")


# --------------------------------------------------------------------------- #
# 测试素材：真实可解码 PNG（PIL 能打开，_probe_dimensions 才有东西可读）
# --------------------------------------------------------------------------- #
def _make_png(w=4, h=4, rgb=(220, 30, 30)) -> bytes:
    def _chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    return (sig + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


_PNG = _make_png(4, 4)
_SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4">'
        b'<rect width="4" height="4" fill="#e11d48"/></svg>')

# 分享页素材：og:image 真图 + 站点 logo + 同图 prv/dft 双令牌 + http/https 重复 +
# srcset + 一个站外脚本（应被过滤）。{} 占位符由 _ImgHandler 填成本地端口。
_SHARE_PAGE_TMPL = """<!doctype html><html><head>
<meta property="og:image" content="//picasso-static.xhscdn.com/fe-platform/logo.png">
<meta property="og:image" content="http://127.0.0.1:{port}/real.png">
<meta name="viewport" content="width=device-width">
</head><body>
<img src="//127.0.0.1:{port}/real.png">
<img src="http://127.0.0.1:{port}/pic/abc123!nd_prv_wlteh_webp_3">
<img src="http://127.0.0.1:{port}/pic/abc123!nd_dft_wlteh_webp_3">
<img src="https://127.0.0.1:{port}/real.png">
<img src="http://127.0.0.1:{port}/assets/icon.svg">
<img src="/relative/real2.png">
<img src="data:image/png;base64,iVBORw0KGgo=">
<img srcset="http://127.0.0.1:{port}/pic/small.png 480w, http://127.0.0.1:{port}/pic/big.png 1080w">
<script src="http://127.0.0.1:{port}/assets/app.js"></script>
</body></html>"""


# --------------------------------------------------------------------------- #
# 本地 HTTP 服务器（离线：全部流量落 127.0.0.1，不碰外网）
# --------------------------------------------------------------------------- #
_LAST_REQ: dict = {}


class _ImgHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "vdl-img-test"

    def log_message(self, *a):  # 静音，避免污染测试输出
        pass

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        global _LAST_REQ
        path = self.path.split("?")[0]
        port = self.server.server_address[1]
        _LAST_REQ = {"referer": self.headers.get("Referer") or "",
                     "cookie": self.headers.get("Cookie") or "",
                     "ua": self.headers.get("User-Agent") or "",
                     "accept_encoding": self.headers.get("Accept-Encoding") or "",
                     "sec_ch_ua": self.headers.get("sec-ch-ua") or "",
                     "sec_fetch_mode": self.headers.get("Sec-Fetch-Mode") or ""}
        if path == "/real.png":
            self._send(200, _PNG, "image/png")
        elif path == "/real.svg":
            self._send(200, _SVG, "image/svg+xml")
        elif path == "/tiny.gif":            # <12 字节：走循环后的兜底 sniff
            self._send(200, b"GIF89a\x01\x00\x01\x00", "image/gif")
        elif path == "/page.html":           # HTML 错误页（text/html）
            self._send(200, b"<html><body><h1>404 Not Found</h1></body></html>",
                       "text/html; charset=utf-8")
        elif path == "/octet.png":           # 图片字节但 CT 是 octet-stream（魔数优先）
            self._send(200, _PNG, "application/octet-stream")
        elif path == "/labeled.png":         # HTML 字节但 CT 声称 image/png（CT 兜底）
            self._send(200, b"<html><body>err</body></html>", "image/png")
        elif path == "/empty":
            self._send(200, b"", "image/png")
        elif path == "/redirect-private":    # 302 → 环回地址（必须被每一跳 SSRF 拦下）
            self._send(302, b"", "text/plain",
                       extra={"Location": f"http://127.0.0.1:{port}/real.png"})
        elif path == "/redirect-public":     # 302 → 公网主机（假 DNS 放行、连接回落本地）
            self._send(302, b"", "text/plain",
                       extra={"Location": f"http://cdn.test:{port}/real.png"})
        elif path == "/big-cl":              # Content-Length 超大：读 body 前就该拒
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", "100000000")
            self.end_headers()
        elif path == "/stream-big":          # 无 CL 的流式超限（CL 预检拦不到，必须读中限）
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            try:
                for _ in range(3):
                    self.wfile.write(_PNG + b"\x00" * (image_download.CHUNK_SIZE - len(_PNG)))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/slow":                # 停滞看门狗素材：发一块、停 1.6s、再发
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(3 * image_download.CHUNK_SIZE))
            self.end_headers()
            try:
                for i in range(3):
                    if i:
                        time.sleep(1.6)
                    self.wfile.write(_PNG + b"\x00" * (image_download.CHUNK_SIZE - len(_PNG)))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/gz.png":              # gzip 压缩的图片（必须解压后再嗅探）
            gz = gzip_module.compress(_PNG, 6)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(gz)))
            self.end_headers()
            try:
                self.wfile.write(gz)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/df.png":              # deflate 压缩的图片
            co = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
            body = co.compress(_PNG) + co.flush()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Encoding", "deflate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif path == "/share.html":          # 分享页：og:image + logo + prv/dft + srcset
            html = _SHARE_PAGE_TMPL.format(port=port)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/login-wall.html":     # 登录墙：只有站点静态 logo，没有正文图
            self._send(200,
                       b"<html><head><meta property='og:image' "
                       b"content='//picasso-static.xhscdn.com/fe-platform/logo.png'>"
                       b"</head><body><div id='login'>\xe7\x99\xbb\xe5\xbd\x95</div>"
                       b"<img src='/assets/icon.svg'></body></html>",
                       "text/html; charset=utf-8")
        elif path == "/redir-to-loopback":   # 302 → 环回：_fetch_page_text 必须逐跳拦下
            self._send(302, b"", "text/plain",
                       extra={"Location": f"http://127.0.0.1:{port}/real.png"})
        elif path.endswith(".png") or "!nd_" in path:
            # 分享页候选的统一素材（含 !nd_dft_wlteh_webp_3 这类带处理令牌的路径）
            self._send(200, _PNG, "image/png")
        else:
            self.send_error(404)


_SRV = None


def _ensure_server() -> int:
    global _SRV
    if _SRV is not None:
        return _SRV.server_address[1]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ImgHandler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    atexit.register(srv.shutdown)
    _SRV = srv
    return srv.server_address[1]


def _u(path: str) -> str:
    return f"http://127.0.0.1:{_ensure_server()}{path}"


# 测试进程内强制「不走代理」：保留真实的 _SafeRedirectHandler（SSRF 护栏正是被测对象），
# 只把 ProxyHandler 换成空表 —— 与桌面 App 本机自连场景一致。
_ORIG_BUILD_OPENER = image_download._build_opener


def _direct_build_opener(proxy: str = ""):
    return urllib.request.build_opener(image_download._SafeRedirectHandler(),
                                       urllib.request.ProxyHandler({}))


image_download._build_opener = _direct_build_opener
atexit.register(lambda: setattr(image_download, "_build_opener", _ORIG_BUILD_OPENER))


# --------------------------------------------------------------------------- #
# 假 DNS：把「SSRF 检查签名（port=None）」与「连接签名（port=int）」分开对待
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _fake_dns(mapping=None, multi=None, gaierror_hosts=()):
    real = socket.getaddrinfo
    mapping = mapping or {}
    multi = multi or {}

    def _fake(host, port, *args, **kwargs):
        if host in gaierror_hosts:
            raise socket.gaierror(-2, "Name or service not known")
        if host in multi:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))
                    for ip in multi[host]]
        if host in mapping:
            ip = mapping[host] if port is None else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                     (ip, port or 0))]
        return real(host, port, *args, **kwargs)

    socket.getaddrinfo = _fake
    try:
        yield
    finally:
        socket.getaddrinfo = real


def _mkstore():
    tmp = Path(tempfile.mkdtemp(prefix="vdl_img_store_"))
    return TaskStore(root=tmp / "root", state_dir=tmp / "state"), tmp


# --------------------------------------------------------------------------- #
# 魔数识别
# --------------------------------------------------------------------------- #
def test_sniff_image_ext_covers_magic_table():
    cases = [
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", ".jpg"),
        (_PNG, ".png"),
        (b"GIF87a" + b"\x01\x00\x01\x00", ".gif"),
        (b"GIF89a" + b"\x01\x00\x01\x00", ".gif"),
        (b"II*\x00" + b"\x08\x00\x00\x00", ".tiff"),
        (b"MM\x00*" + b"\x00\x00\x00\x08", ".tiff"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 12, ".webp"),
        (b"BM\x36\x00\x00\x00\x00\x00\x00\x00", ".bmp"),
        (b"\x00\x00\x00\x20ftypavifmif1", ".avif"),
        (b"\x00\x00\x00\x20ftypheicmif1", ".heic"),
        (b"\x00\x00\x00\x20ftypmif1mif1", ".heif"),
        (_SVG, ".svg"),
        (b"<?xml version='1.0'?>\n<svg xmlns='x'></svg>", ".svg"),
    ]
    for head, want in cases:
        got = image_download.sniff_image_ext(head)
        check(f"魔数识别 {head[:12]!r}", got == want, f"期望 {want}，实际 {got}")
    print(f"✅ 魔数表全覆盖（{len(cases)} 种格式：jpg/png/gif/tiff/webp/bmp/avif/heic/heif/svg）")


def test_sniff_image_ext_content_type_fallback():
    # 魔数不认识但 CT 声明是图片 → 兜底（CDN 常把图片标 octet-stream 或错标）
    check("CT 兜底 image/png", image_download.sniff_image_ext(b"\x00" * 32, "image/png") == ".png")
    check("CT 兜底 image/jpeg", image_download.sniff_image_ext(b"\x00" * 32, "image/jpeg") == ".jpg")
    check("CT 兜底 image/svg+xml", image_download.sniff_image_ext(b"\x00" * 32, "image/svg+xml") == ".svg")
    check("CT 兜底 image/x-icon", image_download.sniff_image_ext(b"\x00" * 32, "image/x-icon") == ".ico")
    # 非图片：魔数不认识 + CT 也不是图片 → None（HTML 错误页 / octet-stream 伪装都走这条）
    check("HTML 页面拒绝", image_download.sniff_image_ext(b"<html><body>404</body></html>", "text/html") is None)
    check("octet-stream 拒绝", image_download.sniff_image_ext(b"\x00" * 32, "application/octet-stream") is None)
    check("空响应拒绝", image_download.sniff_image_ext(b"") is None)
    check("空 CT + 垃圾字节拒绝", image_download.sniff_image_ext(b"\x00" * 32, "") is None)
    # 魔数优先：即使 CT 说是网页，PNG 头仍然判 .png
    check("魔数优先于 CT", image_download.sniff_image_ext(_PNG, "text/html") == ".png")
    print("✅ content-type 兜底与「魔数优先」顺序均被钉死（防 HTML 错误页/伪装流落盘）")


# --------------------------------------------------------------------------- #
# URL 规范化
# --------------------------------------------------------------------------- #
def test_normalize_image_url_scheme_guard():
    # 裸域名补 https；已有协议的原样保留
    check("裸域名补 https", image_download.normalize_image_url("example.com/a.png")
          == "https://example.com/a.png")
    check("http 原样保留", image_download.normalize_image_url("http://a.test/x.png")
          == "http://a.test/x.png")
    check("首尾空白被剥掉", image_download.normalize_image_url("  https://a.test/x.png  ")
          == "https://a.test/x.png")
    check("大写 scheme 放行", image_download.normalize_image_url("HTTPS://A.test/X.PNG")
          == "HTTPS://A.test/X.PNG")
    # 🔴 拒绝清单：早年「无 :// 就补 https」的启发式会被 data: 绕过（补完变成
    # https://data:...，scheme 竟是 https）—— 这条清单就是那次冒烟实测的回归钉。
    for bad in ("data:image/png;base64,AAAA", "DATA:image/png;base64,AAAA",
                "javascript:alert(1)", "file:///etc/passwd", "ftp://a.test/x.png",
                "blob:https://a.test/uuid", "about:blank", "vbscript:msgbox"):
        try:
            image_download.normalize_image_url(bad)
            raise AssertionError(f"应被拒绝但放行了: {bad}")
        except LinkError:
            pass
    # 明显不是链接的输入早拒（给用户可读原因，而不是等到下载才「域名解析失败」）
    for bad, why in (("", "空串"), ("   ", "纯空白"), ("http://", "无主机名"),
                     ("https://exa mple.com/a.png", "主机名含空格")):
        try:
            image_download.normalize_image_url(bad)
            raise AssertionError(f"{why} 应被拒绝但放行了: {bad!r}")
        except LinkError:
            pass
    print("✅ scheme 拒绝清单（data:/javascript:/file:/ftp:/blob:/about:/vbscript:）+ 空/无主机/含空格早拒")


# --------------------------------------------------------------------------- #
# SSRF：初始与每一跳
# --------------------------------------------------------------------------- #
def test_assert_public_url_rejects_private_targets():
    bad_urls = [
        "http://127.0.0.1/x.png",            # 环回
        "http://10.1.2.3/x.png",             # 私网 10/8
        "http://192.168.1.1/x.png",          # 私网 192.168/16
        "http://172.16.5.5/x.png",           # 私网 172.16/12
        "http://169.254.169.254/latest/meta-data/",  # 云元数据（链路本地）
        "http://100.64.0.1/x.png",           # CGNAT：Python3.13 is_private=False，必须显式列举
        "http://[::1]/x.png",                # IPv6 环回
        "http://[fe80::1]/x.png",            # IPv6 链路本地
        "http://[::ffff:127.0.0.1]/x.png",   # IPv4-mapped 环回
        "http://localhost/x.png",            # 名字即拒（不等 DNS）
        "http://printer.local/x.png",        # mDNS
        "http://db.internal/x.png",          # 内网后缀
    ]
    for url in bad_urls:
        try:
            image_download.assert_public_url(url)
            raise AssertionError(f"应被拒绝但放行了: {url}")
        except LinkError:
            pass
    print(f"✅ SSRF 拒绝 {len(bad_urls)} 类非公开目标（环回/私网/CGNAT/链路本地/元数据/local/.internal/mapped）")


def test_assert_public_url_allows_public():
    with _fake_dns(mapping={"cdn.test": "93.184.216.34"}):
        image_download.assert_public_url("http://cdn.test/a.png")   # 公网 IP → 放行
    # 「公网 + 私网混合」视为合法（CDN/DNS 污染场景不能一票否决，与 app._assert_safe_url 同口径）
    with _fake_dns(multi={"mixed.test": ["10.0.0.1", "93.184.216.34"]}):
        image_download.assert_public_url("http://mixed.test/a.png")
    # IPv4-mapped 公网地址同样放行
    with _fake_dns(multi={"v6.test": ["::ffff:93.184.216.34"]}):
        image_download.assert_public_url("http://v6.test/a.png")
    print("✅ 公网目标放行；公网+私网混合不误杀（与 app._assert_safe_url 判据一致）")


def test_assert_public_url_dns_failure_is_fail_closed():
    # DNS 解析失败必须拒绝（fail-open 会让「检查时失败、下载时重绑定到内网」得逞）
    with _fake_dns(gaierror_hosts=("nx.invalid",)):
        try:
            image_download.assert_public_url("http://nx.invalid/a.png")
            raise AssertionError("DNS 失败必须拒绝（fail-closed）")
        except LinkError as e:
            check("DNS 失败给可读原因", "域名解析失败" in str(e))
    print("✅ DNS 解析失败 fail-closed（拒绝而非放行，堵重绑定绕过）")


def test_safe_redirect_handler_blocks_private_hop():
    """重定向每一跳都过 SSRF：否则「公网 URL 302 到 169.254.169.254」即可绕过初始检查。"""
    handler = image_download._SafeRedirectHandler()
    req = urllib.request.Request("http://public.test/redirect")
    with _fake_dns():
        try:
            handler.redirect_request(req, None, 302, "Found", {},
                                     "http://169.254.169.254/latest/meta-data/")
            raise AssertionError("重定向到云元数据地址必须被拒绝")
        except LinkError:
            pass
        try:
            handler.redirect_request(req, None, 302, "Found", {},
                                     "http://127.0.0.1:8321/x.png")
            raise AssertionError("重定向到环回地址必须被拒绝")
        except LinkError:
            pass
    print("✅ _SafeRedirectHandler 对私网/元数据跳转目标直接抛错（每一跳都校验）")


def test_safe_redirect_handler_allows_public_hop():
    handler = image_download._SafeRedirectHandler()
    req = urllib.request.Request("http://127.0.0.1:1/redirect")
    with _fake_dns(mapping={"cdn.test": "93.184.216.34"}):
        new = handler.redirect_request(req, None, 302, "Found", {},
                                       "http://cdn.test/real.png")
    check("公网跳转目标放行（返回新 Request）", new is not None)
    check("新 Request 指向跳转目标", new.full_url == "http://cdn.test/real.png")
    print("✅ _SafeRedirectHandler 放行公网跳转目标（不误伤正常 CDN 302）")


def test_build_opener_wires_safe_redirect_handler():
    # _ORIG_BUILD_OPENER 是打补丁**之前**捕获的真实函数（见模块顶部）
    op = _ORIG_BUILD_OPENER("")
    check("注入 _SafeRedirectHandler（重定向护栏随 opener 走）",
          any(isinstance(h, image_download._SafeRedirectHandler) for h in op.handlers))
    # 显式代理
    op2 = _ORIG_BUILD_OPENER("http://127.0.0.1:8899")
    ph2 = [h for h in op2.handlers if isinstance(h, urllib.request.ProxyHandler)]
    check("显式代理进入 ProxyHandler（http+https 双协议）",
          bool(ph2) and ph2[0].proxies.get("http") == "http://127.0.0.1:8899"
          and ph2[0].proxies.get("https") == "http://127.0.0.1:8899")
    # 环境变量代理：注入标准协议名的 *_proxy，ProxyHandler 必须真的挂上。
    # ⚠️ 不能直接断言「_build_opener('') 一定含 ProxyHandler」：app 启动时
    # _ensure_vps_env() 会设 VDL_WORKER_PROXY=off，经 getproxies_environment()
    # 变成键 "vdl_worker"（含下划线）—— 不匹配 urllib 的 protocol_open 模式，
    # 该 ProxyHandler 会被 add_handler 静默丢弃（实测）。故此处显式给标准协议名。
    _saved = {k: os.environ.get(k) for k in ("http_proxy", "https_proxy")}
    os.environ["http_proxy"] = "http://127.0.0.1:8899"
    os.environ["https_proxy"] = "http://127.0.0.1:8899"
    try:
        op3 = _ORIG_BUILD_OPENER("")
        ph3 = [h for h in op3.handlers if isinstance(h, urllib.request.ProxyHandler)]
        check("环境变量代理被 ProxyHandler 接纳（沿用系统代理，yt-dlp 同款默认行为）",
              bool(ph3) and ph3[0].proxies.get("http") == "http://127.0.0.1:8899")
    finally:
        for k, v in _saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("✅ _build_opener 装配：SSRF 重定向处理器常驻 + 代理（显式/环境变量两路都生效）")


# --------------------------------------------------------------------------- #
# 核心传输
# --------------------------------------------------------------------------- #
def test_fetch_image_success_png():
    dest = Path(tempfile.mkdtemp(prefix="vdl_img_f_")) / "image.part"
    seen = []
    meta = image_download.fetch_image(
        _u("/real.png"), dest,
        progress=lambda d, t: seen.append((d, t)),
        should_stop=lambda: False)
    check("返回字节数", meta["bytes"] == len(_PNG), str(meta))
    check("扩展名按魔数", meta["ext"] == ".png")
    check("content-type 透传", meta["content_type"] == "image/png")
    check("final_url 落到真实地址", meta["final_url"].endswith("/real.png"))
    check("文件内容逐字节一致", dest.read_bytes() == _PNG)
    check("进度回调被调用", bool(seen) and seen[-1][0] == len(_PNG))
    shutil.rmtree(dest.parent, ignore_errors=True)
    print("✅ fetch_image 正常路径：流式落盘 + 魔数扩展名 + 进度回调（%d 字节）" % len(_PNG))


def test_fetch_image_rejects_non_image_before_writing():
    """HTML 错误页必须在写盘前拒绝（而不是把整个页面存成 .png）。"""
    dest = Path(tempfile.mkdtemp(prefix="vdl_img_f_")) / "image.part"
    try:
        image_download.fetch_image(_u("/page.html"), dest)
        raise AssertionError("HTML 页面必须被拒绝")
    except LinkError as e:
        check("错误文案可读", "不是图片" in str(e), str(e))
    check("拒绝时未写入正文", (not dest.exists()) or dest.stat().st_size == 0)
    shutil.rmtree(dest.parent, ignore_errors=True)
    print("✅ 非图片内容早拒：读完第一块即判、正文零落盘（防错误页/伪装流存成图片）")


def test_fetch_image_blocks_redirect_to_private():
    dest = Path(tempfile.mkdtemp(prefix="vdl_img_f_")) / "image.part"
    try:
        image_download.fetch_image(_u("/redirect-private"), dest)
        raise AssertionError("重定向到环回地址必须被拒绝")
    except LinkError as e:
        check("拒绝原因指向非公开网络", "非公开网络" in str(e), str(e))
    shutil.rmtree(dest.parent, ignore_errors=True)
    print("✅ 重定向偷渡被拦：公网入口 302 → 127.0.0.1，每一跳 SSRF 生效")


def test_fetch_image_follows_redirect_to_public():
    dest = Path(tempfile.mkdtemp(prefix="vdl_img_f_")) / "image.part"
    with _fake_dns(mapping={"cdn.test": "93.184.216.34"}):
        meta = image_download.fetch_image(_u("/redirect-public"), dest)
    check("跟随公网跳转成功", meta["bytes"] == len(_PNG))
    check("final_url 是跳转后地址", meta["final_url"] == f"http://cdn.test:{_ensure_server()}/real.png")
    check("落盘内容正确", dest.read_bytes() == _PNG)
    shutil.rmtree(dest.parent, ignore_errors=True)
    print("✅ 正常 CDN 302 跟随后落盘（公网跳转放行 + 流量到达最终地址）")


def test_fetch_image_enforces_content_length_limit():
    dest = Path(tempfile.mkdtemp(prefix="vdl_img_f_")) / "image.part"
    try:
        image_download.fetch_image(_u("/big-cl"), dest, max_bytes=1024)
        raise AssertionError("Content-Length 超限必须拒绝")
    except LinkError as e:
        check("CL 超限文案", "超过上限" in str(e), str(e))
    check("CL 超限在读 body 前拒绝（无落盘）", not dest.exists())
    shutil.rmtree(dest.parent, ignore_errors=True)
    print("✅ Content-Length 预检超限即拒（不浪费带宽、不落盘）")


def test_fetch_image_enforces_stream_limit():
    dest = Path(tempfile.mkdtemp(prefix="vdl_img_f_")) / "image.part"
    limit = 70000
    try:
        image_download.fetch_image(_u("/stream-big"), dest, max_bytes=limit)
        raise AssertionError("流式传输超限必须拒绝")
    except LinkError as e:
        check("流式超限文案", "超过上限" in str(e), str(e))
    size = dest.stat().st_size if dest.exists() else 0
    check("超限时已写字节不超过上限", 0 < size <= limit, f"size={size} limit={limit}")
    shutil.rmtree(dest.parent, ignore_errors=True)
    print(f"✅ 流式体积上限生效（写到 {size}B 即中止，上限 {limit}B）")


def test_fetch_image_stops_on_should_stop():
    dest = Path(tempfile.mkdtemp(prefix="vdl_img_f_")) / "image.part"
    # ① 立即取消
    try:
        image_download.fetch_image(_u("/real.png"), dest, should_stop=lambda: True)
        raise AssertionError("should_stop 命中必须抛 _Stopped")
    except image_download._Stopped:
        pass
    # ② 传输中途取消（第一块进度回调之后）
    state = {"n": 0}

    def _prog(done, total):
        state["n"] += 1

    try:
        image_download.fetch_image(_u("/real.png"), dest,
                                   progress=_prog,
                                   should_stop=lambda: state["n"] >= 1)
        raise AssertionError("中途 should_stop 必须抛 _Stopped")
    except image_download._Stopped:
        pass
    check("中途取消确实发生在写块之后", state["n"] >= 1)
    shutil.rmtree(dest.parent, ignore_errors=True)
    print("✅ 协作式取消在读块边界消费（立即/中途两条路径都落 _Stopped）")


def test_fetch_image_edge_bodies_and_headers():
    tmp = Path(tempfile.mkdtemp(prefix="vdl_img_f_"))
    # 空响应 → 拒绝
    try:
        image_download.fetch_image(_u("/empty"), tmp / "a.part")
        raise AssertionError("空响应必须拒绝")
    except LinkError as e:
        check("空响应文案", "不是图片" in str(e), str(e))
    # 极短（<12B）但魔数合法 → 兜底 sniff 放行
    meta = image_download.fetch_image(_u("/tiny.gif"), tmp / "b.part")
    check("短体 GIF 走兜底 sniff", meta["ext"] == ".gif" and meta["bytes"] == 10)
    # 防盗链 Referer / Cookie 必须带上（部分 CDN 缺了就 403）
    image_download.fetch_image(_u("/real.png"), tmp / "c.part",
                               referer="https://ref.test/page", cookie="k=v")
    check("Referer 透传", _LAST_REQ.get("referer") == "https://ref.test/page", str(_LAST_REQ))
    check("Cookie 透传", _LAST_REQ.get("cookie") == "k=v", str(_LAST_REQ))
    check("默认 UA 已设置", _LAST_REQ.get("ua", "").startswith("Mozilla"))
    # 魔数优先于 content-type：octet-stream 里的 PNG 字节仍判 .png
    meta2 = image_download.fetch_image(_u("/octet.png"), tmp / "d.part")
    check("魔数优先（CT=octet-stream）", meta2["ext"] == ".png")
    # CT 兜底：声称 image/png 的 HTML 也按图片收（CDN 错标场景）
    meta3 = image_download.fetch_image(_u("/labeled.png"), tmp / "e.part")
    check("CT 兜底（CT=image/png）", meta3["ext"] == ".png")
    shutil.rmtree(tmp, ignore_errors=True)
    print("✅ 空响应拒绝 / 短体兜底 / Referer+Cookie 透传 / 魔数与 CT 的优先级边界")


# --------------------------------------------------------------------------- #
# 文件名推导
# --------------------------------------------------------------------------- #
def test_derive_filename_priority_and_sanitize():
    # Content-Disposition 优先（RFC 5987 filename* > 引号 filename > 裸 filename）
    check("filename* 优先",
          image_download.derive_filename("https://x.test/a",
                                         "attachment; filename*=UTF-8''%E4%BD%A0%E5%A5%BD.png",
                                         ".png") == "你好.png")
    check("引号 filename",
          image_download.derive_filename("https://x.test/dir/pic.php",
                                         'attachment; filename="photo.jpg"', ".png") == "photo.png")
    check("裸 filename",
          image_download.derive_filename("https://x.test/a",
                                         "attachment; filename=shot.webp", ".jpg") == "shot.jpg")
    # URL 末段兜底（含百分号解码）
    check("URL 末段兜底 + 解码",
          image_download.derive_filename("https://x.test/dir/%E7%8E%89%E5%8F%B3.png", "", ".png")
          == "玉右.png")
    # 扩展名永远以魔数为准（CDN 动态地址 .php 也按图片存）
    check("扩展名以魔数为准",
          image_download.derive_filename("https://x.test/img.php?id=1", "", ".png") == "img.png")
    # 空 CD + 根路径 → image
    check("兜底名 image", image_download.derive_filename("https://x.test/", "", ".png") == "image.png")
    # 目录穿越 / 引号 / 连续点清洗
    trav = image_download.derive_filename(
        "https://x.test/a", 'attachment; filename="../../etc/漂亮 图/片.png"', ".png")
    check("穿越残留无分隔符", "/" not in trav and "\\" not in trav, trav)
    check("穿越残留无 ..", ".." not in trav, trav)
    check("连续点被消",
          image_download.derive_filename("https://x.test/a", 'attachment; filename="a..b.png"',
                                         ".png") == "a_b.png")
    long_name = image_download.derive_filename(
        "https://x.test/a", 'attachment; filename="%s.png"' % ("n" * 200), ".png")
    check("超长名被截断", len(long_name) <= 84 and long_name.endswith(".png"), str(len(long_name)))
    print("✅ 文件名三源优先级（CD>URL>image）+ 扩展名魔数化 + 穿越/引号/超长清洗")


def test_derive_filename_dedup():
    tmp = Path(tempfile.mkdtemp(prefix="vdl_img_fn_"))
    check("首次无冲突用原名",
          image_download.derive_filename("https://x.test/a.png", "", ".png", tmp) == "a.png")
    (tmp / "a.png").write_bytes(b"x")
    check("冲突加序号 -1",
          image_download.derive_filename("https://x.test/a.png", "", ".png", tmp) == "a-1.png")
    (tmp / "a-1.png").write_bytes(b"x")
    check("连号冲突跳到 -2",
          image_download.derive_filename("https://x.test/a.png", "", ".png", tmp) == "a-2.png")
    shutil.rmtree(tmp, ignore_errors=True)
    print("✅ 同名自动加序号（批量下载不互相覆盖）")


# --------------------------------------------------------------------------- #
# 任务执行器
# --------------------------------------------------------------------------- #
def test_run_image_download_success():
    store, tmp = _mkstore()
    try:
        task = store.create(url=_u("/real.png"), title="", platform="图片",
                            quality="原图", quality_key="origin", kind="image")
        image_download.run_image_download(task, store)
        check("终态 completed", task.status == "completed", f"{task.status} {task.error}")
        check("filepath 已落库（/file 端点靠它放行）",
              task.filepath is not None and task.filepath.exists())
        check("落盘内容一致", task.filepath.read_bytes() == _PNG)
        check("文件名来自 URL 末段", task.filepath.name == "real.png", task.filepath.name)
        check("filesize 正确", task.filesize == len(_PNG))
        want_q = "4×4" if _HAS_PIL else "原图"
        check("宽高入库（PIL 探测）", task.quality == want_q, task.quality)
        if _HAS_PIL:
            with _PILImage.open(task.filepath) as im:
                check("PIL 探测宽高与库一致",
                      f"{im.width}×{im.height}" == task.quality,
                      f"{im.width}×{im.height} vs {task.quality}")
        check("步骤落 done", any(s.get("name") == "下载图片" and s.get("status") == "done"
                                 for s in task.steps))
        check("无 .part 残留", not (task.workdir / "image.part").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ 执行器成功路径：completed + filepath/文件名/体积/宽高/步骤全落库（曾漏 filepath，用户看得到完成却取不到文件）")


def test_run_image_download_failure_paths():
    store, tmp = _mkstore()
    try:
        # 非图片：/page.html 是 HTML 错误页 → 现在会尝试提图，提不到才落 failed
        t1 = store.create(url=_u("/page.html"), title="", platform="图片",
                          quality="原图", kind="image")
        image_download.run_image_download(t1, store)
        check("HTML 错误页落 failed",
              t1.status == "failed" and ("没有找到图片" in t1.error
                                         or "不是图片" in t1.error),
              f"{t1.status} {t1.error}")
        check("失败无 .part 残留", not (t1.workdir / "image.part").exists())
        # 404
        t2 = store.create(url=_u("/404"), title="", platform="图片", quality="原图", kind="image")
        image_download.run_image_download(t2, store)
        check("404 落 failed 且带 HTTP 码", t2.status == "failed" and "HTTP 404" in t2.error,
              f"{t2.status} {t2.error}")
        check("404 有可操作 hint", bool(t2.hint), t2.hint)
        # 取消
        t3 = store.create(url=_u("/real.png"), title="", platform="图片", quality="原图", kind="image")
        t3.cancel_requested = True
        image_download.run_image_download(t3, store)
        check("取消落 canceled", t3.status == "canceled" and "已取消" in t3.error,
              f"{t3.status} {t3.error}")
        check("取消无 .part 残留", not (t3.workdir / "image.part").exists())
        # 缺工作目录
        t4 = store.create(url="https://x.test/a.png", title="", platform="图片",
                          quality="原图", kind="image")
        t4.workdir = None
        image_download.run_image_download(t4, store)
        check("缺 workdir 落 failed", t4.status == "failed" and "工作目录" in t4.error,
              f"{t4.status} {t4.error}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ 执行器失败路径：非图片/404/取消/缺 workdir 全部落终态且不残留 .part")


def test_run_image_download_stall_watchdog():
    store, tmp = _mkstore()
    orig_stall, orig_poll = image_download.DOWNLOAD_STALL_TIMEOUT, image_download.WATCHDOG_POLL
    image_download.DOWNLOAD_STALL_TIMEOUT = 1.0
    image_download.WATCHDOG_POLL = 0.2
    try:
        task = store.create(url=_u("/slow"), title="", platform="图片",
                            quality="原图", kind="image")
        image_download.run_image_download(task, store)
        check("停滞落 failed", task.status == "failed", f"{task.status} {task.error}")
        check("停滞文案", "停滞" in task.error, task.error)
        check("取消原因标记为 stall", task.cancel_reason == "stall", task.cancel_reason)
        check("无 .part 残留", not (task.workdir / "image.part").exists())
    finally:
        image_download.DOWNLOAD_STALL_TIMEOUT = orig_stall
        image_download.WATCHDOG_POLL = orig_poll
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ 停滞看门狗：N 秒无字节增量 → 自动终止并落「下载停滞」终态（与视频下载同口径）")


# --------------------------------------------------------------------------- #
# tasks.py kind 字段三处同源
# --------------------------------------------------------------------------- #
def test_task_kind_field_roundtrip():
    store, tmp = _mkstore()
    try:
        check("默认 kind=video（历史行为不回退）",
              store.create(url="https://x.test/a.mp4", title="t", platform="B站",
                           quality="best").kind == "video")
        t = store.create(url=_u("/real.png"), title="", platform="图片",
                         quality="原图", kind="image")
        check("创建时 kind=image", t.kind == "image")
        check("公开字典带 kind（前端据此显示图片徽标）",
              t.to_public_dict().get("kind") == "image")
        store.flush()
        store2 = TaskStore(root=tmp / "root", state_dir=tmp / "state")
        t2 = store2.get(t.id)
        check("持久化 round-trip 保住 kind", t2 is not None and t2.kind == "image",
              getattr(t2, "kind", None))
        # 历史状态行（无 kind 键）必须回落 video
        legacy = {"version": 1, "saved_at": time.time(), "tasks": [{
            "id": "a" * 16, "url": "https://x.test/a.mp4", "title": "legacy",
            "platform": "B站", "quality": "best", "status": "failed",
            "created_at": time.time(),
        }]}
        (tmp / "root" / ("a" * 16)).mkdir(parents=True, exist_ok=True)
        (tmp / "state" / "tasks_state.json").write_text(
            json.dumps(legacy), encoding="utf-8")
        store3 = TaskStore(root=tmp / "root", state_dir=tmp / "state")
        t3 = store3.get("a" * 16)
        check("历史行缺 kind 回落 video", t3 is not None and t3.kind == "video",
              getattr(t3, "kind", None))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ kind 字段三处同源：创建默认 video / image 显式传入 / 持久化 round-trip / 历史行回落")


def test_human_bytes():
    check("0B", image_download._human(0) == "0B")
    check("512B", image_download._human(512) == "512B")
    check("1.5KB", image_download._human(1536) == "1.5KB")
    check("5.0MB", image_download._human(5 * 1024 * 1024) == "5.0MB")
    print("✅ _human 体积文案（B/KB/MB/GB 四档）")


# --------------------------------------------------------------------------- #
# HTTP 层（TestClient：挂载 / 校验 / SSRF 真实拦截 / 端到端 / kind 分流）
# --------------------------------------------------------------------------- #
_H = {"X-Device-Id": "img-test-device"}


def _client() -> TestClient:
    return TestClient(server_app.app)


def _wait_task(tid, statuses, timeout=20.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        t = server_app.store.get(tid)
        last = t
        if t is not None and t.status in statuses:
            return t
        time.sleep(0.05)
    raise AssertionError(
        f"任务 {tid} 未在 {timeout}s 内进入 {statuses}："
        f"status={getattr(last, 'status', None)} error={getattr(last, 'error', None)}")


def _cleanup_task(client: TestClient, tid: str):
    """等任务落终态后连同文件一起清掉（DELETE 对已结束任务即 remove）。"""
    try:
        _wait_task(tid, ("completed", "failed", "canceled"))
    except AssertionError:
        server_app.store.request_cancel(tid)
        _wait_task(tid, ("completed", "failed", "canceled"))
    r = client.delete(f"/api/tasks/{tid}", headers=_H)
    check("清理任务成功", r.status_code == 200, r.text[:200])
    check("任务已从 store 移除", server_app.store.get(tid) is None)


def _quota_snapshot():
    st = server_app.member_store
    st._ensure_loaded()
    return dict(st._state.get("daily_usage") or {})


def _reset_download_quota():
    st = server_app.member_store
    st._ensure_loaded()
    st._state.setdefault("daily_usage", {})["download"] = 0
    st._persist()


def _quota_restore(snap):
    st = server_app.member_store
    st._ensure_loaded()
    st._state["daily_usage"] = dict(snap)
    st._persist()


_QUOTA_SNAP = None


def setup_module():
    global _QUOTA_SNAP
    _QUOTA_SNAP = _quota_snapshot()
    _reset_download_quota()


def teardown_module():
    if _QUOTA_SNAP is not None:
        _quota_restore(_QUOTA_SNAP)


def test_image_route_mounted_and_validates():
    c = _client()
    # 空 urls → 400（路由已挂载：业务 400，不是 Starlette 的 "Not Found"）
    r = c.post("/api/image/download", json={"urls": []}, headers=_H)
    check("空 urls → 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    check("文案可读", "没有提供有效的图片链接" in r.text, r.text[:200])
    # 超过 50 条 → 422（pydantic max_length）
    r = c.post("/api/image/download", json={"urls": [f"https://x.test/{i}.png" for i in range(51)]},
               headers=_H)
    check("超过批量上限 → 422", r.status_code == 422, f"{r.status_code} {r.text[:200]}")
    # 全部无法识别 → 400
    r = c.post("/api/image/download", json={"urls": ["data:image/png;base64,AAAA"]}, headers=_H)
    check("非 http(s) 全拒 → 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    print("✅ /api/image/download 已挂载：空列表 400 / 超 50 条 422 / 非 http(s) 全拒 400")


def test_image_download_ssrf_guard_over_http():
    """走真实 router 的真实 app._assert_safe_url（不打桩）：私网/元数据地址必须被拒。"""
    c = _client()
    before = {t.id for t in server_app.store.list_all()}
    r = c.post("/api/image/download",
               json={"urls": ["http://127.0.0.1:8321/x.png",
                              "http://169.254.169.254/latest/meta-data/",
                              "http://192.168.1.1/x.png"]},
               headers=_H)
    check("私网批量全拒 → 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    after = {t.id for t in server_app.store.list_all()}
    check("零任务创建（连排队都没进）", not (after - before), str(after - before))
    print("✅ HTTP 层 SSRF 真实拦截：环回/云元数据/私网批量提交 → 400 且零任务创建（fail-closed）")


def test_image_download_end_to_end_with_file_media_type():
    c = _client()
    _reset_download_quota()
    tid = None
    try:
        with patch.object(server_app, "_assert_safe_url", lambda url: None):
            r = c.post("/api/image/download", json={"urls": [_u("/real.png")]}, headers=_H)
        check("创建成功 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        data = r.json()
        check("返回 1 个任务", data["count"] == 1 and len(data["task_ids"]) == 1, str(data))
        check("按次计费 1 次", data["quota"]["free_used"] == 1, str(data.get("quota")))
        tid = data["task_ids"][0]
        t = _wait_task(tid, ("completed", "failed"))
        check("端到端落盘 completed", t.status == "completed", f"{t.status} {t.error}")
        check("kind=image", t.kind == "image")
        # /file 端点：图片 media type（<img>/预览内联渲染）+ 内容逐字节一致
        rf = c.get(f"/api/tasks/{tid}/file", headers=_H)
        check("取文件 200", rf.status_code == 200, f"{rf.status_code} {rf.text[:200]}")
        check("PNG media type", rf.headers.get("content-type") == "image/png",
              rf.headers.get("content-type"))
        check("文件字节一致", rf.content == _PNG)
        # download=1 → 附件（保存场景）
        rd = c.get(f"/api/tasks/{tid}/file?download=1", headers=_H)
        check("附件场景 200", rd.status_code == 200)
        check("附件带 Content-Disposition",
              "attachment" in rd.headers.get("content-disposition", ""),
              rd.headers.get("content-disposition"))
    finally:
        if tid:
            _cleanup_task(c, tid)
    print("✅ HTTP 端到端：创建→调度→completed→/file 取回（image/png + 附件头 + 字节一致）")


def test_image_download_svg_served_as_attachment_only():
    """SVG 可含 <script>：/file 必须强制 octet-stream，绝不能内联渲染。"""
    c = _client()
    _reset_download_quota()
    tid = None
    try:
        with patch.object(server_app, "_assert_safe_url", lambda url: None):
            r = c.post("/api/image/download", json={"urls": [_u("/real.svg")]}, headers=_H)
        check("SVG 创建成功", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        tid = r.json()["task_ids"][0]
        t = _wait_task(tid, ("completed", "failed"))
        check("SVG 落盘 completed", t.status == "completed", f"{t.status} {t.error}")
        check("扩展名 .svg", (t.filepath or Path("")).suffix == ".svg")
        rf = c.get(f"/api/tasks/{tid}/file", headers=_H)
        check("SVG 取文件 200", rf.status_code == 200)
        ct = rf.headers.get("content-type", "")
        check("SVG 绝不内联（image/svg+xml 会暴露会话给图片作者）",
              "image/svg+xml" not in ct and ct.startswith("application/octet-stream"), ct)
        check("内容一致", rf.content == _SVG)
    finally:
        if tid:
            _cleanup_task(c, tid)
    print("✅ SVG 只许附件下载（media type 强制 octet-stream，堵脚本内联渲染）")


def test_image_download_pause_rejected_and_cancel_works():
    c = _client()
    _reset_download_quota()
    tid = None
    try:
        with patch.object(server_app, "_assert_safe_url", lambda url: None):
            r = c.post("/api/image/download", json={"urls": [_u("/slow")]}, headers=_H)
        check("创建成功", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        tid = r.json()["task_ids"][0]
        _wait_task(tid, ("downloading", "completed", "failed"))
        rp = c.post(f"/api/tasks/{tid}/pause", headers=_H)
        check("暂停 200", rp.status_code == 200, f"{rp.status_code} {rp.text[:200]}")
        body = rp.json()
        check("图片暂停被拒（paused=False）", body.get("paused") is False, str(body))
        check("拒绝原因可读", "图片下载不支持暂停" in body.get("message", ""), str(body))
        check("任务未卡在 pausing 中间态",
              server_app.store.get(tid).status in ("downloading", "completed", "failed"),
              server_app.store.get(tid).status)
    finally:
        if tid:
            _cleanup_task(c, tid)
    print("✅ 图片任务暂停被拒且不卡 pausing 中间态（后端与前端隐藏暂停按钮同口径）")


def test_image_download_retry_routes_to_image_executor():
    c = _client()
    _reset_download_quota()
    tid = None
    try:
        with patch.object(server_app, "_assert_safe_url", lambda url: None):
            r = c.post("/api/image/download", json={"urls": [_u("/404")]}, headers=_H)
        tid = r.json()["task_ids"][0]
        t = _wait_task(tid, ("completed", "failed"))
        check("首轮失败", t.status == "failed" and "HTTP 404" in t.error,
              f"{t.status} {t.error}")
        rr = c.post(f"/api/tasks/{tid}/retry", headers=_H)
        check("重试 200", rr.status_code == 200, f"{rr.status_code} {rr.text[:200]}")
        t2 = _wait_task(tid, ("completed", "failed"))
        check("重试后仍走图片执行器（错误文案是图片链路的 HTTP 404，不是 yt-dlp 的 Unsupported URL）",
              t2.status == "failed" and "HTTP 404" in t2.error, f"{t2.status} {t2.error}")
    finally:
        if tid:
            _cleanup_task(c, tid)
    print("✅ 重试分流：kind=image 的任务重试仍进 run_image_download（不落回 yt-dlp 视频管线）")


def test_image_download_mixed_batch_skips_bad_links():
    c = _client()
    _reset_download_quota()
    tid = None
    try:
        with patch.object(server_app, "_assert_safe_url", lambda url: None):
            r = c.post("/api/image/download",
                       json={"urls": ["data:image/png;base64,AAAA",
                                      "javascript:alert(1)",
                                      _u("/real.png")]},
                       headers=_H)
        check("混合批量 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
        data = r.json()
        check("好链接建成 1 个任务", data["count"] == 1, str(data))
        check("坏链接计入 skipped", data["skipped"] == 2, str(data))
        tid = data["task_ids"][0]
        t = _wait_task(tid, ("completed", "failed"))
        check("唯一任务完成", t.status == "completed", f"{t.status} {t.error}")
    finally:
        if tid:
            _cleanup_task(c, tid)
    print("✅ 混合批量：坏链接 skipped 不烧额度、好链接正常落盘（计费只算成功创建的）")


# --------------------------------------------------------------------------- #
# 分享页解析（2026-10-12 用户实测小红书分享链接触发）
# --------------------------------------------------------------------------- #
def test_browser_headers_consistent_and_compressed():
    """浏览器特征必须自洽：UA 与 sec-ch-ua 同版本、声明 gzip/deflate。

    🔴 2026-10-12 实测：xhs WAF 校验 TLS 指纹 + 请求头自洽性 ——
    urllib 默认的 `Accept-Encoding: identity` 会直接被 302 去登录页；
    UA 与 sec-ch-ua 版本不一致同样被墙。这里把两条都钉死。
    """
    h = image_download._BROWSER_HEADERS
    ua_ver = re.search(r"Chrome/(\d+)\.", h.get("User-Agent", ""))
    ch_ver = re.search(r'Chromium";v="(\d+)"', h.get("sec-ch-ua", ""))
    check("UA 与 sec-ch-ua 版本一致",
          ua_ver and ch_ver and ua_ver.group(1) == ch_ver.group(1),
          f"UA={ua_ver and ua_ver.group(1)} sec-ch-ua={ch_ver and ch_ver.group(1)}")
    check("声明 gzip/deflate（不是 identity）",
          h.get("Accept-Encoding") == "gzip, deflate", h.get("Accept-Encoding"))
    check("带 Sec-Fetch-Mode=navigate", h.get("Sec-Fetch-Mode") == "navigate")
    check("带 Upgrade-Insecure-Requests", h.get("Upgrade-Insecure-Requests") == "1")
    # 服务器侧真的收到这些头
    _ensure_server()
    with tempfile.TemporaryDirectory() as d:
        image_download.fetch_image(_u("/real.png"), Path(d) / "a.part")
    check("服务器收到 Accept-Encoding: gzip, deflate",
          _LAST_REQ.get("accept_encoding") == "gzip, deflate",
          _LAST_REQ.get("accept_encoding"))
    check("服务器收到 sec-ch-ua", bool(_LAST_REQ.get("sec_ch_ua")))
    print("✅ 浏览器请求头自洽（UA/sec-ch-ua 同版本 + gzip/deflate + Sec-Fetch）")


def test_fetch_image_decompresses_gzip_and_deflate():
    """声明了压缩就必须真解压：否则魔数嗅到的是 1f 8b 而不是 PNG 头。"""
    for path in ("/gz.png", "/df.png"):
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "x.part"
            meta = image_download.fetch_image(_u(path), dest)
            check(f"{path} 解压后是 PNG",
                  meta["ext"] == ".png" and dest.read_bytes() == _PNG,
                  f"{meta} size={dest.stat().st_size}")
    print("✅ gzip / deflate 响应均正确解压后嗅探落盘（压缩不误判「不是图片」）")


def test_extract_page_images_priority_and_filtering():
    """og:image 优先、静态资源过滤、prv/dft 与 http/https 去重、srcset 取大图。"""
    html = _SHARE_PAGE_TMPL.format(port=_ensure_server())
    imgs = image_download.extract_page_images(html, _u("/share.html"))
    joined = "\n".join(imgs)
    check("og:image 真图入选", any("/real.png" in u for u in imgs), str(imgs))
    check("站点 logo（picasso-static）被过滤", "picasso-static" not in joined)
    check("svg 图标被过滤", ".svg" not in joined)
    check("data: URI 被过滤", "data:" not in joined)
    check("脚本被过滤", "app.js" not in joined)
    check("prv 预览图被丢弃、同图只留 dft",
          any("!nd_dft_" in u for u in imgs) and "!nd_prv_" not in joined, str(imgs))
    check("http/https 同图去重", sum(1 for u in imgs if u.endswith("/real.png")) == 1,
          str(imgs))
    check("相对路径解析成绝对 URL", any("/relative/real2.png" in u for u in imgs))
    check("srcset 取大图（1080w）而非小图",
          any("big.png" in u for u in imgs) and "small.png" not in joined, str(imgs))
    check("候选数受 MAX_PAGE_IMAGES 限制",
          len(imgs) <= image_download.MAX_PAGE_IMAGES, str(len(imgs)))
    print("✅ 页面提取：og:image 优先 + logo/svg/data/js 过滤 + prv→dft + 协议去重 + srcset 取大图")


def test_extract_page_images_respects_max_limit():
    html = "<html><body>" + "".join(
        f'<img src="http://127.0.0.1:1/p{i}.png">' for i in range(40)) + "</body></html>"
    imgs = image_download.extract_page_images(html, "http://127.0.0.1:1/")
    check("超过上限被截断", len(imgs) == image_download.MAX_PAGE_IMAGES, str(len(imgs)))
    print(f"✅ 单页最多展开 {image_download.MAX_PAGE_IMAGES} 张（防一个链接打爆队列）")


def test_fetch_image_page_link_raises_with_candidates():
    """分享页 → PageLinkError 且带上候选（执行器据此展开子任务）。"""
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d) / "x.part"
        try:
            image_download.fetch_image(_u("/share.html"), dest)
            check("分享页必须抛 PageLinkError", False, "没有抛错")
        except image_download.PageLinkError as e:
            check("PageLinkError 带候选", len(e.images) >= 3, str(len(e.images)))
            check("候选不含静态资源",
                  all("picasso-static" not in u and ".svg" not in u for u in e.images))
        except Exception as e:  # noqa: BLE001
            check("分享页必须抛 PageLinkError", False, f"{type(e).__name__}: {e}")
        check("HTML 不落盘", not dest.exists() or dest.stat().st_size == 0)
    print("✅ 分享页抛 PageLinkError（带候选、HTML 不落盘）")


def test_fetch_page_text_rejects_private_redirect_hop():
    """curl_cffi 兜底抓页也必须在每一跳过 SSRF（公网入口 302 到内网必须被拒）。

    🔴 打桩 curl_cffi.requests.get：断言「第二跳根本没发出去」。
    只断言返回空是不够的 —— 环回那跳若返回图片，抓页函数也会因 content-type
    不含 html 而返回空，删掉 SSRF 校验照样绿（变异 M14 实测过）。
    """
    calls = []

    class FakeResp:
        def __init__(self, code, headers, body=b""):
            self.status_code = code
            self.headers = headers
            self._body = body

        def iter_content(self, chunk_size=0):
            yield self._body

    def _fake_get(url, **kw):
        calls.append(url)
        if url == "https://cdn.test/share":
            return FakeResp(302, {"Location": "http://127.0.0.1:9/secret"})
        return FakeResp(200, {"Content-Type": "text/html; charset=utf-8"},
                        b"<html><body>internal secret</body></html>")

    with _fake_dns({"cdn.test": "93.184.216.34"}), \
            patch("curl_cffi.requests.get", _fake_get):
        txt, final = image_download._fetch_page_text("https://cdn.test/share")
    check("跳环回被拒（返回空）", txt == "" and final == "", f"{txt[:40]!r} {final}")
    check("第二跳根本没发出（SSRF 拦在建请求之前）", len(calls) == 1, str(calls))
    print("✅ _fetch_page_text 逐跳 SSRF：公网入口 302 到内网 → 拒绝且不发出第二跳")


def test_fetch_image_login_wall_uses_browser_tls_fallback():
    """urllib 只拿到登录墙（无图）→ 必须走浏览器指纹兜底再抓一次。"""
    seen = {"called": False}

    def _fake_page(url, referer="", cookie="", proxy="", should_stop=None):
        seen["called"] = True
        return (_SHARE_PAGE_TMPL.format(port=_ensure_server()),
                "https://www.xiaohongshu.com/discovery/item/x")

    with patch.object(image_download, "_fetch_page_text", _fake_page):
        with tempfile.TemporaryDirectory() as d:
            try:
                image_download.fetch_image(_u("/login-wall.html"), Path(d) / "x.part")
                check("登录墙也必须抛 PageLinkError", False, "没有抛错")
            except image_download.PageLinkError as e:
                check("兜底抓页被调用", seen["called"])
                check("兜底拿到候选", len(e.images) >= 3, str(len(e.images)))
            except Exception as e:  # noqa: BLE001
                check("登录墙必须抛 PageLinkError", False, f"{type(e).__name__}: {e}")
    print("✅ 登录墙场景：urllib 无图 → 浏览器指纹兜底抓页 → 仍能拿到候选")


def test_run_image_download_expands_page_into_children():
    """父任务（分享页）→ 建 N 个子任务并提交调度 → 父任务落 completed。"""
    store, tmp = _mkstore()
    submitted = []

    class FakeScheduler:
        def submit(self, fn, *args, **kw):
            submitted.append(args[0].id)
            fn(*args, **kw)          # 就地执行，验证子任务真能下完

    try:
        t = store.create(url=_u("/share.html"), title="", platform="图片",
                         quality="原图", kind="image")
        image_download.run_image_download(t, store, None, FakeScheduler())
        kids = [x for x in store.list_all() if x.id != t.id]
        check("父任务 completed 且标题说明展开数",
              t.status == "completed" and "已展开" in (t.title or ""),
              f"{t.status} {t.title!r}")
        check("子任务全部提交调度", len(submitted) == len(kids) >= 3,
              f"submitted={len(submitted)} kids={len(kids)}")
        check("子任务继承 kind=image 与 owner",
              all(k.kind == "image" and k.owner_id == t.owner_id for k in kids))
        check("子任务全部落盘完成",
              all(k.status == "completed" and k.filepath and k.filepath.exists()
                  for k in kids),
              str([(k.status, k.error[:30]) for k in kids]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ 分享页展开：父任务 completed「已展开 N 张」+ 子任务逐个提交并落盘")


def test_run_image_download_page_without_images_platform_hint():
    """页面里没有图 → failed + 按平台给「怎么拿图片直链」的落地提示。"""
    store, tmp = _mkstore()
    try:
        t = store.create(url="https://xhslink.cn/o/abc123", title="",
                         platform="图片", quality="原图", kind="image")
        with patch.object(image_download, "_fetch_page_text", lambda *a, **k: ("", "")):
            with patch.object(image_download, "fetch_image",
                              side_effect=image_download.PageLinkError([])):
                image_download.run_image_download(t, store, None, None)
        check("无图页面落 failed", t.status == "failed", t.status)
        check("错误文案明确", "没有找到图片" in (t.error or ""), t.error)
        check("提示点名小红书并给出正确拿法",
              "小红书" in (t.hint or "") and "复制图片地址" in (t.hint or ""), t.hint)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✅ 页面无图：failed + 平台定制提示（小红书 → 怎么拿图片直链）")


def test_expanded_children_are_discoverable():
    """🔴 2026-10-12 用户报「没看懂，图片呢」：展开出的子任务必须能被前端发现。

    根因：图片子任务 1 秒内下完，前端轮询到时已是 completed，而
    syncMissingCards 只给「活跃状态」补卡片 ⇒ 服务端有图、界面一张都不显示。
    修法：父任务把 children（子任务 ID）随 to_public_dict 下发，前端据此建卡片。
    """
    store, tmp = _mkstore()
    try:
        t = store.create(url=_u("/share.html"), title="", platform="图片",
                         quality="原图", kind="image")
        image_download.run_image_download(t, store, None, InlineSched())
        kids = [x for x in store.list_all() if x.id != t.id]
        check("父任务挂了 children", len(t.children) == len(kids) >= 3,
              f"children={len(t.children)} kids={len(kids)}")
        check("children 就是那些子任务的 ID",
              set(t.children) == {k.id for k in kids})
        pub = t.to_public_dict()
        check("children 出现在公开输出里（前端拿得到）",
              isinstance(pub.get("children"), list) and pub["children"] == t.children,
              str(pub.get("children"))[:60])
        # 持久化往返：重启后父任务仍要记得它的子任务（否则刷新页面又丢）
        store.flush()          # 落盘是防抖的，重建 store 前必须显式 flush
        store2 = TaskStore(root=tmp / "root", state_dir=tmp / "state")
        back = store2.get(t.id)
        check("重启后 children 仍在", back is not None and list(back.children) == list(t.children),
              str(getattr(back, "children", None))[:60])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # 前端接线：paintTask 里必须调 ensureChildCards，且它真的建卡片
    js = pathlib.Path(_SERVER_DIR, "..", "web", "app.js").read_text(encoding="utf-8")
    check("paintTask 调用 ensureChildCards", "ensureChildCards(task);" in js)
    check("ensureChildCards 用函数声明（非 const，避免暂时性死区）",
          "function ensureChildCards(task)" in js)
    check("ensureChildCards 建卡片并跟踪",
          "createTaskCard(cid" in js and "trackTask(cid, refs" in js)
    check("建卡片前查重（幂等，不重复建）",
          'querySelector(`[data-task-id="${cid}"]`)' in js)
    print("✅ 展开的子任务可被发现：children 随父任务下发 + 持久化 + 前端建卡片接线")


class InlineSched:
    """测试用调度器：就地执行子任务（生产里是 BatchScheduler）"""

    def submit(self, fn, *args, **kw):
        fn(*args, **kw)


# --------------------------------------------------------------------------- #
# 源码棘轮（防重构把接线改回去）
# --------------------------------------------------------------------------- #
def test_router_source_wiring():
    src = open(os.path.join(_SERVER_DIR, "routers", "image.py"), encoding="utf-8").read()
    check("入口路由", "@router.post('/api/image/download')" in src)
    check("限流（与 /api/download 同口径）", "app._check_rate_limit(request)" in src)
    check("逐条配额门", "_gate(request)" in src)
    check("scheme 归一", "image_download.normalize_image_url(raw)" in src)
    check("初始 URL SSRF 校验", "app._assert_safe_url(url)" in src)
    check("建 kind=image 任务", "kind='image'" in src)
    check("提交图片执行器", "image_download.run_image_download" in src)
    check("按次计费", "use_daily('download', 1)" in src)
    check("批量上限 50", "MAX_URLS_PER_REQUEST = 50" in src
          and "max_length=MAX_URLS_PER_REQUEST" in src)
    check("配额用尽 402 + MEMBER_QUOTA", "status_code=402" in src and "MEMBER_QUOTA|" in src)
    print("✅ routers/image.py 接线棘轮：限流/配额门/SSRF/kind/执行器/计费/上限/402 九项")


def test_core_kind_routing_wiring():
    src = open(os.path.join(_SERVER_DIR, "routers", "core.py"), encoding="utf-8").read()

    def _body(fn_name):
        m = re.search(rf"\ndef {fn_name}\(.*?(?=\ndef |\n@router|\n# ---)", src, re.S)
        return m.group(0) if m else ""

    b_retry = _body("retry_task")
    check("retry_task 可定位", bool(b_retry))
    check("图片任务跳过 .part 续传判定", "_is_image" in b_retry and "resume = (not _is_image)" in b_retry)
    check("图片重试提交 run_image_download",
          "image_download.run_image_download, task, app.store" in b_retry)
    b_pause = _body("pause_task")
    check("pause_task 可定位", bool(b_pause))
    check("图片暂停入口拒绝（防卡 pausing）", "图片下载不支持暂停" in b_pause)
    b_resume = _body("resume_task")
    check("resume_task 可定位", bool(b_resume))
    check("图片继续入口兜底拒绝", "图片下载不支持暂停/继续" in b_resume)
    b_file = _body("download_file")
    check("download_file 可定位", bool(b_file))
    check("SVG 强制附件（XSS 防线）", "'.svg': 'application/octet-stream'" in b_file)
    check("图片 media type 映射", "'.png': 'image/png'" in b_file and "'.jpg': 'image/jpeg'" in b_file)
    print("✅ routers/core.py kind 分流棘轮：重试换执行器/跳过续传、暂停与继续拒绝、SVG 附件")


def test_app_registers_image_router():
    src = open(os.path.join(_SERVER_DIR, "app.py"), encoding="utf-8").read()
    check("import image router", "from routers import image as _image_rtr" in src)
    check("include image router", "app.include_router(_image_rtr.router)" in src)
    print("✅ app.py 已注册 image router（漏了这行 = 前端 404，而单测照样绿）")


def test_frontend_image_mode_wiring():
    repo = os.path.dirname(_SERVER_DIR)
    appjs = open(os.path.join(repo, "web", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(repo, "web", "index.html"), encoding="utf-8").read()
    css = open(os.path.join(repo, "web", "styles.css"), encoding="utf-8").read()

    m = re.search(r"const setDownloadMode = \(mode\) => \{.*?\n  \};", appjs, re.S)
    body = m.group(0) if m else ""
    check("setDownloadMode 可定位（正则失配即回归）", bool(body))
    check("图片模式显示合规提示", "el.imageModeNote.hidden = !_img;" in body)
    check("图片模式隐藏批量面板", "el.batchToggle.hidden = _img;" in body)
    check("占位符随模式切换",
          "URL_PLACEHOLDER_IMAGE" in body and "URL_PLACEHOLDER_VIDEO" in body)

    m2 = re.search(r"const runImageDownload = async \(raw, cookie, proxy\) => \{.*?\n  \};",
                   appjs, re.S)
    body2 = m2.group(0) if m2 else ""
    check("runImageDownload 可定位", bool(body2))
    check("走 /api/image/download", "request('/api/image/download'" in body2)
    check("按 task_ids 建卡片", "data.task_ids" in body2)
    check("MEMBER_QUOTA 引导开会员", "MEMBER_QUOTA|" in body2)
    check("skipped 有提示", "data.skipped" in body2)

    m3 = re.search(r"const handleResolve = async.*?\n  \};", appjs, re.S)
    body3 = m3.group(0) if m3 else ""
    check("handleResolve 可定位", bool(body3))
    i_img = body3.find("dlMode === 'image'")
    i_raw = body3.find("if (!raw)")
    check("图片模式分支存在于 handleResolve", i_img >= 0)
    check("图片分支早于空输入检查（否则图片模式空粘贴弹「请输入视频链接」）",
          0 <= i_img < i_raw, f"i_img={i_img} i_raw={i_raw}")

    check("图片任务隐藏暂停按钮",
          "refs.togglePause.hidden = task.kind === 'image' ||" in appjs)
    check("图片任务隐藏格式转换入口",
          "refs.convertWrap.hidden = task.kind === 'image';" in appjs)
    check("图片模式按钮接线",
          "el.modeImageBtn.addEventListener('click', () => setDownloadMode('image'));" in appjs)
    check("视频模式按钮接线",
          "el.modeVideoBtn.addEventListener('click', () => setDownloadMode('video'));" in appjs)

    check("模式切换容器", 'class="mode-switch"' in html)
    check("视频/图片两个 tab", 'id="modeVideoBtn"' in html and 'id="modeImageBtn"' in html)
    check("图片合规提示节点（默认隐藏）", 'id="imageModeNote"' in html and "hidden" in html)
    check("mode-switch 样式", ".mode-switch {" in css)
    check("激活态样式", ".mode-btn.is-active" in css)
    check("提示文案样式", ".image-mode-note" in css)
    print("✅ 前端 mode-switch 接线棘轮：函数体关键行 + handleResolve 分支顺序 + HTML/CSS 三件套")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main():
    setup_module()
    tests = [
        test_sniff_image_ext_covers_magic_table,
        test_sniff_image_ext_content_type_fallback,
        test_normalize_image_url_scheme_guard,
        test_assert_public_url_rejects_private_targets,
        test_assert_public_url_allows_public,
        test_assert_public_url_dns_failure_is_fail_closed,
        test_safe_redirect_handler_blocks_private_hop,
        test_safe_redirect_handler_allows_public_hop,
        test_build_opener_wires_safe_redirect_handler,
        test_fetch_image_success_png,
        test_fetch_image_rejects_non_image_before_writing,
        test_fetch_image_blocks_redirect_to_private,
        test_fetch_image_follows_redirect_to_public,
        test_fetch_image_enforces_content_length_limit,
        test_fetch_image_enforces_stream_limit,
        test_fetch_image_stops_on_should_stop,
        test_fetch_image_edge_bodies_and_headers,
        test_derive_filename_priority_and_sanitize,
        test_derive_filename_dedup,
        test_run_image_download_success,
        test_run_image_download_failure_paths,
        test_run_image_download_stall_watchdog,
        test_task_kind_field_roundtrip,
        test_human_bytes,
        test_image_route_mounted_and_validates,
        test_image_download_ssrf_guard_over_http,
        test_image_download_end_to_end_with_file_media_type,
        test_image_download_svg_served_as_attachment_only,
        test_image_download_pause_rejected_and_cancel_works,
        test_image_download_retry_routes_to_image_executor,
        test_image_download_mixed_batch_skips_bad_links,
        test_browser_headers_consistent_and_compressed,
        test_fetch_image_decompresses_gzip_and_deflate,
        test_extract_page_images_priority_and_filtering,
        test_extract_page_images_respects_max_limit,
        test_fetch_image_page_link_raises_with_candidates,
        test_fetch_page_text_rejects_private_redirect_hop,
        test_fetch_image_login_wall_uses_browser_tls_fallback,
        test_run_image_download_expands_page_into_children,
        test_run_image_download_page_without_images_platform_hint,
        test_expanded_children_are_discoverable,
        test_router_source_wiring,
        test_core_kind_routing_wiring,
        test_app_registers_image_router,
        test_frontend_image_mode_wiring,
    ]
    for fn in tests:
        fn()
    teardown_module()
    print(f"\n🎉 图片下载回归测试全部通过（{len(tests)} 项）")


if __name__ == "__main__":
    main()
