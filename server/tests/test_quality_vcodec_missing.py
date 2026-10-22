#!/usr/bin/env python
"""「vcodec 缺失 ≠ 没有视频轨」守卫（web-dev，2026-10-22）

事故（用户截图，桌面端 + 网页版同时中招）：
  金牌影院 `nmwchxx.com/vod/play/116537/sid/877399`《少帅 第1集》，
  「选择清晰度」里只有「仅音频 MP3 / WebM 格式 / 仅音频 M4A」，
  **一个分辨率档都没有** —— 站点明明给了 1080/720/480 三档。

根因（不是站点的问题，是我们的判据）：
  `_nmw_info` 这类自研提取器构造 formats 时**不填 vcodec**（HLS 混合流，
  站点接口只给 resolution 数字）。`_video_tracks_at` 老判据是
  `f.get("vcodec") in (None, "none") → continue`，把「未知」当成「没有视频」，
  于是每个档位的 _tracks 都是空列表，`build_quality_options` 里一路 `continue`，
  最后只剩末尾三个附加格式。

  yt-dlp 自身的口径是：vcodec is None = 未知（仍算视频流），只有显式 "none"
  才是纯音频。这里跟它对齐。

守卫钉两件事：
  A. vcodec 缺失（None / 无该键）时，按 height 平铺真实分辨率档；
  B. vcodec 显式为 "none" 的条目仍必须排除 —— 放宽不能连坐成假档。
"""

import os
import sys
import unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

from downloader import (  # noqa: E402
    AUDIO_KEY, BEST_KEY, M4A_KEY, WEBM_KEY, build_quality_options,
)

ADDON_KEYS = {BEST_KEY, AUDIO_KEY, WEBM_KEY, M4A_KEY}


def _tier_keys(info):
    """真实分辨率档（数字 key），排除 best/audio/webm/m4a 这类附加项。"""
    return [o["key"] for o in build_quality_options(info) if o["key"] not in ADDON_KEYS]


class MissingVcodecTest(unittest.TestCase):
    def test_nmw_shaped_formats_list_all_tiers(self):
        """金牌影院的真实形态：只有 height，没有 vcodec / acodec。"""
        info = {"formats": [
            {"format_id": "nmw-1080", "height": 1080, "ext": "mp4",
             "protocol": "m3u8_native", "url": "https://x/1080.m3u8"},
            {"format_id": "nmw-720", "height": 720, "ext": "mp4",
             "protocol": "m3u8_native", "url": "https://x/720.m3u8"},
            {"format_id": "nmw-480", "height": 480, "ext": "mp4",
             "protocol": "m3u8_native", "url": "https://x/480.m3u8"},
        ]}
        self.assertEqual(["1080", "720", "480"], _tier_keys(info))

    def test_explicit_none_vcodec_still_excluded(self):
        """显式声明无视频轨的，仍不许列成分辨率档。"""
        info = {"formats": [
            {"height": 1080, "vcodec": "none", "acodec": "mp4a", "ext": "m4a"},
            {"height": None, "vcodec": "none", "acodec": "mp4a", "ext": "m4a"},
        ]}
        self.assertEqual([], _tier_keys(info))

    def test_mixed_declared_and_missing(self):
        """一半声明了 vcodec、一半没声明：声明的照旧，缺的不再被误杀。"""
        info = {"formats": [
            {"height": 1080, "vcodec": "avc1.640028", "ext": "mp4", "filesize": 1000},
            {"height": 720, "ext": "mp4", "protocol": "m3u8_native"},
            {"height": None, "vcodec": "none", "acodec": "mp4a", "ext": "m4a"},
        ]}
        self.assertEqual(["1080", "720"], _tier_keys(info))

    def test_audio_only_unaffected(self):
        """纯音频内容不受影响：仍只有 best 音质 + 音频格式选项。"""
        info = {"ext": "mp3", "formats": [
            {"height": None, "vcodec": "none", "acodec": "mp3", "ext": "mp3", "filesize": 90},
        ]}
        keys = [o["key"] for o in build_quality_options(info)]
        self.assertIn(BEST_KEY, keys)
        self.assertIn(AUDIO_KEY, keys)
        self.assertNotIn("1080", keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
