#!/usr/bin/env python
"""「选什么清晰度就下什么清晰度」守卫测试（web-dev，2026-10-21）

事故链条（用户截图：卡片「1080P · 约 398 MB」，实得 641 MiB）：
  1. 卡片按 `height <= max_height` 平铺档位 ⇒ 480P/360P 这些**根本不存在**的
     档也被列出来，用户能选到拿不到的东西；
  2. 卡片体积取 `max(该档所有轨)`，而下载链首选编码未必是最大的那条
     ⇒ 说得和拿到的天生对不上；
  3. 下载链用 `bv*[height<=1080]`：`<=` 既能升也能降 —— 1080P 被 403 时
     末环 `bv*+ba` 无上限 ⇒ 直接跳到 1440P（itag 400+251，671874532 字节，
     与线上任务 downloaded_bytes **一字节不差**）；
  4. 竖屏（1080x1920）更糟：height 是 1920，1080P 这一档匹配不到，
     `height<=1080` 只能命中 720 ⇒ 选 1080P 实得 720P。

本文件离线守卫（不发任何网络请求），钉死四件事：
  A. 分档按**短边**（横竖屏统一），卡片只列真实存在的档；
  B. 卡片体积 = **首选编码组合**的体积，不是该档最大那条；
  C. 下载/降级/HLS 选择器对具体档位一律 `=N` 精确锁定，全链不得出现 `<=`；
  D. 「最佳画质（自动）」保留自动语义（`<=1080`），用户没指定档位就不该被锁死。
"""

import os
import sys
import types
import unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_quality_exact")

import downloader as D  # noqa: E402

MIB = 1048576


def _vid(fid, h, w, vcodec, size, tbr=3000):
    return {
        "format_id": fid, "height": h, "width": w, "vcodec": vcodec,
        "acodec": "none", "filesize": size, "tbr": tbr,
    }


def _aud(fid, ext, size, abr=128):
    return {
        "format_id": fid, "acodec": "mp4a.40.2" if ext == "m4a" else "opus",
        "vcodec": "none", "ext": ext, "filesize": size, "abr": abr,
    }


# 百香果视频 2026-10-21 实测 formats（短边 1440/1080/720）
LANDSCAPE = [
    _vid("400", 1440, 2560, "av01.0.12M", 656480587, 5700),
    _vid("137", 1080, 1920, "avc1.64002", 402242089, 3500),
    _vid("248", 1080, 1920, "vp9", 252240432, 2200),
    _vid("399", 1080, 1920, "av01.0.08M", 264151815, 2300),
    _vid("136", 720, 1280, "avc1.64001", 210813890, 2000),
    _aud("140", "m4a", 15400000),
    _aud("251", "webm", 14000000),
]

# 竖屏短视频（height 是长边 1920，用户认知的「1080P」在短边上）
PORTRAIT = [
    _vid("v1", 1920, 1080, "avc1.64002", 600000000, 3000),
    _vid("v2", 1280, 720, "avc1.64001", 300000000, 1800),
    _aud("a1", "m4a", 12000000),
]

# 该档里 VP9 才是最大的一条 —— 专治「卡片取 max」
VP9_BIGGEST = [
    _vid("137", 1080, 1920, "avc1.64002", 200 * MIB, 2000),
    _vid("248", 1080, 1920, "vp9", 500 * MIB, 5000),
    _aud("140", "m4a", 10 * MIB),
]


class ShortSideTest(unittest.TestCase):
    """A0. 短边判据：横竖屏统一到用户认知的那个数字。"""

    def test_landscape_short_side_is_height(self):
        self.assertEqual(D._short_side(_vid("x", 1080, 1920, "avc1", 1)), 1080)

    def test_portrait_short_side_is_width(self):
        self.assertEqual(D._short_side(_vid("x", 1920, 1080, "avc1", 1)), 1080)

    def test_missing_width_falls_back_to_height(self):
        f = {"height": 720, "width": None, "vcodec": "avc1", "acodec": "none"}
        self.assertEqual(D._short_side(f), 720)

    def test_no_height_returns_none(self):
        self.assertIsNone(D._short_side({"width": 1920, "vcodec": "none"}))


class CardListingTest(unittest.TestCase):
    """A. 卡片只列真实存在的档位。"""

    def _keys(self, info):
        return [o["key"] for o in D.build_quality_options(info)]

    def test_landscape_lists_only_real_tiers(self):
        keys = self._keys({"formats": LANDSCAPE})
        for real in ("1440", "1080", "720"):
            self.assertIn(real, keys)
        for ghost in ("2160", "480", "360"):
            self.assertNotIn(ghost, keys)

    def test_portrait_can_select_1080p(self):
        """竖屏 1080P 必须能被选到（老判据按 height=1920 分档 ⇒ 根本列不出 1080P）。"""
        keys = self._keys({"formats": PORTRAIT})
        self.assertIn("1080", keys)
        self.assertIn("720", keys)
        self.assertNotIn("1440", keys)
        self.assertNotIn("480", keys)

    def test_single_tier_video_lists_only_that_tier(self):
        only1080 = [_vid("137", 1080, 1920, "avc1", 100)] + [_aud("140", "m4a", 10)]
        keys = self._keys({"formats": only1080})
        self.assertIn("1080", keys)
        for ghost in ("720", "480", "360"):
            self.assertNotIn(ghost, keys)

    def test_every_listed_tier_has_a_video_track(self):
        """列出来的每个具体档，都必须真有对应短边的视频轨（否则选了必失败）。"""
        for o in D.build_quality_options({"formats": LANDSCAPE}):
            if o["key"] in ("best", "audio", "webm", "m4a"):
                continue
            self.assertTrue(
                D._video_tracks_at(LANDSCAPE, int(o["key"])),
                f"档位 {o['key']} 被列出但没有任何视频轨",
            )


class CardSizeTest(unittest.TestCase):
    """B. 卡片体积 = 首选编码组合，而非该档最大的那条。"""

    def _opt(self, info, key):
        for o in D.build_quality_options(info):
            if o["key"] == key:
                return o
        raise AssertionError(f"卡片没有 {key} 档")

    def test_landscape_1080p_is_h264_plus_m4a(self):
        """1080P 首选必须是 H.264(137)+m4a 音轨，与卡片承诺一致。"""
        o = self._opt({"formats": LANDSCAPE}, "1080")
        self.assertEqual(o["codec"], "H.264")
        self.assertEqual(o["approx_size"], 402242089 + 15400000)
        # 与用户截图「约 398 MB」对账（402242089+15400000 = 398.3 MiB）
        self.assertEqual(round(o["approx_size"] / MIB), 398)

    def test_size_uses_preferred_codec_not_max(self):
        """该档 VP9 更大的时候，卡片仍按首选 H.264 报价，不能报 VP9 的。"""
        o = self._opt({"formats": VP9_BIGGEST}, "1080")
        self.assertEqual(o["codec"], "H.264")
        self.assertEqual(o["approx_size"], (200 + 10) * MIB)  # 不是 500+10

    def test_codec_preference_order(self):
        tracks = D._video_tracks_at(LANDSCAPE, 1080)
        self.assertEqual([t["format_id"] for t in tracks[:3]], ["137", "248"] + ["399"])
        self.assertEqual([D._codec_label(t) for t in tracks],
                         ["H.264", "VP9", "AV1"])

    def test_audio_prefers_m4a(self):
        self.assertEqual(D._audio_tracks(LANDSCAPE)[0]["format_id"], "140")

    def test_exact_height_matches_key(self):
        for o in D.build_quality_options({"formats": LANDSCAPE}):
            if o["key"] in ("best", "audio", "webm", "m4a"):
                continue
            self.assertEqual(o["exact_height"], int(o["key"]))


class ExactSelectorTest(unittest.TestCase):
    """C. 具体档位的选择器一律 `=N` 精确锁定。"""

    def _assert_exact(self, selector, n, what):
        self.assertNotIn("<=", selector, f"{what} 出现 <=（会跨档）")
        self.assertNotIn("bv*", selector, f"{what} 出现 bv*（无上限）")
        self.assertIn(f"height={n}", selector, f"{what} 没有锁 height={n}")
        self.assertIn(f"width={n}", selector, f"{what} 没有锁 width={n}（竖屏会失效）")

    def test_format_selector_1080_is_exact(self):
        self._assert_exact(D._format_selector("1080"), 1080, "_format_selector(1080)")

    def test_format_selector_720_is_exact(self):
        self._assert_exact(D._format_selector("720"), 720, "_format_selector(720)")

    def test_format_selector_1440_is_exact(self):
        self._assert_exact(D._format_selector("1440"), 1440, "_format_selector(1440)")

    def test_hls_selector_is_exact(self):
        s = D._youtube_hls_selector("1080")
        self._assert_exact(s, 1080, "_youtube_hls_selector(1080)")
        self.assertIn("protocol^=m3u8][height=1080]", s)

    def test_fallback_chains_are_exact(self):
        for chain in D._yt_fallback_chains("1080"):
            self._assert_exact(chain, 1080, "_yt_fallback_chains(1080)")

    def test_fallback_chains_720_are_exact(self):
        for chain in D._yt_fallback_chains("720"):
            self._assert_exact(chain, 720, "_yt_fallback_chains(720)")

    def test_quality_cap_matches_requested_tier(self):
        self.assertEqual(D._quality_cap("1080"), 1080)
        self.assertEqual(D._quality_cap("720"), 720)


class BestKeyStaysAutomatic(unittest.TestCase):
    """D. 「最佳画质（自动）」保留自动语义——用户没指定档位，不该被锁死。"""

    def test_best_uses_upper_bound(self):
        self.assertIn("height<=1080", D._format_selector(D.BEST_KEY))

    def test_best_fallback_keeps_upper_bound(self):
        # 显式 itag 链不带 height 语法，但每个 itag 都隐含高度（299/248/137=1080p）
        _ITAG_H = {"299": 1080, "248": 1080, "137": 1080, "136": 720,
                   "135": 480, "134": 360, "133": 240, "160": 144}
        for chain in D._yt_fallback_chains(D.BEST_KEY):
            if "height<=1080" in chain:
                continue
            for token in chain.split("/"):
                itag = token.split("+")[0]
                self.assertLessEqual(
                    _ITAG_H.get(itag, 9999), 1080,
                    f"自动档出现 >1080p 的 itag：{itag}",
                )

    def test_best_never_unbounded(self):
        """自动档可以 ≤1080，但绝不允许出现无上限的 bv*+ba（会跳到 1440P/4K）。"""
        for chain in D._yt_fallback_chains(D.BEST_KEY):
            for token in chain.split("/"):
                if token.startswith("bv*+ba") or token == "best":
                    self.fail(f"自动档出现无上限片段：{token}")


class ActualQualityCheckTest(unittest.TestCase):
    """E. 下载完成后核对「请求档 vs 实得档」——不符必须摆到用户眼前。

    精确锁定的代码能保证不换档，但保证本身也需要被验证：万一哪天有人把
    `<=` 或 `bv*` 加回去，静默换档会再次发生。这条守卫让用户第一时间看见 ⚠️。
    """

    def _note(self, quality_key, info):
        r = D._ProgressReporter.__new__(D._ProgressReporter)
        r._actual_short = D._info_short_side(info) or 0
        r._task = types.SimpleNamespace(quality_key=quality_key)
        return r._actual_note()

    def test_short_side_from_dash_requested_formats(self):
        info = {"requested_formats": [
            {"height": 1080, "width": 1920, "vcodec": "avc1", "acodec": "none"},
            {"height": None, "width": None, "vcodec": "none", "acodec": "mp4a"},
        ]}
        self.assertEqual(D._info_short_side(info), 1080)

    def test_short_side_from_portrait_dash(self):
        info = {"requested_formats": [
            {"height": 1920, "width": 1080, "vcodec": "avc1", "acodec": "none"},
        ]}
        self.assertEqual(D._info_short_side(info), 1080)

    def test_short_side_from_hls_single_stream(self):
        self.assertEqual(D._info_short_side({"height": 1080, "width": 1920}), 1080)

    def test_short_side_unknown_is_none(self):
        self.assertIsNone(D._info_short_side({}))

    def test_note_matched_has_no_warning(self):
        info = {"requested_formats": [
            {"height": 1080, "width": 1920, "vcodec": "avc1", "acodec": "none"}]}
        n = self._note("1080", info)
        self.assertIn("实际 1080P", n)
        self.assertNotIn("⚠️", n)

    def test_note_mismatch_is_flagged(self):
        """就是 2026-10-21 那个 1080P→1440P 事故，现在必须在界面上喊出来。"""
        info = {"requested_formats": [
            {"height": 1440, "width": 2560, "vcodec": "av01", "acodec": "none"}]}
        n = self._note("1080", info)
        self.assertIn("⚠️", n)
        self.assertIn("请求 1080P", n)
        self.assertIn("实得 1440P", n)

    def test_note_mismatch_when_downgraded(self):
        """降档同样要报：1080P 请求拿到 720P 也是「说得不算数」。"""
        info = {"requested_formats": [
            {"height": 720, "width": 1280, "vcodec": "avc1", "acodec": "none"}]}
        n = self._note("1080", info)
        self.assertIn("⚠️", n)
        self.assertIn("实得 720P", n)

    def test_best_key_never_flagged(self):
        """自动档用户没指定档位，拿到什么都是合理的。"""
        n = self._note("best", {"height": 720, "width": 1280})
        self.assertIn("实际 720P", n)
        self.assertNotIn("⚠️", n)

    def test_unknown_actual_says_plain(self):
        self.assertEqual(self._note("1080", {}), "音视频下载完成")


if __name__ == "__main__":
    unittest.main(verbosity=2)
