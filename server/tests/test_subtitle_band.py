"""server/tests/test_subtitle_band.py — 原字幕带探测回归测试（2026-09-16）。

背景：`擦除原字幕` 原先完全靠管线自动探测、UI 零暴露 —— 探测偏了只能跑完整条任务
才发现。现在 App 侧新增「原字幕羽化」面板：预览窗口先用 Canvas 复刻擦除效果、可拖
动微调位置，调好的**比例**再回传给管线渲染（= 预览看到什么，成片就烧什么）。

本算法与管线 `scripts/edit_ffmpeg.py::_prepare_feather / _band_rows` **同源**：
判据（只认白字 >=205、从画面 70% 起扫、允许 2 行间隙、丢 >25% 的异常帧、中位数聚合、
上下各扩 5% 字幕高且至少 0.3% 画面）必须与那边一致 —— 只改一边会让比例漂移，
直观看就是「预览和成片位置不一致」，正是本功能要消灭的问题。

覆盖：
  detect_finds_white_text_band           底部白字带能被找到
  detect_clean_frame_returns_not_found   干净画面不误报
  detect_requires_two_hits               命中帧不足（1/4）时不确认
  detect_10frames_needs_three_hits       10 帧门槛 = max(2, 30% 帧数)（2026-09-18）
  detect_ignores_oversized_band          大字幕墙/演职员表（>25% 高）被丢弃
  detect_pads_band_slightly              返回的带比原字略大（包住描边/辉光）
  band_ratio_independent_of_resolution   同一内容不同分辨率给出同一比例
  band_rows_rejects_bright_spill         亮场景溢出（贴扫描区顶且高 >15%）弃帧
  band_rows_keeps_short_band_touching_scan_top   守卫双条件，不误伤矮带
  mark_matches_pipeline                  关键常量与管线口径一致（防单边漂移）

运行：
    cd server && python tests/test_subtitle_band.py
"""
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from PIL import Image, ImageDraw  # noqa: E402

import subtitle_band  # noqa: E402


def _mk_frame(w=480, h=270, band_top=None, band_h=24, bg=(30, 40, 60)):
    """合成一帧：底部可选一条「白字」带（交替短竖条模拟文字笔画）。"""
    im = Image.new("RGB", (w, h), bg)
    if band_top is not None:
        d = ImageDraw.Draw(im)
        step = max(4, w // 22)
        bar = max(2, w // 44)
        x = int(w * 0.18)
        while x < w * 0.82:
            d.rectangle([x, band_top, x + bar, band_top + band_h], fill=(255, 255, 255))
            x += step
    return im


def test_detect_finds_white_text_band():
    imgs = [_mk_frame(band_top=228) for _ in range(4)]
    r = subtitle_band.detect_band_ratio(imgs)
    assert r["found"] is True, r
    # 原字 228/270 ≈ 0.844；扣除「带比原字略大」的 padding 后，带顶应略高于此
    assert 0.78 < r["band_y_ratio"] < 0.845, r
    assert 0.03 < r["band_h_ratio"] < 0.16, r
    assert r["hits"] == 4 and r["total"] == 4, r


def test_detect_clean_frame_returns_not_found():
    r = subtitle_band.detect_band_ratio([_mk_frame() for _ in range(4)])
    assert r["found"] is False, r
    assert r["hits"] == 0, r
    assert "未探测到" in r["note"], r


def test_detect_requires_two_hits():
    # 4 帧里只有 1 帧有字幕 → 不足以确认（避免把偶然出现的画面元素当成字幕带）
    imgs = [_mk_frame(band_top=228)] + [_mk_frame() for _ in range(3)]
    r = subtitle_band.detect_band_ratio(imgs)
    assert r["found"] is False, r
    assert r["hits"] == 1, r


def test_detect_10frames_needs_three_hits():
    # 2026-09-18 前端抽帧 4→10：门槛随帧数走（max(2, 30% 帧数)），10 帧要求 ≥3。
    # 2/10 命中不确认；3/10 命中确认且带位置贴合真实字幕。
    good = _mk_frame(band_top=228, band_h=20)
    r2 = subtitle_band.detect_band_ratio([good, good] + [_mk_frame() for _ in range(8)])
    assert r2["found"] is False, r2
    assert r2["hits"] == 2, r2
    r3 = subtitle_band.detect_band_ratio([good] * 3 + [_mk_frame() for _ in range(7)])
    assert r3["found"] is True, r3
    assert 0.78 < r3["band_y_ratio"] < 0.9 and r3["band_h_ratio"] < 0.16, r3


def _mk_bright_spill_frame(w=480, h=270):
    """合成「亮场景」帧：白窗/白墙从画面 50% 高一直亮到底（占宽 30%，落在
    hi_w=45% 以内 —— 正是少帅实测里骗过旧判据的亮窗形态）。"""
    im = Image.new("RGB", (w, h), (30, 40, 60))
    d = ImageDraw.Draw(im)
    d.rectangle([int(w * 0.10), int(h * 0.50), int(w * 0.40), h - 1], fill=(255, 255, 255))
    return im


def test_band_rows_rejects_bright_spill():
    # 亮场景溢出守卫（2026-09-18）：带贴扫描区顶（0.70h）且高 >15% 画面 → 弃帧。
    # 不弃的话少帅实测 4 帧里 3 帧亮场景会把中位带高撑到 ~0.17（糊掉大片画面）。
    b = subtitle_band._band_rows(_mk_bright_spill_frame().convert("L"), 480, 270)
    assert b is None, b


def test_band_rows_keeps_short_band_touching_scan_top():
    # 守卫是「贴顶 + 高」双条件，不误伤：贴着扫描区顶但矮（≤15% 画面）的带仍保留
    b = subtitle_band._band_rows(_mk_frame(band_top=192, band_h=26).convert("L"), 480, 270)
    assert b is not None, b
    assert b[0] == 192, b


def test_detect_ignores_oversized_band():
    # 带高占画面 >25%（演职员表/大字幕墙）应被整帧丢弃，不能把整屏当字幕带
    r = subtitle_band.detect_band_ratio([_mk_frame(band_top=190, band_h=70) for _ in range(4)])
    assert r["found"] is False, r
    assert r["hits"] == 0, r


def test_detect_pads_band_slightly():
    # 返回的带必须略大于原字（上下各扩 _PAD_RATIO＝5% 字幕高），否则描边/辉光会残字；
    # 但也不该扩到翻倍（那会糊住带外一大片）。
    # ⚠️ 2026-09-19 用户反馈默认带高偏大（9.5 → 想 9）后由 8% 收到 5%。
    imgs = [_mk_frame(band_top=230, band_h=20) for _ in range(4)]
    r = subtitle_band.detect_band_ratio(imgs)
    assert r["found"] is True, r
    raw = 20 / 270.0
    assert r["band_h_ratio"] > raw, r
    assert r["band_h_ratio"] < raw * 1.5, r


def test_band_ratio_independent_of_resolution():
    # 「传比例而非像素」的前提：同一画面内容在不同分辨率下必须给出同一比例。
    # 这是竖屏（管线 canvas 固定 480x854）与横屏（canvas = 源分辨率）能共用一套参数的基础。
    small = subtitle_band.detect_band_ratio([_mk_frame(480, 270, band_top=228, band_h=20)
                                             for _ in range(4)])
    big = subtitle_band.detect_band_ratio([_mk_frame(1280, 720, band_top=608, band_h=53)
                                           for _ in range(4)])
    assert small["found"] and big["found"], (small, big)
    assert abs(small["band_y_ratio"] - big["band_y_ratio"]) < 0.03, (small, big)
    assert abs(small["band_h_ratio"] - big["band_h_ratio"]) < 0.03, (small, big)


def test_detect_covers_varying_subtitle_position():
    """🔴 2026-09-18 用户反馈「有部分字预览没有擦除」的回归锁。

    实测根因：同一条片子里字幕位置并不恒定（两处交替 / 缓慢漂移 / 单行与双行混排），
    而旧实现**只取中心中位数**，会把带放在"两头都不沾"的位置——字幕在 620/560 两处
    交替时实测只盖住字高 42%~47%（露 28~30 行），正是用户截图里「字的下半截露在带外」。

    现在改为「鲁棒并集」（顶 p10 / 底 p90，相对中位带最多再长 6% 画高），
    要求两个位置的字体**都要被盖满**。间距取实测幅度：两处相差约 8% 画高
    （270px 高的合成帧 → 22px；真机那一例是 720p 里差 60px）。"""
    imgs = []
    for i in range(10):
        imgs.append(_mk_frame(band_top=228 if i % 2 == 0 else 206))   # 两处交替，相距 8% 画高
    r = subtitle_band.detect_band_ratio(imgs)
    assert r["found"] is True, r
    top = r["band_y_ratio"] * 270
    bot = (r["band_y_ratio"] + r["band_h_ratio"]) * 270
    for band_top in (228, 206):
        band_bot = band_top + 24
        cov = max(0, min(bot, band_bot) - max(top, band_top))
        assert cov >= 24 * 0.95, (
            "位置 %d~%d 未被盖满（覆盖 %.0f%%，带 %.0f~%.0f）" % (band_top, band_bot, 100.0 * cov / 24, top, bot))
    # 并集必须仍然受增长上限约束，不许退化成糊一大片
    assert r["band_h_ratio"] < 0.25, r


def test_detect_keeps_tight_band_when_position_fixed():
    """位置固定时不许被"并集"改胖：固定片子的带应与中位数口径一致（约 24/270 + padding）。"""
    imgs = [_mk_frame(band_top=228) for _ in range(10)]
    r = subtitle_band.detect_band_ratio(imgs)
    assert r["found"] is True, r
    assert r["band_h_ratio"] < 0.13, r          # 24/270=0.089 + 8% padding ≈ 0.096
    assert 0.78 < r["band_y_ratio"] < 0.845, r


def test_per_frame_empty_when_not_found():
    """未达门槛时 per_frame 必须是空列表：整片结论都不可信，零星噪声更不该拿去跟幕。"""
    imgs = [_mk_frame(band_top=214)] + [_mk_frame() for _ in range(9)]
    r = subtitle_band.detect_band_ratio(imgs)
    assert r["found"] is False, r
    assert r.get("per_frame") == [], r


def test_per_frame_tracks_each_scene():
    """per_frame 与入参**索引对齐**，且给出的是**该帧自己**的带 —— 前端「跟幕」全靠它。

    2026-09-18 第三轮：用户「有部分字预览没有擦除」修完之后接着问「我们不是有探测
    原字幕的设计吗？根据这幕来羽化吗，为什么没有产生作用？」——光回一条全片聚合带不够：
    同一片内字幕位置会变，预览必须能取到**每一帧/每一幕**的带才行。
    """
    # ⚠️ 两个位置都必须落在扫描区内（y0 = int(h*0.70) = 189）：低于 189 的字会被
    #    扫描起点截断，逐帧带自然盖不全 —— 那是用法问题，不是跟幕的问题。
    tops = [204 if i % 2 == 0 else 244 for i in range(12)]
    imgs = [_mk_frame(band_top=t) for t in tops]
    r = subtitle_band.detect_band_ratio(imgs)
    assert r["found"] is True, r
    pf = r.get("per_frame")
    assert isinstance(pf, list) and len(pf) == len(imgs), (len(pf) if pf else pf, len(imgs))
    h = 270
    for i, t in enumerate(tops):
        assert pf[i]["found"] is True, (i, pf[i])
        y = pf[i]["band_y_ratio"] * h
        bh = pf[i]["band_h_ratio"] * h
        # 逐帧带必须单独贴合自己那一帧的字（含 pad），既不能漏也不能是别的帧的带
        assert y <= t + 1, (i, y, t)
        assert y + bh >= t + 24 - 1, (i, y, bh, t)
    # 关键：两处必须有明显落差 —— 若相等就说明退化回了「全片一条带」
    assert abs(pf[0]["band_y_ratio"] - pf[1]["band_y_ratio"]) * h > 30, pf[:2]


def test_mark_matches_pipeline():
    # 关键常量与管线同源：改任一处都要同步，否则预览与成片位置会漂移
    src = open(os.path.join(_SERVER_DIR, "subtitle_band.py"), encoding="utf-8").read()
    assert "0.70" in src, "起扫位置应为画面高 70%（同管线 y0 = int(h0 * 0.70)）"
    assert ">= 205" in src, "白字阈值应与管线一致（近白像素 >= 205）"
    assert "0.08" in src, "带外扩比例应与管线一致（字幕高的 8%）"
    assert "0.25" in src, ">25% 的异常帧丢弃口径应与管线一致"
    assert "0.045" in src, "双行合并阈值应与管线 v9 一致（max(6 行, 4.5%)）"
    assert "0.15" in src, "亮场景溢出守卫应与管线一致（贴扫描区顶且高 >15% 弃帧）"
    # 2026-09-18：聚合口径从「纯中位数」升级为「鲁棒并集 + 增长上限」，
    # 管线的 _prepare_feather 与自适应那处必须用同一个 _GROW_CAP=0.06
    assert "_GROW_CAP = 0.06" in src, "鲁棒并集的增长上限 0.06 必须显式写死，供管线对照同步"
    assert "_percentile" in src, "并集用 p10/p90 分位，别退回 min/max（会被单点撑大）"


_TESTS = [
    test_detect_finds_white_text_band,
    test_detect_clean_frame_returns_not_found,
    test_detect_requires_two_hits,
    test_detect_10frames_needs_three_hits,
    test_detect_ignores_oversized_band,
    test_detect_pads_band_slightly,
    test_band_ratio_independent_of_resolution,
    test_band_rows_rejects_bright_spill,
    test_band_rows_keeps_short_band_touching_scan_top,
    test_detect_covers_varying_subtitle_position,
    test_detect_keeps_tight_band_when_position_fixed,
    test_mark_matches_pipeline,
    test_per_frame_empty_when_not_found,
    test_per_frame_tracks_each_scene,
]

if __name__ == "__main__":
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} 通过")
    raise SystemExit(1 if failed else 0)
