#!/usr/bin/env python
"""YouTube「无 PO Token ⇒ ≤1080p 优先 HLS 混合流」守卫测试（2026-10-19）

背景（用户实测反馈「下载速度慢」）：
桌面端下一个 637MB 的 1080P YouTube 视频，界面瞬时 81.3KB/s、ETA 1:40，
最终在 35.5% 报「网络连接超时」。分层实测归因：
  - 代理裸速 2MB/s、HLS 分片单连接 1.5MB/s ⇒ 网络没问题；
  - 该任务走的是 **DASH 自适应流**（16 连接聚合仅 ~600KB/s，且频繁硬阻断）；
  - 同源 **HLS 混合流（itag 91-96，avc1+aac）不被限速**：单连接 1.5MB/s，
    修复后 30s 实测下 175.8MB（均速 4.6MB/s，1 次 stall）。
⇒ 根因：googlevideo 对**无 PO Token 的 DASH 流按连接限速**；App 原有
  「PO Token 饥饿 → web_safari/HLS 补救」只在格式列表饥饿（≤360P）时触发，
  DASH 可用时不触发，用户只能吃到限速链路。

本文件离线守卫（不发任何网络请求）：
1. `_yt_prefer_hls()` 的档位矩阵（≤1080p/最佳走 HLS；>1080p、音频、VP9 不走）；
2. `_youtube_hls_selector()` 的结构与回落（拿不到 HLS 时原样回落 DASH 链）；
3. `_download_options()` 的接线：YouTube + POT 不可用 ⇒ HLS 选择器；
   POT 可用 / >1080p / 非 YouTube ⇒ 原选择器不动；
4. `_pot_usable()` 的代理感知判定（HK 实测：裸 socket 报「可达」但 socks
   代理打不通 4416 ⇒ POT 静默失效；本机无 server ⇒ 一律不可用）；
5. `VDL_YT_JS_RUNTIME=off` 运维开关（HK 2 核小机 deno 解 nsig 要 43s+，
   关掉后格式列表不变、解析 1.3~5.8s）；
6. 源码级接线守卫：`_resolve_youtube` 的 fetch_pot 决策必须走 `_pot_usable`。
"""
import os
import sys
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_yt_hls")

import downloader as D  # noqa: E402


def _read_source() -> str:
    with open(os.path.join(SERVER_DIR, "downloader.py"), encoding="utf-8") as f:
        return f.read()


class _Reporter:
    """_download_options 需要的最小 reporter 桩。"""

    def __call__(self, p):  # noqa: D102
        pass

    def on_postprocess(self, p):  # noqa: D102
        pass


class _Task:
    def __init__(self, url: str):
        self.url = url
        self.workdir = "/tmp/vdl_test_yt_hls"


class TestYtPreferHls(unittest.TestCase):
    """档位矩阵 + env 总开关。"""

    def test_quality_matrix(self):
        # quality_key 只会是数字档（"1080"）或常量档；标签形如「1080P 高清」
        # 是 quality 字段、不会流进选择器（_format_selector 同样只认这两类）。
        for qk in ("1080", "720", "480", D.BEST_KEY):
            self.assertTrue(D._yt_prefer_hls(qk), qk)
        for qk in ("1440", "2160", "4320", D.AUDIO_KEY, D.M4A_KEY, D.WEBM_KEY, ""):
            self.assertFalse(D._yt_prefer_hls(qk), qk)

    def test_env_kill_switch(self):
        with mock.patch.dict(os.environ, {"VDL_YT_PREFER_HLS": "0"}):
            self.assertFalse(D._yt_prefer_hls("1080"))
            self.assertFalse(D._yt_prefer_hls(D.BEST_KEY))
        # 默认（无 env）恢复
        self.assertTrue(D._yt_prefer_hls("1080"))


class TestYoutubeHlsSelector(unittest.TestCase):
    """选择器结构：HLS 优先、DASH 链兜底。"""

    def test_numeric_quality(self):
        s = D._youtube_hls_selector("1080")
        self.assertTrue(s.startswith("b[protocol^=m3u8][height<=1080]/"), s)
        self.assertIn("best[protocol^=m3u8][height<=1080]/", s)
        # 回落段必须是原 DASH 选择器（vcodec^=avc1 链），保证拿不到 HLS 时行为不变
        self.assertIn(D._format_selector("1080"), s)

    def test_best_key_caps_1080(self):
        s = D._youtube_hls_selector(D.BEST_KEY)
        self.assertTrue(s.startswith("b[protocol^=m3u8][height<=1080]"), s)
        self.assertIn(D._format_selector(D.BEST_KEY), s)

    def test_other_quality(self):
        s = D._youtube_hls_selector("720")
        self.assertIn("height<=720", s)
        self.assertIn(D._format_selector("720"), s)


class TestDownloadOptionsWiring(unittest.TestCase):
    """_download_options 接线：POT 不可用才切 HLS。"""

    URL = "https://www.youtube.com/watch?v=CuK9hrKXb8U"

    def _fmt(self, url, quality, pot_ok):
        task = _Task(url)
        with mock.patch.object(D, "_pot_usable", return_value=pot_ok):
            return D._download_options(task, quality, _Reporter())["format"]

    def test_youtube_no_pot_uses_hls(self):
        fmt = self._fmt(self.URL, "1080", pot_ok=False)
        self.assertTrue(fmt.startswith("b[protocol^=m3u8][height<=1080]"), fmt)

    def test_youtube_pot_ok_keeps_dash(self):
        fmt = self._fmt(self.URL, "1080", pot_ok=True)
        self.assertFalse(fmt.startswith("b[protocol^=m3u8]"), fmt)
        self.assertEqual(fmt, D._format_selector("1080"))

    def test_youtube_above_1080_keeps_dash(self):
        # >1080p 只有 DASH 一条路，HLS 混合流封顶 1080p ⇒ 不切
        fmt = self._fmt(self.URL, "2160", pot_ok=False)
        self.assertFalse(fmt.startswith("b[protocol^=m3u8]"), fmt)
        self.assertEqual(fmt, D._format_selector("2160"))

    def test_youtube_audio_keeps_dash(self):
        fmt = self._fmt(self.URL, D.AUDIO_KEY, pot_ok=False)
        self.assertFalse(fmt.startswith("b[protocol^=m3u8]"), fmt)

    def test_non_youtube_untouched(self):
        fmt = self._fmt("https://www.bilibili.com/video/BV1xx411c7mD", "1080", pot_ok=False)
        self.assertEqual(fmt, D._format_selector("1080"))

    def test_env_kill_switch_keeps_dash(self):
        task = _Task(self.URL)
        with mock.patch.dict(os.environ, {"VDL_YT_PREFER_HLS": "off"}), \
                mock.patch.object(D, "_pot_usable", return_value=False):
            fmt = D._download_options(task, "1080", _Reporter())["format"]
        self.assertEqual(fmt, D._format_selector("1080"))


class _PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"pong")

    def log_message(self, *args):  # 静音
        pass


class TestPotUsable(unittest.TestCase):
    """_pot_usable：server 在 + 代理放行 localhost 才算可用。"""

    def setUp(self):
        D._POT_USABLE_CACHE.clear()
        self.srv = None
        try:
            self.srv = HTTPServer(("127.0.0.1", 4416), _PingHandler)
        except OSError:
            self.skipTest("4416 已被占用（本机跑着真 bgutil？跳过正向用例）")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        D._POT_USABLE_CACHE.clear()

    def test_server_up_no_proxy(self):
        self.assertTrue(D._pot_usable(""))

    def test_server_up_dead_proxy(self):
        # 代理打不通 ⇒ 插件请求必然失败 ⇒ 不可用
        # （HK 实测同型：socks 代理打不通 127.0.0.1:4416，POT 静默失效）
        # 🔧 用 mock 精确模拟「死代理 open 抛异常」，而非依赖真实死端口：
        # 本机若挂了透明代理，localhost:9 的连接会被拦截转发给 4416 ⇒
        # open 反而 200，误判为「可用」。死代理的语义本就是 open 失败，这里直接模拟。
        with mock.patch("urllib.request.build_opener") as mba:
            mba.return_value.open.side_effect = urllib.error.URLError("connection refused")
            self.assertFalse(D._pot_usable("http://127.0.0.1:9"))

    def test_cache_per_proxy(self):
        D._POT_USABLE_CACHE["http://cached:1"] = True
        self.assertTrue(D._pot_usable("http://cached:1"))
        with mock.patch.object(D, "_bgutil_reachable", return_value=True):
            D._POT_USABLE_CACHE.clear()
            # 死代理 ⇒ open 失败 ⇒ 不可用（mock 掉网络，避免本机透明代理误判）
            with mock.patch("urllib.request.build_opener") as mba:
                mba.return_value.open.side_effect = urllib.error.URLError("refused")
                self.assertFalse(D._pot_usable("http://127.0.0.1:9"))
            D._POT_USABLE_CACHE.clear()
            # 缓存命中不再探测
            D._POT_USABLE_CACHE["http://cached:2"] = False
            with mock.patch.object(D, "_bgutil_reachable", side_effect=AssertionError("不应再探测")):
                self.assertFalse(D._pot_usable("http://cached:2"))


class TestPotUsableNoServer(unittest.TestCase):
    """没有 bgutil server 时，无论有没有代理都不可用。"""

    def setUp(self):
        D._POT_USABLE_CACHE.clear()

    def tearDown(self):
        D._POT_USABLE_CACHE.clear()

    def test_no_server(self):
        with mock.patch.object(D, "_bgutil_reachable", return_value=False):
            self.assertFalse(D._pot_usable(""))
            self.assertFalse(D._pot_usable("http://127.0.0.1:7892"))


class TestJsRuntimeSwitch(unittest.TestCase):
    """VDL_YT_JS_RUNTIME=off：HK 2 核小机跳过 43s 的 deno nsig 求解。"""

    def test_off_returns_empty_runtimes(self):
        # 🔴 必须返回 {"js_runtimes": {}} 而不是 {}：调用处是
        # options.update(...)，空 dict 是 no-op，而 yt-dlp 库默认
        # js_runtimes={'deno': {}} ⇒ 不写这个键等于没关（deno 照跑）。
        with mock.patch.dict(os.environ, {"VDL_YT_JS_RUNTIME": "off"}):
            self.assertEqual(D._js_challenge_options(), {"js_runtimes": {}})

    def test_off_variants(self):
        for v in ("0", "OFF", "none", "disabled", "false", "no"):
            with mock.patch.dict(os.environ, {"VDL_YT_JS_RUNTIME": v}):
                self.assertEqual(D._js_challenge_options(), {"js_runtimes": {}}, v)

    def test_off_actually_reaches_base_options(self):
        """接线级守卫：off 必须让 _base_options 的 YouTube 分支带上空 js_runtimes。

        这条用例专防「函数返回对了但 update 丢键」——2026-10-19 实际栽过：
        _js_challenge_options 返回 {} 而 update({}) 是 no-op，deno 照跑。
        """
        with mock.patch.dict(os.environ, {"VDL_YT_JS_RUNTIME": "off"}):
            opts = D._base_options(1, "www.youtube.com")
        self.assertEqual(opts.get("js_runtimes"), {},
                         "_base_options 没有把 off 开关写进 js_runtimes（deno 仍会启用）")

    def test_default_does_not_crash(self):
        # 默认值取决于机器上装没装 deno/node：装了就报 runtimes，没报空。
        # 守卫重点是「不抛异常」且 shape 正确。
        env = dict(os.environ)
        env.pop("VDL_YT_JS_RUNTIME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            out = D._js_challenge_options()
        self.assertIsInstance(out, dict)


class TestSourceWiring(unittest.TestCase):
    """源码级接线守卫：fetch_pot 决策必须走 _pot_usable（代理感知）。"""

    def test_resolve_youtube_uses_pot_usable(self):
        src = _read_source()
        i = src.find("def _resolve_youtube(")
        self.assertNotEqual(i, -1)
        body = src[i:i + 12000]
        self.assertIn('_pot_usable(effective_proxy)', body,
                      "_resolve_youtube 的 fetch_pot 决策没有走 _pot_usable")
        # 旧写法（裸 bgutil 可达性）不应再出现在 fetch_pot 决策上
        self.assertNotIn('_bgutil_reachable() else ["never"]', body)

    def test_download_options_calls_hls_selector(self):
        src = _read_source()
        i = src.find("def _download_options(")
        self.assertNotEqual(i, -1)
        body = src[i:i + 4000]
        self.assertIn("_youtube_hls_selector(quality_key)", body)
        self.assertIn("_yt_prefer_hls(quality_key)", body)
        self.assertIn("_pot_usable(", body)


if __name__ == "__main__":
    print("🧪 YouTube 无 POT ⇒ HLS 优先 守卫测试\n")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(
        loader.loadTestsFromTestCase(c) for c in (
            TestYtPreferHls,
            TestYoutubeHlsSelector,
            TestDownloadOptionsWiring,
            TestPotUsable,
            TestPotUsableNoServer,
            TestJsRuntimeSwitch,
            TestSourceWiring,
        )
    )
    r = unittest.TextTestRunner(verbosity=2).run(suite)
    n_ok = r.testsRun - len(r.failures) - len(r.errors)
    if r.failures or r.errors:
        print(f"\n❌ 失败 {len(r.failures) + len(r.errors)} 项")
        sys.exit(1)
    print(f"\n🎉 YouTube HLS 优先守卫全部通过（{n_ok} 项）")
