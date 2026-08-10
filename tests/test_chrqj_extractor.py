"""chrqj.com 专用提取器回归测试。"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 触发 yt-dlp 插件自动注册
import yt_dlp_plugins  # noqa: F401
from yt_dlp_plugins.extractor.chrqj import ChrqjIE


def _make_ie():
    ie = ChrqjIE()
    ie._downloader = MagicMock()
    # 用真实 dict 满足 yt-dlp InfoExtractor 初始化时读取的参数
    ie._downloader.params = {
        'compat_opts': set(),
        'verbose': False,
        'geo_bypass_ip_block': None,
        'geo_bypass_country': None,
        'proxy': None,
        'socket_timeout': 30,
    }
    return ie


def test_multiple_resolutions_all_returned():
    """API 返回多清晰度时，每个清晰度都应生成独立 format；之前缩进 bug 导致只保留最后一项。"""
    ie = _make_ie()

    api_response = {
        "code": 200,
        "data": {
            "list": [
                {"url": "https://cdn.example/1080.m3u8", "resolution": 1080, "resolutionName": "1080P", "needLogin": False},
                {"url": "https://cdn.example/720.m3u8", "resolution": 720, "resolutionName": "720P", "needLogin": False},
                {"url": "https://cdn.example/480.m3u8", "resolution": 480, "resolutionName": "480P", "needLogin": False},
            ]
        },
    }

    with patch.object(ie, '_download_json', return_value=api_response), \
         patch.object(ie, '_download_webpage', return_value='<title>少帅 第9集</title>'):
        info = ie.extract('https://www.chrqj.com/vod/play/116537/1/877439')

    formats = info['formats']
    assert len(formats) == 3, f"应返回 3 个清晰度，实际 {len(formats)}"

    heights = sorted([f['height'] for f in formats], reverse=True)
    assert heights == [1080, 720, 480], f"高度列表不对: {heights}"

    # 验证每个 format 的关键字段
    by_height = {f['height']: f for f in formats}
    assert by_height[1080]['format_id'] == '1080-1080P'
    assert by_height[1080]['protocol'] == 'm3u8_native'
    assert by_height[1080]['preference'] == 1  # 免登录


def test_string_resolution_parsed_as_int():
    """部分接口返回字符串分辨率，应正确转成 int 供清晰度选择器使用。"""
    ie = _make_ie()

    api_response = {
        "code": 200,
        "data": {
            "list": [
                {"url": "https://cdn.example/hd.m3u8", "resolution": "1080", "resolutionName": "1080P", "needLogin": False},
            ]
        },
    }

    with patch.object(ie, '_download_json', return_value=api_response), \
         patch.object(ie, '_download_webpage', return_value='<title>test</title>'):
        info = ie.extract('https://www.chrqj.com/vod/play/1/1/1')

    assert len(info['formats']) == 1
    assert info['formats'][0]['height'] == 1080


def test_login_required_format_marked():
    """需登录的清晰度 preference 应为 -1，避免默认选中。"""
    ie = _make_ie()

    api_response = {
        "code": 200,
        "data": {
            "list": [
                {"url": "https://cdn.example/vip.m3u8", "resolution": 1080, "resolutionName": "1080P", "needLogin": True, "flag": False},
                {"url": "https://cdn.example/free.m3u8", "resolution": 480, "resolutionName": "480P", "needLogin": False},
            ]
        },
    }

    with patch.object(ie, '_download_json', return_value=api_response), \
         patch.object(ie, '_download_webpage', return_value='<title>test</title>'):
        info = ie.extract('https://www.chrqj.com/vod/play/1/1/1')

    by_height = {f['height']: f for f in info['formats']}
    assert by_height[1080]['preference'] == -1
    assert by_height[1080]['format_note'] == '1080P（需登录）'
    assert by_height[480]['preference'] == 1
