"""probe() 对 YouTube 应让 yt-dlp 真实抛 DownloadError，再由 except 分支降级 + 透传错误。

回归背景（2026-08-21）：用户反馈 YouTube 链接解析失败，错误信息是笼统的
「未获取到视频信息 / extract_info 返回空结果（无异常）」，但 yt-dlp 真实原因是
"This video is unavailable"。

根因：probe() 对 YouTube 设了 ignoreerrors="only_download"，yt-dlp 2026.07 在该
模式下对业务错误（unavailable/sign-in/format not available）既不抛 DownloadError
也不 logger.error，直接返回空 dict，原始错误被完全吞掉。

修复：移除 YouTube 的 ignoreerrors 设置，让 yt-dlp 真实抛 DownloadError；probe()
的 except DownloadError 分支会捕获并透传真实错误，同时 logger 捕获作为兜底。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

import logging

import pytest

import downloader as dl
from yt_dlp.utils import DownloadError


class _YDLRaisesDownloadError:
    """模拟 yt-dlp 抛 DownloadError（如 YouTube unavailable）。"""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        raise DownloadError("ERROR: [youtube] eIvAx63QSgc: This video is unavailable")


class _YDLReturnsEmpty:
    """模拟 yt-dlp 静默返回空（无错误）。"""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return None


class _YDLReturnsValidYouTube:
    """模拟 yt-dlp 正常解析 YouTube 视频。"""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return {
            "id": "abc",
            "title": "Test Video",
            "duration": 100,
            "formats": [
                {"url": "https://x/v.mp4", "format_id": "f1", "ext": "mp4", "height": 720},
            ],
            "webpage_url": url,
        }


def _base_patches(monkeypatch, fake_youtube_module=True):
    monkeypatch.setattr(dl, "_host_of", lambda u: "youtu.be")
    monkeypatch.setattr(dl, "_resolve_proxy", lambda *a, **kw: "")
    monkeypatch.setattr(dl, "is_china_host", lambda *a, **kw: False)
    monkeypatch.setattr(dl, "_looks_like_direct_file", lambda *a, **kw: False)
    # 默认 opts 不带 ignoreerrors（验证移除后行为）
    monkeypatch.setattr(
        dl,
        "_base_options",
        lambda *a, **kw: {
            "proxy": "",
            "http_headers": {},
            "format": None,
            "extractor_args": {},
        },
    )


def test_probe_youtube_unavailable_passes_real_error(monkeypatch):
    """yt-dlp 抛 DownloadError 时，probe() 应透传真实错误（不再用占位提示）。"""
    _base_patches(monkeypatch)
    monkeypatch.setattr(dl, "_YoutubeDL", _YDLRaisesDownloadError)

    from downloader import probe, ResolveError

    try:
        probe("https://youtu.be/eIvAx63QSgc")
    except ResolveError as e:
        assert "This video is unavailable" in (e.hint or ""), (
            f"应透传 yt-dlp 真实错误，实际：{(e.hint or '')[:300]}"
        )
    else:
        raise AssertionError("应抛 ResolveError")


def test_probe_youtube_silent_empty_falls_back_to_placeholder(monkeypatch):
    """yt-dlp 既不抛错也不 logger.error 静默返回空：用占位提示（兜底兼容）。"""
    _base_patches(monkeypatch)
    monkeypatch.setattr(dl, "_YoutubeDL", _YDLReturnsEmpty)

    from downloader import probe, ResolveError

    try:
        probe("https://youtu.be/eIvAx63QSgc")
    except ResolveError as e:
        hint = e.hint or ""
        assert "extract_info 返回空结果" in hint or "常见原因" in hint
    else:
        raise AssertionError("应抛 ResolveError")


def test_probe_youtube_logger_capture_as_safety_net(monkeypatch):
    """logger 捕获兜底：若 yt-dlp 不抛错只 logger.error，应透传日志。"""
    _base_patches(monkeypatch)

    class _YDLLogOnlyError(_YDLReturnsEmpty):
        def extract_info(self, url, download=False):
            logging.getLogger("yt_dlp").error(
                "[youtube] abc: Sign in to confirm you're not a bot"
            )
            return None

    monkeypatch.setattr(dl, "_YoutubeDL", _YDLLogOnlyError)

    from downloader import probe, ResolveError

    try:
        probe("https://youtu.be/abc")
    except ResolveError as e:
        assert "Sign in to confirm" in (e.hint or ""), (
            f"应透传 yt-dlp logger 错误，实际：{(e.hint or '')[:300]}"
        )
    else:
        raise AssertionError("应抛 ResolveError")


def test_probe_youtube_valid_video_works(monkeypatch):
    """可用 YouTube 视频应正常解析（移除 ignoreerrors 后不影响健康路径）。"""
    _base_patches(monkeypatch)
    monkeypatch.setattr(dl, "_YoutubeDL", _YDLReturnsValidYouTube)

    from downloader import probe

    info = probe("https://youtu.be/abc")
    assert info.get("title") == "Test Video"
    assert len(info.get("formats") or []) == 1


def test_probe_logger_handler_cleanup(monkeypatch):
    """每次 probe() 调用后应清理 logger handler，不污染后续请求。"""
    _base_patches(monkeypatch)
    monkeypatch.setattr(dl, "_YoutubeDL", _YDLRaisesDownloadError)

    from downloader import probe

    ydl_logger = logging.getLogger("yt_dlp")
    n_before = sum(
        1 for h in ydl_logger.handlers if type(h).__name__ == "_YdlLogCapture"
    )
    try:
        probe("https://youtu.be/abc")
    except Exception:
        pass
    n_after = sum(
        1 for h in ydl_logger.handlers if type(h).__name__ == "_YdlLogCapture"
    )
    assert n_after == n_before, f"handler 泄漏：before={n_before} after={n_after}"