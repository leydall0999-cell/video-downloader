"""批量下载调度器：在共享线程池之上做「可动态调整的并发上限」。

- 单下与批量下载共用同一个线程池和并发计数，保证全局并发不超过上限。
- 并发数可运行时调整（前端滑块 / API），立即生效。
- 失败自动重试由 downloader.run_download 的 max_retries 参数负责（在 worker 线程内循环，
  不额外占用并发槽），本模块只负责「同一时刻跑几个」。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor


class BatchScheduler:
    def __init__(self, executor: ThreadPoolExecutor, default_concurrency: int = 3, hard_max: int = 8) -> None:
        self._executor = executor
        self._hard_max = max(1, int(hard_max))
        self._concurrency = max(1, min(int(default_concurrency), self._hard_max))
        self._active = 0
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

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
            # 调大后唤醒正在 _acquire 里等待的 worker，让更多任务并发跑起来
            self._cv.notify_all()

    def active_count(self) -> int:
        with self._lock:
            return self._active

    def _acquire(self) -> None:
        with self._cv:
            while self._active >= self._concurrency:
                self._cv.wait()
            self._active += 1

    def _release(self) -> None:
        with self._lock:
            self._active -= 1
            self._cv.notify_all()

    def submit(self, fn, *args, **kwargs):
        """提交一个后台任务；fn 真正执行前先占用一个并发槽，结束后释放。"""

        def worker() -> None:
            self._acquire()
            try:
                fn(*args, **kwargs)
            finally:
                self._release()

        return self._executor.submit(worker)
