"""下载任务的内存状态仓库。

只保存"当前进程内"的任务，重启即清空；文件落在 downloads/<task_id>/ 下，
配合 TTL 定期清理，避免磁盘无限增长。
"""

from __future__ import annotations

import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

TaskStatus = Literal["pending", "downloading", "merging", "paused", "completed", "failed", "canceled"]
StepStatus = Literal["pending", "running", "done", "error"]

TASK_TTL_SECONDS = 60 * 60  # 成品文件保留 1 小时
TASK_ID_LENGTH = 16
TASK_DIR_PATTERN = re.compile(rf"^[0-9a-f]{{{TASK_ID_LENGTH}}}$")
_MAX_LOG_LINES = 200


@dataclass
class DownloadTask:
    id: str
    url: str
    title: str
    platform: str
    quality: str
    quality_key: str = "best"          # 原始清晰度 key（如 best/1080/audio），重试时用它重下
    status: TaskStatus = "pending"
    progress: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    eta: int = 0
    filename: str = ""
    filesize: int = 0
    error: str = ""
    hint: str = ""
    created_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    pause_requested: bool = False
    workdir: Path | None = None
    filepath: Path | None = None
    # 过程展示：结构化步骤 + 文本日志
    steps: list[dict] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    def __post_init__(self):
        # 初始任务自动带有"排队等待"步骤，让前端过程面板一创建就有东西展示
        if not self.steps:
            self.add_step("排队等待", "pending")

    @property
    def is_finished(self) -> bool:
        return self.status in ("completed", "failed", "canceled")

    def add_step(self, name: str, status: StepStatus = "running", detail: str = "") -> None:
        """记录/更新一个执行步骤。同名步骤会覆盖状态，避免列表无限膨胀。"""
        now = time.time()
        for step in self.steps:
            if step.get("name") == name:
                step["status"] = status
                step["detail"] = detail
                step["updated_at"] = now
                return
        self.steps.append({
            "name": name,
            "status": status,
            "detail": detail,
            "created_at": now,
            "updated_at": now,
        })

    def log(self, line: str) -> None:
        """追加一行带时间戳的运行日志，限制最大行数防止内存泄漏。"""
        if not line:
            return
        ts = time.strftime("%H:%M:%S", time.localtime())
        self.logs.append(f"{ts}  {line.strip()}")
        if len(self.logs) > _MAX_LOG_LINES:
            self.logs[:] = self.logs[-_MAX_LOG_LINES:]

    def to_public_dict(self) -> dict:
        return {
            "task_id": self.id,
            "status": self.status,
            "progress": round(self.progress, 2),
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed": round(self.speed, 2),
            "eta": self.eta,
            "title": self.title,
            "platform": self.platform,
            "quality": self.quality,
            "filename": self.filename,
            "filesize": self.filesize,
            "error": self.error,
            "hint": self.hint,
            "steps": self.steps,
            "logs": self.logs,
        }


class TaskStore:
    """线程安全的任务表。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._tasks: dict[str, DownloadTask] = {}
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)

    def create(self, *, url: str, title: str, platform: str, quality: str, quality_key: str = "best") -> DownloadTask:
        task_id = uuid.uuid4().hex[:TASK_ID_LENGTH]
        workdir = self._root / task_id
        workdir.mkdir(parents=True, exist_ok=True)
        task = DownloadTask(
            id=task_id, url=url, title=title, platform=platform, quality=quality,
            quality_key=quality_key, workdir=workdir,
        )
        with self._lock:
            self._tasks[task_id] = task
        return task

    def get(self, task_id: str) -> DownloadTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **fields) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            for key, value in fields.items():
                setattr(task, key, value)

    def request_cancel(self, task_id: str) -> bool:
        """标记取消。仍在解析阶段（无进度回调）时直接置为已取消，让界面立即响应。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.is_finished:
                return False
            task.cancel_requested = True
            if task.status == "pending":
                task.status = "canceled"
                task.error = "已取消下载"
                task.add_step("排队等待", "error", "用户已取消")
            return True

    def clear_files(self, task_id: str) -> None:
        """清空任务的临时文件，但保留任务记录（便于前端读取终态）。"""
        task = self.get(task_id)
        if task and task.workdir and task.workdir.exists():
            shutil.rmtree(task.workdir, ignore_errors=True)
            task.workdir.mkdir(parents=True, exist_ok=True)

    def remove(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task and task.workdir:
            shutil.rmtree(task.workdir, ignore_errors=True)

    def list_all(self) -> list["DownloadTask"]:
        """返回当前所有任务的安全快照（不暴露内部 dict），供队列概览使用。"""
        with self._lock:
            return list(self._tasks.values())

    def purge_expired(self, ttl: int = TASK_TTL_SECONDS) -> int:
        deadline = time.time() - ttl
        with self._lock:
            stale = [tid for tid, t in self._tasks.items() if t.created_at < deadline]
        for task_id in stale:
            self.remove(task_id)
        return len(stale)

    def purge_orphans(self) -> int:
        """清理上次进程遗留的任务目录（任务状态只存在内存中，重启后即为孤儿）。"""
        with self._lock:
            known = set(self._tasks)
        removed = 0
        for path in self._root.iterdir():
            if path.is_dir() and TASK_DIR_PATTERN.match(path.name) and path.name not in known:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        return removed
