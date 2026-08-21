"""YouTube 自动降级解析（方法一免 Cookie + PO Token → 方法二 Cookie 源自动切换）。

回归背景（2026-08-22）：YouTube 2025 起对数据中心 IP 强制 bot 检测，所有
player_client 轮换无效，需登录态 Cookie。用户要求"完全自动"：方法一解析不了时
自动替换方法二，无需手动操作。

实现：_resolve_youtube 先无 Cookie 尝试（bgutil PO Token 尽力）；被 bot 拦截
（Sign in to confirm you're not a bot）时，自动按序尝试 Cookie 源
（用户显式 > 环境变量 VDL_YOUTUBE_COOKIE > 本机缓存 > 公共池）；全部失败抛
cookie_required。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

import pytest

import downloader as dl
from yt_dlp.utils import DownloadError


BOT_MSG = "ERROR: [youtube] eVAx63QSgc4: Sign in to confirm you're not a bot"
OK_INFO = {"id": "eVAx63QSgc4", "title": "Test Video", "duration": 120,
           "formats": [{"url": "https://x/v.mp4", "format_id": "f1", "ext": "mp4"}]}


class _FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        # 按 opts 里的 Cookie 决定行为：带 "GOOD_COOKIE" 成功，否则 bot 拦截
        cookie = (self.opts.get("http_headers") or {}).get("Cookie", "")
        if "GOOD_COOKIE" in cookie:
            return dict(OK_INFO)
        if "BAD_COOKIE" in cookie:
            raise DownloadError("ERROR: [youtube] abc: Please sign in to confirm you're not a bot")
        raise DownloadError(BOT_MSG)


def _patch_env(monkeypatch):
    monkeypatch.setattr(dl, "_host_of", lambda u: "youtu.be")
    monkeypatch.setattr(dl, "_resolve_proxy", lambda *a, **kw: "")
    monkeypatch.setattr(dl, "is_china_host", lambda *a, **kw: False)
    monkeypatch.setattr(dl, "_YoutubeDL", _FakeYDL)
    monkeypatch.setattr(
        dl,
        "_base_options",
        lambda *a, **kw: {
            "proxy": "",
            "http_headers": {"Cookie": kw.get("cookie", "")} if kw.get("cookie") else {},
            "format": None,
            "extractor_args": {},
        },
    )
    # 清空 cookie 源，避免测试间相互污染
    monkeypatch.delenv("VDL_YOUTUBE_COOKIE", raising=False)
    try:
        import cookie_cache
        monkeypatch.setattr(cookie_cache, "get_cached_cookie_header", lambda *a: "")
    except Exception:
        pass
    try:
        import cookie_pool
        monkeypatch.setattr(cookie_pool, "get_cookie", lambda *a: "")
    except Exception:
        pass


def test_youtube_method1_success_no_cookie(monkeypatch):
    """方法一：无 Cookie 直接成功（bgutil PO Token 生效场景）。"""
    _patch_env(monkeypatch)

    class _OKYDL(_FakeYDL):
        def extract_info(self, url, download=False):
            return dict(OK_INFO)

    monkeypatch.setattr(dl, "_YoutubeDL", _OKYDL)
    info = dl._resolve_youtube("https://youtu.be/eVAx63QSgc4")
    assert info["title"] == "Test Video"


def test_youtube_autofallback_cache_cookie(monkeypatch):
    """方法一被 bot 拦 → 自动从本机缓存取 Cookie 重试成功。"""
    _patch_env(monkeypatch)
    import cookie_cache
    monkeypatch.setattr(cookie_cache, "get_cached_cookie_header", lambda host: "GOOD_COOKIE=abc")

    info = dl._resolve_youtube("https://youtu.be/eVAx63QSgc4")
    assert info["title"] == "Test Video"


def test_youtube_autofallback_env_cookie(monkeypatch):
    """方法一被 bot 拦 → 自动用环境变量 VDL_YOUTUBE_COOKIE 重试成功。"""
    _patch_env(monkeypatch)
    monkeypatch.setenv("VDL_YOUTUBE_COOKIE", "GOOD_COOKIE=env")
    info = dl._resolve_youtube("https://youtu.be/eVAx63QSgc4")
    assert info["title"] == "Test Video"


def test_youtube_user_cookie_priority(monkeypatch):
    """用户显式 Cookie 优先于环境变量/缓存。"""
    _patch_env(monkeypatch)
    monkeypatch.setenv("VDL_YOUTUBE_COOKIE", "GOOD_COOKIE=env")  # env 也是好 Cookie
    # user cookie 是 BAD_COOKIE（会被 bot 拦）→ 应继续尝试 env
    info = dl._resolve_youtube("https://youtu.be/eVAx63QSgc4", user_cookie="BAD_COOKIE=x")
    # user(坏) → env(好) 成功
    assert info["title"] == "Test Video"


def test_youtube_all_cookie_fail_raises_cookie_required(monkeypatch):
    """所有 Cookie 源都是坏 Cookie → 抛 cookie_required 提示。"""
    _patch_env(monkeypatch)
    import cookie_cache
    monkeypatch.setattr(cookie_cache, "get_cached_cookie_header", lambda host: "BAD_COOKIE=abc")

    from downloader import ResolveError
    try:
        dl._resolve_youtube("https://youtu.be/eVAx63QSgc4")
        raise AssertionError("应抛 ResolveError")
    except ResolveError as e:
        assert e.category == "cookie_required"
        assert "Cookie" in e.message


def test_youtube_no_cookie_sources(monkeypatch):
    """没有任何 Cookie 源 → 抛 cookie_required，提示贴 Cookie 一次即缓存。"""
    _patch_env(monkeypatch)
    from downloader import ResolveError
    try:
        dl._resolve_youtube("https://youtu.be/eVAx63QSgc4")
        raise AssertionError("应抛 ResolveError")
    except ResolveError as e:
        assert e.category == "cookie_required"
        assert "VDL_YOUTUBE_COOKIE" in (e.hint or "")
