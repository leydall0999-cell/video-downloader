"""VDL 免费用户配额闸门（云端按量计费的护栏）。

规则（docs/llm-cloud-fallback-design.md §12.4，用户已拍板）：
- 终身云端事件：3 次（不可恢复；auto 回落云端 / cloud_only 各耗 1 次）
- 每日 auto 运行：1 次（自然日重置；每天第 1 次 auto 运行不论本机成败都占 1 次）
- 单条上传视频时长：≤ 30 分钟（免费用户；会员不限）
会员：解除全部限制（云端无限 + 时长不限）。

持久化：~/.video-downloader/quota.json（与 membership 同目录）。
设计要点（对齐 membership.py）：纯标准库、零外部依赖；now_fn / path / is_member_fn
全部可注入，测试无需 mock 系统时钟、无需真实会员引擎。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Optional

# ── 配额常量（唯一真源）────────────────────────────────────────────────────── #
LIFETIME_CLOUD_EVENTS = 3          # 终身云端事件上限（不可恢复）
DAILY_AUTO_RUNS = 1                # 每日 auto 运行额度（自然日重置）
FREE_MAX_DURATION_SEC = 30 * 60    # 免费用户单条视频时长上限（秒）

DEFAULT_BASE_DIR = Path(os.path.expanduser("~/.video-downloader"))


class QuotaManager:
    """免费用户配额状态机。线程不安全（单人单机使用，可接受）。"""

    def __init__(
        self,
        base_dir: Optional[Path | str] = None,
        now_fn: Optional[Callable[[], float]] = None,
        is_member_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_BASE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / "quota.json"
        self._now = now_fn or time.time
        self._is_member = is_member_fn or (lambda: False)

    # ── 持久化 ──────────────────────────────────────────────────────────── #
    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8") or "{}")
            except Exception:
                return {}
        return {}

    def _save(self, st: dict) -> None:
        self.path.write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self._now()))

    def _roll_daily(self, st: dict) -> None:
        today = self._today()
        if st.get("daily_date") != today:
            st["daily_date"] = today
            st["daily_auto_used"] = 0

    def _state(self) -> dict:
        st = self._load()
        self._roll_daily(st)
        return st

    def _persist(self, st: dict) -> None:
        self._roll_daily(st)
        self._save(st)

    # ── 查询 ────────────────────────────────────────────────────────────── #
    def is_member(self) -> bool:
        try:
            return bool(self._is_member())
        except Exception:
            return False

    def lifetime_cloud_remaining(self) -> int:
        if self.is_member():
            return 10 ** 9
        return max(0, LIFETIME_CLOUD_EVENTS - int(self._state().get("lifetime_cloud_used", 0)))

    def daily_auto_remaining(self) -> int:
        if self.is_member():
            return 10 ** 9
        return max(0, DAILY_AUTO_RUNS - int(self._state().get("daily_auto_used", 0)))

    def can_upload_video(self, duration_sec: float) -> bool:
        """免费用户单条视频 ≤ 30 分钟才放行；会员 / 时长未知(≤0) 不拦（前端主拦，服务端兜底宽松）。"""
        if self.is_member():
            return True
        if not duration_sec or duration_sec <= 0:
            return True
        return duration_sec <= FREE_MAX_DURATION_SEC

    # ── 变更 ────────────────────────────────────────────────────────────── #
    def consume_cloud_event(self) -> bool:
        """扣 1 次终身云端事件。额度不足返回 False。"""
        if self.is_member():
            return True
        st = self._state()
        if self.lifetime_cloud_remaining() <= 0:
            return False
        st["lifetime_cloud_used"] = int(st.get("lifetime_cloud_used", 0)) + 1
        self._persist(st)
        return True

    def consume_daily_auto(self) -> bool:
        """扣 1 次每日 auto 额度。额度不足返回 False。"""
        if self.is_member():
            return True
        st = self._state()
        if self.daily_auto_remaining() <= 0:
            return False
        st["daily_auto_used"] = int(st.get("daily_auto_used", 0)) + 1
        self._persist(st)
        return True

    # ── 决策（供 llm_script 云端回落使用）────────────────────────────────── #
    def decide_cloud_fallback(self) -> str:
        """引擎 auto 模式、本机失败需回落云端时的闸门决策。

        返回 'allow'（可走云端并扣额度）/ 'deny'（禁止，改走 local_only + 人话提示）。
        会员永远 allow。
        """
        if self.is_member():
            return "allow"
        if self.lifetime_cloud_remaining() <= 0:
            return "deny"          # 终身云端额度耗尽
        if self.daily_auto_remaining() <= 0:
            return "deny"          # 当日 auto 已用满 → 强制 local_only
        return "allow"

    def status(self) -> dict:
        st = self._state()
        member = self.is_member()
        return {
            "is_member": member,
            "lifetime_cloud_used": 0 if member else int(st.get("lifetime_cloud_used", 0)),
            "lifetime_cloud_limit": LIFETIME_CLOUD_EVENTS,
            "lifetime_cloud_remaining": self.lifetime_cloud_remaining(),
            "daily_auto_used": 0 if member else int(st.get("daily_auto_used", 0)),
            "daily_auto_limit": DAILY_AUTO_RUNS,
            "daily_auto_remaining": self.daily_auto_remaining(),
            "free_max_duration_sec": FREE_MAX_DURATION_SEC,
        }


# 模块级单例（默认非会员；is_member 由调用方或 server 端注入）
_default_manager: Optional[QuotaManager] = None


def get_manager() -> QuotaManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = QuotaManager()
    return _default_manager


def set_member_fn(fn: Callable[[], bool]) -> None:
    """注入会员判定（server 端调用，避免管线层依赖会员引擎）。"""
    global _default_manager
    if _default_manager is None:
        _default_manager = QuotaManager(is_member_fn=fn)
    else:
        _default_manager._is_member = fn  # type: ignore[assignment]
