"""标题传播链路验证（unittest 风格，无需 pytest）。

运行：PYTHONPATH=server:tests python -m unittest tests.test_title_propagation -v

验证点：
1. /api/download 接收 title 字段并写入 task
2. /api/commentary 优先从 task.title 推导成片名，并作为 title 传给 _commentary_run
3. task.title 为空时，_commentary_title 能从步骤详情反解标题
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("VDL_COMMENTARY_ENABLED", "true")
os.environ["VDL_COMMENTARY_MODE"] = "http"
os.environ["VDL_COMMENTARY_ENDPOINT"] = "http://fake-worker.local"

SERVER = str(Path(__file__).resolve().parent.parent / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from unittest.mock import patch, MagicMock  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as m  # noqa: E402

m.COMMENTARY_ENABLED = True
m.COMMENTARY_MODE = "http"
m.COMMENTARY_ENDPOINT = "http://fake-worker.local"

client = TestClient(m.app)


def _fake_task(status, filepath, title, steps=None, url="https://example.com/video"):
    class _T:
        pass
    t = _T()
    t.status = status
    t.filepath = filepath
    t.title = title
    t.url = url
    t.steps = steps or []
    return t


class TitlePropagationTest(unittest.TestCase):
    def test_download_accepts_title(self):
        """/api/download 把 title 写进 task"""
        url = "https://www.bilibili.com/video/BV1xx411c7mD"
        title = "测试剧集 - 第3集"
        with patch.object(m.scheduler, "submit") as submit:
            r = client.post("/api/download", json={
                "url": url,
                "quality": "best",
                "title": title,
            })
            self.assertEqual(r.status_code, 200, r.text)
            args, _ = submit.call_args
            task = args[1]
            self.assertEqual(task.title, title)

    def test_commentary_title_prefers_task_title(self):
        """_commentary_title 优先取 task.title"""
        src = Path("/tmp/51b0038ee08b.mp4")
        t = _fake_task("completed", src, "甄嬛传 第3集")
        with patch.object(m, "_require_task", return_value=t):
            payload = MagicMock()
            payload.task_id = "t1"
            payload.file_id = ""
            got = m._commentary_title(payload, str(src))
        self.assertEqual(got, "甄嬛传 第3集")

    def test_commentary_title_fallback_from_step_detail(self):
        """task.title 为空时，从步骤详情反解标题"""
        src = Path("/tmp/51b0038ee08b.mp4")
        steps = [{"name": "解析视频信息", "detail": "已获取标题《伪装者 第12集》"}]
        t = _fake_task("completed", src, "", steps=steps)
        with patch.object(m, "_require_task", return_value=t):
            payload = MagicMock()
            payload.task_id = "t2"
            payload.file_id = ""
            got = m._commentary_title(payload, str(src))
        self.assertEqual(got, "伪装者 第12集")

    def test_commentary_passes_title_to_run(self):
        """/api/commentary 把 task.title 作为 title 参数传给 _commentary_run"""
        src = Path("/tmp/51b0038ee08b.mp4")
        if not src.exists():
            src.write_bytes(b"x")
        t = _fake_task("completed", src, "甄嬛传 第3集")
        captured = {}

        def _capture_run(job_id, _src, _vertical, _voice, **kwargs):
            captured.update(kwargs)
            captured["job_id"] = job_id
            with m._commentary_lock:
                m.commentary_jobs[job_id].update(status="completed", output_path="/tmp/ok.mp4")

        with patch.object(m, "_require_task", return_value=t), \
             patch.object(m, "executor") as ex, \
             patch.object(m, "_commentary_run") as run:
            ex.submit.side_effect = lambda fn, *a, **k: fn(*a, **k)
            run.side_effect = _capture_run
            r = client.post("/api/commentary", json={"task_id": "t1"})
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(captured.get("title"), "甄嬛传 第3集")


if __name__ == "__main__":
    unittest.main()
