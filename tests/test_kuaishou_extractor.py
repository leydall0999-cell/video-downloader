"""快手提取器离线单元测试（无需网络 / 浏览器）。

覆盖：
  - _extract_state_blob：平衡括号抽取，兼容字符串内括号、尾随 JS
  - _find_photo：按 photoId 精确匹配 + 退化取首个含视频字段对象
  - _build_result：直链 MP4 + videoResource HLS 多清晰度构造、去重、时长换算
  - _dedupe_formats
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from yt_dlp_plugins.extractor.kuaishou import KuaishouIE


def _ie():
    return KuaishouIE()


# ── _extract_state_blob ─────────────────────────────────────
def test_extract_state_blob_basic():
    html = 'window.__APOLLO_STATE__ = {"a": 1, "b": {"c": 2}}; var x = 1;'
    data = KuaishouIE._extract_state_blob(html, "__APOLLO_STATE__")
    assert data == {"a": 1, "b": {"c": 2}}


def test_extract_state_blob_string_with_brace():
    # 字符串内部含 '}' 不能被误判为结束
    html = 'window.__APOLLO_STATE__ = {"a": {"b": "}]} weird"}, "c": 1};'
    data = KuaishouIE._extract_state_blob(html, "__APOLLO_STATE__")
    assert data == {"a": {"b": "}]} weird"}, "c": 1}


def test_extract_state_blob_missing_returns_none():
    assert KuaishouIE._extract_state_blob("no such variable here", "__APOLLO_STATE__") is None


# ── _find_photo ─────────────────────────────────────────────
def test_find_photo_by_target_id():
    state = {
        "clients": {
            "defaultClient": {
                "photo:1": {"id": "aaa", "photoH265Url": "http://x/1.mp4"},
                "photo:2": {"id": "bbb", "photoH264Url": "http://x/2.mp4"},
            }
        }
    }
    assert _ie()._find_photo(state, "bbb")["id"] == "bbb"


def test_find_photo_no_match_returns_none():
    state = {"x": {"id": "aaa", "photoH265Url": "http://x/1.mp4"}}
    assert _ie()._find_photo(state, "zzz") is None


def test_find_photo_fallback_to_first_video():
    state = {"x": {"id": "aaa", "photoH265Url": "http://x/1.mp4"}}
    # 退化：target=None 时取首个含视频字段的对象
    assert _ie()._find_photo(state, None)["id"] == "aaa"


# ── _build_result ───────────────────────────────────────────
def _sample_photo():
    return {
        "id": "vid1",
        "caption": "测试标题 #tag",
        "duration": 12345,
        "photoH265Url": "https://v4.oskwai.com/ksc2/abc.mp4?pkey=1",
        "videoResource": {
            "type": "json",
            "json": {
                "h264": {
                    "adaptationSet": [{
                        "representation": [{
                            "id": 1,
                            "url": "https://v1-vod.kwaicdn.com/ksc2/x.m3u8?p=1",
                            "backupUrl": ["https://v4-vod.kwaicdn.com/ksc2/x.m3u8?p=2"],
                            "height": 720,
                            "maxBitrate": 1768,
                        }]
                    }]
                }
            },
        },
        "coverUrl": "https://img/cover.jpg",
        "author": {"name": "up主", "id": "u123"},
    }


def test_build_result_mp4_and_hls():
    res = _ie()._build_result("vid1", _sample_photo())
    assert res["id"] == "vid1"
    assert res["title"] == "测试标题 #tag"
    assert res["duration"] == 12.345
    assert res["thumbnail"] == "https://img/cover.jpg"
    assert res["uploader"] == "up主"
    assert res["uploader_id"] == "u123"

    fmts = res["formats"]
    mp4 = [f for f in fmts if f["ext"] == "mp4"]
    m3u8 = [f for f in fmts if f["ext"] == "m3u8"]
    assert mp4, "应有直链 MP4"
    assert m3u8, "应有 HLS m3u8 流"
    assert any(f["format_id"] == "photoH265Url" for f in mp4)
    # 直链 mp4 优先级应高于 HLS
    mp4_fmt = next(f for f in mp4 if f["format_id"] == "photoH265Url")
    assert mp4_fmt["preference"] > m3u8[0]["preference"]


def test_build_result_no_video_raises():
    with pytest.raises(Exception):
        _ie()._build_result("x", {"id": "x"})


def test_dedupe_formats():
    fmts = [
        {"url": "http://a/1.mp4", "ext": "mp4"},
        {"url": "http://a/1.mp4", "ext": "mp4"},
        {"url": "http://a/2.mp4", "ext": "mp4"},
    ]
    out = KuaishouIE._dedupe_formats(fmts)
    assert len(out) == 2


def test_to_seconds_ms():
    assert _ie()._to_seconds(580833) == 580.833
    assert _ie()._to_seconds(580.5) == 580.5
    assert _ie()._to_seconds(None) is None
