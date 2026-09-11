"""chroma-key（纯色背景色度键）算法前向离线回归测试（2026-09-11 新增）。

chroma-key 是 algorithm=True 引擎——无 ML 权重、纯 OpenCV 算法（边缘估计背景色 →
颜色距离遮罩 → 软边羽化 → 强去溢色 despill → 用户选区硬边界）。它**不走** ONNX
session，所以不能用「假 session 注入」法，而是直接验证**真实算法的数值行为**：

  - 纯色/近似纯色背景（绿幕）应被透明化（alpha≈0）
  - 与背景异色的主体（红块）应保持不透明（alpha≈255）
  - 用户框选 (box, 归一化 x,y,w,h) 作为硬边界：框外区域强制透明

构造「绿边 + 红心」图：边缘像素全是绿 → 背景色 key=(0,1,0)；中心红块与 key 颜色距离
大 → 不透明；绿边与 key 距离 0 → 透明。这正好命中 chroma-key 的正解场景。

运行：
    cd server && python tests/test_matting_chroma_key.py
    cd server && python -m pytest tests/test_matting_chroma_key.py -v
"""
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import matting_ai as m  # noqa: E402


def _green_border_red_center(size=120, border=12):
    """整图红心 + 绿色边框：边缘=绿(背景色)，内部=红(主体)。"""
    img = Image.new("RGB", (size, size), (255, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, size - 1, border - 1], fill=(0, 255, 0))
    d.rectangle([0, size - border, size - 1, size - 1], fill=(0, 255, 0))
    d.rectangle([0, 0, border - 1, size - 1], fill=(0, 255, 0))
    d.rectangle([size - border, 0, size - 1, size - 1], fill=(0, 255, 0))
    return img


def test_chroma_key_bg_transparent_subject_opaque():
    """绿边背景 → 透明（alpha 近 0）；红心主体 → 不透明（alpha 近 255）。"""
    img = _green_border_red_center(120, 12)
    rgba = m._matting_chroma_key(img, 120, 120)
    assert rgba.mode == "RGBA"
    alpha = np.array(rgba)[..., 3]
    # 角落属于绿边框 → 应透明
    bg_a = int(alpha[2, 2])
    # 中心属于红心 → 应不透明
    fg_a = int(alpha[60, 60])
    assert bg_a < 30, ("背景未被透明化", bg_a)
    assert fg_a > 220, ("主体未被保留", fg_a)
    print("✅ chroma-key：纯色背景透明(alpha=%d)、异色主体不透明(alpha=%d)" % (bg_a, fg_a))


def test_chroma_key_box_hard_boundary():
    """框选 (x,y,w,h) 归一化 = 中心 50%：框内红心保持不透明，框外红心被强制透明。"""
    img = _green_border_red_center(120, 12)
    # 归一化 box 覆盖中心区域：x∈[0.25,0.75], y∈[0.25,0.75]
    box = (0.25, 0.25, 0.5, 0.5)
    rgba = m._matting_chroma_key(img, 120, 120, box=box)
    alpha = np.array(rgba)[..., 3]
    # 框内左中 (x=30 在 [25,75] 内) 红心 → 不透明
    inside_a = int(alpha[60, 30])
    # 框外右中 (x=95 在 [25,75] 外) 红心 → 被框强制透明
    outside_a = int(alpha[60, 95])
    assert inside_a > 200, ("框内主体被错误透明化", inside_a)
    assert outside_a < 30, ("框外未强制透明", outside_a)
    print("✅ chroma-key box 硬边界：框内不透明(%d)、框外强制透明(%d)" % (inside_a, outside_a))


def test_chroma_key_uniform_background_is_fully_transparent():
    """整图纯色（无主体）时，背景色估计命中全图 → 整图应被键成透明。"""
    img = Image.new("RGB", (100, 100), (0, 200, 0))  # 纯绿幕
    rgba = m._matting_chroma_key(img, 100, 100)
    alpha = np.array(rgba)[..., 3]
    assert int(alpha.mean()) < 20, ("纯色背景未整体透明", int(alpha.mean()))
    print("✅ chroma-key：纯色幕布整图透明(alpha均值=%d)" % int(alpha.mean()))


if __name__ == "__main__":
    test_chroma_key_bg_transparent_subject_opaque()
    test_chroma_key_box_hard_boundary()
    test_chroma_key_uniform_background_is_fully_transparent()
    print("\n🎉 chroma-key 算法前向测试全部通过（3 项）")
