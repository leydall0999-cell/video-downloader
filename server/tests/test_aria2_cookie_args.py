#!/usr/bin/env python
"""aria2c 必须显式打开 cookie 支持（2026-10-24 实测发现）

现象：网盘直链交给 aria2c 下载直接 403，抓 aria2 自己的 debug 日志看到发出的
请求头里**根本没有 Cookie 一行**（只有 Accept / Host / User-Agent）。

根因：aria2 的 ``--enable-cookie`` 默认是 **false**。此时它把 yt-dlp 通过
``--header="Cookie: ..."`` 传进来的登录态收进内部 cookieStorage，却**不随请求发出**。
⇒ 任何需要登录态的源（百度网盘直链、部分需要 Cookie 的视频源）走 aria2c 一律 403。

2026-10-24 抓包对照（同一条 d.pcs.baidu.com 直链）：
  - 不加 --enable-cookie=true → 请求头无 Cookie → HTTP 403
  - 加上 --enable-cookie=true → 请求头带 Cookie → HTTP 302（进入 CDN 跳转）

⚠️ 注意：开了 cookie 也**不能**让网盘直链走 aria2c。302 跳到 *.baidupcs.com 之后
aria2 按 domain 匹配 cookie，Cookie 又带不过去，仍然 403。所以网盘直链必须走原生
单连接（见 test_netdisk_download_options.py）。本文件守的是「其它平台别被这个
默认开关坑了」——Cookie 被静默吞掉是很难查的一类故障。
"""
import os
import sys
import unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_aria2_cookie")

import downloader as D  # noqa: E402


def _read_source() -> str:
    with open(os.path.join(SERVER_DIR, "downloader.py"), encoding="utf-8") as f:
        return f.read()


class TestAria2CookieEnabled(unittest.TestCase):
    def test_args_contain_enable_cookie(self):
        """传给 aria2c 的参数必须显式打开 cookie 支持。"""
        args = D._build_aria2c_args(4)
        self.assertIn("--enable-cookie=true", args,
                      "缺 --enable-cookie=true：aria2 会静默吞掉 Cookie，"
                      "需要登录态的源全部 403")

    def test_enable_cookie_survives_all_concurrency(self):
        """任意并发档位都要带上（有人改动参数拼装时容易漏）。"""
        for n in (1, 2, 4, 8, 16):
            with self.subTest(n=n):
                self.assertIn("--enable-cookie=true", D._build_aria2c_args(n))

    def test_still_keeps_concurrency_flags(self):
        """加新开关不能把原来的 -x/-s/-k/--continue 挤掉。"""
        args = D._build_aria2c_args(8)
        for flag in ("-x", "-s", "-k", "--continue=true"):
            self.assertIn(flag, args)

    def test_source_documents_why(self):
        """源码里必须写明原因，否则下一个人看到会以为是多余的开关给删掉。"""
        src = _read_source()
        # rfind：注释里也会提到这个开关，真正生效的是参数列表里那一次
        idx = src.rfind("--enable-cookie=true")
        self.assertNotEqual(idx, -1, "源码里找不到 --enable-cookie=true")
        window = src[max(0, idx - 1200):idx]
        self.assertTrue(
            ("Cookie" in window) or ("cookie" in window),
            "--enable-cookie=true 附近应有注释说明「默认关闭会吞 Cookie」的原因",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
