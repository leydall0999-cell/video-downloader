"""测试 iqy.net 短链展开为 iqiyi.com 长链，以及 _normalize_share_url 接入。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from downloader import _expand_iqiyi_short_url, _normalize_share_url, _strip_tracking_params
import platforms as platforms_mod  # noqa: E402


class _FakeRequestsResponse:
    def __init__(self, status_code: int, location: str = "", url: str = "", history=None, text: str = ""):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}
        self.url = url
        self.history = history or []
        self.text = text
        self._closed = False

    def close(self):
        self._closed = True


def test_expand_iqiyi_via_head_redirects(monkeypatch):
    """HEAD allow_redirects=True 直接拿到 iqiyi.com 最终 URL，返回展开结果。"""
    calls = []

    def fake_head(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        calls.append({"url": url, "allow_redirects": allow_redirects, "proxies": proxies})
        # requests 已帮我们跟随 302，这里直接模拟跟随后的最终响应
        return _FakeRequestsResponse(200, url="https://www.iqiyi.com/v_19rr9mcb2g.html")

    monkeypatch.setattr("requests.head", fake_head)

    url = _expand_iqiyi_short_url("https://iqy.net/i/faJHDJK-79")
    assert url == "https://www.iqiyi.com/v_19rr9mcb2g.html"
    # 只发了 HEAD，没有走 GET fallback
    assert len(calls) == 1
    assert calls[0]["allow_redirects"] is True


def test_expand_iqiyi_via_get_fallback(monkeypatch):
    """HEAD 没拿到有效 Location → 回退 GET + allow_redirects=True。"""
    head_calls = []
    get_calls = []

    def fake_head(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        head_calls.append(url)
        # HEAD 返回 200 + 空 Location（CDN 丢弃 Location 场景）
        return _FakeRequestsResponse(200, "")

    def fake_get(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None, stream=None):
        get_calls.append({"url": url, "allow_redirects": allow_redirects, "stream": stream})
        # 模拟 GET 跟随 302 后的最终 URL（注意 resp.url 是最终地址）
        return _FakeRequestsResponse(
            200, url="https://www.iqiyi.com/v_xyz123.html", history=[_FakeRequestsResponse(302)]
        )

    monkeypatch.setattr("requests.head", fake_head)
    monkeypatch.setattr("requests.get", fake_get)

    url = _expand_iqiyi_short_url("https://iqy.net/i/abc123")
    assert url == "https://www.iqiyi.com/v_xyz123.html"
    assert len(head_calls) == 1
    assert len(get_calls) == 1
    assert get_calls[0]["allow_redirects"] is True
    assert get_calls[0]["stream"] is True


def test_expand_iqiyi_head_rejects_non_iqiyi_location(monkeypatch):
    """HEAD Location 不指向 iqiyi.com 时不回退信任，必须再走 GET 兜底。"""
    get_calls = []

    def fake_head(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        # Location 指向无关域：拒绝
        return _FakeRequestsResponse(302, "https://example.com/foo")

    def fake_get(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None, stream=None):
        get_calls.append(url)
        # GET 跟随后最终 URL 指向 iqiyi.com
        return _FakeRequestsResponse(
            200, url="https://www.iqiyi.com/v_real.html", history=[_FakeRequestsResponse(302)]
        )

    monkeypatch.setattr("requests.head", fake_head)
    monkeypatch.setattr("requests.get", fake_get)

    url = _expand_iqiyi_short_url("https://iqy.net/i/zzz")
    assert url == "https://www.iqiyi.com/v_real.html"
    assert len(get_calls) == 1


def test_expand_iqiyi_both_fail_returns_original(monkeypatch):
    """HEAD 与 GET 都失败时，保留原 URL（让 yt-dlp 给「找不到视频」明确错误）。"""

    def fake_head(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        raise RuntimeError("network down")

    def fake_get(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None, stream=None):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.head", fake_head)
    monkeypatch.setattr("requests.get", fake_get)

    original = "https://iqy.net/i/faJHDJK-79?vfrm=pcw_album_auto"
    url = _expand_iqiyi_short_url(original)
    assert url == original


def test_expand_iqiyi_non_iqynnet_url_no_request(monkeypatch):
    """非 iqy.net URL 不发请求，原样返回。"""
    called = {"n": 0}

    def fake_head(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        called["n"] += 1
        return _FakeRequestsResponse(200)

    monkeypatch.setattr("requests.head", fake_head)

    assert _expand_iqiyi_short_url("https://www.iqiyi.com/v_abc.html") == "https://www.iqiyi.com/v_abc.html"
    assert called["n"] == 0


def test_normalize_share_url_iqynnet(monkeypatch):
    """_normalize_share_url 对 iqy.net 链接先展开再剥离追踪参数。"""

    def fake_expand(url, *, proxy=""):
        return "https://www.iqiyi.com/v_19rr9mcb2g.html"

    monkeypatch.setattr("downloader._expand_iqiyi_short_url", fake_expand)

    out = _normalize_share_url("https://iqy.net/i/faJHDJK-79?vfrm=pcw_album_auto")
    assert out == "https://www.iqiyi.com/v_19rr9mcb2g.html"


def test_normalize_share_url_iqynnet_expand_fails_strips_params(monkeypatch):
    """iqy.net 展开失败时仍走 _strip_tracking_params（已知追踪参数被剥离）。"""

    def fake_expand(url, *, proxy=""):
        return url  # 展开失败

    monkeypatch.setattr("downloader._expand_iqiyi_short_url", fake_expand)

    out = _normalize_share_url("https://iqy.net/i/faJHDJK-79?vd_source=test")
    # vd_source 属于通用追踪参数，被剥离；非追踪参数保留
    assert out == "https://iqy.net/i/faJHDJK-79"


def test_normalize_share_url_iq_com_intl(monkeypatch):
    """iq.com (国际版) 链接走通用追踪参数净化分支，不被 iqy.net 误伤。"""
    out = _normalize_share_url("https://www.iq.com/play/abc123?vd_source=test")
    # iq.com 不在 iqy.net 分支里，应走 _strip_tracking_params 兜底（vd_source 被去掉）
    assert out == "https://www.iq.com/play/abc123"


def test_iqynnet_in_iqiyi_platform_and_china_domains():
    """platforms：iqy.net 应同时进入 iqiyi 平台域名与 CHINA_DOMAINS。"""
    iqiyi = next(p for p in platforms_mod.SUPPORTED_PLATFORMS if p.key == "iqiyi")
    assert "iqy.net" in iqiyi.domains
    assert "iqiyi.com" in iqiyi.domains
    assert "iqy.net" in platforms_mod.CHINA_DOMAINS
    # 走 _match_platform 时应命中 iqiyi 平台而非 generic
    matched = platforms_mod._match_platform("iqy.net")
    assert matched.key == "iqiyi"
    matched2 = platforms_mod._match_platform("www.iq.com")
    assert matched2.key == "iqiyi"


def test_strip_tracking_params_keeps_iqiyi_playshare_ids():
    """爱奇艺 playShare 的 shareId / positiveId 是视频标识，不能当追踪参数剥离。"""
    url = (
        "https://www.iqiyi.com/playShare.html?shareId=NDUzNTE0NDg5Mzk3NTUwMA=="
        "&positiveId=NDUzNTE0NDg5Mzk3NTUwMA==&type=0"
        "&rpage=sharepage_new&p1=2_22_222&qr_template=directshare"
        "&social_platform=link&vd_source=foo"
    )
    out = _strip_tracking_params(url, keep={"shareId", "positiveId"})
    assert "shareId=NDUzNTE0NDg5Mzk3NTUwMA%3D%3D" in out
    assert "positiveId=NDUzNTE0NDg5Mzk3NTUwMA%3D%3D" in out
    assert "vd_source" not in out
    # rpage 不是通用追踪参数，保留即可
    assert "type=0" in out


def test_normalize_share_url_iqynnet_playshare_preserves_shareid(monkeypatch):
    """iqy.net 展开成 playShare.html 后必须保留 shareId / positiveId。"""

    def fake_expand(url, *, proxy=""):
        return (
            "https://www.iqiyi.com/playShare.html?shareId=NDUzNTE0NDg5Mzk3NTUwMA=="
            "&positiveId=NDUzNTE0NDg5Mzk3NTUwMA==&type=0&vd_source=foo"
        )

    monkeypatch.setattr("downloader._expand_iqiyi_short_url", fake_expand)

    out = _normalize_share_url("https://qy.net/08JJ7ZI-53?vfrm=pcw_album_auto")
    assert "shareId=NDUzNTE0NDg5Mzk3NTUwMA%3D%3D" in out
    assert "positiveId=NDUzNTE0NDg5Mzk3NTUwMA%3D%3D" in out
    assert "vd_source" not in out

def test_normalize_direct_iqiyi_playshare_preserves_shareid():
    """直接粘贴 www.iqiyi.com/playShare.html?shareId=... 时，归一化必须保留
    shareId / positiveId，否则 VPS worker 拿到 bare playShare.html 会被爱奇艺
    跳到 error.html?errortype=2，导致抓不到 m3u8。"""
    url = (
        "https://www.iqiyi.com/playShare.html?shareId=NTA0MTMxMTU0Mzg5MDcwMA=="
        "&positiveId=NTA0MTMxMTU0Mzg5MDcwMA==&type=0"
        "&rpage=sharepage_new&p1=2_22_222&qr_template=directshare"
        "&social_platform=link&vd_source=abc"
    )
    out = _normalize_share_url(url)
    assert "shareId=NTA0MTMxMTU0Mzg5MDcwMA%3D%3D" in out
    assert "positiveId=NTA0MTMxMTU0Mzg5MDcwMA%3D%3D" in out
    # 真正的追踪参数应被剥除
    assert "vd_source" not in out
    # 必须是 playShare 完整页，而非 bare；其余分享参数（rpage/p1 等）原样保留
    assert out.startswith("https://www.iqiyi.com/playShare.html?")
