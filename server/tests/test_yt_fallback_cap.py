#!/usr/bin/env python
"""YouTube 403 降级链「不许超过用户选的清晰度」守卫（web-dev，2026-10-21）

事故（用户提问「上方选 1080P 显示 398M，下方下载 641M，为什么差这么多」）：
  视频 Fcyih0Z4G2k，用户选「1080P 高清」，卡片按 `_video_size_at(1080)`
  = itag 137(403MB, H.264 1080p) + 140(14.7MB) 算得 **398 MiB**。
  该视频的 1080P H.264 分片被 googlevideo 403 拒绝（逐视频 IP 门禁，偶发），
  于是进入 `_run_once` 的 403 降级链；前几环全是 avc1/137 → 同样 403，
  最后落到旧末环 `bv*+ba/best[height<=1080]/best` —— 它**没有 height 上限**，
  直接选了 itag 400(AV1 2560×1440) + 251：
    任务记录 downloaded_bytes = 671874532
    与 `bv*+ba` 选出的 400+251 filesize 之和 671874532 **完全一致**（实测闭环）。
  后果：① 用户多花 1.6 倍流量；② UI 上 1440P 标着「PRO·原画需会员」，
  免费用户被降级链白送了一个会员档位。

修法：把降级链抽成 `_yt_fallback_chains(quality_key)`，**每一条都受
`_quality_cap()` 约束**；硬编码 itag 链（299/248/137 都是 1080p）也按档裁剪。

本文件离线守卫（不发任何网络请求）：
1. `_quality_cap()` 语义（数字档取数字；best/audio/webm/m4a ⇒ 1080）；
2. 任一档位下，**所有**降级链的通用选择器都带 `[height<=cap]`；
3. 显式 itag 链里不得出现高于 cap 的 itag（720 档不许有 299/248/137）；
4. 源码级接线：`_run_once` 必须调 `_yt_fallback_chains(quality_key)`，
   且源码里不得再出现无上限的 `bv*+ba/best[height<=1080]/best`。
"""
import os
import re
import sys
import unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import downloader  # noqa: E402


def _alternatives(chain: str) -> list[str]:
    """把一条链按顶层 `/` 拆成备选（链里不含带 `/` 的过滤器，可直接 split）。"""
    return [p for p in chain.split("/") if p.strip()]


GENERIC = ("bv*", "bv", "best", "b[", "b ")


def _is_explicit_itags(alt: str) -> bool:
    return bool(re.fullmatch(r"\d+(\+\d+)+", alt.strip()))


class QualityCapTest(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(downloader._quality_cap("1080"), 1080)
        self.assertEqual(downloader._quality_cap("720"), 720)
        self.assertEqual(downloader._quality_cap("2160"), 2160)

    def test_non_numeric_defaults_to_1080(self):
        for key in ("best", "audio", "webm", "m4a", "", "?"):
            self.assertEqual(downloader._quality_cap(key), 1080, key)


class FallbackChainCapTest(unittest.TestCase):
    def test_every_generic_selector_is_height_capped(self):
        """具体档位的通用选择器必须锁在 [cap±3%] 的窄区间内。

        2026-10-21 第二轮：单边的 `<=cap` 只堵住升档，还留着降档的口子 ——
        1080P 被 403 时会悄悄退到 720P，用户以为拿了 1080P 实际是 720P。
        改成双边区间才叫「选什么下什么」；留 ±3% 是为了 1072 这类非标准高度
        （见 _tier_tolerance）。竖屏那半（1080x1920，短边在 width 上）同理。

        自动档 best 不在此列：它保留 `<=1080` 的自动语义（用户没指定档位，
        锁死反而拿不到东西），由 test_quality_exact.py 的 BestKeyStaysAutomatic 守。
        """
        for key in ("360", "480", "720", "1080", "1440", "2160"):
            cap = downloader._quality_cap(key)
            lo = cap - max(4, int(cap * 0.03))
            for chain in downloader._yt_fallback_chains(key):
                for alt in _alternatives(chain):
                    if _is_explicit_itags(alt):
                        continue
                    if not any(alt.startswith(g) or g in alt for g in GENERIC):
                        continue
                    self.assertTrue(
                        f"height>={lo}" in alt or f"width>={lo}" in alt,
                        f"{key} 档出现非精确选择器：{alt}（chain={chain}）",
                    )

    def test_no_height_above_cap_in_filter(self):
        """过滤器里出现的所有 height<=N 都必须 ≤ 档位上界（cap + 容差）。

        上界允许放宽到 cap+3%（容纳 1072 这类非标准高度），但**不许再宽** ——
        把手抄错的数字（比如 1080 档写成 <=1440）挡在外面。
        """
        for key in ("360", "720", "1080"):
            cap = downloader._quality_cap(key)
            hi = cap + max(4, int(cap * 0.03))
            for chain in downloader._yt_fallback_chains(key):
                for n in re.findall(r"height<=(\d+)", chain):
                    self.assertLessEqual(int(n), hi,
                                         f"{key} 档链里出现 height<={n}（越档）")

    def test_explicit_itag_chain_only_for_best(self):
        """显式 itag 链只属于「最佳画质（自动）」，具体档位一律走精确链。

        2026-10-21 第二轮：itag 链（299/248/137…）是按高度拼的，本身就隐含
        「≤cap 里挑一个」，做不到精确锁定。具体档位改用 `[height=N]` 精确链后
        自然不再需要它；它只留在自动档，那里 `<=1080` 才是正确语义。
        """
        best_joined = "|".join(downloader._yt_fallback_chains("best"))
        self.assertIn("137+140", best_joined, "自动档应保留 itag 链 137+140")
        self.assertIn("136+140", best_joined, "自动档应保留 itag 链 136+140")

        for key in ("360", "480", "720", "1080", "1440", "2160"):
            joined = "|".join(downloader._yt_fallback_chains(key))
            for itag in ("299", "248", "137", "136", "135", "134"):
                self.assertNotIn(
                    f"{itag}+140", joined,
                    f"{key} 档不该再出现 itag 链 {itag}+140（应走精确链）",
                )

    def test_upscale_never_requested(self):
        """末环必须是最小兜底，且不得出现裸 bv*+ba（本次事故的根因）。"""
        for key in ("720", "1080"):
            chains = downloader._yt_fallback_chains(key)
            self.assertFalse(any("bv*+ba" in c for c in chains),
                             f"{key} 档仍存在无上限的 bv*+ba")


class SourceWiringTest(unittest.TestCase):
    def test_run_once_uses_helper(self):
        src = open(os.path.join(SERVER_DIR, "downloader.py"), encoding="utf-8").read()
        self.assertIn("_fallback_chains = _yt_fallback_chains(quality_key)", src)

    def test_old_unbounded_chain_gone(self):
        src = open(os.path.join(SERVER_DIR, "downloader.py"), encoding="utf-8").read()
        # 只允许出现在 _yt_fallback_chains 的注释里（说明事故），代码里不许有
        code = re.sub(r'""".*?"""', "", src, flags=re.S)
        code = re.sub(r"#.*", "", code)
        self.assertNotIn('"bv*+ba/best[height<=1080]/best"', code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
