#!/usr/bin/env python
"""百度网盘直链「单连接 + 客户端身份」守卫测试（2026-10-24）

背景（用户反馈「没突破下载限制，速度跟网盘下载一样慢」）：
本机实测 dlink 直连（非会员账号 vip_type=0）：

  | 方案                | 速度        |
  |---------------------|-------------|
  | 1 连接              | 78.8 KB/s   |
  | 4 连接              | 80.9 KB/s   |
  | 8 连接              | 61.0 KB/s   |
  | 16 连接             | 36.6 KB/s   |
  | P2SP UA + 1 连接    | 139~166 KB/s|

两条结论：
  1. 百度按**账号总带宽**限速，不是按连接 ⇒ 多连接（aria2c 分片）是纯负优化，
     连接多了还会被掐断、拿不满字节；
  2. **请求身份**决定限速档：浏览器/空 UA 落在 ~78 KB/s 档，老 PC 客户端的
     P2SP 标识能到 ~145 KB/s（1.5MB 交替 A/B 两轮一致）。

⇒ 网盘直链必须：强制原生单连接（连用户显式选 aria2c 也兜住）+ P2SP UA + 禁分片并发。
本文件离线守卫（不发任何网络请求），防的是「哪天有人给网盘也开上多连接」。
"""
import os
import sys
import unittest
from unittest import mock

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_netdisk_opts")

import downloader as D  # noqa: E402

DLINK = "https://d.pcs.baidu.com/file/0edddf313med10c49a01286c8cb08b1c?fid=1&sign=FDtAERV"
DLINK_CDN = "https://xaky-cm00.baidupcs.com/file/0edddf313med10c49a01286c8cb08b1c?fid=1"


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
        self.workdir = "/tmp/vdl_test_netdisk_opts"


class TestNetdiskDirectDetection(unittest.TestCase):
    """_is_netdisk_direct 域名矩阵：只认百度自己的网盘直链域。"""

    def test_hits(self):
        for u in (DLINK, DLINK_CDN, "https://pcs.baidu.com/file/x",
                  "d.pcs.baidu.com", "xaky-cm00.baidupcs.com", "bj.baidupcs.com"):
            self.assertTrue(D._is_netdisk_direct(u), u)

    def test_misses(self):
        # 仿冒域名：末尾不是 baidupcs.com / pcs.baidu.com，或前缀不是「.」开头
        for u in ("https://baidupcs.com.evil.com/file/x",
                  "https://notbaidupcs.com/file/x",
                  "https://evilpcs.baidu.com/file/x",
                  "https://pan.baidu.com/s/1abc",
                  "https://www.youtube.com/watch?v=x",
                  "https://www.bilibili.com/video/BV1",
                  "", "not-a-url"):
            self.assertFalse(D._is_netdisk_direct(u), u)


class TestNetdiskDownloadOptions(unittest.TestCase):
    """网盘直链：单连接 + P2SP UA + 禁分片并发。"""

    def _opts(self, url, downloader_type=""):
        task = _Task(url)
        # 假装本机装了 aria2c：网盘链路仍不许用它（否则多连接反而更慢）
        with mock.patch.object(D, "_aria2c_path", return_value="/usr/bin/aria2c"):
            return D._download_options(task, D.BEST_KEY, _Reporter(),
                                       downloader_type=downloader_type)

    def test_no_aria2c_for_netdisk(self):
        for u in (DLINK, DLINK_CDN):
            o = self._opts(u)
            self.assertNotEqual(o.get("downloader"), "aria2c", u)
            self.assertNotIn("aria2c", o.get("downloader_args", {}), u)

    def test_explicit_aria2c_is_overridden(self):
        # 前端高级选项里可能还留着 aria2c，服务端必须兜住
        o = self._opts(DLINK, downloader_type="aria2c")
        self.assertNotEqual(o.get("downloader"), "aria2c")
        self.assertNotIn("aria2c", o.get("downloader_args", {}))

    def test_netdisk_ua_applied(self):
        for u in (DLINK, DLINK_CDN):
            o = self._opts(u)
            self.assertEqual(o.get("http_headers", {}).get("User-Agent"),
                             D._NETDISK_UA, u)

    def test_single_fragment_concurrency(self):
        o = self._opts(DLINK)
        self.assertEqual(o.get("concurrent_fragment_downloads"), 1)

    def test_non_netdisk_untouched(self):
        # 非网盘：并发策略保持原样（不能被这次改动误伤）
        o = self._opts("https://www.bilibili.com/video/BV1xx411c7mD")
        if sys.platform == "win32":
            # Windows 上 aria2c 是**全面禁用**的（2026-10-18 用户实测：GUI 进程 fork
            # 出的 aria2c.exe 会不断弹控制台黑窗，且窗口里跑不起来挂住不退出）。
            # 所以这条「不受网盘改动影响」的断言在 Windows 上要反过来断：
            # 非网盘也必须走内置下载器，不能被网盘策略误伤成 aria2c。
            self.assertNotEqual(o.get("downloader"), "aria2c")
        else:
            self.assertEqual(o.get("downloader"), "aria2c")
        self.assertNotEqual(o.get("http_headers", {}).get("User-Agent"),
                            D._NETDISK_UA)


class TestSourceWiring(unittest.TestCase):
    """源码级接线：两处判据都必须是 _is_netdisk_direct(_task_host)，别写死字符串。"""

    def test_both_branches_call_detector(self):
        src = _read_source()
        self.assertGreaterEqual(
            src.count("_is_netdisk_direct(_task_host)"), 2,
            "网盘判定缺失：应同时有「强制单连接」与「设 UA + 禁并发」两处")

    def test_no_hardcoded_ua(self):
        src = _read_source()
        self.assertIn('_NETDISK_UA = "netdisk;P2SP;', src)
        self.assertIn('options.setdefault("http_headers", {})["User-Agent"] = _NETDISK_UA', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
