"""验证 _purge_commentary_work 清理逻辑：
- 超过保留期（默认 7 天）的 12 位 hex job 目录被删
- 未到期的保留
- commentary_jobs 中 running 的 src_path 对应目录跳过
- 非 12 位 hex 命名的目录不碰"""
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import app as m


def _make_job_dir(root: Path, name: str, mtime_days_ago: float) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "upload.mp4").write_bytes(b"x")
    old = time.time() - mtime_days_ago * 86400
    os.utime(d, (old, old))
    return d


def test_purge_commentary_work(tmp_path):
    """只删过期的 hex job，保留新目录/运行中目录/非 hex 目录。"""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    old_hex = _make_job_dir(work, "aaaaaaaaaaaa", 30)      # 30 天前 → 删
    new_hex = _make_job_dir(work, "bbbbbbbbbbbb", 1)        # 1 天前 → 留
    running_hex = _make_job_dir(work, "cccccccccccc", 30)   # 30 天前但 running → 留
    not_hex = _make_job_dir(work, "not-a-job", 30)          # 非 hex → 留
    m.commentary_jobs["j1"] = {"src_path": str(running_hex / "upload.mp4"), "status": "running"}

    try:
        with patch.object(m, "COMMENTARY_WORK_DIR", work), \
             patch.object(m, "COMMENTARY_WORK_RETENTION_DAYS", 7):
            removed = m._purge_commentary_work()
        assert removed == 1, f"应只删 1 个（old_hex），got {removed}"
        assert not old_hex.exists(), "30 天前的 hex job 应被删"
        assert new_hex.exists(), "1 天前的应保留"
        assert running_hex.exists(), "running 中的应保留"
        assert not_hex.exists(), "非 hex 目录应保留"
    finally:
        m.commentary_jobs.clear()


def test_purge_retention_days_env(tmp_path):
    """VDL_COMMENTARY_WORK_RETENTION_DAYS 覆盖保留天数。"""
    work = tmp_path / "work"
    work.mkdir(parents=True)
    d5 = _make_job_dir(work, "dddddddddddd", 5)  # 5 天前
    try:
        with patch.object(m, "COMMENTARY_WORK_DIR", work), \
             patch.object(m, "COMMENTARY_WORK_RETENTION_DAYS", 30):
            removed = m._purge_commentary_work()
        assert removed == 0, "保留 30 天时 5 天前的不该删"
    finally:
        m.commentary_jobs.clear()


def test_purge_no_work_dir(tmp_path):
    """work 目录不存在时安全返回 0。"""
    with patch.object(m, "COMMENTARY_WORK_DIR", tmp_path / "absent"):
        assert m._purge_commentary_work() == 0
