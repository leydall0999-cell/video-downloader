"""server/tests/test_dewatermark_diffusion.py — 扩散去水印模块测试。

不依赖真实 torch/diffusers/权重（CI 与低配机均无），只验证：
- 内存门槛分级（<16GB 整体不可用；16~32 仅 sd15；>=32 含 sdxl）。
- torch/diffusers 缺失时 available() 返回 False（import 守护，不拖垮低配机）。
- 真实推理函数 ai_image_inpaint 在不可用时抛 RuntimeError（不静默崩）。
- 【按需下载】diffusion_supported（硬件门）/ runtime_installed（运行库是否就位）语义。
- 运行库安装编排（resolve→download→extract）与最小 wheel 安装器（解压 + .so 加执行位 + 标记）。
- 能力探测：扩散档在「硬件支持但未安装」时选项可选但默认不推荐（避免一打开就静默下载 2GB）。
"""
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

# 让测试在 server/tests 下也能 import 兄弟模块（与 test_dewatermark_forward.py 同款写法）
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)
import dewatermark_diffusion as dwd  # noqa: E402
import capability as cap  # noqa: E402


class TestDiffusionCapability(unittest.TestCase):
    def _patch(self, ram_gb, torch_present, diffusers_present):
        """统一构造：内存 + torch/diffusers 可用性。

        find_spec 真实语义：模块存在返回 ModuleSpec（真值），不存在返回 None。
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
        p1, p2 = self._patch(8.0, True, True)
        with p1, p2:
            self.assertFalse(dwd.available())
            self.assertEqual(dwd.list_diffusion_models(), [])

    def test_high_ram_needs_torch(self):
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
        real_cv2 = dwd._cv2
        try:
            dwd._cv2 = None
            p1, p2 = self._patch(32.0, True, True)
            with p1, p2:
                self.assertFalse(dwd.available())
        finally:
            dwd._cv2 = real_cv2

    def test_inpaint_refuses_when_unavailable(self):
        p1, p2 = self._patch(8.0, True, True)
        with p1, p2:
            with self.assertRaises(RuntimeError):
                dwd.ai_image_inpaint("x.png", "y.png", [{"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, "op": "add"}])

    def test_model_registry_contract(self):
        for name, spec in dwd.MODELS_DIFFUSION.items():
            for k in ("repo", "subdir", "native_size", "min_ram_gb", "dtype"):
                self.assertIn(k, spec, f"{name} 缺 {k}")
            self.assertGreater(spec["native_size"], 0)
            self.assertGreaterEqual(spec["min_ram_gb"], dwd.DIFFUSION_MIN_RAM_GB)


class TestDiffusionOnDemandRuntime(unittest.TestCase):
    """按需下载运行库（torch/diffusers 栈，约 2GB）的安装编排与最小 wheel 安装器。"""

    def test_diffusion_supported_ram_gate(self):
        with mock.patch.object(dwd, "_total_ram_gb", return_value=8.0):
            self.assertFalse(dwd.diffusion_supported())
        with mock.patch.object(dwd, "_total_ram_gb", return_value=16.0):
            self.assertTrue(dwd.diffusion_supported())
        with mock.patch.object(dwd, "_total_ram_gb", return_value=32.0):
            self.assertTrue(dwd.diffusion_supported())

    def test_runtime_installed_false_without_torch(self):
        # 测试环境无 torch/diffusers → 应返回 False（低配机永不触发下载）
        self.assertFalse(dwd.runtime_installed())

    def test_pick_wheel_prefers_platform_over_linux(self):
        urls = [
            {"filename": "torch-2.5.1-cp313-cp313-manylinux1_x86_64.whl", "packagetype": "bdist_wheel"},
            {"filename": "torch-2.5.1-cp313-cp313-macosx_11_0_arm64.whl", "packagetype": "bdist_wheel"},
        ]
        pick = dwd._pick_wheel(urls, "cp313", "macosx_11_0_arm64")
        self.assertEqual(pick["filename"], "torch-2.5.1-cp313-cp313-macosx_11_0_arm64.whl")

    def test_pick_wheel_pure_python(self):
        urls = [{"filename": "filelock-3.16.1-py3-none-any.whl", "packagetype": "bdist_wheel"}]
        self.assertEqual(dwd._pick_wheel(urls, "cp313", "macosx_11_0_arm64")["filename"],
                         "filelock-3.16.1-py3-none-any.whl")

    def test_pick_wheel_rejects_no_wheel(self):
        self.assertIsNone(dwd._pick_wheel([{"filename": "x.tar.gz", "packagetype": "sdist"}], "cp313", "macosx"))

    def test_install_wheel_extracts_and_chmods(self):
        # 构造伪 wheel，验证最小安装器：解压 .py/.so + .so 加执行位 + 写已安装标记
        tmp = tempfile.mkdtemp()
        lib = os.path.join(tmp, "diff_lib")
        wheel = os.path.join(tmp, "fakepkg-1.0-py3-none-any.whl")
        with zipfile.ZipFile(wheel, "w") as z:
            z.writestr("fakepkg/__init__.py", "X = 1\n")
            z.writestr("fakepkg/_c.so", b"\x00\x01\x02")
            z.writestr("fakepkg-1.0.dist-info/METADATA", "name: fakepkg\n")
        with mock.patch.object(dwd, "RUNTIME_LIB_DIR", __import__("pathlib").Path(lib)):
            dwd._install_wheel(__import__("pathlib").Path(wheel), __import__("pathlib").Path(lib), None)
        self.assertTrue(os.path.exists(os.path.join(lib, "fakepkg", "__init__.py")))
        so = os.path.join(lib, "fakepkg", "_c.so")
        self.assertTrue(os.path.exists(so))
        self.assertEqual((open(os.path.join(lib, "fakepkg", "__init__.py")).read()), "X = 1\n")
        self.assertTrue(stat.S_IMODE(os.stat(so).st_mode) & stat.S_IXUSR)
        self.assertTrue(os.path.exists(os.path.join(lib, ".installed", "fakepkg-1.0-py3-none-any.whl-done")))

    def test_ensure_runtime_orchestrates_install(self):
        # 编排：遍历清单 → resolve/download/extract；未安装时触发；安装后 runtime_installed 变 True
        tmp = tempfile.mkdtemp()
        lib = os.path.join(tmp, "diff_lib")
        calls = []

        def _fake_resolve(name, version, index_url):
            return {"name": name, "version": version, "url": "http://x/" + name,
                    "sha256": None, "filename": f"{name}-{version}-py3-none-any.whl", "size": 10}

        def _fake_download(meta, libdir, cb):
            calls.append(("dl", meta["name"]))
            return __import__("pathlib").Path(libdir) / "wheels" / meta["filename"]

        def _fake_install(wp, libdir, cb):
            calls.append(("ex", wp.name))

        states = {"n": 0}

        def _rt():
            states["n"] += 1
            return states["n"] > 1

        with mock.patch.object(dwd, "RUNTIME_LIB_DIR", __import__("pathlib").Path(lib)), \
             mock.patch.object(dwd, "_total_ram_gb", return_value=32.0), \
             mock.patch.object(dwd, "runtime_installed", _rt), \
             mock.patch.object(dwd, "_resolve_wheel", _fake_resolve), \
             mock.patch.object(dwd, "_download_wheel", _fake_download), \
             mock.patch.object(dwd, "_install_wheel", _fake_install):
            ok = dwd.ensure_diffusion_runtime()
        self.assertTrue(ok)
        # 每个依赖都经历了「下载 + 解压」
        self.assertEqual(len([c for c in calls if c[0] == "ex"]), len(dwd._RUNTIME_REQUIREMENTS))

    def test_ensure_runtime_low_ram_raises(self):
        with mock.patch.object(dwd, "_total_ram_gb", return_value=8.0), \
             mock.patch.object(dwd, "runtime_installed", lambda: False):
            with self.assertRaises(RuntimeError):
                dwd.ensure_diffusion_runtime()

    def test_ensure_runtime_skip_when_already_installed(self):
        # 所有依赖标记已存在 → 不触发任何下载/解压，直接返回 True
        tmp = tempfile.mkdtemp()
        lib = os.path.join(tmp, "diff_lib")
        os.makedirs(os.path.join(lib, ".installed"))
        for (name, version) in dwd._RUNTIME_REQUIREMENTS:
            with open(os.path.join(lib, ".installed", f"{name}-{version}-done"), "w") as fh:
                fh.write("ok")
        calls = []

        def _fake_resolve(name, version, index_url):
            calls.append(("r", name))
            return {}

        with mock.patch.object(dwd, "RUNTIME_LIB_DIR", __import__("pathlib").Path(lib)), \
             mock.patch.object(dwd, "_total_ram_gb", return_value=32.0), \
             mock.patch.object(dwd, "runtime_installed", lambda: True), \
             mock.patch.object(dwd, "_resolve_wheel", _fake_resolve):
            ok = dwd.ensure_diffusion_runtime()
        self.assertTrue(ok)
        self.assertEqual(calls, [], "已安装时不应再解析/下载")


class TestCapabilityDiffusionConsistency(unittest.TestCase):
    """能力探测文案必须与「扩散档到底能不能跑」严格一致（2026-09-12 修正的真 bug 回归）。

    按需下载语义：diffusion_available（=硬件支持）决定是否「选项可选」；
    recommended_engine 仅在「已安装」时才推荐 diffusion，未安装则推荐 auto（避免一打开就
    静默触发 2GB 下载）。
    """

    def _probe(self, ram_gb, supported, installed, models):
        with mock.patch.object(cap, "_total_ram_bytes", return_value=int(ram_gb * 1024 ** 3)):
            return cap.probe_capability(
                lama_available=True,
                diffusion_supported=supported,
                diffusion_installed=installed,
                diffusion_models=list(models),
            )

    def test_high_ram_supported_not_installed_option_enabled_recommend_auto(self):
        # 16GB 硬件支持但未装运行库 → 选项可选（diffusion_available True），但不推荐下载
        c = self._probe(16.0, True, False, ["sd15"])
        self.assertEqual(c["tier"], "high")
        self.assertTrue(c["diffusion_available"])
        self.assertTrue(c["engines"]["diffusion"])
        self.assertFalse(c["diffusion_installed"])
        self.assertEqual(c["recommended_engine"], "auto")
        # 文案须说明「首次选择会下载」，且不得出现「推荐默认扩散」的自相矛盾措辞
        self.assertIn("自动下载", c["recommendation"])

    def test_extreme_ram_supported_not_installed_option_enabled_recommend_auto(self):
        c = self._probe(32.0, True, False, ["sd15", "sdxl"])
        self.assertEqual(c["tier"], "extreme")
        self.assertTrue(c["diffusion_available"])
        self.assertTrue(c["engines"]["diffusion"])
        self.assertEqual(c["recommended_engine"], "auto")
        self.assertIn("自动下载", c["recommendation"])

    def test_high_ram_installed_recommends_diffusion(self):
        c = self._probe(16.0, True, True, ["sd15"])
        self.assertTrue(c["diffusion_available"])
        self.assertTrue(c["diffusion_installed"])
        self.assertEqual(c["recommended_engine"], "diffusion")
        self.assertTrue(c["engines"]["diffusion"])
        self.assertEqual(c["diffusion_models"], ["sd15"])

    def test_extreme_ram_installed_recommends_diffusion_with_sdxl(self):
        c = self._probe(32.0, True, True, ["sd15", "sdxl"])
        self.assertEqual(c["recommended_engine"], "diffusion")
        self.assertEqual(c["diffusion_models"], ["sd15", "sdxl"])
        self.assertTrue(c["engines"]["diffusion"])

    def test_low_ram_diffusion_disabled(self):
        c = self._probe(8.0, False, False, [])
        self.assertFalse(c["diffusion_available"])
        self.assertFalse(c["engines"]["diffusion"])


if __name__ == "__main__":
    unittest.main()
