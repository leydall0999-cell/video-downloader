#!/usr/bin/env python
"""YouTube「无 PO Token ⇒ ≤1080p 优先 HLS 混合流」守卫测试（web-dev，2026-10-19）

背景（用户实测反馈「下载速度慢 / 网页版解析超时」）：
桌面端下一个 637MB 的 1080P YouTube 视频，界面瞬时 81.3KB/s、ETA 1:40，
最终在 35.5% 报「网络连接超时」；网页版（HK 2 核小机）YouTube 解析 70s 超时。
分层实测归因：
  - 根因①（下载慢）：googlevideo 对**无 PO Token 的 DASH 自适应流按连接限速**
    ——单连接 ~40~80KB/s，16 连接聚合仅 ~600KB/s，且频繁硬阻断；
    同源 **HLS 混合流（itag 91-96，avc1+aac）不限速：单连接实测 1.5MB/s**
    （修复后 30s 下 175.8MB，均速 4.6MB/s）。
  - 根因②（网页版超时）：HK 2 核小机上 deno 解一次 nsig 挑战要 43s+
    （桌面 M1 同操作 1~5s），叠加多级重试链必撞穿前端 70s 解析预算
    ⇒ 运维开关 VDL_YT_JS_RUNTIME=off 关掉 JS 求解（格式列表不变，
     bare yt-dlp 实测 50 格式 / 1.3~5.8s），下载侧配合 HLS 优先。
  - POT 经 socks 代理静默失效（插件请求走 yt-dlp 全局代理，CONNECT
    127.0.0.1:4416 必失败）⇒ fetch_pot 决策必须代理感知（_pot_usable）。

本文件离线守卫（不发任何网络请求）：
1. `_yt_prefer_hls()` 的档位矩阵（≤1080p/最佳走 HLS；>1080p、音频、VP9 不走）；
2. `_youtube_hls_selector()` 的结构与回落（拿不到 HLS 时原样回落 DASH 链）；
3. `_download_options()` 的接线：YouTube + POT 不可用 ⇒ HLS 选择器；
   POT 可用 / >1080p / 非 YouTube ⇒ 原选择器不动；
4. `_pot_usable()` 的代理感知判定（HK 实测：裸 socket 报「可达」但 socks
   代理打不通 4416 ⇒ POT 静默失效；本机无 server ⇒ 一律不可用）；
5. `VDL_YT_JS_RUNTIME=off` 运维开关必须真正写进 `_base_options` 的
   `js_runtimes`（空 dict）——yt-dlp 库默认 `{'deno': {}}`，不写=没关；
6. 源码级接线守卫：`_resolve_youtube` / `_run_once` 的 fetch_pot 决策必须走
   `_pot_usable`（代理感知），不再用裸 `_bgutil_reachable()`。
"""
import os
import sys
import threading
import unittest
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_yt_hls_web")

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
        self.workdir = "/tmp/vdl_test_yt_hls_web"


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
        # HLS 段同样锁在 1080 档的窄区间内（横屏 height / 竖屏 width），
        # 且不许出现单边上界 —— 单边上界正是会静默跨档的写法
        self.assertTrue(
            s.startswith("b[protocol^=m3u8][height>=1048][height<=1112]/"), s)
        self.assertIn("b[protocol^=m3u8][width>=1048][width<=1112]/", s)
        self.assertNotIn("[height<=1080]", s, "HLS 段出现单边上界 1080（会跨档）")
        # 回落段必须是原 DASH 选择器（精确 avc1 链），保证拿不到 HLS 时行为不变
        self.assertIn(D._format_selector("1080"), s)

    def test_best_key_caps_1080(self):
        s = D._youtube_hls_selector(D.BEST_KEY)
        self.assertTrue(s.startswith("b[protocol^=m3u8][height<=1080]"), s)
        self.assertIn(D._format_selector(D.BEST_KEY), s)

    def test_other_quality(self):
        s = D._youtube_hls_selector("720")
        self.assertIn("height>=699", s)
        self.assertIn("width>=699", s)
        self.assertIn("height<=741", s)
        self.assertNotIn("[height<=720]", s)
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
        self.assertTrue(
            fmt.startswith("b[protocol^=m3u8][height>=1048][height<=1112]"), fmt)

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


@contextmanager
def _force_proxy_use():
    """强制探测「真的经过代理」（屏蔽 no_proxy / 系统级例外名单）。

    🔴 2026-10-20 实测踩坑：本机（macOS + 代理客户端）系统代理例外里带
    127.0.0.1，`urllib.request.proxy_bypass("127.0.0.1:4416")` 返回 **True**
    ⇒ ProxyHandler 直接放行直连，于是「代理指到死端口」也探测成功、用例假红。
    这不是代码 bug：插件的请求同样走 urllib 的 ProxyHandler，同样会被绕过
    ⇒ 此时 POT 确实可用。用例要验的是「代理真被使用」时的判定（HK Linux
    实测同型），所以必须在探测期间强制不绕过。
    """
    orig = urllib.request.proxy_bypass
    urllib.request.proxy_bypass = lambda host: False
    try:
        yield
    finally:
        urllib.request.proxy_bypass = orig


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
        # 代理指到没人听的端口 ⇒ 代理真被使用时请求必然失败 ⇒ 不可用
        # （HK 实测同型：socks 代理打不通 127.0.0.1:4416，POT 静默失效）
        with _force_proxy_use():
            self.assertFalse(D._pot_usable("http://127.0.0.1:9"))

    def test_proxy_bypass_means_direct_and_usable(self):
        """系统/环境把 127.0.0.1 列入「绕过代理」时，插件请求本身也是直连
        ⇒ 探测必须跟着判「可用」（否则 macOS + Clash 一类用户会白丢 POT，
        退回 SABR 受限的 360p 画质）。这条守的是「别为了对付代理把绕过逻辑
        一起改掉」。"""
        orig = urllib.request.proxy_bypass
        urllib.request.proxy_bypass = lambda host: True
        try:
            D._POT_USABLE_CACHE.clear()
            self.assertTrue(D._pot_usable("http://127.0.0.1:9"))
        finally:
            urllib.request.proxy_bypass = orig
            D._POT_USABLE_CACHE.clear()

    def test_cache_per_proxy(self):
        D._POT_USABLE_CACHE["http://cached:1"] = True
        self.assertTrue(D._pot_usable("http://cached:1"))
        with mock.patch.object(D, "_bgutil_reachable", return_value=True):
            D._POT_USABLE_CACHE.clear()
            with _force_proxy_use():
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
    """VDL_YT_JS_RUNTIME=off：HK 2 核小机跳过 43s 的 deno nsig 求解。

    web-dev 没有 _js_challenge_options（deno 是 yt-dlp 库默认行为），
    开关直接写在 _base_options 的 YouTube 分支里。
    """

    def test_off_writes_empty_runtimes(self):
        # 🔴 必须显式写空 dict：yt-dlp 库默认 js_runtimes={'deno': {}}
        # （YoutubeDL.py: params.get('js_runtimes', {'deno': {}})），
        # 不写这个键等于没关（deno 照跑，HK 上就是 43s+）。空 dict 能过
        # _clean_js_runtimes（不 raise），_js_runtimes 迭代为空。
        with mock.patch.dict(os.environ, {"VDL_YT_JS_RUNTIME": "off"}):
            opts = D._base_options(1, "www.youtube.com")
        self.assertEqual(opts.get("js_runtimes"), {},
                         "_base_options 没有把 off 开关写进 js_runtimes（deno 仍会启用）")

    def test_off_variants(self):
        for v in ("0", "OFF", "none", "disabled", "false", "no"):
            with mock.patch.dict(os.environ, {"VDL_YT_JS_RUNTIME": v}):
                opts = D._base_options(1, "www.youtube.com")
            self.assertEqual(opts.get("js_runtimes"), {}, v)

    def test_default_does_not_disable(self):
        # 默认（无 env）绝不能是空 dict——那等于默认关掉 JS 求解
        env = dict(os.environ)
        env.pop("VDL_YT_JS_RUNTIME", None)
        with mock.patch.dict(os.environ, env):
            opts = D._base_options(1, "www.youtube.com")
        self.assertNotEqual(opts.get("js_runtimes"), {},
                            "默认值不应禁用 JS 求解（VDL_YT_JS_RUNTIME 未设置）")

    def test_non_youtube_untouched(self):
        with mock.patch.dict(os.environ, {"VDL_YT_JS_RUNTIME": "off"}):
            opts = D._base_options(1, "www.bilibili.com")
        self.assertNotIn("js_runtimes", opts)


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

    def test_run_once_uses_pot_usable(self):
        src = _read_source()
        i = src.find("def _run_once(")
        self.assertNotEqual(i, -1)
        body = src[i:i + 30000]
        self.assertIn('_pot_usable(effective_proxy)', body,
                      "_run_once 的 fetch_pot 决策没有走 _pot_usable")
        self.assertNotIn('_bgutil_reachable() else ["never"]', body)

    def test_download_options_calls_hls_selector(self):
        src = _read_source()
        i = src.find("def _download_options(")
        self.assertNotEqual(i, -1)
        body = src[i:i + 4000]
        self.assertIn("_youtube_hls_selector(quality_key)", body)
        self.assertIn("_yt_prefer_hls(quality_key)", body)
        self.assertIn("_pot_usable(", body)


class TestPlayerClientNotForced(unittest.TestCase):
    """🔴 2026-10-19：不得再强制 player_client=web_safari。

    香港节点客户端矩阵实测（CuK9hrKXb8U，visitor_data 已注入、fetch_pot=never）：
        (default 链)  → 9.5s / 50 格式（m3u8_native×16 最高 1440p + https×30）
        web_safari    → FAIL "Requested format is not available"（js 开/关都败）
        android       → 仅 5 格式（无 m3u8）；tv/ios/mweb/web/web_embedded/tv_simply 全 FAIL
    web_safari 是 2026-09-21 按「与 bgutil PO Token 同 client」选的，只在 POT
    **真可用**时成立；POT 不可用时它连格式列表都拿不到 ⇒ 网页版 YouTube 解析全坏
    （用户看到的 70s「超时」= 重试链烧穿预算）。且 yt-dlp 的 player_client 是
    「合并」模式，传任何单客户端都会覆盖默认链 ⇒ 默认链一个都不传。
    """

    def test_base_options_does_not_force_client(self):
        """运行时：_base_options 的 youtube extractor_args 不该有 player_client。"""
        with mock.patch.object(D, "_resolve_proxy", lambda *a, **kw: ""), \
             mock.patch.object(D, "_cn_proxy_url", lambda *a, **kw: ""):
            env = dict(os.environ)
            env.pop("VDL_YT_HLS_CLIENT", None)
            with mock.patch.dict(os.environ, env, clear=True):
                opts = D._base_options(host="youtube.com")
        ya = (opts.get("extractor_args") or {}).get("youtube") or {}
        self.assertNotIn("player_client", ya,
                         f"不得强制 player_client（应走默认客户端链）：{ya}")

    def test_source_has_no_forced_web_safari(self):
        """源码级：_base_options 内不得再出现 player_client = ["web_safari"]。"""
        src = _read_source()
        i = src.find("def _base_options(")
        self.assertNotEqual(i, -1)
        body = src[i:i + 8000]
        self.assertNotIn('"player_client"] = ["web_safari"]', body,
                         "_base_options 仍在强制 web_safari（HK 实测它会直接 FAIL）")

    def test_fallback_clients_default_first(self):
        """下载降级链：默认客户端链必须前置，单客户端降为兜底。"""
        src = _read_source()
        i = src.find("_yt_clients = [")
        self.assertNotEqual(i, -1)
        line = src[i:src.find("]", i) + 1]
        self.assertIn('"(default)"', line, f"默认链应前置：{line}")
        self.assertTrue(line.index('"(default)"') < line.index('"web_safari"'),
                        f"(default) 必须排在 web_safari 之前：{line}")

    def test_pot_starved_fallback_tries_client_chain(self):
        """PO Token 饥饿补救：单一客户端会整段报废，必须依次尝试客户端链。"""
        src = _read_source()
        self.assertIn("for _hls_client in _hls_clients", src,
                      "饥饿补救仍是单客户端（web_safari FAIL 时整段报废）")
        self.assertIn('_hls_clients = [_hls_client_env] if _hls_client_env else ["web_safari", "android"]',
                      src, "饥饿补救的客户端链内容变了，请同步本守卫")


if __name__ == "__main__":
    print("🧪 YouTube 无 POT ⇒ HLS 优先 守卫测试（web-dev）\n")
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
            TestPlayerClientNotForced,
        )
    )
    r = unittest.TextTestRunner(verbosity=2).run(suite)
    n_ok = r.testsRun - len(r.failures) - len(r.errors)
    if r.failures or r.errors:
        print(f"\n❌ 失败 {len(r.failures) + len(r.errors)} 项")
        sys.exit(1)
    print(f"\n🎉 YouTube HLS 优先守卫全部通过（{n_ok} 项）")
