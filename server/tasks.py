"""下载任务的内存状态仓库。

只保存"当前进程内"的任务，重启即清空；文件落在 downloads/<task_id>/ 下，
配合 TTL 定期清理，避免磁盘无限增长。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from atomic_io import atomic_write_json

TaskStatus = Literal["pending", "downloading", "merging", "paused", "completed", "failed", "canceled"]
StepStatus = Literal["pending", "running", "done", "error"]

TASK_TTL_SECONDS = 60 * 60  # 成品文件保留 1 小时
TASK_ID_LENGTH = 16
TASK_DIR_PATTERN = re.compile(rf"^[0-9a-f]{{{TASK_ID_LENGTH}}}$")
_MAX_LOG_LINES = 200

# ---- 落盘持久化（B2，2026-10-02）----
# 任务状态写 DOWNLOAD_DIR/tasks_state.json：App 重启后未完成任务恢复为
# 「失败 + 可续传」（磁盘分片本来就在，同任务点继续即可续），完成任务保留
# 最近 50 条元数据（文件仍在 TTL 内才能重新保存）。
STATE_FILENAME = "tasks_state.json"
_PERSIST_INTERVAL = 2.0        # 进度类变更的落盘节流（秒）
_MAX_PERSISTED_COMPLETED = 50  # 状态文件里最多保留多少条已完成任务

_PERSIST_FIELDS = (
    "url", "title", "platform", "quality", "quality_key", "status",
    "progress", "downloaded_bytes", "total_bytes", "filename", "filesize",
    "error", "hint", "created_at", "extract_mode",
    "concurrent_fragments", "downloader_type", "cookie", "proxy",
    "source_url", "play_url", "watch_options", "is_hls", "resumable",
)


def _has_partial_file(workdir: Path | None) -> bool:
    """工作目录里是否残留可续传的部分文件（与 downloader._has_partial 同口径，避免循环导入）。"""
    if not workdir or not workdir.is_dir():
        return False
    try:
        for p in workdir.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if name.endswith((".part", ".aria2", ".ytdl")) or ".Frag" in name:
                return True
        return False
    except OSError:
        return False


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
    # 错误分类：由 _friendly_error 产出（cookie_required / cookie_invalid_or_expired /
    # cdn_forbidden / restricted / network / unknown 等），前端据此给出针对性行动建议
    category: str = ""
    # 慢速告警：下载中速率持续过低时由看门狗写入，前端据此弹出「建议换清晰度/代理」提示
    slow_warning: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    pause_requested: bool = False
    workdir: Path | None = None
    filepath: Path | None = None
    # 提取文案：模式（"spoken"/"description"/"both"/""）+ 状态 + 结果
    extract_mode: str = ""
    extract_status: str = "none"   # none / running / done / error
    extracted_text: dict = field(default_factory=dict)
    source_url: str = ""
    # 断点续传：保存首次下载时的关键参数，供「重试/继续」时原样复用（避免从 0 重下）
    concurrent_fragments: int = 0
    downloader_type: str = ""
    cookie: str = ""
    proxy: str = ""
    # 续传标记：取消/失败时若工作目录残留 .part 分片，置 True，前端据此提示「可断点续传」
    resumable: bool = False
    # 在线观看：解析时提取的播放地址与清晰度列表，存入任务后前端可在任务面板直接打开观看
    play_url: str = ""
    watch_options: list[dict] = field(default_factory=list)
    is_hls: bool = False
    # 过程展示：结构化步骤 + 文本日志
    steps: list[dict] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    # 设备隔离（2026-08-22）：创建任务的前端页面/标签页设备 ID（sessionStorage 级）。
    # 空字符串 = 系统/无归属任务（后台自动创建、遗留任务），对所有设备可见；
    # 非空 = 仅同 device_id 的页面可见（手机任务电脑端完全看不见）。
    device_id: str = ""

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
            "category": self.category,
            "slow_warning": self.slow_warning,
            "extract_mode": self.extract_mode,
            "extract_status": self.extract_status,
            "extracted_text": self.extracted_text,
            "source_url": self.source_url,
            "resumable": self.resumable,
            "play_url": self.play_url,
            "watch_options": self.watch_options,
            "is_hls": self.is_hls,
            "steps": self.steps,
            "logs": self.logs,
        }


class TaskStore:
    """线程安全的任务表。状态落盘 tasks_state.json，重启后可恢复。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._tasks: dict[str, DownloadTask] = {}
        self._lock = threading.Lock()
        self._root.mkdir(parents=True, exist_ok=True)
        self._state_path = self._root / STATE_FILENAME
        self._last_persist = 0.0
        self._persist_dirty = False
        self._load_state()

    # ---- 落盘 ----

    def _snapshot_locked(self) -> list[dict]:
        rows = []
        for t in self._tasks.values():
            row = {k: getattr(t, k) for k in _PERSIST_FIELDS}
            row["id"] = t.id
            row["filepath"] = str(t.filepath) if t.filepath else ""
            rows.append(row)
        # 状态文件只保留最近 N 条已完成，未完成任务全量保留（它们才是恢复的意义所在）
        finished = sorted(
            (r for r in rows if r["status"] in ("completed", "failed", "canceled")),
            key=lambda r: r.get("created_at") or 0, reverse=True)
        if len(finished) > _MAX_PERSISTED_COMPLETED:
            drop = {r["id"] for r in finished[_MAX_PERSISTED_COMPLETED:]}
            rows = [r for r in rows if r["id"] not in drop]
        return rows

    def _persist_locked(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_persist) < _PERSIST_INTERVAL:
            self._persist_dirty = True
            return
        self._last_persist = now
        self._persist_dirty = False
        # 原子落盘（守卫 test_config_atomic_write 抓过固定名 .tmp 的撕裂反证）：
        # tasks_state.json 与配额/账号表同级——半截 JSON 会让重启恢复直接丢光。
        try:
            atomic_write_json(self._state_path, {
                "version": 1, "saved_at": now, "tasks": self._snapshot_locked(),
            })
        except OSError:
            pass

    def flush(self) -> None:
        """立即落盘（供关键节点与关停前调用）。"""
        with self._lock:
            self._persist_locked(force=True)

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            return
        now = time.time()
        restored = 0
        for row in raw.get("tasks", []):
            tid = str(row.get("id") or "")
            if not TASK_DIR_PATTERN.match(tid) or tid in self._tasks:
                continue
            workdir = self._root / tid
            if not workdir.is_dir():
                continue  # 工作目录已被 TTL 清掉：只剩元数据没有意义
            status = row.get("status")
            fp = None
            if status == "completed":
                fp = Path(str(row.get("filepath") or "")) if row.get("filepath") else None
                if not fp or not fp.exists():
                    continue  # 成品已随 TTL 清理：不恢复僵尸条目
                created_at = float(row.get("created_at") or now)
            else:
                created_at = float(row.get("created_at") or now)
            task = DownloadTask(
                id=tid, url=str(row.get("url") or ""), title=str(row.get("title") or ""),
                platform=str(row.get("platform") or ""), quality=str(row.get("quality") or ""),
                quality_key=str(row.get("quality_key") or "best"),
                status=status if isinstance(status, str) else "pending",
                progress=float(row.get("progress") or 0.0),
                downloaded_bytes=int(row.get("downloaded_bytes") or 0),
                total_bytes=int(row.get("total_bytes") or 0),
                filename=str(row.get("filename") or ""),
                filesize=int(row.get("filesize") or 0),
                error=str(row.get("error") or ""), hint=str(row.get("hint") or ""),
                created_at=created_at,
                extract_mode=str(row.get("extract_mode") or ""),
                concurrent_fragments=int(row.get("concurrent_fragments") or 0),
                downloader_type=str(row.get("downloader_type") or ""),
                cookie=str(row.get("cookie") or ""), proxy=str(row.get("proxy") or ""),
                source_url=str(row.get("source_url") or ""),
                play_url=str(row.get("play_url") or ""),
                watch_options=list(row.get("watch_options") or []),
                is_hls=bool(row.get("is_hls")), workdir=workdir, filepath=fp,
            )
            task.steps = []   # 恢复条目不带走历史步骤，__post_init__ 重建初始步骤
            task.__post_init__()
            if status == "completed":
                task.add_step("成片已就绪", "done")
            elif status in ("pending", "downloading", "merging", "paused"):
                # 中断的任务统一恢复为「失败 + 可续传」，前端出「继续下载」按钮
                task.status = "failed"
                task.error = "应用退出，任务已中断"
                task.hint = "分片仍在磁盘上，点「继续下载」可从断点续传"
                task.resumable = _has_partial_file(workdir)
                task.created_at = now   # 重置 TTL，给用户时间点继续
                task.add_step("下载", "error", "应用重启中断")
            else:  # failed / canceled：恢复原状态；有分片则保留续传入口并重置 TTL
                task.status = status
                task.resumable = _has_partial_file(workdir)
                if task.resumable:
                    task.created_at = now
            with self._lock:
                self._tasks[tid] = task
            restored += 1
        if restored:
            self._persist_locked(force=True)

    def create(self, *, url: str, title: str, platform: str, quality: str,
                quality_key: str = "best", extract_mode: str = "",
                concurrent_fragments: int = 0, downloader_type: str = "",
                cookie: str = "", proxy: str = "",
                play_url: str = "", watch_options: list[dict] | None = None,
                is_hls: bool = False, device_id: str = "") -> DownloadTask:
        task_id = uuid.uuid4().hex[:TASK_ID_LENGTH]
        workdir = self._root / task_id
        workdir.mkdir(parents=True, exist_ok=True)
        task = DownloadTask(
            id=task_id, url=url, title=title, platform=platform, quality=quality,
            quality_key=quality_key, workdir=workdir, extract_mode=extract_mode,
            concurrent_fragments=concurrent_fragments, downloader_type=downloader_type,
            cookie=cookie, proxy=proxy,
            play_url=play_url, watch_options=watch_options or [], is_hls=is_hls,
            device_id=device_id,
        )
        with self._lock:
            self._tasks[task_id] = task
            self._persist_locked(force=True)
        return task

    def get(self, task_id: str) -> DownloadTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **fields) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            changed = False
            for key, value in fields.items():
                if getattr(task, key, None) != value:
                    changed = True
                setattr(task, key, value)
            if changed and any(k in _PERSIST_FIELDS for k in fields):
                self._persist_locked()

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
            self._persist_locked()
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
            self._persist_locked(force=True)
        if task and task.workdir:
            shutil.rmtree(task.workdir, ignore_errors=True)

    def list_all(self, device: str = "") -> list["DownloadTask"]:
        """返回当前设备可见的任务安全快照（不暴露内部 dict），供队列概览使用。

        设备隔离规则：
        - device 非空：返回「无归属（device_id 为空，系统任务）或属于该设备」的任务；
        - device 为空（未提供/兼容路径）：只返回无归属系统任务——绝不泄露
          任何设备专属任务（无头请求/老客户端拿不到用户任务）。
        """
        with self._lock:
            tasks = list(self._tasks.values())
        if device:
            return [t for t in tasks if not t.device_id or t.device_id == device]
        return [t for t in tasks if not t.device_id]

    def purge_expired(self, ttl: int = TASK_TTL_SECONDS) -> int:
        deadline = time.time() - ttl
        with self._lock:
            stale = [tid for tid, t in self._tasks.items() if t.created_at < deadline]
            if stale:
                self._persist_dirty = True   # remove() 里会 force 落盘
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
