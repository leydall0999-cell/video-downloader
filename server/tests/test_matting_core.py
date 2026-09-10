"""一键抠图核心纯函数回归测试（2026-09-10 新增）。

背景：matting_ai.py 是一键抠图的核心（184 KB），承载前处理、选区解析、
边缘柔化等关键逻辑，是用户直接感知出图质量的路径。此前零测试保护。

本测试只覆盖**纯函数**部分（不加载任何模型权重、不联网、不推理），
聚焦最易回归、也最容易静默出错的三类逻辑：

  1. 前处理 _preprocess —— 缩放到固定尺寸 + ImageNet 归一化（模型输入的生命线，
     归一化写错会导致出图整体发灰/发白，且不会报错）
  2. 选区解析 _normalize_box / _norm_box_from_inputs —— 矩形框归一化坐标
     转像素、有效性判定、多来源选区优先级
  3. 边缘柔化 _edge_soften_mask —— 硬边变自然过渡（直接影响观感）与安全边界

运行：
    cd server && python tests/test_matting_core.py
    cd server && python -m pytest tests/test_matting_core.py -v
"""
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import matting_ai as m  # noqa: E402


# --------------------------------------------------------------------------- #
# _preprocess —— 模型输入前处理（归一化写错会让出图整体色偏且不报错）
# --------------------------------------------------------------------------- #
def test_preprocess_output_shape_and_dtype():
    """输出必须是 (1,3,H,W) float32 CHW 张量。"""
    img = Image.new("RGB", (800, 600), (128, 64, 32))
    t = m._preprocess(img, (1024, 1024), norm="255")
    assert t.shape == (1, 3, 1024, 1024), t.shape
    assert t.dtype == np.float32, t.dtype
    print("✅ _preprocess 输出 (1,3,1024,1024) float32")


def test_preprocess_norm255_uses_imagenet_normalization():
    """norm='255'：先 ÷255，再按 ImageNet mean/std 归一化。

    全白图（255）除以 255 得 1.0，故 R 通道应等于 (1 - mean_R) / std_R。
    """
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    t = m._preprocess(img, (64, 64), norm="255")
    for c, (mean, std) in enumerate(zip(m._MEAN, m._STD)):
        expected = (1.0 - mean) / std
        assert abs(float(t[0, c].mean()) - expected) < 1e-3, (c, float(t[0, c].mean()), expected)
    print("✅ norm='255' 正确套用 ImageNet 归一化")


def test_preprocess_max_norm_divides_by_global_max():
    """norm='max'：÷全图最大像素值（BiRefNet/rembg 风格）。

    灰度 (128,64,32) 的 max 为 128 → 归一后 R=1.0, G=0.5, B=0.25，再套 ImageNet。
    最后除以最大通道值，故最大通道（R）应落在 (1-mean_R)/std_R 附近。
    """
    img = Image.new("RGB", (64, 64), (128, 64, 32))
    t = m._preprocess(img, (64, 64), norm="max")
    expected_r = (1.0 - m._MEAN[0]) / m._STD[0]
    assert abs(float(t[0, 0].mean()) - expected_r) < 1e-3, float(t[0, 0].mean())
    print("✅ norm='max' 按全图最大值归一化")


def test_preprocess_converts_non_rgb_input():
    """灰度/带透明通道输入也必须被转换为 RGB 三通道，不能抛异常。"""
    gray = Image.new("L", (32, 32), 100)
    t = m._preprocess(gray, (16, 16), norm="255")
    assert t.shape == (1, 3, 16, 16), t.shape
    rgba = Image.new("RGBA", (32, 32), (10, 20, 30, 128))
    t2 = m._preprocess(rgba, (16, 16), norm="255")
    assert t2.shape == (1, 3, 16, 16), t2.shape
    print("✅ 灰度/RGBA 输入被正确转为 RGB 三通道")


# --------------------------------------------------------------------------- #
# _sigmoid
# --------------------------------------------------------------------------- #
def test_sigmoid_basic():
    """sigmoid(0)=0.5，单调递增，饱和于 0/1。"""
    assert abs(float(m._sigmoid(0)) - 0.5) < 1e-9
    assert float(m._sigmoid(-20)) < 1e-6
    assert abs(float(m._sigmoid(20)) - 1.0) < 1e-6
    vals = [float(m._sigmoid(v)) for v in (-3, -1, 0, 1, 3)]
    assert vals == sorted(vals), vals
    print("✅ _sigmoid 中点/单调性/饱和性正确")


# --------------------------------------------------------------------------- #
# _normalize_box —— 归一化 (x,y,w,h) → 像素 (x0,y0,x1,y1)
# --------------------------------------------------------------------------- #
def test_normalize_box_valid_conversion():
    """全图框 → 满尺寸；半图框 → 正确像素坐标。"""
    assert m._normalize_box([0, 0, 1, 1], 100, 100) == (0, 0, 100, 100)
    assert m._normalize_box([0.25, 0.25, 0.5, 0.5], 100, 100) == (25, 25, 75, 75)
    assert m._normalize_box([0.1, 0.2, 0.4, 0.5], 200, 100) == (20, 20, 100, 70)
    print("✅ _normalize_box 归一化框转像素正确")


def test_normalize_box_rejects_invalid():
    """空框 / 面积过小 / 零宽高 / 起点越界 → None（走整图推理）。"""
    assert m._normalize_box(None, 100, 100) is None
    assert m._normalize_box([], 100, 100) is None
    assert m._normalize_box([0, 0, 0.01, 0.01], 100, 100) is None      # 面积 < 0.001
    assert m._normalize_box([0.1, 0.1, 0, 0.5], 100, 100) is None      # 零宽
    assert m._normalize_box([0.1, 0.1, 0.5, 0], 100, 100) is None      # 零高
    assert m._normalize_box([1.5, 0.1, 0.2, 0.2], 100, 100) is None    # x 起点越界
    assert m._normalize_box([0.1, 1.5, 0.2, 0.2], 100, 100) is None    # y 起点越界
    print("✅ _normalize_box 非法框一律返回 None")


def test_normalize_box_clamps_overflow():
    """框超出右下边界时夹紧到图像范围内，不得越界。"""
    got = m._normalize_box([0.9, 0.9, 0.5, 0.5], 100, 100)
    assert got == (90, 90, 100, 100), got
    print("✅ _normalize_box 溢出框被夹紧到图像边界")


def test_normalize_box_rejects_too_small_pixel_span():
    """换算后任一边长 < 8px 视为误点，返回 None。"""
    assert m._normalize_box([0.5, 0.5, 0.05, 0.05], 100, 100) is None   # 5px × 5px
    got = m._normalize_box([0.5, 0.5, 0.1, 0.1], 100, 100)              # 10px × 10px
    assert got == (50, 50, 60, 60), got
    print("✅ _normalize_box 过小像素框被拒绝")


# --------------------------------------------------------------------------- #
# _norm_box_from_inputs —— 多来源选区优先级
# --------------------------------------------------------------------------- #
def test_norm_box_priority_vision_first():
    """优先级：VLM 视觉框 > 套索 polygon > 矩形框。"""
    vision = m._norm_box_from_inputs([0.6, 0.6, 0.2, 0.2], [[0, 0], [1, 1], [0.5, 0.5]], [0, 0, 1, 1])
    assert vision == [0.2, 0.2, 0.6, 0.6], vision   # vision_box 胜出并归一为 min/max
    poly = m._norm_box_from_inputs(None, [[0.1, 0.1], [0.5, 0.2], [0.3, 0.9]], [0, 0, 1, 1])
    assert poly == [0.1, 0.1, 0.5, 0.9], poly        # polygon 胜出，取包围盒
    print("✅ 选区优先级 vision_box > polygon > box 正确")


def test_norm_box_box_is_xywh():
    """矩形框入参是 (x,y,w,h)，需转成 (x1,y1,x2,y2) 以统一语义。"""
    got = m._norm_box_from_inputs(None, None, [0.1, 0.2, 0.3, 0.4])
    assert got[0] == 0.1 and got[1] == 0.2
    assert abs(got[2] - 0.4) < 1e-9 and abs(got[3] - 0.6) < 1e-9, got
    print("✅ 矩形框 (x,y,w,h) 正确转 (x1,y1,x2,y2)")


def test_norm_box_all_empty_returns_none():
    """无任何选区 → None（语义为「抠全图主主体」）。"""
    assert m._norm_box_from_inputs(None, None, None) is None
    assert m._norm_box_from_inputs([], [], []) is None
    assert m._norm_box_from_inputs(None, [[0, 0], [1, 1]], None) is None   # polygon 少于 3 点
    print("✅ 无有效选区时返回 None")


# --------------------------------------------------------------------------- #
# _is_person_label —— 人像标签路由
# --------------------------------------------------------------------------- #
def test_is_person_label_detects_person():
    for lb in ("人像", "人物", "肖像", "a person", "portrait", "young woman", "小孩"):
        assert m._is_person_label(lb), lb
    print("✅ 人像标签被正确识别")


def test_is_person_label_rejects_non_person():
    for lb in ("猫", "cat", "产品", "car", "风景", "", None):
        assert not m._is_person_label(lb), lb
    print("✅ 非人像/空标签不被误判为人像")


# --------------------------------------------------------------------------- #
# _edge_soften_mask —— 边缘柔化
# --------------------------------------------------------------------------- #
def test_edge_soften_creates_transition():
    """硬边（0/255 一刀切）经处理后应产生中间过渡值，消除锯齿。"""
    arr = np.zeros((64, 64), dtype=np.uint8)
    arr[:, 32:] = 255
    src = Image.fromarray(arr, mode="L")
    out = m._edge_soften_mask(src)
    o = np.array(out)
    assert out.size == src.size and out.mode == "L"
    mid = o[(o > 0) & (o < 255)]
    assert mid.size > 0, "柔化后应出现中间过渡像素"
    print(f"✅ 硬边柔化产生 {int(mid.size)} 个过渡像素（范围 {int(mid.min())}–{int(mid.max())}）")


def test_edge_soften_no_edge_returns_original():
    """纯前景/纯背景无边缘环带，应原样返回，不引入任何修改。"""
    allfg = Image.new("L", (32, 32), 255)
    assert m._edge_soften_mask(allfg) is allfg
    allbg = Image.new("L", (32, 32), 0)
    assert sorted(set(np.array(m._edge_soften_mask(allbg)).ravel().tolist())) == [0]
    print("✅ 无边缘时原样返回，不误改")


def test_edge_soften_preserves_interior():
    """柔化只作用于边界环带，前景内部与背景深处必须保持原值。"""
    arr = np.zeros((64, 64), dtype=np.uint8)
    arr[:, 32:] = 255
    o = np.array(m._edge_soften_mask(Image.fromarray(arr, mode="L")))
    assert int(o[10, 60]) == 255, "前景深处不应被改动"
    assert int(o[10, 4]) == 0, "背景深处不应被改动"
    print("✅ 边缘柔化不污染前景内部与背景深处")


if __name__ == "__main__":
    test_preprocess_output_shape_and_dtype()
    test_preprocess_norm255_uses_imagenet_normalization()
    test_preprocess_max_norm_divides_by_global_max()
    test_preprocess_converts_non_rgb_input()
    test_sigmoid_basic()
    test_normalize_box_valid_conversion()
    test_normalize_box_rejects_invalid()
    test_normalize_box_clamps_overflow()
    test_normalize_box_rejects_too_small_pixel_span()
    test_norm_box_priority_vision_first()
    test_norm_box_box_is_xywh()
    test_norm_box_all_empty_returns_none()
    test_is_person_label_detects_person()
    test_is_person_label_rejects_non_person()
    test_edge_soften_creates_transition()
    test_edge_soften_no_edge_returns_original()
    test_edge_soften_preserves_interior()
    print("\n🎉 抠图核心纯函数测试全部通过（17 项）")
