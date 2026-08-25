"""tests/test_upload_src_filename.py — 上传场景：保留 file.filename 到 job 与 step[0].detail。

背景：upload 端点把文件落盘成 upload.<ext>，导致 UI「源视频: ...」只能展示占位名。
约定：upload 端点必须把 file.filename 存进 commentary_jobs[job_id].src_filename，
并在 _commentary_run 中优先用它生成 step[0].detail。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "server"))

import app as srv  # noqa: E402
from routers.commentary import (  # noqa: E402
    create_commentary_upload,
    create_script_only_upload,
)


class _FakeFile:
    """模拟 FastAPI UploadFile，构造时打开 /dev/null 即可"""

    def __init__(self, name: str):
        self.filename = name
        self.file = open("/dev/null", "rb")

    def close(self):
        try:
            self.file.close()
        except Exception:
            pass


def _capture_create_commentary_upload(fake_filename: str, work_dir: Path, title: str = ""):
    """调 create_commentary_upload 端点，捕获传给 _commentary_run 的 kwargs。"""
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    fake_file = _FakeFile(fake_filename)

    with patch.object(srv, "_commentary_run", fake_run), \
         patch.object(srv, "_commentary_lock"), \
         patch.object(srv, "_commentary_work_dir", return_value=work_dir), \
         patch.object(srv, "COMMENTARY_ENABLED", True), \
         patch.object(srv, "COMMENTARY_MODE", "local"), \
         patch.object(srv, "executor") as ex:
        ex.submit.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
        try:
            resp = create_commentary_upload(
                file=fake_file,
                vertical=False,
                voice="",
                trim_start=0.0,
                trim_end=0.0,
                mode="highlights",
                title=title,
            )
        finally:
            fake_file.close()
    return resp, captured


def _capture_create_script_only_upload(fake_filename: str, work_dir: Path, title: str = ""):
    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    fake_file = _FakeFile(fake_filename)

    with patch.object(srv, "_commentary_run", fake_run), \
         patch.object(srv, "_commentary_lock"), \
         patch.object(srv, "_commentary_work_dir", return_value=work_dir), \
         patch.object(srv, "COMMENTARY_ENABLED", True), \
         patch.object(srv, "COMMENTARY_MODE", "local"), \
         patch.object(srv, "executor") as ex:
        ex.submit.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
        try:
            resp = create_script_only_upload(
                file=fake_file,
                vertical=False,
                voice="",
                trim_start=0.0,
                trim_end=0.0,
                mode="highlights",
                commentary_type="deep_hl",
                highlight_source="ai",
                intro_highlight=False,
                skip_intro_outro=False,
                no_narrate_intro_outro=True,
                retain_pct=None,
                web=False,
                one_click=False,
                style="none",
                title=title,
            )
        finally:
            fake_file.close()
    return resp, captured


def test_create_commentary_upload_stores_src_filename(tmp_path):
    """上传 → commentary_jobs 里必须能取到原始文件名 + 透传给 _commentary_run。"""
    real_name = "少帅_国产剧_第8集_共48集_金牌影院_免费在线播放_高清全集_电影网站_在线视频.mp4"
    work = tmp_path / "work"
    work.mkdir()

    resp, captured = _capture_create_commentary_upload(
        fake_filename=real_name,
        work_dir=work,
    )
    job = srv.commentary_jobs[resp["job_id"]]
    assert job.get("src_filename") == real_name, \
        f"job 应当保留原始文件名，实际：{job.get('src_filename')!r}"
    assert captured["kwargs"].get("src_filename") == real_name, \
        f"_commentary_run 应透传 src_filename，实际：{captured['kwargs'].get('src_filename')!r}"


def test_create_script_only_upload_stores_src_filename(tmp_path):
    """脚本-only 上传端点同样需要保留 src_filename。"""
    real_name = "两只老虎.mp4"
    work = tmp_path / "work"
    work.mkdir()

    resp, captured = _capture_create_script_only_upload(
        fake_filename=real_name,
        work_dir=work,
    )
    job = srv.commentary_jobs[resp["job_id"]]
    assert job.get("src_filename") == real_name
    assert captured["kwargs"].get("src_filename") == real_name


def test_create_commentary_upload_falls_back_when_filename_empty(tmp_path):
    """file.filename 为空时 src_filename 应回退到 upload.<ext> 而非空串。"""
    work = tmp_path / "work"
    work.mkdir()

    resp, captured = _capture_create_commentary_upload(
        fake_filename="",
        work_dir=work,
    )
    job = srv.commentary_jobs[resp["job_id"]]
    assert job.get("src_filename") == "upload.mp4"
    assert captured["kwargs"].get("src_filename") == "upload.mp4"


def test_commentary_run_signature_has_src_filename():
    """回归保险：_commentary_run 必须接受 src_filename 形参。"""
    import inspect

    sig = inspect.signature(srv._commentary_run)
    assert "src_filename" in sig.parameters, \
        f"_commentary_run 必须支持 src_filename 形参，当前参数：{list(sig.parameters)}"
    assert sig.parameters["src_filename"].default == "", \
        "src_filename 应有默认值 '' 以兼容旧调用方"