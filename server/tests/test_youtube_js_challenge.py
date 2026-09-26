#!/usr/bin/env python
"""YouTube JS 挑战（nsig）求解链：守卫测试（2026-09-27）

背景（来自竞品 DataTool 的静态分析 + 本机 A/B 实测）：
DataTool 之所以能稳定下 YouTube，靠的是**两半机制**，我们此前两半都缺：

  ① 会话自持：它每次都用自己浏览器里那份「活着」的会话（同机同 IP）；
     ⇒ VDL 侧由 downloader 的 `browser` Cookie 源负责（见 test_youtube_cookie_sources.py）。
  ② JS 挑战求解：它自带 deno + yt-dlp-ejs，并设置 `js_runtimes` / `remote_components`；
     ⇒ 本文件守护这一半。

为什么这半同样致命（本机实测，video=YK9a6TC9j54，同机/同代理/同 Cookie）：
    yt-dlp 的 `--js-runtimes` **默认只启用 deno**（yt_dlp/options.py），不会去
    「自动找 PATH 里任何一个 JS runtime」。机器上没有 deno 时 JS 挑战无人可解：
      实时会话 + 无 JS runtime → "The page needs to be reloaded"（SABR 流，直接卡死）
      实时会话 + deno + EJS   → ✅ 解析成功（35 个格式，含 4K）
      缓存快照 + deno + EJS   → 仍 bot 拦截（⇒ 会话那一半也不能少）
    ⇒ 只修一半等于没修。

本文件离线守卫（不发任何网络请求）：
1. `_find_js_runtime_binary()` 的查找顺序（环境变量 → _MEIPASS 内置 → PATH）；
2. `_js_challenge_options()` 只报「装好的 yt-dlp 真正支持的」runtime 与组件
   （`YoutubeDL._clean_js_runtimes()` 对不认识的名字会直接 ValueError，
    所以必须按注册表过滤，不能硬编码）；
3. `_base_options()` 把 JS 选项**只**挂给 YouTube，其它站点不受影响；
4. 依赖与打包不退化：requirements.txt 必须留着 yt-dlp-ejs / deno，
   两个 build 脚本必须真的把 deno 打进包（少一处 = 打包后静默回到「解不了挑战」）。
"""
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(SERVER_DIR)
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_yt_js")

import downloader as D  # noqa: E402


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class TestFindJsRuntimeBinary(unittest.TestCase):
    """runtime 二进制定位：显式指定 > 打包内置 > PATH。"""

    def setUp(self):
        self._old_env = os.environ.pop("VDL_JS_RUNTIME", None)
        self._old_meipass = getattr(sys, "_MEIPASS", None)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("VDL_JS_RUNTIME", None)
        else:
            os.environ["VDL_JS_RUNTIME"] = self._old_env
        if self._old_meipass is None:
            if hasattr(sys, "_MEIPASS"):
                try:
                    del sys._MEIPASS  # type: ignore[attr-defined]
                except Exception:
                    pass
        else:
            sys._MEIPASS = self._old_meipass  # type: ignore[attr-defined]

    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "mydeno"
            _make_executable(fake)
            os.environ["VDL_JS_RUNTIME"] = f"deno:{fake}"
            self.assertEqual(D._find_js_runtime_binary("deno"), str(fake))

    def test_env_override_for_other_runtime_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "mydeno"
            _make_executable(fake)
            os.environ["VDL_JS_RUNTIME"] = f"node:{fake}"
            # 指定的是 node，问 deno 时不该返回它（否则等于把 node 当 deno 用）
            self.assertNotEqual(D._find_js_runtime_binary("deno"), str(fake))

    def test_frozen_bundled_binary_found(self):
        """打包后 deno 在 <_MEIPASS>/bin/ 下（build_mac.sh 的 --add-binary …:bin）。"""
        with tempfile.TemporaryDirectory() as td:
            bundled = Path(td) / "bin" / "deno"
            _make_executable(bundled)
            sys._MEIPASS = td  # type: ignore[attr-defined]
            self.assertEqual(D._find_js_runtime_binary("deno"), str(bundled))

    def test_unknown_runtime_returns_none(self):
        self.assertIsNone(D._find_js_runtime_binary("definitely-not-a-runtime-xyz"))


class TestJsChallengeOptions(unittest.TestCase):
    """选项装配：只报支持的 runtime / 组件，形态符合 yt-dlp 要求。"""

    def test_only_supported_runtimes_are_reported(self):
        from yt_dlp.globals import supported_js_runtimes
        ok = set(supported_js_runtimes.value.keys())
        opts = D._js_challenge_options().get("js_runtimes") or {}
        self.assertTrue(set(opts) <= ok,
                        f"报了 yt-dlp 不支持的 runtime：{set(opts) - ok}。"
                        "YoutubeDL._clean_js_runtimes() 会直接 raise ValueError —— "
                        "等于打包后 YouTube 解析全挂。")

    def test_runtime_config_shape(self):
        opts = D._js_challenge_options().get("js_runtimes") or {}
        for name, cfg in opts.items():
            with self.subTest(runtime=name):
                self.assertIsInstance(cfg, dict, f"{name} 的配置必须是 dict")
                self.assertIn("path", cfg, f"{name} 必须带 path（否则 yt-dlp 会退回找 PATH）")
                self.assertTrue(os.path.isabs(cfg["path"]), f"{name} 的 path 必须是绝对路径")

    def test_ejs_component_declared(self):
        from yt_dlp.globals import supported_remote_components
        if "ejs:github" not in (supported_remote_components.value or []):
            self.skipTest("当前 yt-dlp 不支持 ejs:github")
        comps = D._js_challenge_options().get("remote_components")
        self.assertIsInstance(comps, list, "remote_components 必须是 list（yt-dlp 内部转 set）")
        self.assertIn("ejs:github", comps)


class TestBaseOptionsWiring(unittest.TestCase):
    """接线：YouTube 必须吃到 JS 选项，其它站点一点都不沾。"""

    def test_youtube_gets_js_challenge_options(self):
        expected = D._js_challenge_options()
        got = D._base_options(3, "youtube.com", cookie="SID=x")
        self.assertEqual(got.get("js_runtimes"), expected.get("js_runtimes"),
                         "_base_options 没把 JS runtime 挂到 YouTube 上 ⇒ 打包后 "
                         "nsig 挑战无解，带登录态会卡在 'The page needs to be reloaded'。")
        self.assertEqual(got.get("remote_components"), expected.get("remote_components"))

    def test_youtu_be_short_link_also_gets_them(self):
        expected = D._js_challenge_options()
        got = D._base_options(3, "youtu.be", cookie="SID=x")
        self.assertEqual(got.get("js_runtimes"), expected.get("js_runtimes"))

    def test_non_youtube_hosts_untouched(self):
        for host in ("bilibili.com", "douyin.com", "vimeo.com"):
            with self.subTest(host=host):
                o = D._base_options(3, host)
                self.assertNotIn("js_runtimes", o, f"{host} 不该被塞 JS runtime")
                self.assertNotIn("remote_components", o, f"{host} 不该被塞远程组件")


class TestDepsAndPackaging(unittest.TestCase):
    """不退化守卫：依赖与打包脚本里少了任何一处，打包后都会静默回到「解不了挑战」。"""

    def test_requirements_keep_ejs_and_deno(self):
        req = _read(os.path.join(REPO, "requirements.txt"))
        self.assertRegex(req, r"(?m)^yt-dlp-ejs[=><]",
                         "requirements.txt 缺 yt-dlp-ejs —— 本地 EJS 求解脚本没了，"
                         "yt-dlp 只能去 GitHub 拉远程组件（离线/被墙即失败）。")
        self.assertRegex(req, r"(?m)^deno[=><]",
                         "requirements.txt 缺 deno —— 没有任何 JS runtime，"
                         "yt-dlp 的 nsig 挑战无人可解（默认只启用 deno）。")

    def test_build_scripts_bundle_deno_binary(self):
        for script, sep in (("desktop/build_mac.sh", ":"), ("desktop/build_win.sh", ";")):
            with self.subTest(script=script):
                src = _read(os.path.join(REPO, script))
                hit = re.search(r"--add-binary\s+\"[^\"]*deno[^\"]*\"", src)
                self.assertIsNotNone(
                    hit,
                    f"{script} 没把 deno 二进制用 --add-binary 打进包 —— "
                    "pip 装的 deno 只在 venv/bin 里，不随 PyInstaller 产物分发，"
                    "打包后 _find_js_runtime_binary() 找不到它。")

    def test_downloader_resolver_mentions_js_challenge(self):
        src = _read(os.path.join(REPO, "server", "downloader.py"))
        self.assertIn("_js_challenge_options()", src)
        i = src.find("def _base_options(")
        self.assertNotEqual(i, -1)
        body = src[i: i + 9000]
        self.assertIn("_js_challenge_options()", body,
                      "_base_options 里没有调用 _js_challenge_options()")


if __name__ == "__main__":
    print("🧪 YouTube JS 挑战（nsig）求解链 守卫测试\n")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromTestCase(c) for c in (
            TestFindJsRuntimeBinary,
            TestJsChallengeOptions,
            TestBaseOptionsWiring,
            TestDepsAndPackaging,
        )
    )
    r = unittest.TextTestRunner(verbosity=2).run(suite)
    n_ok = r.testsRun - len(r.failures) - len(r.errors)
    if r.failures or r.errors:
        print(f"\n❌ 失败 {len(r.failures) + len(r.errors)} 项")
        sys.exit(1)
    print(f"\n🎉 YouTube JS 挑战守卫全部通过（{n_ok} 项）")
