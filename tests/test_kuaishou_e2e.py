"""快手提取器端到端验证（需网络 + 本机 Chrome 快手 Cookie）。

默认跳过（离线门禁不跑）。设置环境变量 VDL_RUN_KS_E2E=1 时运行：
  VDL_RUN_KS_E2E=1 .build_venv/bin/python -m pytest tests/test_kuaishou_e2e.py -s

会真实拉取一个快手分享链接，验证 SSR 解析能拿到 title / duration / 可播放格式。
"""
import os
import sys

import pytest

if not os.environ.get("VDL_RUN_KS_E2E"):
    pytest.skip("离线门禁默认跳过；设 VDL_RUN_KS_E2E=1 运行真实网络验证", allow_module_level=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from yt_dlp import YoutubeDL

URL = "https://v.kuaishou.com/JLWeyN5p"


def test_kuaishou_e2e():
    opts = {
        "cookiesfrombrowser": ("chrome", "Profile 33"),
        "simulate": True,
        "skip_download": True,
        "format": None,
        "quiet": False,
        "no_warnings": False,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(URL, download=False)

    fmts = info.get("formats") or []
    assert info.get("title"), "title 为空"
    assert info.get("duration"), "duration 为空"
    assert any(f.get("ext") == "mp4" for f in fmts) or any(
        f.get("ext") == "m3u8" for f in fmts
    ), "无可用格式"
    print("\nE2E OK:", info.get("title"), "formats=", len(fmts))
