#!/usr/bin/env python
"""网盘直链「按文件直下」守卫测试（2026-10-24）

用户实测报错：「视频解析失败 — [generic] 3eab43fb6m1795...」
任务记录里暴露的真实症状：
  - filename 变成 `3eab43fb6m17959086004425e3138a2d.mp4`（直链路径段 + 错误后缀）
  - 真实文件名「008.爱的故事-孙耀威.mp3」丢了，mp3 被存成 mp4

根因：网盘 dlink 仍被交给 yt-dlp，generic 提取器把直链当媒体页处理。
修法：`run_download` 开头对网盘直链短路，走 `_download_netdisk_direct`
（单连接 + P2SP UA + 手动 Cookie + 用解析阶段的真实文件名落盘）。

本文件是纯离线守卫，不发任何网络请求。
"""
import os
import sys
import unittest

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_netdisk_direct")

import downloader as D  # noqa: E402
from tasks import DownloadTask  # noqa: E402

DLINK = "https://d.pcs.baidu.com/file/3eab43fb6m17959086004425e3138a2d?fid=1&sign=X"


def _task(title: str, url: str = DLINK) -> DownloadTask:
    return DownloadTask(id="t1", url=url, title=title, platform="百度网盘",
                        quality="最佳画质（自动）")


def _src() -> str:
    with open(os.path.join(SERVER_DIR, "downloader.py"), encoding="utf-8") as f:
        return f.read()


class TestOutputName(unittest.TestCase):
    def test_keeps_real_name_with_ext(self):
        """解析阶段带来的真实文件名（含扩展名）必须原样保留。"""
        t = _task("008.爱的故事-孙耀威.mp3")
        self.assertEqual(D._netdisk_output_name(t, "audio/mpeg", DLINK),
                         "008.爱的故事-孙耀威.mp3")

    def test_mp3_not_turned_into_mp4(self):
        """回归核心：mp3 绝不能被改成 .mp4。"""
        t = _task("008.爱的故事-孙耀威.mp3")
        self.assertFalse(D._netdisk_output_name(t, "audio/mpeg", DLINK).endswith(".mp4"))

    def test_adds_ext_from_content_type_when_missing(self):
        t = _task("008.爱的故事-孙耀威")
        self.assertEqual(D._netdisk_output_name(t, "audio/mpeg; charset=utf-8", DLINK),
                         "008.爱的故事-孙耀威.mp3")

    def test_no_title_falls_back_to_url_and_ct(self):
        t = _task("")
        name = D._netdisk_output_name(t, "application/pdf", DLINK)
        self.assertTrue(name.endswith(".pdf"), name)
        self.assertNotIn("?", name)

    def test_strips_path_separators(self):
        """标题里带路径分隔符时不能越界写到别处。"""
        t = _task("../../etc/passwd.mp3")
        name = D._netdisk_output_name(t, "audio/mpeg", DLINK)
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertTrue(name.endswith(".mp3"))

    def test_unknown_ext_is_completed_by_ct(self):
        """标题里那个"扩展名"其实是乱码段（如 .3eab43fb6m）时，按 CT 补正确的。"""
        t = _task("3eab43fb6m17959086004425e3138a2d")
        name = D._netdisk_output_name(t, "audio/mpeg", DLINK)
        self.assertTrue(name.endswith(".mp3"), name)


class TestSourcing(unittest.TestCase):
    def test_run_download_short_circuits_netdisk(self):
        """run_download 必须在进入 yt-dlp 之前对网盘直链短路。"""
        src = _src()
        idx = src.find("def run_download(")
        self.assertNotEqual(idx, -1)
        head = src[idx:idx + 1200]
        self.assertIn("_is_netdisk_direct(task.url)", head,
                      "run_download 没有网盘直链短路，会继续走 yt-dlp 解析")
        self.assertIn("_download_netdisk_direct(", head)

    def test_direct_download_headers(self):
        """直链下载的三个硬要求（2026-10-24 实测，缺一即 403）：

        1) 必须带 Range —— 裸 GET 会被判"无权限/过期"（伪错误码 31326/31360）；
        2) 不能用 P2SP 那个 UA —— 同一链接 P2SP UA 403、浏览器 UA 206；
        3) 必须带登录态 Cookie。
        """
        src = _src()
        idx = src.find("def _download_netdisk_direct(")
        body = src[idx:idx + 3000]
        self.assertIn('"Range": "bytes=0-"', body, "缺 Range：百度会直接 403")
        self.assertIn("_NETDISK_DOWNLOAD_UA", body, "必须用浏览器 UA，不能用 P2SP")
        self.assertNotIn('"User-Agent": _NETDISK_UA', body, "P2SP UA 会被 403")
        self.assertIn('headers["Cookie"] = cookie', body)
        # 单连接：不允许出现并发下载器参数
        self.assertNotIn("concurrent_fragments", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
