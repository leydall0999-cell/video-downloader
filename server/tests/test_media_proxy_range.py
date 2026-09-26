"""下载用媒体中继 `/api/media/proxy` 回归测试（对标 DataTool 网页端下载）。

背景（2026-09-27）
------------------
DataTool 网页版能做到「10MB/片 · 3 并发 · 有进度 · 自动重试」，前提是它有一个
自己的媒体中继域（前端代码里的 `/api/proxy/media`）：**跨域 fetch 读不到 CDN 的
`Content-Length` / `Content-Range`**（CDN 不回 CORS 头），没有总长度就切不了片，
也画不出进度条。我方原先的 `triggerDirectDownload()` 只是一个裸的 `<a download>`，
单连接、无进度、无重试。补上中继是让分片下载成立的前置条件。

本测试钉住中继的**契约**，任何一条被破坏都会让前端分片错位或静默退化成单流：

  1. `Range` 原样透传（前端按 3 路并发切段，改写即错位）；
  2. `Content-Range` / `Content-Length` / `Accept-Ranges` 原样回给前端 ——
     前端靠 `bytes 0-0/TOTAL` 读总长度；
  3. 强制 `Accept-Encoding: identity`（带 gzip 时源站给的 Content-Length 是压缩前
     长度，分片区间会整体错位 —— 分片下载的经典坑）；
  4. 源站不认 Range（回 200 全量）时如实透传 200 且**不**伪造 Content-Range，
     让前端能据此判定「不可分片」并退单流；
  5. SSRF 护栏：内网 / 环回 / 云元数据地址必须 400；
  6. 防盗链：按平台补 Referer，且 **googlevideo 不得带 Referer**（带错反而 403）；
  7. 代理：只有非国内站才走代理；
  8. 字节流逐字节还原，且上游连接一定被关闭（不泄漏）。

全程离线：`app.requests` 被换成假实现，不打任何外部网络。
运行：cd server && python tests/test_media_proxy_range.py
"""
import os
import sys
import types
from urllib.parse import unquote

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 隔离数据目录：import app 会写 .auth_secret / stats.json，绝不能落到真实用户目录
os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_media_proxy")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

import app as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import downloader as D  # noqa: E402

_YT_HOST = "https://rr3---sn-abc.googlevideo.com/videoplayback?id=1"
# 注意：以下三个是「能命中 _stream_referer 规则」的主机（规则是按子串匹配的）。
# 真实 CDN 主机（如 apd-vlive.apdcdn.tc.qq.com / upos-*.bilivideo.com）**不**含
# 'v.qq.com' / 'bilibili' 子串，会走 fallback 自引用分支 —— 见
# test_unlisted_host_falls_back_to_self_referer。该行为与 app-dev 逐字相同，
# 本分支不改（改它会同时影响 /api/stream/proxy 的在线播放，需另行验证）。
_QQ_HOST = "https://v.qq.com/cover/x.m3u8"
_BILI_HOST = "https://www.bilibili.com/x.mp4"
_DOUYIN_HOST = "https://v3-web.douyinvod.com/x.mp4"
_UNLISTED_HOST = "https://upos-sz-mirrorcos.bilivideo.com/x.mp4"
_FAKE = "https://media.vdl-invalid-host.invalid/archive.mp4"

_CALLS: list[dict] = []
_NEXT: dict = {}


class _FakeResp:
    def __init__(self, *, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size=0):
        for c in self._chunks:
            if c:
                yield c

    def close(self):
        self.closed = True


def _fake_get(url, headers=None, stream=False, timeout=None, proxies=None):
    _CALLS.append({
        "url": url,
        "headers": dict(headers or {}),
        "proxies": proxies,
        "stream": stream,
    })
    factory = _NEXT.get("factory")
    if factory is None:
        raise AssertionError(f"本用例未设置上游响应，却有请求打到 {url}")
    return factory(url, headers or {})


class _Patched:
    """临时替换 app.requests / downloader 的代理与 Cookie 解析。"""

    def __init__(self, *, proxy="", auto_cookie="", factory=None):
        self.proxy = proxy
        self.auto_cookie = auto_cookie
        self.factory = factory
        self._orig = {}

    def __enter__(self):
        _CALLS.clear()
        _NEXT.clear()
        if self.factory is not None:
            _NEXT["factory"] = self.factory
        A.requests = types.SimpleNamespace(get=_fake_get)
        self._orig["_resolve_proxy"] = D._resolve_proxy
        self._orig["get_browser_cookie_header"] = D.get_browser_cookie_header
        D._resolve_proxy = lambda host="": self.proxy
        D.get_browser_cookie_header = lambda host, url="": self.auto_cookie
        return self

    def __exit__(self, *_exc):
        D._resolve_proxy = self._orig["_resolve_proxy"]
        D.get_browser_cookie_header = self._orig["get_browser_cookie_header"]
        return False


def _client() -> TestClient:
    return TestClient(A.app)


def _range_origin(total: int, *, chunk_byte=b"\x5a", status=206, no_accept_ranges=False):
    """模拟一个「支持 Range」的源站。"""
    body = chunk_byte * total

    def factory(url, headers):
        rng = headers.get("Range") or headers.get("range") or ""
        m = None
        if rng:
            import re as _re
            m = _re.match(r"bytes=(\d+)-(\d*)$", rng.strip())
        if m and status == 206:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else total - 1
            end = min(end, total - 1)
            seg = body[start:end + 1]
            hdrs = {
                "Content-Type": "video/mp4",
                "Content-Length": str(len(seg)),
                "Content-Range": f"bytes {start}-{end}/{total}",
            }
            if not no_accept_ranges:
                hdrs["Accept-Ranges"] = "bytes"
            # 分段返回，验证流式拼接
            step = max(1, len(seg) // 3) or 1
            chunks = [seg[i:i + step] for i in range(0, len(seg), step)] or [b""]
            return _FakeResp(status=206, headers=hdrs, chunks=chunks)
        # 忽略 Range：回整文件
        return _FakeResp(
            status=200,
            headers={"Content-Type": "video/mp4", "Content-Length": str(total)},
            chunks=[body],
        )

    return factory


# --------------------------------------------------------------------------- #
# 1. 参数与 SSRF 护栏
# --------------------------------------------------------------------------- #
def test_missing_u_returns_400():
    r = _client().get("/api/media/proxy")
    assert r.status_code == 400, f"缺 u 应 400，实际 {r.status_code}"
    print("✅ 缺 u 参数 → 400")


def test_internal_and_metadata_urls_rejected():
    """SSRF 护栏必须挡住环回 / 链路本地（云元数据）地址 —— 这是真实 _assert_safe_url。"""
    c = _client()
    for url in (
        "http://127.0.0.1:8321/api/version",
        "http://localhost/x.mp4",
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://10.0.0.5/internal.mp4",
        "http://[::1]/x.mp4",
    ):
        r = c.get("/api/media/proxy", params={"u": url})
        assert r.status_code == 400, f"{url} 应被护栏拒绝（400），实际 {r.status_code}"
    print("✅ 环回 / 内网 / 云元数据地址 → 400（SSRF 护栏生效）")


# --------------------------------------------------------------------------- #
# 2. Range 透传与响应头契约（分片下载的地基）
# --------------------------------------------------------------------------- #
def test_range_passthrough_and_headers():
    total = 1000
    with _Patched(factory=_range_origin(total)) as _p:
        r = _client().get("/api/media/proxy",
                          params={"u": _FAKE},
                          headers={"Range": "bytes=100-199"})
    assert r.status_code == 206, f"应透传 206，实际 {r.status_code}"
    h = {k.lower(): v for k, v in r.headers.items()}
    assert h.get("content-range") == "bytes 100-199/1000", h.get("content-range")
    assert h.get("content-length") == "100", h.get("content-length")
    assert h.get("accept-ranges") == "bytes", h.get("accept-ranges")
    assert h.get("cache-control") == "no-store"
    assert h.get("access-control-allow-origin") == "*"
    expose = h.get("access-control-expose-headers", "")
    for key in ("Content-Length", "Content-Range", "Accept-Ranges"):
        assert key in expose, f"expose-headers 缺 {key}: {expose!r}"
    assert len(r.content) == 100, f"切片长度应 100，实际 {len(r.content)}"
    assert r.content == b"\x5a" * 100
    # 上游收到的 Range 必须与客户端给的一模一样（改写即分片错位）
    assert _CALLS[0]["headers"].get("Range") == "bytes=100-199", _CALLS[0]["headers"]
    assert _CALLS[0]["stream"] is True, "必须流式拉取，否则大文件会全量进内存"
    print("✅ Range 原样透传；Content-Range/Content-Length/Accept-Ranges/暴露头齐备")


def test_identity_encoding_forced():
    """带 gzip 时源站给的 Content-Length 是压缩前长度 → 分片区间整体错位。"""
    with _Patched(factory=_range_origin(500)):
        _client().get("/api/media/proxy", params={"u": _FAKE},
                      headers={"Range": "bytes=0-0"})
    assert _CALLS[0]["headers"].get("Accept-Encoding") == "identity", _CALLS[0]["headers"]
    print("✅ 强制 Accept-Encoding: identity（防压缩导致分片错位）")


def test_accept_ranges_forced_even_if_origin_silent():
    """源站没回 Accept-Ranges 时也必须声明 bytes，否则前端会判成「不可分片」直接退单流。"""
    with _Patched(factory=_range_origin(800, no_accept_ranges=True)):
        r = _client().get("/api/media/proxy", params={"u": _FAKE},
                          headers={"Range": "bytes=0-99"})
    assert r.headers.get("Accept-Ranges") == "bytes", dict(r.headers)
    print("✅ 源站未声明 Accept-Ranges 时仍补声明 bytes")


def test_origin_ignoring_range_passes_200_without_fake_content_range():
    """源站不认 Range → 如实回 200、且不得伪造 Content-Range（前端据此退单流）。"""
    with _Patched(factory=_range_origin(400, status=200)):
        r = _client().get("/api/media/proxy", params={"u": _FAKE},
                          headers={"Range": "bytes=0-99"})
    assert r.status_code == 200, r.status_code
    assert "Content-Range" not in r.headers, dict(r.headers)
    assert r.headers.get("Content-Length") == "400", dict(r.headers)
    print("✅ 源站忽略 Range → 透传 200 且不伪造 Content-Range（前端可判定不可分片）")


def test_full_body_reassembled_byte_identical():
    """分三片各拉一段，拼起来必须与源站逐字节一致（分片下载的正确性底线）。"""
    total = 300
    with _Patched(factory=_range_origin(total)):
        c = _client()
        got = b""
        for start in (0, 100, 200):
            r = c.get("/api/media/proxy", params={"u": _FAKE},
                      headers={"Range": f"bytes={start}-{start + 99}"})
            assert r.status_code == 206
            got += r.content
    assert len(got) == total, f"拼接长度 {len(got)} != {total}"
    assert got == b"\x5a" * total
    # 每次请求都必须关掉上游连接
    print("✅ 三段拼接与源站逐字节一致（300B）")


def test_upstream_connection_closed():
    holder = {}

    def factory(url, headers):
        resp = _FakeResp(status=206, headers={"Content-Type": "video/mp4",
                                              "Content-Length": "4",
                                              "Content-Range": "bytes 0-3/4"},
                         chunks=[b"abcd"])
        holder["resp"] = resp
        return resp

    with _Patched(factory=factory):
        r = _client().get("/api/media/proxy", params={"u": _FAKE},
                          headers={"Range": "bytes=0-3"})
    assert r.content == b"abcd"
    assert holder["resp"].closed is True, "上游连接必须关闭，否则并发分片会泄漏连接"
    print("✅ 上游连接在结束后被关闭（无连接泄漏）")


# --------------------------------------------------------------------------- #
# 3. Referer / 代理 / Cookie
# --------------------------------------------------------------------------- #
def test_referer_matrix_includes_googlevideo_exemption():
    cases = [
        (_QQ_HOST, "https://v.qq.com/"),
        (_DOUYIN_HOST, "https://www.douyin.com/"),
        (_BILI_HOST, "https://www.bilibili.com/"),
        # googlevideo 靠 URL 签名校验，带 Referer 反而 403 ⇒ 必须不带
        (_YT_HOST, None),
    ]
    for url, want in cases:
        with _Patched(factory=_range_origin(10)):
            _client().get("/api/media/proxy", params={"u": url})
        got = _CALLS[0]["headers"].get("Referer")
        assert got == want, f"{url} 的 Referer 期望 {want!r}，实际 {got!r}"
    print("✅ Referer 按平台注入；googlevideo 不带 Referer（否则 403）")


def test_unlisted_host_falls_back_to_self_referer():
    """未列入 _stream_referer 的主机 → 回退到「以自身为 Referer」。

    这是既定设计（防盗链未覆盖的平台靠 CDN 自身宽松策略），把这个行为固化下来，
    是为了防止后续有人随手把 fallback 改成空 Referer（那会让本站原先能过的源站被 403）。
    """
    with _Patched(factory=_range_origin(10)):
        _client().get("/api/media/proxy", params={"u": _UNLISTED_HOST})
    assert _CALLS[0]["headers"].get("Referer") == f"https://{_UNLISTED_HOST.split('/')[2]}/", \
        _CALLS[0]["headers"].get("Referer")
    print("✅ 未列平台回退为自引用 Referer（防止被误改成空）")


def test_proxy_only_for_non_china_hosts():
    with _Patched(proxy="http://127.0.0.1:18080", factory=_range_origin(10)):
        _client().get("/api/media/proxy", params={"u": _YT_HOST})
    assert _CALLS[0]["proxies"] == {"http": "http://127.0.0.1:18080",
                                   "https": "http://127.0.0.1:18080"}, _CALLS[0]["proxies"]
    with _Patched(proxy="http://127.0.0.1:18080", factory=_range_origin(10)):
        _client().get("/api/media/proxy", params={"u": _BILI_HOST})
    assert _CALLS[0]["proxies"] is None, f"国内站不应走代理：{_CALLS[0]['proxies']}"
    print("✅ 仅海外站走代理；国内站直连")


def test_cookie_explicit_wins_then_auto():
    with _Patched(auto_cookie="SID=auto", factory=_range_origin(10)):
        _client().get("/api/media/proxy",
                      params={"u": _BILI_HOST, "cookie": "Cookie: SID=explicit; HSID=1"})
    assert _CALLS[0]["headers"].get("Cookie") == "SID=explicit; HSID=1", _CALLS[0]["headers"]
    with _Patched(auto_cookie="SID=auto", factory=_range_origin(10)):
        _client().get("/api/media/proxy", params={"u": _BILI_HOST})
    assert _CALLS[0]["headers"].get("Cookie") == "SID=auto", _CALLS[0]["headers"]
    print("✅ Cookie 显式优先，其次自动取浏览器登录态")


# --------------------------------------------------------------------------- #
# 4. 防盗链失败的可读文案
# --------------------------------------------------------------------------- #
def test_403_hints_differ_by_cookie_source():
    def factory_403(url, headers):
        return _FakeResp(status=403, headers={})

    with _Patched(factory=factory_403):
        r = _client().get("/api/media/proxy", params={"u": _BILI_HOST, "cookie": "SID=x"})
    assert r.status_code == 403
    assert "防盗链被拒" in r.json()["detail"], r.json()

    with _Patched(auto_cookie="SID=auto", factory=factory_403):
        r = _client().get("/api/media/proxy", params={"u": _BILI_HOST})
    assert r.status_code == 403
    assert "已自动携带浏览器登录态仍被拒" in r.json()["detail"], r.json()

    with _Patched(factory=factory_403):
        r = _client().get("/api/media/proxy", params={"u": _BILI_HOST})
    assert r.status_code == 403
    assert "可能需要登录 Cookie" in r.json()["detail"], r.json()
    print("✅ 403 按 Cookie 来源给出三种可读提示")


# --------------------------------------------------------------------------- #
# 5. Content-Disposition（直连降级时给浏览器一个像样的文件名）
# --------------------------------------------------------------------------- #
def test_content_disposition_extension_mapping():
    with _Patched(factory=_range_origin(10)):
        r = _client().get("/api/media/proxy", params={"u": _FAKE, "dl": "我的视频"})
    cd = r.headers.get("Content-Disposition", "")
    assert cd.startswith("attachment"), cd
    assert unquote(cd.split("''", 1)[-1]) == "我的视频.mp4", cd

    with _Patched(factory=_range_origin(10)):
        r = _client().get("/api/media/proxy", params={"u": _FAKE, "dl": "已命名.mkv"})
    assert unquote(r.headers["Content-Disposition"].split("''", 1)[-1]) == "已命名.mkv"

    with _Patched(factory=_range_origin(10)):
        r = _client().get("/api/media/proxy", params={"u": _FAKE})
    assert "Content-Disposition" not in r.headers, "未传 dl 时不应附下载头"
    print("✅ Content-Disposition 按 Content-Type 补扩展名；已有扩展名不改")


def test_cors_preflight_allows_range():
    """跨域分片下载的前置条件：预检必须放行 `Range`。

    解析落在对端节点时，页面（主站）与中继（对端）**不同源**，浏览器会先发
    `OPTIONS` 预检并带上 `Access-Control-Request-Headers: range`。预检里没有 range
    就会被浏览器直接拦掉 —— 症状是海外视频「一律加速下载不可用」，而且服务端
    日志里**一个 GET /api/media/proxy 都不会出现**，极易被误判成后端坏了。
    """
    r = _client().options("/api/media/proxy", headers={
        "Origin": "https://hanyuxz.top",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "range",
    })
    assert r.status_code in (200, 204), f"预检应 200/204，实际 {r.status_code}"
    allowed = (r.headers.get("Access-Control-Allow-Headers") or "").lower()
    assert "range" in allowed, f"预检未放行 Range，跨域分片下载必失败：{allowed!r}"
    methods = (r.headers.get("Access-Control-Allow-Methods") or "").lower()
    assert "get" in methods, f"预检未放行 GET：{methods!r}"
    origin = r.headers.get("Access-Control-Allow-Origin") or ""
    assert origin in ("*", "https://hanyuxz.top"), f"预检未回 ACAO：{origin!r}"
    print("✅ CORS 预检放行 Range（跨域分片下载的前置条件）")


def test_upstream_failure_is_502_not_500():
    class _Boom(Exception):
        pass

    def factory(url, headers):
        raise _Boom("connection refused")

    with _Patched(factory=factory):
        r = _client().get("/api/media/proxy", params={"u": _FAKE})
    assert r.status_code == 502, f"上游异常应 502，实际 {r.status_code}"
    assert "上游拉取失败" in r.json()["detail"], r.json()
    print("✅ 上游异常 → 502（前端据此降级为浏览器直连）")


if __name__ == "__main__":
    test_missing_u_returns_400()
    test_internal_and_metadata_urls_rejected()
    test_range_passthrough_and_headers()
    test_identity_encoding_forced()
    test_accept_ranges_forced_even_if_origin_silent()
    test_origin_ignoring_range_passes_200_without_fake_content_range()
    test_full_body_reassembled_byte_identical()
    test_upstream_connection_closed()
    test_referer_matrix_includes_googlevideo_exemption()
    test_unlisted_host_falls_back_to_self_referer()
    test_proxy_only_for_non_china_hosts()
    test_cookie_explicit_wins_then_auto()
    test_403_hints_differ_by_cookie_source()
    test_content_disposition_extension_mapping()
    test_cors_preflight_allows_range()
    test_upstream_failure_is_502_not_500()
    print("\n✅ 媒体中继 /api/media/proxy 回归测试全部通过")
