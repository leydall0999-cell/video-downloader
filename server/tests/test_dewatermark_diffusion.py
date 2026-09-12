"""server/tests/test_dewatermark_diffusion.py — 扩散去水印模块的能力分级与 import 守护测试。

不依赖真实 torch/diffusers/权重（CI 与低配机均无），只验证：
- 内存门槛分级（<16GB 整体不可用；16~32 仅 sd15；>=32 含 sdxl）。
- torch/diffusers 缺失时 available() 返回 False（import 守护，不拖垮低配机）。
- 真实推理函数 ai_image_inpaint 在不可用时抛 RuntimeError（不静默崩）。
"""
import importlib.util
import os
import sys
import unittest
from unittest import mock

# 让测试在 server/tests 下也能 import 兄弟模块（与 test_dewatermark_forward.py 同款写法）
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)
import dewatermark_diffusion as dwd  # noqa: E402


class TestDiffusionCapability(unittest.TestCase):
    def _patch(self, ram_gb, torch_present, diffusers_present):
        """统一构造：内存 + torch/diffusers 可用性。

        find_spec 真实语义：模块存在返回 ModuleSpec（真值），不存在返回 None。
        故这里用 None 表示缺失、用 object() 表示存在，严格对齐 available() 的 `is None` 判断。
        """
        def _fake_find_spec(name):
            present = (name == "torch" and torch_present) or (name == "diffusers" and diffusers_present)
            return object() if present else None

        return mock.patch.object(
            dwd, "_total_ram_gb", return_value=ram_gb
        ), mock.patch.object(
            importlib.util, "find_spec", side_effect=_fake_find_spec
        )

    def test_low_ram_unavailable_even_with_torch(self):
        # 8GB 机器：即使装了 torch，也因内存门槛被拒（避免 OOM 拖垮 app）
        p1, p2 = self._patch(8.0, True, True)
        with p1, p2:
            self.assertFalse(dwd.available())
            self.assertEqual(dwd.list_diffusion_models(), [])

    def test_high_ram_needs_torch(self):
        # 16GB 但没装 torch/diffusers → 不可用（import 守护）
        p1, p2 = self._patch(16.0, False, False)
        with p1, p2:
            self.assertFalse(dwd.available())

    def test_16gb_with_torch_only_sd15(self):
        p1, p2 = self._patch(16.0, True, True)
        with p1, p2:
            self.assertTrue(dwd.available())
            self.assertEqual(dwd.list_diffusion_models(), ["sd15"])

    def test_32gb_with_torch_sd15_and_sdxl(self):
        p1, p2 = self._patch(32.0, True, True)
        with p1, p2:
            self.assertTrue(dwd.available())
            self.assertEqual(dwd.list_diffusion_models(), ["sd15", "sdxl"])

    def test_missing_cv2_blocks_available(self):
        # cv2 缺失（import 守护）时即使内存/ torch 都够也不可用
        real_cv2 = dwd._cv2
        try:
            dwd._cv2 = None
            p1, p2 = self._patch(32.0, True, True)
            with p1, p2:
                self.assertFalse(dwd.available())
        finally:
            dwd._cv2 = real_cv2

    def test_inpaint_refuses_when_unavailable(self):
        # ai_image_inpaint 在不可用时抛 RuntimeError（不静默返回坏图）
        p1, p2 = self._patch(8.0, True, True)
        with p1, p2:
            with self.assertRaises(RuntimeError):
                dwd.ai_image_inpaint("x.png", "y.png", [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "op": "add"}])

    def test_model_registry_contract(self):
        # 注册表契约自检：每个模型都有必要字段
        for name, spec in dwd.MODELS_DIFFUSION.items():
            for k in ("repo", "subdir", "native_size", "min_ram_gb", "dtype"):
                self.assertIn(k, spec, f"{name} 缺 {k}")
            self.assertGreater(spec["native_size"], 0)
            self.assertGreaterEqual(spec["min_ram_gb"], dwd.DIFFUSION_MIN_RAM_GB)


if __name__ == "__main__":
    unittest.main()
