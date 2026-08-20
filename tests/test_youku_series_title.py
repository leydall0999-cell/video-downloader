"""优酷剧集：从网页 <title> 补全整部剧名。"""

import pytest
from unittest.mock import MagicMock

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from downloader import _enrich_youku_series, _combine_series_title


class _FakeResponse:
    def __init__(self, text, status=200, exc=None):
        self.text = text
        self.status_code = status
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc


def _mock_get(monkeypatch, text, status=200, exc=None):
    calls = []

    def fake_get(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        calls.append({"url": url, "proxies": proxies})
        return _FakeResponse(text, status, exc)

    monkeypatch.setattr("requests.get", fake_get)
    return calls


def test_enrich_extracts_series_from_webpage_title(monkeypatch):
    calls = _mock_get(
        monkeypatch,
        "<title>神墓 辰南觉醒 第1话 我自远古来 - 优酷视频</title>",
    )
    info = {
        "title": "第1话 我自远古来",
        "webpage_url": "https://v.youku.com/v_show/id_XNTg4NjYwMzk0OA==.html",
    }
    out = _enrich_youku_series(info, proxy="http://proxy.example:8080")
    assert out["series"] == "神墓 辰南觉醒"
    assert len(calls) == 1
    assert calls[0]["proxies"] == {"http": "http://proxy.example:8080", "https": "http://proxy.example:8080"}


def test_enrich_skips_non_episode_title(monkeypatch):
    calls = _mock_get(monkeypatch, "<title>某某短视频 - 优酷视频</title>")
    info = {
        "title": "某某短视频",
        "webpage_url": "https://v.youku.com/v_show/id_abc.html",
    }
    out = _enrich_youku_series(info)
    assert "series" not in out
    assert len(calls) == 0


def test_enrich_skips_non_youku_url(monkeypatch):
    calls = _mock_get(monkeypatch, "<title>某剧 第1集 - 其他视频</title>")
    info = {
        "title": "第1集 开场",
        "webpage_url": "https://example.com/video/123",
    }
    out = _enrich_youku_series(info)
    assert "series" not in out
    assert len(calls) == 0


def test_enrich_ignores_network_error(monkeypatch):
    calls = _mock_get(monkeypatch, "", exc=RuntimeError("connection failed"))
    info = {
        "title": "第1话 我自远古来",
        "webpage_url": "https://v.youku.com/v_show/id_XNTg4NjYwMzk0OA==.html",
    }
    out = _enrich_youku_series(info)
    assert "series" not in out


def test_enrich_no_double_prefix_when_title_already_contains_series(monkeypatch):
    calls = _mock_get(monkeypatch, "<title>神墓 辰南觉醒 第1话 我自远古来 - 优酷视频</title>")
    info = {
        "title": "第1话 我自远古来",
        "webpage_url": "https://v.youku.com/v_show/id_XNTg4NjYwMzk0OA==.html",
        "series": "神墓 辰南觉醒",
    }
    out = _enrich_youku_series(info)
    # 已有 series 时仍然会被覆盖为同一值，最终组合不会重复
    combined = _combine_series_title(out)
    assert combined == "神墓 辰南觉醒 - 第1话 我自远古来"


def test_combine_series_title_prefers_series_over_alt():
    info = {"title": "第1话 开场", "series": "神墓", "alt_title": "墓地神话"}
    assert _combine_series_title(info) == "神墓 - 第1话 开场"


def test_combine_series_title_falls_back_to_alt_title():
    info = {"title": "第1话 开场", "alt_title": "神墓"}
    assert _combine_series_title(info) == "神墓 - 第1话 开场"
