"""上传视频直接转码端点 /api/upload-convert 的测试。

复用现有 ffmpeg 管线（_run_convert / CONVERT_JOBS / 状态与下载路由），
本测试只 mock subprocess.run（假装 ffmpeg 产出文件），验证：
- 参数校验（非法 target / 非视频 / 超大小上限）
- 正常流程：提交→轮询到 completed→下载文件
- to_library：完成后复制进媒体库（DOWNLOAD_DIR）并回传 library_id
- 不影响既有 /api/convert（task 模式）的 _run_convert 默认参数
"""
import time
from pathlib import Path

import app
from fastapi.testclient import TestClient


def _client():
    return TestClient(app.app)


def _redirect_dirs(tmp_path):
    """把下载/转码/上传目录指向临时目录，避免污染真实 ~/Downloads。"""
    app.DOWNLOAD_DIR = tmp_path
    app.CONVERT_DIR = tmp_path / "conversions"
    app.UPLOAD_TMP = tmp_path / "uploads"
    app.CONVERT_DIR.mkdir(parents=True, exist_ok=True)
    app.UPLOAD_TMP.mkdir(parents=True, exist_ok=True)


def _fake_ffmpeg(monkeypatch, tmp_path):
    """mock subprocess.run：按命令最后一个参数（输出路径）写个假文件。"""
    orig = tmp_path

    def fake_run(cmd, *a, **k):
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-converted")

        class R:
            returncode = 0
            stderr = ""

        return R()

    monkeypatch.setattr(app.subprocess, "run", fake_run)


def _wait_completed(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/convert/{job_id}")
        if r.status_code == 200 and r.json()["status"] in ("completed", "failed"):
            return r.json()
        time.sleep(0.05)
    return client.get(f"/api/convert/{job_id}").json()


# ---- 参数校验 ----

def test_upload_convert_bad_target(tmp_path, monkeypatch):
    _redirect_dirs(tmp_path)
    client = _client()
    r = client.post("/api/upload-convert",
                    files={"file": ("a.mp4", b"x" * 50, "video/mp4")},
                    data={"target": "docx"})
    assert r.status_code == 400


def test_upload_convert_non_video(tmp_path, monkeypatch):
    _redirect_dirs(tmp_path)
    client = _client()
    r = client.post("/api/upload-convert",
                    files={"file": ("a.txt", b"hello", "text/plain")},
                    data={"target": "mp4"})
    assert r.status_code == 409


def test_upload_convert_size_limit(tmp_path, monkeypatch):
    _redirect_dirs(tmp_path)
    app.UPLOAD_MAX_BYTES = 10  # 故意调小，>10 字节即超限
    try:
        client = _client()
        r = client.post("/api/upload-convert",
                        files={"file": ("a.mp4", b"x" * 100, "video/mp4")},
                        data={"target": "mp4"})
        assert r.status_code == 413
    finally:
        app.UPLOAD_MAX_BYTES = int(__import__("os").environ.get("VDL_UPLOAD_MAX_BYTES") or 2_000_000_000)


# ---- 正常流程 ----

def test_upload_convert_happy_path(tmp_path, monkeypatch):
    _redirect_dirs(tmp_path)
    _fake_ffmpeg(monkeypatch, tmp_path)
    client = _client()
    r = client.post("/api/upload-convert",
                    files={"file": ("clip.mp4", b"x" * 200, "video/mp4")},
                    data={"target": "mp4", "resolution": "720", "bitrate": "1M",
                          "audio": "true", "rotate": "90"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running" and body["job_id"]
    job_id = body["job_id"]

    status = _wait_completed(client, job_id)
    assert status["status"] == "completed", status
    assert status["library_id"] == ""  # 未要求入库

    # 下载产物
    dl = client.get(f"/api/convert/{job_id}/file")
    assert dl.status_code == 200
    assert dl.content == b"fake-converted"


def test_upload_convert_to_library(tmp_path, monkeypatch):
    _redirect_dirs(tmp_path)
    _fake_ffmpeg(monkeypatch, tmp_path)
    client = _client()
    r = client.post("/api/upload-convert",
                    files={"file": ("clip.mp4", b"x" * 200, "video/mp4")},
                    data={"target": "mkv", "to_library": "true"})
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    status = _wait_completed(client, job_id)
    assert status["status"] == "completed", status
    assert status["library_id"]  # 入库后应有库内 id
    # 文件确实落到了媒体库根目录
    assert (app.DOWNLOAD_DIR / status["filename"]).exists()


def test_run_convert_default_params_no_break(tmp_path, monkeypatch):
    """既有 task 模式 /api/convert 调用 _run_convert(job_id, src, target, resolution)
    仍应正常工作（新增参数走默认）。"""
    _redirect_dirs(tmp_path)
    _fake_ffmpeg(monkeypatch, tmp_path)
    job_id = "abc123def456"
    src = tmp_path / "src.mp4"
    src.write_bytes(b"src")
    out = app.CONVERT_DIR / f"t_conv_{job_id}.mp4"
    with app.CONVERT_LOCK:
        app.CONVERT_JOBS[job_id] = {"status": "running", "out_path": str(out),
                                    "error": "", "filename": out.name,
                                    "to_library": False, "library_id": ""}
    app._run_convert(job_id, str(src), "mp4", "original")  # 仅 4 个位置参数
    assert app.CONVERT_JOBS[job_id]["status"] == "completed"
    assert out.exists()
