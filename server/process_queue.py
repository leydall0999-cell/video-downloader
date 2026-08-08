"""加工队列：对 ffmpeg 本地处理做并发控制与批量提交。

- Condition+计数器模式做并发上限（默认 2，硬顶 4）。
- 作业状态只存内存，重启即丢（与批量下载/torrent 一致）。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable


class ProcessQueue:
    def __init__(self, executor: ThreadPoolExecutor, default_concurrency: int = 2, hard_max: int = 4) -> None:
        self._executor = executor
        self._hard_max = max(1, int(hard_max))
        self._concurrency = max(1, min(int(default_concurrency), self._hard_max))
        self._active = 0

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

        # job_id → {status, op, name, lib_id, error, out_path, count, is_dir, submitted_at, _fn, _args}
        self._jobs: dict[str, dict] = {}

    # -- 属性 ----------------------------------------------------------------

    @property
    def concurrency(self) -> int:
        with self._lock:
            return self._concurrency

    def set_concurrency(self, n: int) -> None:
        n = max(1, min(int(n), self._hard_max))
        with self._lock:
            if n == self._concurrency:
                return
            self._concurrency = n
            self._cv.notify_all()
        self._dispatch()

    def active_count(self) -> int:
        with self._lock:
            return self._active

    @property
    def jobs(self) -> dict:
        return self._jobs

    @property
    def lock(self):
        return self._lock

    # -- 提交 ----------------------------------------------------------------

    def submit(self, job_id: str, name: str, lib_id: str, op: str,
               fn: Callable, *args) -> None:
        """提交一个加工任务，入列后自动调度。"""
        with self._lock:
            self._jobs[job_id] = {
                "status": "pending", "op": op, "name": name,
                "lib_id": lib_id, "error": "", "out_path": "",
                "count": 0, "is_dir": False, "submitted_at": time.time(),
                "_fn": fn, "_args": args,
            }
        self._dispatch()

    def submit_batch(self, items: list[dict], fn: Callable, *fn_args_template) -> list[str]:
        """批量提交：items 每条含 job_id/name/lib_id/op。一次性入列后统一调度。

        返回所有 job_id 列表。
        """
        job_ids = []
        with self._lock:
            for item in items:
                jid = item["job_id"]
                self._jobs[jid] = {
                    "status": "pending", "op": item["op"], "name": item["name"],
                    "lib_id": item["lib_id"], "error": "", "out_path": "",
                    "count": 0, "is_dir": False, "submitted_at": time.time(),
                    "_fn": fn, "_args": fn_args_template,
                }
                job_ids.append(jid)
        self._dispatch()
        return job_ids

    # -- 调度 ----------------------------------------------------------------

    def _dispatch(self) -> None:
        """从等待中的 pending 作业出队，直到并发满或队列空。"""
        dispatched = []
        with self._lock:
            while self._active < self._concurrency:
                # 找第一个 pending 作业
                jid = next((j for j, v in self._jobs.items() if v["status"] == "pending"), None)
                if not jid:
                    break
                self._jobs[jid]["status"] = "running"
                self._active += 1
                dispatched.append(jid)
        for jid in dispatched:
            self._executor.submit(self._worker, jid)

    def _worker(self, job_id: str) -> None:
        """在线程池里执行 fn，完成后 release 并触发下一轮调度。"""
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            self._release()
            return
        fn = job.pop("_fn", None)
        args = job.pop("_args", ())
        try:
            if fn:
                fn(*args)
        finally:
            self._release()

    def _release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._cv.notify_all()
        self._dispatch()

    # -- 查询 ----------------------------------------------------------------

    def get_queue(self) -> dict:
        """返回队列快照（不含内部 _fn/_args）。"""
        with self._lock:
            jobs = []
            for jid in sorted(self._jobs, key=lambda j: self._jobs[j].get("submitted_at", 0)):
                j = {k: v for k, v in self._jobs[jid].items() if not k.startswith("_")}
                j["job_id"] = jid
                jobs.append(j)
            return {
                "jobs": jobs,
                "running": sum(1 for j in jobs if j["status"] == "running"),
                "pending": sum(1 for j in jobs if j["status"] == "pending"),
                "concurrency": self._concurrency,
                "hard_max": self._hard_max,
            }
