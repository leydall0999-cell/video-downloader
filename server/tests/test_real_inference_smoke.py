"""
真实权重冒烟测试（@slow，CI 外额外保险）。

默认【不运行】：需显式设置环境变量 RUN_SLOW_REAL_INFERENCE=1 才执行；
权重文件不存在时自动 skip（reason 说明缺哪个文件）。

覆盖本机安全可加载的小权重：
  - modnet_photographic_portrait_matting.onnx (25MB)  → 抠图真实前向
  - lama_fp32.onnx (208MB)                            → 去水印真实前向

【重要限制】本机 8GB 内存 + swap 常年打满，972MB 的 BiRefNet 系列真实加载会
OOM，故默认不覆盖。如需验证 birefnet-general 真实前向，额外设 RUN_REAL_BIREFRNET=1
（仍需 RUN_SLOW_REAL_INFERENCE=1），并确保在 ≥16GB 内存机器上运行。

此文件【不接入 run_offline_tests.sh】（@slow 性质，CI/全量门禁默认不跑）。

触发方式：
  cd server && RUN_SLOW_REAL_INFERENCE=1 .build_venv/bin/python -m pytest \
      tests/test_real_inference_smoke.py -v -rs
单独验证 birefnet：
  RUN_SLOW_REAL_INFERENCE=1 RUN_REAL_BIREFRNET=1 .build_venv/bin/python -m pytest \
      tests/test_real_inference_smoke.py -v -rs
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pytest
from PIL import Image, ImageDraw

import matting_ai as m
import dewatermark_ai as dw

RUN_SLOW = os.environ.get("RUN_SLOW_REAL_INFERENCE") == "1"
RUN_BIREFRNET = os.environ.get("RUN_REAL_BIREFRNET") == "1"


def _weights_present(*filenames):
    """探测 ~/.vdl_models（或 VDL_MODELS_DIR 下 .vdl_models / vdl_models）是否存在且 > 1MB。"""
    base = os.environ.get("VDL_MODELS_DIR")
    candidates = []
    if base:
        candidates.append(Path(base) / ".vdl_models")
        candidates.append(Path(base) / "vdl_models")
    candidates.append(Path.home() / ".vdl_models")
    for f in filenames:
        if not any((c / f).exists() and (c / f).stat().st_size > 1_000_000 for c in candidates):
            return False
    return True


_modnet_w = _weights_present("modnet_photographic_portrait_matting.onnx")
_lama_w = _weights_present("lama_fp32.onnx")
_birefnet_w = _weights_present("BiRefNet-general-epoch_244.onnx")


def _human_silhouette(size=256):
    """绿底 + 头椭圆 + 身矩形 的人形剪影（可触发 modnet 非平凡前景响应）。"""
    img = Image.new("RGB", (size, size), (0, 150, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([88, 30, 168, 120], fill=(220, 180, 150))   # 头
    d.rectangle([95, 115, 160, 230], fill=(60, 90, 200))  # 身
    return img


@pytest.mark.slow
@pytest.mark.skipif(not RUN_SLOW, reason="需设 RUN_SLOW_REAL_INFERENCE=1 才跑真实权重冒烟（CI 默认跳过）")
@pytest.mark.skipif(not _modnet_w, reason="modnet 权重缺失（~/.vdl_models/modnet_photographic_portrait_matting.onnx）")
def test_real_modnet_loads_and_infers():
    """modnet 真实权重：加载 + 前向不崩，且对人形剪影输出非平凡、方向正确。"""
    img = _human_silhouette(256)
    mask = m.predict_mask(img, "modnet-photographic")
    assert mask.mode == "L", mask.mode
    assert mask.size == (256, 256), mask.size
    arr = np.array(mask)
    assert arr.dtype == np.uint8
    # 非平凡：真实权重确实在推理（不是全 0 / NaN）
    assert int(arr.max()) > 100, ("真实权重未产生显著前景，疑似权重未真正参与推理", int(arr.max()))
    # 方向性：头中心（前景）比背景角更不透明
    fg = int(arr[75, 128])
    bg = int(arr[8, 8])
    assert fg > 50, ("头中心应明显不透明", fg)
    assert fg > bg, ("头应比背景不透明（方向性）", fg, bg)
    print("✅ modnet 真实权重前向：加载+推理 OK，人形剪影输出方向正确（fg=%d bg=%d）" % (fg, bg))


@pytest.mark.slow
@pytest.mark.skipif(not RUN_SLOW, reason="需设 RUN_SLOW_REAL_INFERENCE=1 才跑真实权重冒烟（CI 默认跳过）")
@pytest.mark.skipif(not _lama_w, reason="lama 权重缺失（~/.vdl_models/lama_fp32.onnx）")
def test_real_lama_loads_and_inpaints():
    """lama_fp32 真实权重：加载 + 去水印前向，水印区改写、非水印区零改动。"""
    h = w = 512
    img_bgr = np.zeros((h, w, 3), np.float32)        # 黑底
    img_bgr[200:312, 200:312] = (255, 255, 255)      # 中心白块水印
    mask2d = np.zeros((h, w), np.float32)
    mask2d[200:312, 200:312] = 255.0
    out = dw._inpaint_bgr_to_bgr(img_bgr, mask2d)
    assert out.shape == (h, w, 3), out.shape
    assert out.dtype == np.uint8
    assert out.min() >= 0 and out.max() <= 255
    # 水印区被真实 LaMa 改写（与输入不同）；非水印角逐字节不变
    assert np.any(out != img_bgr), "水印区未被真实 LaMa 改写"
    assert np.array_equal(out[10, 10], img_bgr[10, 10]), "非水印角被意外改动"
    print("✅ lama_fp32 真实权重前向：水印区改写、非水印区零改动")


@pytest.mark.slow
@pytest.mark.skipif(not RUN_SLOW, reason="需设 RUN_SLOW_REAL_INFERENCE=1")
@pytest.mark.skipif(not RUN_BIREFRNET, reason="需额外设 RUN_REAL_BIREFRNET=1（972MB 权重，≥16GB 内存）")
@pytest.mark.skipif(not _birefnet_w, reason="birefnet-general 权重缺失")
def test_real_birefnet_general_loads_and_infers():
    """birefnet-general 真实权重（972MB，默认不跑，避免 OOM）。绿底红块→红块不透明、绿底透明。"""
    img = Image.new("RGB", (96, 96), (0, 150, 0))
    ImageDraw.Draw(img).rectangle([32, 32, 64, 64], fill=(200, 0, 0))
    mask = m.predict_mask(img, "birefnet-general")
    arr = np.array(mask)
    assert int(arr[48, 48]) > 100, ("红块中心应不透明", int(arr[48, 48]))
    assert int(arr[4, 4]) < 100, ("绿底角应透明", int(arr[4, 4]))
    print("✅ birefnet-general 真实权重前向：方向正确")


if __name__ == "__main__":
    if not RUN_SLOW:
        print("⏭️  真实权重冒烟测试已跳过：需设 RUN_SLOW_REAL_INFERENCE=1 才执行（默认 CI 不跑）")
        sys.exit(0)
    import pytest as _pytest
    sys.exit(_pytest.main([__file__, "-v", "-rs"]))
