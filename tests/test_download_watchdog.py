"""下载停滞看门狗 + 整体硬超时的回归测试。

模拟一个「下到一半就卡死、进度回调再也不来」的下载（最贴近 chrqj m3u8 假死场景），
验证 run_download 不会永久挂起：看门狗置取消标记，硬超时兜底把任务标 failed 并释放并发槽。
"""

import os
import sys
import time
from pathlib import Path

import pytest

# server 包在 PYTHONPATH 上，确保能 import
_SERVER = str(Path(__file__).resolve().parent.parent / "server")
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

import downloader as dl  # noqa: E402
from tasks import TaskStore  # noqa: E402


def test_run_download_stall_does_not_hang(monkeypatch):
    # 把超时与轮询调小，让测试在 ~2 秒内完成
    monkeypatch.setattr(dl, "DOWNLOAD_STALL_TIMEOUT", 0.3)
    monkeypatch.setattr(dl, "DOWNLOAD_HARD_TIMEOUT", 1.5)
    monkeypatch.setattr(dl, "WATCHDOG_POLL", 0.2)

    # 用一个「卡死」的 _run_once 替代真实下载：进入 downloading 后永远 sleep，
    # 且不检查 cancel_requested（模拟进度回调彻底不来）
    def fake_run_once(task, store, quality_key, cookie="", proxy="", format_id="", concurrent_fragments=0, downloader_type="", resume=False):
        store.update(task.id, status="downloading")
        time.sleep(30)  # 假死

    monkeypatch.setattr(dl, "_run_once", fake_run_once)

    root = Path(__file__).resolve().parent / "_wd_tmp"
    root.mkdir(parents=True, exist_ok=True)
    store = TaskStore(root)
    task = store.create(url="http://example.com/x", title="卡死测试", platform="test", quality="best")

    dl.run_download(task, store, "best")

    t = store.get(task.id)
    assert t.status == "failed", f"期望 failed，实际 {t.status}"
    assert t.cancel_requested is True, "看门狗/硬超时应当置取消标记"
    assert "超时" in (t.error or ""), f"错误应提及超时，实际：{t.error}"


def test_run_download_stall_canceled_via_hook(monkeypatch):
    """进度回调会检查 cancel_requested：看门狗置取消后，真实 _run_once 的 hook 会抛终止。"""
    monkeypatch.setattr(dl, "DOWNLOAD_STALL_TIMEOUT", 0.3)
    monkeypatch.setattr(dl, "WATCHDOG_POLL", 0.2)

    # 模拟「下载中、每 0.1s 来一次进度回调、但字节数不涨」——看门狗会判停滞并置取消，
    # 下一次回调里 _ProgressReporter 检查 cancel_requested 抛 DownloadCanceled
    import threading

    class _FakeReporter:
        def __init__(self):
            self._stop = threading.Event()

        def __call__(self, payload):
            from downloader import DownloadCanceled
            if getattr(self, "_task", None) and self._task.cancel_requested:
                raise DownloadCanceled()
            # 模拟进度回调：标记 downloading 但不推进字节
            if self._task.status != "downloading":
                self._task.status = "downloading"

        def on_postprocess(self, payload):
            pass

    def fake_run_once(task, store, quality_key, cookie="", proxy="", format_id="", concurrent_fragments=0, downloader_type="", resume=False):
        reporter = _FakeReporter()
        reporter._task = task
        store.update(task.id, status="downloading")
        # 反复触发回调，模拟持续但无进度的下载
        try:
            for _ in range(400):
                if task.cancel_requested:
                    raise dl.DownloadCanceled()
                reporter({})  # 触发回调检查 cancel_requested
                time.sleep(0.01)
        except dl.DownloadCanceled:
            store.update(task.id, status="canceled", error="已取消下载", progress=0.0)

    monkeypatch.setattr(dl, "_run_once", fake_run_once)

    root = Path(__file__).resolve().parent / "_wd_tmp"
    root.mkdir(parents=True, exist_ok=True)
    store = TaskStore(root)
    task = store.create(url="http://example.com/y", title="停滞回调测试", platform="test", quality="best")

    dl.run_download(task, store, "best")

    t = store.get(task.id)
    # 看门狗把 cancel_requested 置 True 后，任务应被终止（canceled 或 failed，均非 running/pending）
    assert t.status in ("canceled", "failed"), f"期望终止态，实际 {t.status}"
    assert t.cancel_requested is True
