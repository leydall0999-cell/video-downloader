"""验证下载拆分为「解析元数据」+「下载音视频」两阶段，步骤状态准确过渡。"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SERVER = str(Path(__file__).resolve().parent.parent / "server")
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

import downloader as dl  # noqa: E402
from tasks import TaskStore  # noqa: E402


def _step_status(task, name):
    for s in task.steps:
        if s.get("name") == name:
            return s.get("status"), s.get("detail", "")
    return None, ""


def test_run_once_splits_extract_and_download():
    """extract_info(..., download=False) 后应标记解析完成并进入下载阶段，再调用 process_info。"""
    root = Path(__file__).resolve().parent / "_phases_tmp"
    root.mkdir(parents=True, exist_ok=True)
    store = TaskStore(root)
    task = store.create(
        url="http://example.com/video",
        title="",
        platform="test",
        quality="1080P",
        quality_key="1080",
    )

    fake_info = {
        "title": "测试标题",
        "requested_downloads": [{"filepath": str(root / "测试标题.mp4")}],
    }
    # 造一个空文件，让 _locate_output 能找到成品
    (root / "测试标题.mp4").write_bytes(b"x")

    fake_ydl = MagicMock()
    fake_ydl.extract_info.return_value = fake_info
    fake_ydl.process_info.return_value = None
    fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
    fake_ydl.__exit__ = MagicMock(return_value=None)

    with patch.object(dl, "_YoutubeDL", return_value=fake_ydl):
        dl._run_once(task, store, "1080")

    # 调用了 extract_info 且 download=False
    fake_ydl.extract_info.assert_called_once_with(task.url, download=False)
    # 解析完成后调用了 process_info 真正下载
    fake_ydl.process_info.assert_called_once_with(fake_info)

    assert task.title == "测试标题"
    assert _step_status(task, "解析视频信息")[0] == "done"
    # 下载阶段已启动（具体 done 由进度/后处理 hook 推进，mock 不触发 hook）
    assert _step_status(task, "下载音视频")[0] == "running"
    assert task.status == "completed"


def test_run_once_marks_extract_error_on_failure():
    """extract_info 阶段失败应把「解析视频信息」标红，不应进入下载阶段。"""
    root = Path(__file__).resolve().parent / "_phases_tmp"
    root.mkdir(parents=True, exist_ok=True)
    store = TaskStore(root)
    task = store.create(
        url="http://example.com/bad",
        title="",
        platform="test",
        quality="best",
        quality_key="best",
    )

    fake_ydl = MagicMock()
    from yt_dlp.utils import DownloadError

    fake_ydl.extract_info.side_effect = DownloadError("无法解析")
    fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
    fake_ydl.__exit__ = MagicMock(return_value=None)

    with patch.object(dl, "_YoutubeDL", return_value=fake_ydl):
        dl._run_once(task, store, "best")

    fake_ydl.extract_info.assert_called_once_with(task.url, download=False)
    fake_ydl.process_info.assert_not_called()

    assert _step_status(task, "解析视频信息")[0] == "error"
    assert task.status == "failed"
