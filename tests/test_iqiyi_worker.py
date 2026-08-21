"""爱奇艺 VPS Playwright worker 接入测试（不依赖真实网络/浏览器）。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import downloader as dl  # noqa: E402
from downloader import ResolveError  # noqa: E402


def test_is_iqiyi_host():
    assert dl._is_iqiyi_host("iqiyi.com")
    assert dl._is_iqiyi_host("www.iqiyi.com")
    assert dl._is_iqiyi_host("iq.com")
    assert dl._is_iqiyi_host("www.iq.com")
    assert not dl._is_iqiyi_host("youku.com")
    assert not dl._is_iqiyi_host("iqy.net")  # 停放域名不算
    assert not dl._is_iqiyi_host("")


def test_iqiyi_info_builds_m3u8_info(monkeypatch):
    """worker 返回 m3u8 直链 → info dict 带 protocol=m3u8_native + Referer。"""

    def fake_call(platform, url):
        assert platform == "iqiyi"
        return {
            "ok": True,
            "title": "测试视频",
            "duration": 120,
            "video_id": "a1b2c3d4",
            "tvid": "5041311543890700",
            "video_url": "http://cache.video.iqiyi.com/m3u8/master.m3u8",
            "quality": "5",
            "webpage_url": "https://www.iqiyi.com/v_xxx.html",
            "thumbnail": "http://pic.iqiyi.com/thumb.jpg",
            "ext": "mp4",
        }

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    info = dl._iqiyi_info("https://www.iqiyi.com/playShare.html?shareId=test")
    assert info is not None
    assert info["url"] == "http://cache.video.iqiyi.com/m3u8/master.m3u8"
    assert info["protocol"] == "m3u8_native"
    assert info["extractor"] == "iqiyi"
    assert info["title"] == "测试视频"
    assert info["id"] == "a1b2c3d4"
    assert info["http_headers"]["Referer"] == "https://www.iqiyi.com/v_xxx.html"
    assert info["http_headers"]["User-Agent"]


def test_iqiyi_info_fallback_when_worker_unconfigured_and_cookie(monkeypatch):
    """worker 未配置 + 用户已贴 Cookie + 非分享页 → 返回 None 让 probe 回退 yt-dlp。"""

    def fake_call(platform, url):
        raise ResolveError("视频解析服务未配置", "请配置 VDL_COOKIE_REFILL_URL / VDL_COOKIE_REFILL_TOKEN")

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    # 非分享页：贴 Cookie 优先走 yt-dlp，不经过 worker
    info = dl._iqiyi_info("https://www.iqiyi.com/v_19rr9mcb2g.html", cookie="P00001=xxx")
    assert info is None


def test_iqiyi_info_normal_page_no_cookie_uses_worker(monkeypatch):
    """普通播放页 v_xxx.html 无 Cookie → 优先走 worker（不报错、不回退）。"""

    def fake_call(platform, url):
        assert platform == "iqiyi"
        return {
            "ok": True,
            "title": "普通页视频",
            "video_url": "http://cache.video.iqiyi.com/m3u8/v.m3u8",
            "webpage_url": "https://www.iqiyi.com/v_19rr9mcb2g.html",
            "ext": "mp4",
        }

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    info = dl._iqiyi_info("https://www.iqiyi.com/v_19rr9mcb2g.html")
    assert info is not None
    assert info["title"] == "普通页视频"
    assert info["url"] == "http://cache.video.iqiyi.com/m3u8/v.m3u8"


def test_iqiyi_info_album_page_no_cookie_uses_worker(monkeypatch):
    """专辑详情页 a_xxx.html 无 Cookie → 同样优先走 worker。"""

    def fake_call(platform, url):
        return {
            "ok": True,
            "title": "专辑视频",
            "video_url": "http://cache.video.iqiyi.com/m3u8/a.m3u8",
            "webpage_url": "https://www.iqiyi.com/a_19rr9mcb2g.html",
            "ext": "mp4",
        }

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    info = dl._iqiyi_info("https://www.iqiyi.com/a_19rr9mcb2g.html")
    assert info is not None
    assert info["title"] == "专辑视频"


def test_iqiyi_info_worker_unconfigured_no_cookie_raises(monkeypatch):
    """worker 未配置且无 Cookie（含分享页与普通页）→ 提示启用解析服务/贴 Cookie。"""

    def fake_call(platform, url):
        raise ResolveError("视频解析服务未配置", "请配置 VDL_COOKIE_REFILL_URL")

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    for url in (
        "https://www.iqiyi.com/playShare.html?shareId=abc",
        "https://www.iqiyi.com/v_19rr9mcb2g.html",
    ):
        try:
            dl._iqiyi_info(url)
            raise AssertionError(f"应抛 ResolveError: {url}")
        except ResolveError as e:
            assert e.category == "cookie_required"
            assert "Cookie" in e.message


def test_iqiyi_info_worker_real_error_no_cookie_raises(monkeypatch):
    """worker 已配置但解析失败（VIP/失效）且无 Cookie → 直接抛原错误。"""

    def fake_call(platform, url):
        raise ResolveError("视频解析失败", "爱奇艺流获取失败: A00001")

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    try:
        dl._iqiyi_info("https://www.iqiyi.com/v_19rr9mcb2g.html")
        raise AssertionError("应抛 ResolveError")
    except ResolveError as e:
        assert "视频解析失败" in e.message


def test_iqiyi_info_worker_fails_but_cookie_falls_back(monkeypatch):
    """worker 解析失败但有 Cookie（含分享页）→ 回退 yt-dlp 直下兜底，不报错。"""

    def fake_call(platform, url):
        raise ResolveError("视频解析失败", "爱奇艺流获取失败: A00001")

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    for url in (
        "https://www.iqiyi.com/playShare.html?shareId=abc",
        "https://www.iqiyi.com/v_19rr9mcb2g.html",
    ):
        info = dl._iqiyi_info(url, cookie="P00001=xxx")
        assert info is None, f"有 Cookie 应回退 yt-dlp: {url}"


def test_iqiyi_info_playShare_with_cookie_uses_worker(monkeypatch):
    """分享页即使贴了 Cookie 也必须走 worker（yt-dlp 拿不到 playShare 的 tvid）。"""

    def fake_call(platform, url):
        return {
            "ok": True,
            "title": "分享页视频",
            "video_url": "http://cache.video.iqiyi.com/m3u8/p.m3u8",
            "webpage_url": "https://www.iqiyi.com/playShare.html?shareId=abc",
            "ext": "mp4",
        }

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    info = dl._iqiyi_info(
        "https://www.iqiyi.com/playShare.html?shareId=abc", cookie="P00001=xxx"
    )
    assert info is not None
    assert info["title"] == "分享页视频"


def test_iqiyi_info_empty_m3u8_no_cookie_raises(monkeypatch):
    """worker 返回 ok 但无 video_url 且无 Cookie → 报解析失败。"""

    def fake_call(platform, url):
        return {"ok": True, "title": "x", "video_url": ""}

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    try:
        dl._iqiyi_info("https://www.iqiyi.com/v_19rr9mcb2g.html")
        raise AssertionError("应抛 ResolveError")
    except ResolveError as e:
        assert e.category == "parse_failed"


def test_iqiyi_info_empty_m3u8_with_cookie_falls_back(monkeypatch):
    """worker 返回 ok 但无 video_url 且有 Cookie → 回退 yt-dlp。"""

    def fake_call(platform, url):
        return {"ok": True, "title": "x", "video_url": ""}

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    info = dl._iqiyi_info("https://www.iqiyi.com/v_19rr9mcb2g.html", cookie="P00001=xxx")
    assert info is None