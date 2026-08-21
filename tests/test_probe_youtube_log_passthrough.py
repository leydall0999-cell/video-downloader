"""probe() 应透传 yt-dlp logger 真实错误，而非用「未获取到视频信息」占位提示误导用户。

回归背景（2026-08-21）：用户反馈 YouTube 链接解析失败，错误信息是笼统的
「未获取到视频信息 / extract_info 返回空结果（无异常）」，但 yt-dlp 真实原因是
"This video is unavailable"。原因：probe() 对 YouTube 设了 ignoreerrors="only_download"，
yt-dlp 在该模式下不抛错只 logger.error；原代码在 `if not info:` 时只看 _last_err，
不读 yt-dlp 日志，导致真实错误被遮蔽。

修复：probe() 在 yt-dlp 调用前后捕获 WARNING/ERROR 日志，`if not info:` 时优先
透传 yt-dlp 真实错误。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

import logging

import pytest

import downloader as dl


class _FakeYDL:
    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        # 模拟 yt-dlp 在 ignoreerrors 模式下不抛错，但 logger.error
        logging.getLogger("yt_dlp").error(
            "[youtube] eIvAx63QSgc: This video is unavailable"
        )
        return None


class _SilentYDL(_FakeYDL):
    def extract_info(self, url, download=False):
        return None


@pytest.fixture
def patch_youtube_url(monkeypatch):
    """YouTube host + ignoreerrors="only_download"，让 yt-dlp 走 ignoreerrors 路径。"""
    monkeypatch.setattr(dl, "_host_of", lambda u: "youtu.be")
    monkeypatch.setattr(dl, "_YoutubeDL", _FakeYDL)
    monkeypatch.setattr(
        dl,
        "_base_options",
        lambda *a, **kw: {
            "proxy": "",
            "http_headers": {},
            "format": None,
            "extractor_args": {},
            "ignoreerrors": "only_download",
        },
    )
    monkeypatch.setattr(dl, "_resolve_proxy", lambda *a, **kw: "")
    monkeypatch.setattr(dl, "is_china_host", lambda *a, **kw: False)
    monkeypatch.setattr(dl, "_looks_like_direct_file", lambda *a, **kw: False)


def test_probe_unavailable_youtube_passes_yt_dlp_log(patch_youtube_url):
    """yt-dlp 不抛错但 logger.error 时，probe() 应透传真实错误给用户。"""
    from downloader import probe, ResolveError

    try:
        probe("https://youtu.be/eIvAx63QSgc")
    except ResolveError as e:
        assert "This video is unavailable" in (e.hint or ""), (
            f"应透传 yt-dlp 真实错误，实际：{(e.hint or '')[:300]}"
        )
        assert e.message == "未获取到视频信息"
    else:
        raise AssertionError("应抛 ResolveError")


def test_probe_no_yt_dlp_log_uses_placeholder(patch_youtube_url, monkeypatch):
    """yt-dlp 既没抛错也没 logger：保留占位提示（兼容极端情况）。"""
    monkeypatch.setattr(dl, "_YoutubeDL", _SilentYDL)

    from downloader import probe, ResolveError

    try:
        probe("https://youtu.be/eIvAx63QSgc")
    except ResolveError as e:
        hint = e.hint or ""
        assert "extract_info 返回空结果" in hint or "常见原因" in hint
    else:
        raise AssertionError("应抛 ResolveError")


def test_probe_logger_handler_cleanup(patch_youtube_url):
    """每次 probe() 调用后应清理 logger handler，不污染后续请求。"""
    from downloader import probe

    ydl_logger = logging.getLogger("yt_dlp")
    n_before = sum(1 for h in ydl_logger.handlers if type(h).__name__ == "_YdlLogCapture")

    try:
        probe("https://youtu.be/abc")
    except Exception:
        pass

    n_after = sum(
        1 for h in ydl_logger.handlers if type(h).__name__ == "_YdlLogCapture"
    )
    assert n_after == n_before, f"handler 泄漏：before={n_before} after={n_after}"