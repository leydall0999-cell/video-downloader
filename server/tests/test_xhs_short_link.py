"""小红书短链展开 + Windows 浏览器 Cookie 兜底（离线单测）。

钉住三个 2026-10-25 修掉的真问题：

1. `_expand_xhs_short` 必须能从「落在 /login?redirectPath=…」的展开结果里
   解出笔记页 —— 旧实现只认终点 URL 里的 /explore/ 或 /discovery/item/，
   桌面 UA 下单短链必被误判成「无法展开」，进而给用户一句错误的
   「任何下载工具都无法解析」。
2. `_win_self_cookie_header` 读不到 Cookie 时必须返回空串（不是抛异常），
   因为上层要靠「空」来决定「不带 Cookie 继续」，而不是让整次解析崩掉。
3. `_friendly_error` 必须把 yt-dlp 的
   "Could not copy Chrome cookie database" 单独识别成「浏览器锁库」，
   而不是笼统的「视频解析失败」——这条与站点无关，会打断所有站点的解析。
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import downloader as D  # noqa: E402

NOTE_ID = "6ad1f2510000000002029240"


class _Resp:
    def __init__(self, url: str, history: list | None = None):
        self.url = url
        self.history = history or []


def test_expand_xhs_short_accepts_direct_note_url():
    """移动 UA 直达笔记页：终点 URL 直接用。"""
    note = f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=abc"
    with mock.patch("requests.get", return_value=_Resp(note)):
        got = D._expand_xhs_short("https://xhslink.cn/o/6be6c9ovTmE")
    assert got == note


def test_expand_xhs_short_unwraps_login_redirect_path():
    """桌面 UA 落在 /login?redirectPath=…：必须从参数里解出笔记页（旧实现漏在这里）。"""
    from urllib.parse import quote

    inner = f"http://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=abc"
    login = "https://www.xiaohongshu.com/login?redirectPath=" + quote(inner, safe="")
    with mock.patch("requests.get", return_value=_Resp(login)):
        got = D._expand_xhs_short("https://xhslink.com/o/6be6c9ovTmE")
    assert "/discovery/item/" + NOTE_ID in got
    assert got.startswith("https://www.xiaohongshu.com/"), "http 必须升级为 https"


def test_expand_xhs_short_uses_history_location():
    """终点不是笔记页，但跳转链里有笔记页 Location：取它。"""
    note = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=abc"
    hop = mock.Mock(headers={"Location": note})
    with mock.patch("requests.get", return_value=_Resp("https://www.xiaohongshu.com/", history=[hop])):
        got = D._expand_xhs_short("https://xhslink.cn/o/xxx")
    assert "/explore/" + NOTE_ID in got


def test_expand_xhs_short_returns_original_when_nothing_matches():
    """完全展不开：原样返回，交给上层给「短链没能展开」的提示。"""
    with mock.patch("requests.get", return_value=_Resp("https://www.xiaohongshu.com/")):
        got = D._expand_xhs_short("https://xhslink.cn/o/xxx")
    assert got == "https://xhslink.cn/o/xxx"


def test_expand_xhs_short_survives_network_error():
    with mock.patch("requests.get", side_effect=RuntimeError("boom")):
        assert D._expand_xhs_short("https://xhslink.cn/o/xxx") == "https://xhslink.cn/o/xxx"


def test_win_self_cookie_header_empty_on_failure(monkeypatch):
    """读不到 → 空串（上层据此「不带 Cookie 继续」，不能让解析崩掉）。"""
    fake = mock.Mock()
    fake.find_source.side_effect = RuntimeError("locked")
    monkeypatch.setattr(D, "_browser_cookies", fake)
    assert D._win_self_cookie_header("xiaohongshu.com") == ""


def test_win_self_cookie_header_builds_header(monkeypatch):
    fake = mock.Mock()
    fake.find_source.return_value = {"db": "x", "kind": "chromium", "profile": "Default",
                                     "browser": "edge", "status": "ok"}
    fake.read_cookies.return_value = ([{"name": "a1", "value": "v"}], "")
    fake.build_cookie_header.return_value = "a1=v"
    monkeypatch.setattr(D, "_browser_cookies", fake)
    assert D._win_self_cookie_header("xiaohongshu.com") == "a1=v"


@pytest.mark.parametrize("raw", [
    "ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271",
    "Could not copy Chrome cookie database",
])
def test_friendly_error_flags_locked_cookie_db(raw):
    import yt_dlp

    exc = yt_dlp.utils.DownloadError(raw)
    ctx = D._build_diag_context("https://www.xiaohongshu.com/explore/abc", cookie="", proxy="", options={})
    fe = D._friendly_error(exc, ctx)
    assert fe.category == "cookie_db_locked", "必须单独识别，否则退化成无从下手的「视频解析失败」"
    assert "不代表你没登录" in (fe.hint or "")


def test_short_link_message_no_longer_claims_unresolvable():
    """文案不许再说「任何下载工具都无法解析」——实测可以展开。"""
    import yt_dlp

    exc = yt_dlp.utils.DownloadError("ERROR: [generic] Unable to download webpage")
    ctx = D._build_diag_context("https://xhslink.cn/o/xxx", cookie="", proxy="", options={})
    fe = D._friendly_error(exc, ctx)
    blob = (fe.message or "") + (fe.hint or "")
    assert fe.category == "xhs_short_link"
    assert "任何下载工具都无法" not in blob
    assert "短链本身有效" in blob
