"""DownloadTask 过程展示字段单元测试。"""
import sys
from pathlib import Path

SERVER = str(Path(__file__).resolve().parent.parent / "server")
TESTS = str(Path(__file__).resolve().parent)
for p in [SERVER, TESTS]:
    if p not in sys.path:
        sys.path.insert(0, p)

from tasks import DownloadTask  # noqa: E402


def test_task_initial_step():
    t = DownloadTask(id="abc", url="https://x", title="t", platform="x", quality="best")
    assert len(t.steps) == 1
    assert t.steps[0]["name"] == "排队等待"
    assert t.steps[0]["status"] == "pending"


def test_add_step_appends():
    t = DownloadTask(id="abc", url="https://x", title="t", platform="x", quality="best")
    t.add_step("解析视频信息", "running", "解析中")
    assert len(t.steps) == 2
    assert t.steps[1]["name"] == "解析视频信息"
    assert t.steps[1]["status"] == "running"


def test_add_step_updates_same_name():
    t = DownloadTask(id="abc", url="https://x", title="t", platform="x", quality="best")
    t.add_step("解析视频信息", "running")
    t.add_step("解析视频信息", "done", "完成")
    assert len(t.steps) == 2
    assert t.steps[1]["status"] == "done"
    assert t.steps[1]["detail"] == "完成"


def test_log_truncates():
    t = DownloadTask(id="abc", url="https://x", title="t", platform="x", quality="best")
    for i in range(250):
        t.log(f"line {i}")
    assert len(t.logs) == 200
    assert "line 249" in t.logs[-1]


def test_to_public_dict_includes_steps_and_logs():
    t = DownloadTask(id="abc", url="https://x", title="t", platform="x", quality="best")
    t.add_step("下载完成", "done")
    t.log("done")
    d = t.to_public_dict()
    assert "steps" in d
    assert "logs" in d
    assert d["steps"][-1]["name"] == "下载完成"
    assert len(d["logs"]) == 1
