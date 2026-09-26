#!/usr/bin/env python
"""YouTube player_client 选择：守卫测试（2026-09-26）

背景（真实事故）：
2026-09-26 用户开着 VPN 仍无法解析 YouTube。实测 root cause 有两条环环相扣：

1. `_base_options()` 里对 youtube.com / youtu.be **强制** `player_client=["web_safari"]`。
   YouTube 端 SABR 调整后（2026-09-26 实测，yt-dlp 2026.08.19），
   web_safari / mweb / web / web_embedded 一律抛
   "Requested format is not available"，**只有不传 player_client
   （即 yt-dlp 默认客户端链）能拿到完整格式列表（实测 48 个格式）**。
   ⇒ 那条覆盖写 = 把唯一能走通的路堵死了。

2. 带登录态 Cookie 的请求会触发 YouTube SABR 新页流程，返回
   "The page needs to be reloaded"；**不带 Cookie 的干净请求反而成功**。
   而 App 会自动注入本机浏览器 Cookie ⇒ 必须能在失败时自动剥掉 Cookie 裸重试。

本文件用**离线**方式守卫这两条结论：
- 只读脚本文本（不发起任何网络请求），确保：
  * `_base_options` 不再给 YouTube 强制任何 player_client
  * 下载阶段 YouTube 兜底客户端链的第一位是 "(default)"（不覆盖 player_client）
  * `_resolve_youtube` 里保留了「带 Cookie 失败 → 剥 Cookie 重试」的兜底分支

为什么用文本守卫而不是直接跑 yt-dlp：这条规则是**靠实测外站行为**得来的，
离线环境（无代理/无网络）无法复现 SABR 差异，只能守住「代码形态不退化」。
真正的端到端判定请走桌面端真机 + 代理复测。
"""
import os
import re
import sys
import unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOADER = os.path.join(SERVER_DIR, "downloader.py")


def _src() -> str:
    with open(DOWNLOADER, encoding="utf-8") as f:
        return f.read()


class TestYoutubePlayerClient(unittest.TestCase):
    """YouTube 相关 player_client 选择的回归守卫。"""

    def test_base_options_does_not_force_player_client_for_youtube(self):
        """_base_options 不得给 YouTube 强制 player_client（2026-09-26 SABR 后失效）。"""
        src = _src()
        # 允许出现在注释里（./注释保留的是踩坑史），禁止出现在可执行赋值里
        bad = re.search(
            r"^\s*options\.setdefault\(\s*[\"']extractor_args[\"'].*player_client[\"']\s*\]\s*=",
            src,
            re.M,
        )
        self.assertIsNone(
            bad,
            "_base_options() 又给 YouTube 强制了 player_client："
            "2026-09-26 实测 SABR 后所有手动指定 client 都会抛 "
            "'Requested format is not available'，必须交给 yt-dlp 默认客户端链。\n"
            f"命中：{bad.group(0) if bad else ''}",
        )

    def test_download_fallback_tries_default_client_first(self):
        """YouTube 下载兜底链第一位必须是 "(default)"（即不覆盖 player_client）。"""
        src = _src()
        m = re.search(r"_yt_clients\s*=\s*\[([^\]]*)\]", src)
        self.assertIsNotNone(m, "未找到 _yt_clients 定义")
        clients = [c.strip().strip("\"'") for c in m.group(1).split(",") if c.strip()]
        self.assertTrue(clients, "_yt_clients 为空")
        self.assertEqual(
            clients[0],
            "(default)",
            f"_yt_clients 第一位必须是 '(default)'，实际为 {clients[0]!r}。"
            "顺序错了会让下载先踩一遍已失效的 client，白白增加失败与耗时。",
        )

    def test_default_client_must_not_set_player_client(self):
        """"(default)" 分支不能设置 player_client，否则退化成本来要修的老行为。"""
        src = _src()
        m = re.search(r"_yt_clients = \[.*?\]", src, re.S)
        self.assertIsNotNone(m)
        tail = src[m.end(): m.end() + 1500]
        self.assertIn(
            'if _client != "(default)"',
            tail,
            "缺少 `if _client != \"(default)\"` 守卫："
            "带 '(default)' 时不能再写 player_client，否则等于没修复。",
        )

    def test_resolve_has_cookieless_retry(self):
        """_resolve_youtube 必须能在「带 Cookie 失败」时剥掉 Cookie 重试。"""
        src = _src()
        i = src.find("def _resolve_youtube(")
        self.assertNotEqual(i, -1, "未找到 _resolve_youtube")
        body = src[i: i + 6000]
        self.assertIn(
            "page needs to be reloaded",
            body,
            "_resolve_youtube 缺少 SABR 特征识别："
            "2026-09-26 实测带登录 Cookie 会触发 'The page needs to be reloaded'，"
            "必须据此剥掉 Cookie 裸重试，否则 App 自动注入的浏览器 Cookie 会一直毒化解析。",
        )
        self.assertIn(
            '_try("", use_visitor=False)',
            body,
            "_resolve_youtube 缺少剥 Cookie 的裸重试调用 _try(\"\", use_visitor=False)。",
        )


if __name__ == "__main__":
    print("🧪 YouTube player_client 守卫测试\n")
    suite = unittest.TestLoader().loadTestsFromTestCase(TestYoutubePlayerClient)
    r = unittest.TextTestRunner(verbosity=2).run(suite)
    n_ok = r.testsRun - len(r.failures) - len(r.errors)
    if r.failures or r.errors:
        print(f"\n❌ 失败 {len(r.failures) + len(r.errors)} 项")
        sys.exit(1)
    print(f"\n🎉 YouTube player_client 守卫全部通过（{n_ok} 项）")
