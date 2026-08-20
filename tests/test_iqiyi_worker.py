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


def test_iqiyi_info_fallback_when_worker_unconfigured(monkeypatch):
    """worker 未配置（本地桌面）且非分享页 → 返回 None 让 probe 回退 yt-dlp。"""

    def fake_call(platform, url):
        raise ResolveError("视频解析服务未配置", "请配置 VDL_COOKIE_REFILL_URL / VDL_COOKIE_REFILL_TOKEN")

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    info = dl._iqiyi_info("https://www.iqiyi.com/v_19rr9mcb2g.html")
    assert info is None


def test_iqiyi_info_share_requires_worker(monkeypatch):
    """分享页必须 Playwright：worker 未配置也直接抛错，回退 yt-dlp 无意义。"""

    def fake_call(platform, url):
        raise ResolveError("视频解析服务未配置", "请配置 VDL_COOKIE_REFILL_URL")

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    try:
        dl._iqiyi_info("https://www.iqiyi.com/playShare.html?shareId=abc")
        raise AssertionError("应抛 ResolveError")
    except ResolveError as e:
        assert "未配置" in e.message


def test_iqiyi_info_worker_real_error_raises(monkeypatch):
    """worker 已配置但真实失败（不可达/返回错误）→ 直接抛错，不回退。"""

    def fake_call(platform, url):
        raise ResolveError("视频解析失败", "爱奇艺流获取失败: A00001")

    monkeypatch.setattr(dl, "_call_vps_worker", fake_call)
    try:
        dl._iqiyi_info("https://www.iqiyi.com/playShare.html?shareId=abc")
        raise AssertionError("应抛 ResolveError")
    except ResolveError as e:
        assert "视频解析失败" in e.message