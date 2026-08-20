"""优酷剧集：从网页 <title> 补全整部剧名。

优酷单集播放页真实 <title> 形如：
    "神墓 辰南觉醒 第1话 我自远古来-动漫-高清完整正版视频在线观看-优酷"
单集标题与 "-优酷" 之间夹着站点描述，不能用 endswith 精确匹配。

抓取走 VDL_PROXY_CN，失败/跳过的路径都会写入 _series_source 诊断字段。
"""

import sys
import os

import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from downloader import (
    _enrich_youku_series,
    _combine_series_title,
    _extract_page_title,
    _parse_series_from_title,
)


class _FakeResponse:
    def __init__(self, text, status=200, exc=None):
        self.text = text
        self.status_code = status
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc


def _mock_get(monkeypatch, responder):
    """responder(url) -> _FakeResponse。返回 calls 列表。"""
    calls = []

    def fake_get(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        calls.append({"url": url, "proxies": proxies})
        return responder(url)

    monkeypatch.setattr("requests.get", fake_get)
    # 重试用 sleep，测试里直接跳过加速
    monkeypatch.setattr("downloader.time.sleep", lambda *_a, **_k: None)
    return calls


# 真实抓到的优酷单集页 <title>（带站点描述后缀）
REAL_TITLE = "神墓 辰南觉醒 第1话 我自远古来-动漫-高清完整正版视频在线观看-优酷"
SHOW_PAGE_TITLE = "神墓 辰南觉醒-动漫-高清完整正版视频在线观看-优酷"


def test_enrich_extracts_series_from_real_webpage_title(monkeypatch):
    """真实格式：单集标题后夹着 -动漫-高清...-优酷。"""
    calls = _mock_get(monkeypatch, lambda u: _FakeResponse(f"<title>{REAL_TITLE}</title>"))
    info = {
        "title": "第1话 我自远古来",
        "webpage_url": "https://v.youku.com/v_show/id_XNTg4NjYwMzk0OA==.html",
    }
    out = _enrich_youku_series(info, proxy="http://proxy.example:8080")
    assert out["series"] == "神墓 辰南觉醒"
    assert out["_series_source"] == "web_title"
    assert len(calls) == 1
    assert calls[0]["proxies"] == {
        "http": "http://proxy.example:8080",
        "https": "http://proxy.example:8080",
    }


def test_enrich_legacy_format_with_spaces_still_works(monkeypatch):
    """旧格式（带空格 + '视频'）仍能正确提取。"""
    calls = _mock_get(
        monkeypatch,
        lambda u: _FakeResponse("<title>神墓 辰南觉醒 第1话 我自远古来 - 优酷视频</title>"),
    )
    info = {
        "title": "第1话 我自远古来",
        "webpage_url": "https://v.youku.com/v_show/id_XNTg4NjYwMzk0OA==.html",
    }
    out = _enrich_youku_series(info)
    assert out["series"] == "神墓 辰南觉醒"


def test_enrich_fallback_when_title_digits_mismatch(monkeypatch):
    """策略2兜底：网页用 '第01话' 而 yt-dlp 返回 '第1话'（缺前导零）。"""
    title = REAL_TITLE.replace("第1话", "第01话")
    calls = _mock_get(monkeypatch, lambda u: _FakeResponse(f"<title>{title}</title>"))
    info = {
        "title": "第1话 我自远古来",
        "webpage_url": "https://v.youku.com/v_show/id_X.html",
    }
    out = _enrich_youku_series(info)
    assert out["series"] == "神墓 辰南觉醒"


def test_enrich_show_page_fallback(monkeypatch):
    """v_show 页未拿到标题时，回退到 show_page 总页（URL 带 s= show id）。"""
    def resp(u):
        if "show_page" in u:
            return _FakeResponse(f"<title>{SHOW_PAGE_TITLE}</title>")
        # v_show 返回空页面（被风控/JS 跳转）
        return _FakeResponse("<title>优酷</title>")
    calls = _mock_get(monkeypatch, resp)
    info = {
        "title": "第1话 我自远古来",
        "webpage_url": "https://v.youku.com/v_show/id_X.html?spm=x&s=fcaa728a97b5497a9175",
    }
    out = _enrich_youku_series(info)
    assert out["series"] == "神墓 辰南觉醒"
    # 应至少请求了 show_page 兜底
    assert any("show_page" in c["url"] for c in calls)


def test_enrich_skips_non_episode_title(monkeypatch):
    calls = _mock_get(monkeypatch, lambda u: _FakeResponse("<title>某某短视频-优酷</title>"))
    info = {
        "title": "某某短视频",
        "webpage_url": "https://v.youku.com/v_show/id_abc.html",
    }
    out = _enrich_youku_series(info)
    assert "series" not in out
    assert out["_series_source"] == "not_episode"
    assert len(calls) == 0


def test_enrich_skips_non_youku_url(monkeypatch):
    calls = _mock_get(monkeypatch, lambda u: _FakeResponse("<title>某剧 第1集 - 其他视频</title>"))
    info = {
        "title": "第1集 开场",
        "webpage_url": "https://example.com/video/123",
    }
    out = _enrich_youku_series(info)
    assert "series" not in out
    assert out["_series_source"] == "not_youku"
    assert len(calls) == 0


def test_enrich_network_error_records_diagnostic(monkeypatch):
    calls = _mock_get(monkeypatch, lambda u: _FakeResponse("", exc=RuntimeError("conn failed")))
    info = {
        "title": "第1话 我自远古来",
        "webpage_url": "https://v.youku.com/v_show/id_XNTg4NjYwMzk0OA==.html",
    }
    out = _enrich_youku_series(info)
    assert "series" not in out
    assert out["_series_source"].startswith("fetch_failed")


def test_enrich_no_double_prefix_when_title_already_contains_series(monkeypatch):
    calls = _mock_get(monkeypatch, lambda u: _FakeResponse(f"<title>{REAL_TITLE}</title>"))
    info = {
        "title": "第1话 我自远古来",
        "webpage_url": "https://v.youku.com/v_show/id_XNTg4NjYwMzk0OA==.html",
        "series": "神墓 辰南觉醒",
    }
    out = _enrich_youku_series(info)
    combined = _combine_series_title(out)
    assert combined == "神墓 辰南觉醒 - 第1话 我自远古来"


# ---- 辅助函数单测 ----

def test_parse_series_from_title_strategy1():
    assert _parse_series_from_title(REAL_TITLE, "第1话 我自远古来") == "神墓 辰南觉醒"


def test_parse_series_from_title_strategy2_digit_mismatch():
    title = REAL_TITLE.replace("第1话", "第01话")
    assert _parse_series_from_title(title, "第1话 我自远古来") == "神墓 辰南觉醒"


def test_parse_series_from_title_empty():
    assert _parse_series_from_title("", "第1话 x") == ""
    assert _parse_series_from_title("神墓 辰南觉醒-优酷", "") == ""


def test_extract_page_title_prefers_title_tag():
    html = '<title>神墓 辰南觉醒 第1话-优酷</title>'
    assert _extract_page_title(html) == "神墓 辰南觉醒 第1话-优酷"


def test_extract_page_title_og_title_fallback():
    html = '<meta property="og:title" content="神墓 辰南觉醒 第2话-优酷">'
    assert _extract_page_title(html) == "神墓 辰南觉醒 第2话-优酷"


def test_extract_page_title_jsonld_fallback():
    html = '<script type="application/ld+json">{"name":"神墓 辰南觉醒"}</script>'
    assert _extract_page_title(html) == "神墓 辰南觉醒"


def test_extract_page_title_empty():
    assert _extract_page_title("") == ""
    assert _extract_page_title("<html>无标题</html>") == ""


# ---- 组合 ----

def test_combine_series_title_prefers_series_over_alt():
    info = {"title": "第1话 开场", "series": "神墓", "alt_title": "墓地神话"}
    assert _combine_series_title(info) == "神墓 - 第1话 开场"


def test_combine_series_title_falls_back_to_alt_title():
    info = {"title": "第1话 开场", "alt_title": "神墓"}
    assert _combine_series_title(info) == "神墓 - 第1话 开场"
