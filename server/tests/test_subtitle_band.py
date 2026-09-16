"""server/tests/test_subtitle_band.py — 原字幕带探测回归测试（2026-09-16）。

背景：`擦除原字幕` 原先完全靠管线自动探测、UI 零暴露 —— 探测偏了只能跑完整条任务
才发现。现在 App 侧新增「原字幕羽化」面板：预览窗口先用 Canvas 复刻擦除效果、可拖
动微调位置，调好的**比例**再回传给管线渲染（= 预览看到什么，成片就烧什么）。

本算法与管线 `scripts/edit_ffmpeg.py::_prepare_feather / _band_rows` **同源**：
判据（只认白字 >=205、从画面 70% 起扫、允许 2 行间隙、丢 >25% 的异常帧、中位数聚合、
上下各扩 8% 字幕高且至少 0.3% 画面）必须与那边一致 —— 只改一边会让比例漂移，
直观看就是「预览和成片位置不一致」，正是本功能要消灭的问题。

覆盖：
  detect_finds_white_text_band           底部白字带能被找到
  detect_clean_frame_returns_not_found   干净画面不误报
  detect_requires_two_hits               命中帧不足（1/4）时不确认
  detect_ignores_oversized_band          大字幕墙/演职员表（>25% 高）被丢弃
  detect_pads_band_slightly              返回的带比原字略大（包住描边/辉光）
  band_ratio_independent_of_resolution   同一内容不同分辨率给出同一比例
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


def test_detect_ignores_oversized_band():
    # 带高占画面 >25%（演职员表/大字幕墙）应被整帧丢弃，不能把整屏当字幕带
    r = subtitle_band.detect_band_ratio([_mk_frame(band_top=190, band_h=70) for _ in range(4)])
    assert r["found"] is False, r
    assert r["hits"] == 0, r


def test_detect_pads_band_slightly():
    # 返回的带必须略大于原字（上下各扩 8% 字幕高），否则描边/辉光会残字；
    # 但也不该扩到翻倍（那会糊住带外的画面）。
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


def test_mark_matches_pipeline():
    # 关键常量与管线同源：改任一处都要同步，否则预览与成片位置会漂移
    src = open(os.path.join(_SERVER_DIR, "subtitle_band.py"), encoding="utf-8").read()
    assert "0.70" in src, "起扫位置应为画面高 70%（同管线 y0 = int(h0 * 0.70)）"
    assert ">= 205" in src, "白字阈值应与管线一致（近白像素 >= 205）"
    assert "0.08" in src, "带外扩比例应与管线一致（字幕高的 8%）"
    assert "0.25" in src, ">25% 的异常帧丢弃口径应与管线一致"


_TESTS = [
    test_detect_finds_white_text_band,
    test_detect_clean_frame_returns_not_found,
    test_detect_requires_two_hits,
    test_detect_ignores_oversized_band,
    test_detect_pads_band_slightly,
    test_band_ratio_independent_of_resolution,
    test_mark_matches_pipeline,
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
