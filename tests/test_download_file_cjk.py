"""测试 /api/tasks/{id}/file 中文/CJK 文件名下载不 500。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import app
from fastapi.testclient import TestClient


def _make_completed_task(filename: str) -> str:
    task = app.store.create(
        url="https://example.com/video",
        title="测试",
        platform="test",
        quality="best",
        quality_key="best",
    )
    out = task.workdir / filename
    out.write_bytes(b"fake video bytes")
    task.status = "completed"
    task.filepath = out
    task.filename = out.name
    task.filesize = out.stat().st_size
    return task.id


def test_download_ascii_filename():
    tid = _make_completed_task("normal.mp4")
    client = TestClient(app.app)
    r = client.get(f"/api/tasks/{tid}/file?download=1")
    assert r.status_code == 200, r.text
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert r.content == b"fake video bytes"


def test_download_cjk_filename():
    tid = _make_completed_task("中文视频_测试.mp4")
    client = TestClient(app.app)
    r = client.get(f"/api/tasks/{tid}/file?download=1")
    assert r.status_code == 200, r.text
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert "filename*=UTF-8''" in cd
    assert r.content == b"fake video bytes"


def test_download_cjk_with_space_and_symbol():
    tid = _make_completed_task("我的视频 (2026) #抖音.mp4")
    client = TestClient(app.app)
    r = client.get(f"/api/tasks/{tid}/file?download=1")
    assert r.status_code == 200, r.text
    cd = r.headers.get("content-disposition", "")
    assert "filename*=UTF-8''" in cd
    # 确认 body 正确
    assert r.content == b"fake video bytes"


if __name__ == "__main__":
    test_download_ascii_filename()
    test_download_cjk_filename()
    test_download_cjk_with_space_and_symbol()
    print("download_file CJK tests passed")
