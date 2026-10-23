#!/usr/bin/env python
"""「选什么清晰度就下什么清晰度」守卫测试（app-dev 版，2026-10-21）

与 web-dev 的同名文件分工：本文件只守**下载链**与**分档判据**这两件
跨分支一致的东西；卡片列档的细节（app-dev 无「最佳画质（自动）」首项、
用 _bucket_height 归档非标准高度）由 web-dev 那份覆盖，不在此重复。

钉死三件事：
  A. 下载/降级/HLS 三处选择器对具体档一律锁在 [n±3%] 窄区间，且上下界成对
     （单边上界 `<=N` 既能升也能降，是「选 1080P 拿到 1440P/720P」的根因）；
  B. 分档按**短边**（竖屏 1080x1920 的短边才是 1080），否则竖屏视频整档消失；
  C. 「最佳画质（自动）」保留 `<=1080` 自动语义 —— 用户没指定档位不该被锁死。
"""

import os
import re
import sys
import unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_quality_exact_app")

import downloader as D  # noqa: E402


def _vid(fid, h, w, vcodec, size, tbr=3000):
    return {"format_id": fid, "height": h, "width": w, "vcodec": vcodec,
            "acodec": "none", "filesize": size, "tbr": tbr}


def _aud(fid, ext, size, abr=128):
    return {"format_id": fid, "acodec": "mp4a.40.2", "vcodec": "none",
            "ext": ext, "filesize": size, "abr": abr}


LANDSCAPE = [
    _vid("137", 1080, 1920, "avc1.64002", 402242089, 3500),
    _vid("248", 1080, 1920, "vp9", 252240432, 2200),
    _vid("136", 720, 1280, "avc1.64001", 210813890, 2000),
    _aud("140", "m4a", 15400000),
]

PORTRAIT = [
    _vid("v1", 1920, 1080, "avc1.64002", 600000000, 3000),
    _vid("v2", 1280, 720, "avc1.64001", 300000000, 1800),
    _aud("a1", "m4a", 12000000),
]


class ExactSelectorTest(unittest.TestCase):
    """A. 具体档位的选择器必须锁在窄区间，且每个上界都要配一个下界。"""

    def _assert_exact(self, selector, n, what):
        tol = max(4, int(n * 0.03))
        lo, hi = n - tol, n + tol
        self.assertNotIn("bv*", selector, f"{what} 出现 bv*（无区间约束）")
        self.assertIn(f"height>={lo}", selector, f"{what} 缺下界 height>={lo}")
        self.assertIn(f"height<={hi}", selector, f"{what} 缺上界 height<={hi}")
        self.assertIn(f"width>={lo}", selector, f"{what} 缺竖屏下界 width>={lo}")
        self.assertIn(f"width<={hi}", selector, f"{what} 缺竖屏上界 width<={hi}")
        # 孤立的单边上界正是会静默跨档的写法，必须成对出现
        for key in ("height", "width"):
            for m in re.finditer(r"\[" + key + r"<=(\d+)\]", selector):
                seg = selector[max(0, m.start() - 24):m.start()]
                self.assertIn(
                    f"[{key}>=", seg,
                    f"{what} 出现单边上界 [{key}<={m.group(1)}]（会跨档）",
                )

    def test_format_selector_1080(self):
        self._assert_exact(D._format_selector("1080"), 1080, "_format_selector(1080)")

    def test_format_selector_720(self):
        self._assert_exact(D._format_selector("720"), 720, "_format_selector(720)")

    def test_format_selector_1440(self):
        self._assert_exact(D._format_selector("1440"), 1440, "_format_selector(1440)")

    def test_hls_selector(self):
        s = D._youtube_hls_selector("1080")
        self._assert_exact(s, 1080, "_youtube_hls_selector(1080)")
        self.assertIn("protocol^=m3u8][height>=1048]", s)

    def test_fallback_chains(self):
        for chain in D._yt_fallback_chains("1080"):
            self._assert_exact(chain, 1080, "_yt_fallback_chains(1080)")
        for chain in D._yt_fallback_chains("720"):
            self._assert_exact(chain, 720, "_yt_fallback_chains(720)")

    def test_quality_cap_matches_tier(self):
        self.assertEqual(D._quality_cap("1080"), 1080)
        self.assertEqual(D._quality_cap("720"), 720)


class ShortSideTest(unittest.TestCase):
    """B. 分档按短边：横竖屏统一到用户认知的那个数字。"""

    def test_landscape(self):
        self.assertEqual(D._short_side(_vid("x", 1080, 1920, "avc1", 1)), 1080)

    def test_portrait(self):
        self.assertEqual(D._short_side(_vid("x", 1920, 1080, "avc1", 1)), 1080)

    def test_missing_width_falls_back(self):
        self.assertEqual(D._short_side({"height": 720, "width": None}), 720)

    def test_no_height_is_none(self):
        self.assertIsNone(D._short_side({"width": 1920}))

    def test_tolerance_values(self):
        self.assertEqual(D._tier_tolerance(1080), 32)
        self.assertEqual(D._tier_tolerance(360), 10)
        self.assertEqual(D._tier_tolerance(144), 4)


class CardListsSelectableTier(unittest.TestCase):
    """B2. 卡片必须能列出用户真能选到的档，竖屏 1080P 不能整档消失。"""

    def _keys(self, formats):
        return [o["key"] for o in D.build_quality_options({"formats": formats})]

    def test_landscape_lists_1080p(self):
        keys = self._keys(LANDSCAPE)
        self.assertIn("1080", keys)
        self.assertIn("720", keys)

    def test_portrait_lists_1080p(self):
        """竖屏按 height=1920 分档的话，1080P 这一档会直接消失。"""
        keys = self._keys(PORTRAIT)
        self.assertIn("1080", keys)

    def test_odd_height_1072_maps_to_1080p(self):
        odd = [_vid("o1", 1072, 1912, "avc1", 380000000, 3300)]
        self.assertIn("1080", self._keys(odd))


class BestKeyStaysAutomatic(unittest.TestCase):
    """C. 「最佳画质（自动）」保留自动语义 —— 用户没指定档位不该被锁死。"""

    def test_best_keeps_upper_bound(self):
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
                self.assertLessEqual(_ITAG_H.get(itag, 9999), 1080,
                                     f"自动档出现 >1080p 的 itag：{itag}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
