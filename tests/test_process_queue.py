"""加工队列测试：并发控制 + 批量提交 + 路由。mock 掉 ffmpeg，无需真装。

运行：PYTHONPATH=server:tests python -m pytest tests/test_process_queue.py -q
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SERVER = str(Path(__file__).resolve().parent.parent / "server")
TESTS = str(Path(__file__).resolve().parent)
for p in [SERVER, TESTS]:
    if p not in sys.path:
        sys.path.insert(0, p)

from unittest.mock import patch  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---- 模块级 setup -----------------------------------------------------------

import process_queue as pq_mod  # noqa: E402

_exec = ThreadPoolExecutor(max_workers=4)


# ---- ProcessQueue 单元测试 ---------------------------------------------------

class TestProcessQueueUnit:

    def setup_method(self):
        self.q = pq_mod.ProcessQueue(_exec, default_concurrency=2, hard_max=4)

    def _make_slow_fn(self):
        """返回一个会 sleep 0.1s 再标记完成的 mock 函数。"""
        q = self.q
        def _fn(job_id, src, op, params):
            time.sleep(0.1)
            with q.lock:
                if job_id in q.jobs:
                    q.jobs[job_id].update(status="completed", name=f"done-{job_id}")
        return _fn

    def test_concurrency_caps_active(self):
        import uuid
        fn = self._make_slow_fn()
        for i in range(6):
            jid = uuid.uuid4().hex[:8]
            self.q.submit(jid, f"test-{i}", f"lib-{i}", "audio", fn,
                          jid, "/tmp/x.mp4", "audio", {})

        time.sleep(0.05)
        with self.q.lock:
            active = sum(1 for j in self.q.jobs.values() if j["status"] == "running")
        assert active <= 2
        pending = sum(1 for j in self.q.jobs.values() if j["status"] == "pending")
        assert pending > 0

    def test_all_complete_eventually(self):
        import uuid
        ids = [uuid.uuid4().hex[:8] for _ in range(3)]
        fn = self._make_slow_fn()
        for i, jid in enumerate(ids):
            self.q.submit(jid, f"test-{i}", f"lib-{i}", "audio", fn,
                          jid, "/tmp/x.mp4", "audio", {})

        for _ in range(30):
            with self.q.lock:
                done = all(self.q.jobs.get(jid, {}).get("status") == "completed"
                           for jid in ids)
            if done:
                break
            time.sleep(0.1)
        with self.q.lock:
            for jid in ids:
                assert self.q.jobs[jid]["status"] == "completed"

    def test_submit_batch_returns_all_ids(self):
        import uuid
        items = []
        for i in range(5):
            items.append({
                "job_id": uuid.uuid4().hex[:8],
                "name": f"batch-{i}",
                "lib_id": f"lib-{i}",
                "op": "audio",
            })
        fn = self._make_slow_fn()
        ids = self.q.submit_batch(items, fn, "x", "/tmp/x.mp4", "audio", {})
        assert len(ids) == 5
        with self.q.lock:
            for jid in ids:
                assert jid in self.q.jobs
                assert self.q.jobs[jid]["status"] in ("pending", "running", "completed")

    def test_concurrency_adjustment(self):
        assert self.q.concurrency == 2
        self.q.set_concurrency(3)
        assert self.q.concurrency == 3
        self.q.set_concurrency(10)
        assert self.q.concurrency == 4
        self.q.set_concurrency(0)
        assert self.q.concurrency == 1

    def test_get_queue_snapshot(self):
        import uuid
        fn = self._make_slow_fn()
        for i in range(3):
            self.q.submit(uuid.uuid4().hex[:8], f"q-{i}", f"lib-{i}", "gif",
                          fn, uuid.uuid4().hex[:8], "/tmp/x.mp4", "gif", {})

        snap = self.q.get_queue()
        assert "jobs" in snap
        assert "running" in snap
        assert "pending" in snap
        assert snap["concurrency"] == 2
        assert snap["hard_max"] == 4
        assert snap["running"] + snap["pending"] <= 3

    def test_submit_sets_pending(self):
        import uuid
        jid = uuid.uuid4().hex[:8]
        self.q.submit(jid, "x", "lib-x", "audio", lambda *a: None,
                      jid, "/tmp/x.mp4", "audio", {})
        with self.q.lock:
            assert self.q.jobs[jid]["status"] in ("pending", "running", "completed")

    def test_submit_includes_steps_and_logs(self):
        import uuid
        jid = uuid.uuid4().hex[:8]
        self.q.submit(jid, "x", "lib-x", "audio", lambda *a: None,
                      jid, "/tmp/x.mp4", "audio", {})
        with self.q.lock:
            job = self.q.jobs[jid]
            assert "steps" in job
            assert "logs" in job
            assert len(job["steps"]) == 4
            assert job["steps"][0]["name"] == "排队等待"
            assert job["steps"][0]["status"] == "pending"
            assert job["steps"][2]["name"] == "执行 audio"
        snap = self.q.get_queue()
        found = next((j for j in snap["jobs"] if j["job_id"] == jid), None)
        assert found is not None
        assert "steps" in found
        assert "logs" in found


# ---- 路由集成测试 -----------------------------------------------------------

@pytest.fixture(scope="module")
def app_client():
    import app as m  # noqa: E402
    return TestClient(m.app), m


def test_process_route_invalid_op(app_client, tmp_path):
    client, m = app_client
    import base64
    vid = tmp_path / "invalid.mp4"
    vid.write_bytes(b"data")
    fid = base64.urlsafe_b64encode(b"invalid.mp4").rstrip(b"=").decode()

    with patch.object(m, "DOWNLOAD_DIR", tmp_path), \
         patch.dict("os.environ", {"VDL_LIBRARY_ENABLED": "1"}, clear=False):
        r = client.post("/api/process/run", json={"lib_id": fid, "op": "invalid_op_xyz"})
    assert r.status_code == 400


def test_process_single_lib_id_returns_job(app_client, tmp_path):
    client, m = app_client
    import base64
    vid = tmp_path / "single.mp4"
    vid.write_bytes(b"data")
    fid = base64.urlsafe_b64encode(b"single.mp4").rstrip(b"=").decode()

    with patch.object(m, "DOWNLOAD_DIR", tmp_path), \
         patch.object(m, "_run_process") as rp, \
         patch.dict("os.environ", {"VDL_LIBRARY_ENABLED": "1"}, clear=False):
        rp.side_effect = lambda jid, src, op, params: None
        resp = client.post("/api/process/run", json={"lib_id": fid, "op": "audio"})
    assert resp.status_code == 200
    data = resp.json()
    assert "job_id" in data
    assert data["status"] == "running"


def test_process_batch_lib_ids_returns_jobs(app_client, tmp_path):
    client, m = app_client
    import base64
    ids = []
    for i in range(3):
        f = tmp_path / f"batch{i}.mp4"
        f.write_bytes(b"data")
        ids.append(base64.urlsafe_b64encode(f"batch{i}.mp4".encode()).rstrip(b"=").decode())

    with patch.object(m, "DOWNLOAD_DIR", tmp_path), \
         patch.object(m, "_run_process") as rp, \
         patch.dict("os.environ", {"VDL_LIBRARY_ENABLED": "1"}, clear=False):
        rp.side_effect = lambda jid, src, op, params: None
        resp = client.post("/api/process/run", json={"lib_ids": ids, "op": "audio"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["total"] == 3
    assert len(data["jobs"]) == 3


def test_process_no_lib_id_400(app_client):
    client, m = app_client
    with patch.dict("os.environ", {"VDL_LIBRARY_ENABLED": "1"}, clear=False):
        r = client.post("/api/process/run", json={"op": "audio"})
    assert r.status_code == 400


def test_process_queue_route(app_client, tmp_path):
    client, m = app_client
    import base64
    vid = tmp_path / "qtest.mp4"
    vid.write_bytes(b"data")
    fid = base64.urlsafe_b64encode(b"qtest.mp4").rstrip(b"=").decode()

    with patch.object(m, "DOWNLOAD_DIR", tmp_path), \
         patch.object(m, "_run_process") as rp, \
         patch.dict("os.environ", {"VDL_LIBRARY_ENABLED": "1"}, clear=False):
        rp.side_effect = lambda jid, src, op, params: None
        client.post("/api/process/run", json={"lib_id": fid, "op": "gif"})
        resp = client.get("/api/process/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert "jobs" in data
    assert "concurrency" in data
    assert len(data["jobs"]) >= 1


def test_process_concurrency_route(app_client):
    client, m = app_client
    with patch.dict("os.environ", {"VDL_LIBRARY_ENABLED": "1"}, clear=False):
        resp = client.post("/api/process/concurrency", json={"n": 3})
    assert resp.status_code == 200
    assert resp.json()["concurrency"] == 3
