"""解说桥接路由测试（TestClient）：mock 掉真实解说算力（subprocess / 独立 worker），只验桥接与分支。

无需装 whisper / ffmpeg / commentary-pipeline，无需联网。
运行：PYTHONPATH=server:tests python -m pytest tests/test_commentary_routes.py -q
"""
import os
import sys
from pathlib import Path

# 在 import app 前把解说开关打开、并默认走 http 模式（最干净的桥接形态）
os.environ.setdefault("VDL_COMMENTARY_ENABLED", "true")
os.environ["VDL_COMMENTARY_MODE"] = "http"
os.environ["VDL_COMMENTARY_ENDPOINT"] = "http://fake-worker.local"

SERVER = str(Path(__file__).resolve().parent.parent / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from unittest.mock import patch  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as m  # noqa: E402

client = TestClient(m.app)


@pytest.fixture(autouse=True)
def _ensure_commentary_config():
    # 强制开启并固定为 http 模式 + 有效 endpoint，避免依赖测试文件导入顺序。
    # 注：pytest 收集顺序不确定时，app 可能在本测试的 env 设置前被其他测试文件（如
    # test_torrent_routes）先 import，导致 COMMENTARY_* 模块全局落到默认值（local / None），
    # 使 create 路由误走 local 分支而返回 503。各专项 503 测试会在自身上下文内 patch 覆盖。
    m.COMMENTARY_ENABLED = True
    m.COMMENTARY_MODE = "http"
    m.COMMENTARY_ENDPOINT = "http://fake-worker.local"
    yield


def _fake_task(status="completed", filepath=None):
    class _T:
        pass
    t = _T()
    t.status = status
    t.filepath = filepath
    return t


def _run_ok(job_id, src, vertical, voice):
    with m._commentary_lock:
        m.commentary_jobs[job_id].update(status="completed", output_path="/tmp/commentary_out/ok.mp4")


def _run_fail(job_id, src, vertical, voice):
    with m._commentary_lock:
        m.commentary_jobs[job_id].update(status="failed", error="boom")


@pytest.fixture
def src_file(tmp_path):
    p = tmp_path / "src.mp4"
    p.write_bytes(b"data")
    return p


@pytest.fixture
def patch_task(src_file):
    with patch.object(m, "_require_task", return_value=_fake_task("completed", src_file)):
        yield


def test_disabled_returns_503():
    with patch.object(m, "COMMENTARY_ENABLED", False):
        r = client.post("/api/commentary", json={"task_id": "x"})
    assert r.status_code == 503


def test_http_mode_missing_endpoint_503():
    with patch.object(m, "COMMENTARY_ENABLED", True), \
         patch.object(m, "COMMENTARY_MODE", "http"), \
         patch.object(m, "COMMENTARY_ENDPOINT", ""):
        r = client.post("/api/commentary", json={"task_id": "x"})
    assert r.status_code == 503
    assert "endpoint" in r.json()["detail"].lower() or "worker" in r.json()["detail"].lower()


def test_local_mode_missing_pipeline_503():
    with patch.object(m, "COMMENTARY_ENABLED", True), \
         patch.object(m, "COMMENTARY_MODE", "local"), \
         patch.object(m, "COMMENTARY_DIR", Path("/no_such_pipeline_xyz")):
        r = client.post("/api/commentary", json={"task_id": "x"})
    assert r.status_code == 503


def test_task_not_found_404(patch_task):
    with patch.object(m, "_require_task", side_effect=HTTPException(status_code=404, detail="no")):
        r = client.post("/api/commentary", json={"task_id": "nope"})
    assert r.status_code == 404


def test_task_not_completed_409():
    with patch.object(m, "_require_task", return_value=_fake_task("downloading", None)):
        r = client.post("/api/commentary", json={"task_id": "x"})
    assert r.status_code == 409


def test_create_runs_and_completes(patch_task):
    with patch.object(m, "executor") as ex, patch.object(m, "_commentary_run") as run:
        ex.submit.side_effect = lambda fn, *a, **k: fn(*a, **k)
        run.side_effect = _run_ok
        r = client.post("/api/commentary", json={"task_id": "t1", "vertical": True})
        assert r.status_code == 200
        assert r.json()["status"] == "running"
        jid = r.json()["job_id"]
        st = client.get(f"/api/commentary/{jid}").json()
    assert st["status"] == "completed"
    assert st["ready"] is True


def test_create_failure_reported(patch_task):
    with patch.object(m, "executor") as ex, patch.object(m, "_commentary_run") as run:
        ex.submit.side_effect = lambda fn, *a, **k: fn(*a, **k)
        run.side_effect = _run_fail
        jid = client.post("/api/commentary", json={"task_id": "t2"}).json()["job_id"]
        st = client.get(f"/api/commentary/{jid}").json()
    assert st["status"] == "failed"
    assert st["error"]


def test_status_unknown_404():
    r = client.get("/api/commentary/does_not_exist")
    assert r.status_code == 404


def test_file_download(tmp_path):
    out = tmp_path / "out.mp4"
    out.write_bytes(b"video-bytes")

    def _run(job_id, src, vertical, voice):
        with m._commentary_lock:
            m.commentary_jobs[job_id].update(status="completed", output_path=str(out))

    src = tmp_path / "s.mp4"
    src.write_bytes(b"x")
    with patch.object(m, "_require_task", return_value=_fake_task("completed", src)), \
         patch.object(m, "executor") as ex, patch.object(m, "_commentary_run") as r2:
        ex.submit.side_effect = lambda fn, *a, **k: fn(*a, **k)
        r2.side_effect = _run
        jid = client.post("/api/commentary", json={"task_id": "t3"}).json()["job_id"]
        fr = client.get(f"/api/commentary/{jid}/file")
    assert fr.status_code == 200
    assert fr.content == b"video-bytes"


def test_file_not_ready_409():
    def _run(job_id, src, vertical, voice):
        with m._commentary_lock:
            m.commentary_jobs[job_id].update(status="running", output_path="")

    src = Path("/tmp/__cs.mp4")
    src.write_bytes(b"x")
    with patch.object(m, "_require_task", return_value=_fake_task("completed", src)), \
         patch.object(m, "executor") as ex, patch.object(m, "_commentary_run") as r2:
        ex.submit.side_effect = lambda fn, *a, **k: fn(*a, **k)
        r2.side_effect = _run
        jid = client.post("/api/commentary", json={"task_id": "t4"}).json()["job_id"]
        fr = client.get(f"/api/commentary/{jid}/file")
    assert fr.status_code == 409


def test_file_missing_410(tmp_path):
    def _run(job_id, src, vertical, voice):
        with m._commentary_lock:
            m.commentary_jobs[job_id].update(status="completed", output_path=str(tmp_path / "gone.mp4"))

    src = tmp_path / "s.mp4"
    src.write_bytes(b"x")
    with patch.object(m, "_require_task", return_value=_fake_task("completed", src)), \
         patch.object(m, "executor") as ex, patch.object(m, "_commentary_run") as r2:
        ex.submit.side_effect = lambda fn, *a, **k: fn(*a, **k)
        r2.side_effect = _run
        jid = client.post("/api/commentary", json={"task_id": "t5"}).json()["job_id"]
        fr = client.get(f"/api/commentary/{jid}/file")
    assert fr.status_code == 410


def test_local_mode_builds_and_locates_output(tmp_path):
    """local 模式：命令构造正确（--auto/--voice），且能从 output/ 定位最新成片。"""
    (tmp_path / "input").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "process.py").write_text("# fake pipeline entry")

    def _fake_subprocess_run(args, **kw):
        # 不真跑 process.py，只按命名规则造一个成片，模拟管线产出
        assert "--auto" in args, "local 模式必须带 --auto"
        assert "--voice" in args, "voice 应注入命令（即便默认音色）"
        infile = args[2]  # [PYTHON, "process.py", in_file, "--auto", ...]
        base = Path(infile).stem
        out = tmp_path / "output" / f"{base}_成片.mp4"
        out.write_bytes(b"rendered")
        class _R:
            returncode = 0
            stderr = ""
            stdout = ""
        return _R()

    src = tmp_path / "s.mp4"
    src.write_bytes(b"x")
    with patch.object(m, "COMMENTARY_ENABLED", True), \
         patch.object(m, "COMMENTARY_MODE", "local"), \
         patch.object(m, "COMMENTARY_DIR", tmp_path), \
         patch.object(m, "_require_task", return_value=_fake_task("completed", src)), \
         patch.object(m, "executor") as ex, \
         patch.object(m, "subprocess") as sp:
        ex.submit.side_effect = lambda fn, *a, **k: fn(*a, **k)
        sp.run.side_effect = _fake_subprocess_run
        jid = client.post("/api/commentary", json={"task_id": "t6", "vertical": False}).json()["job_id"]
        st = client.get(f"/api/commentary/{jid}").json()
    assert st["status"] == "completed"
    # output_path 仅存于内部 jobs 表，status 接口不暴露；直接校验成片定位结果
    assert "成片.mp4" in m.commentary_jobs[jid]["output_path"]


# --- 媒体库 file_id 来源（新增） ---

import base64  # noqa: E402


def _make_file_id(rel: str) -> str:
    return base64.urlsafe_b64encode(rel.encode()).rstrip(b"=").decode()


def test_create_from_file_id_ok(tmp_path):
    """媒体库里已存在的视频文件，通过 file_id 创建解说。"""
    (tmp_path / "videos").mkdir()
    vid = tmp_path / "videos" / "my-video.mp4"
    vid.write_bytes(b"fake-video")
    fid = _make_file_id("videos/my-video.mp4")

    with patch.object(m, "DOWNLOAD_DIR", tmp_path), \
         patch.object(m, "executor") as ex, \
         patch.object(m, "_commentary_run") as run:
        ex.submit.side_effect = lambda fn, *a, **k: fn(*a, **k)
        run.side_effect = _run_ok
        r = client.post("/api/commentary", json={"file_id": fid, "vertical": True})
        assert r.status_code == 200
        assert r.json()["status"] == "running"
        jid = r.json()["job_id"]
        st = client.get(f"/api/commentary/{jid}").json()
    assert st["status"] == "completed"
    assert st["ready"] is True


def test_create_file_id_encrypted_409(tmp_path):
    """加密文件（.vdlenc）不支持生成解说，应返回 409。"""
    enc = tmp_path / "secret.mp4.vdlenc"
    enc.write_bytes(b"encrypted-data")
    fid = _make_file_id("secret.mp4.vdlenc")

    with patch.object(m, "DOWNLOAD_DIR", tmp_path):
        r = client.post("/api/commentary", json={"file_id": fid})
    assert r.status_code == 409
    assert "加密" in r.json()["detail"]


def test_create_file_id_non_video_409(tmp_path):
    """非视频文件（如 .txt）不支持生成解说，应返回 409。"""
    txt = tmp_path / "readme.txt"
    txt.write_text("hello")
    fid = _make_file_id("readme.txt")

    with patch.object(m, "DOWNLOAD_DIR", tmp_path):
        r = client.post("/api/commentary", json={"file_id": fid})
    assert r.status_code == 409
    assert "视频" in r.json()["detail"]


def test_create_no_source_400():
    """既不给 task_id 也不给 file_id，应返回 400。"""
    r = client.post("/api/commentary", json={})
    assert r.status_code == 400


# --- 独立「解说成片」标签页：list + file/{id} ---

def test_list_commentary_outputs(tmp_path):
    """扫描 COMMENTARY_LOCAL_OUTPUT 返回按时间倒序的成片列表。"""
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.mp4").write_bytes(b"aaa")
    (out / "b.mov").write_bytes(b"bbb")
    (out / "skip.txt").write_text("skip")
    import os as _os
    _os.utime(out / "a.mp4", (1700000000, 1700000001))
    _os.utime(out / "b.mov", (1700000000, 1700000002))

    with patch.object(m, "COMMENTARY_LOCAL_OUTPUT", out):
        r = client.get("/api/commentary/list")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert items[0]["name"] == "b.mov"
    assert items[1]["name"] == "a.mp4"
    assert all("id" in it and "size" in it and "mtime" in it for it in items)


def test_file_by_id(tmp_path):
    """通过 list 返回的 id 直接播放/下载成片，且拒绝路径穿越。"""
    out = tmp_path / "out"
    out.mkdir()
    vid = out / "ok.mp4"
    vid.write_bytes(b"video")

    with patch.object(m, "COMMENTARY_LOCAL_OUTPUT", out):
        lst = client.get("/api/commentary/list").json()["items"]
        cid = lst[0]["id"]
        r = client.get(f"/api/commentary/file/{cid}")
    assert r.status_code == 200
    assert r.content == b"video"


def test_file_by_id_escape(tmp_path):
    """路径穿越 id 应返回 403/404，不能访问输出目录外的文件。"""
    out = tmp_path / "out"
    out.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("leak")

    bad_cid = base64.urlsafe_b64encode(str(secret).encode()).decode()
    with patch.object(m, "COMMENTARY_LOCAL_OUTPUT", out):
        r = client.get(f"/api/commentary/file/{bad_cid}")
    assert r.status_code in (403, 404)
