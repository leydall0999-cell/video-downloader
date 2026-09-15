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

    # ── 退还（任务失败不该算用户头上）──────────────────────────────────── #
    def snapshot(self) -> dict:
        """当前用量快照，供「失败了要不要退」比对。"""
        st = self._state()
        return {
            "lifetime_cloud_used": int(st.get("lifetime_cloud_used", 0)),
            "daily_auto_used": int(st.get("daily_auto_used", 0)),
        }

    def refund(self, lifetime: int = 0, daily: int = 0) -> dict:
        """退还已扣额度（失败任务 / 异常终止的补偿）。

        用户视角的铁律：**没出片就不该扣额度**。管线在云端调用前就扣了 1 次
        终身额度，若之后整条任务失败（网络、超时、模型返回空内容），用户既没
        拿到成片、又少了 1 次机会——体感等同「花了钱买失败」。故在父进程侧按
        实际增量退还，绝不少扣也绝不超退（clamp 到 0）。
        """
        if self.is_member():
            return self.snapshot()
        if lifetime <= 0 and daily <= 0:
            return self.snapshot()
        st = self._state()
        cur_life = int(st.get("lifetime_cloud_used", 0))
        cur_daily = int(st.get("daily_auto_used", 0))
        new_life = max(0, cur_life - int(lifetime))
        new_daily = max(0, cur_daily - int(daily))
        if new_life == cur_life and new_daily == cur_daily:
            return self.snapshot()   # 无变化不落盘：避免无谓写文件与 mtime 抖动
        st["lifetime_cloud_used"] = new_life
        st["daily_auto_used"] = new_daily
        self._persist(st)
        return self.snapshot()

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

    # ── 前置可行性预检（在「开始前」拦住做不完的任务）────────────────────── #
    def precheck_commentary(
        self,
        duration_sec: float,
        local_engine_ready: bool = False,
        engine: str = "auto",
    ) -> dict:
        """解说任务开始前的可行性预检，返回结构化结论供调用方转人话提示。

        为什么必须前置（2026-09-15 用户实测反馈）：免费用户跑一条超出本机能力
        的片子，会在转写与脚本生成**全部跑完之后**才因云端额度不足失败——用户
        白等十几分钟，拿到的是废片。故所有解说入口在「开始前」统一过这道闸门，
        不通过就立刻拒绝并说明两条出路，绝不进入执行阶段。

        判定顺序（免费用户）：
        1. 时长为 0/未知 → fail-open 放行（探测失败不应误拦）。
        2. 时长 > FREE_MAX_DURATION_SEC → 拒绝。这条片必然要走云端，而免费档位
           的上限就是 30 分钟，没有可用的执行路径。
        3. 时长合法 → 本机引擎就绪则放行（零消耗）；否则需要云端，
           终身云端额度为 0 时拒绝。

        返回 {allowed, code, reason, hint, will_use_cloud}。
        字段名用 allowed 而非 ok：前端 request() 会把「HTTP 成功」统一标成 ok，
        两者同名会让调用方分不清「请求成功」与「业务放行」。
        """
        if self.is_member():
            return {"allowed": True, "code": "member", "reason": "", "hint": "",
                    "will_use_cloud": False}
        if not duration_sec or duration_sec <= 0:
            return {"allowed": True, "code": "unknown_duration", "reason": "", "hint": "",
                    "will_use_cloud": False}

        limit_min = int(FREE_MAX_DURATION_SEC // 60)
        dur_min = int(round(float(duration_sec) / 60.0))
        if duration_sec > FREE_MAX_DURATION_SEC:
            return {
                "allowed": False,
                "code": "duration_exceeded",
                "reason": f"视频约 {dur_min} 分钟，超过免费版单条 {limit_min} 分钟上限",
                "hint": (
                    f"免费版单条解说视频最长 {limit_min} 分钟，当前视频约 {dur_min} 分钟，"
                    f"超出 {dur_min - limit_min} 分钟。请换用 {limit_min} 分钟以内的素材，"
                    f"或开通会员解锁长视频解说。"
                ),
                "will_use_cloud": False,
                "duration_sec": float(duration_sec),
                "limit_sec": FREE_MAX_DURATION_SEC,
                "over_sec": float(duration_sec) - FREE_MAX_DURATION_SEC,
            }

        # 走云端的两种来源语义不同，提示文案必须区分，否则会误导用户
        # （明明是自己选的「纯云端」，却被提示「本机引擎不可用」）。
        _explicit_cloud = str(engine or "").strip().lower() == "cloud"
        need_cloud = _explicit_cloud or (not local_engine_ready)
        if need_cloud and self.lifetime_cloud_remaining() <= 0:
            return {
                "allowed": False,
                "code": "cloud_quota_exhausted",
                "reason": "本机引擎不可用，且免费云端额度已用完",
                "hint": (
                    "这条视频需要云端生成解说词，但免费云端额度（终身 "
                    f"{LIFETIME_CLOUD_EVENTS} 次）已用完。开通会员即可解锁无限云端解说。"
                ),
                "will_use_cloud": True,
                "duration_sec": float(duration_sec),
            }

        if not need_cloud:
            _hint = ""
        elif _explicit_cloud:
            _hint = "你选择的是「纯云端」，本次将消耗 1 次云端额度。"
        else:
            _hint = "本机 AI 引擎不可用，本次将由云端生成解说词，消耗 1 次云端额度。"

        return {
            "allowed": True,
            "code": "will_use_cloud" if need_cloud else "local",
            "reason": "",
            "hint": _hint,
            "will_use_cloud": need_cloud,
            "duration_sec": float(duration_sec),
        }

    # ── 决策（供 llm_script 云端回落使用）────────────────────────────────── #
    def decide_cloud_fallback(self, mode: str = "auto") -> str:
        """云端闸门决策（两种云端来源语义不同，必须分开判）。

        mode='auto'       引擎 auto，本机跑不动/失败时**回落**云端：
                          受「终身额度」+「每日 auto 运行额度」双重约束
                          （每日额度是给自动回落行为设的频次护栏）。
        mode='cloud_only' 用户在设置里**明确选云端**：只受「终身额度」约束。
                          每日 auto 额度不该拦显式选择，否则当天跑过 1 次后
                          第 2 次会以「额度已用完」被误拒（实际终身还剩额度）。

        返回 'allow'（放行）/ 'deny'（禁止，调用方给人话提示）。会员永远 allow。
        """
        if self.is_member():
            return "allow"
        if self.lifetime_cloud_remaining() <= 0:
            return "deny"          # 终身云端额度耗尽
        if mode == "cloud_only":
            return "allow"         # 显式选云端：不检查也不消耗每日 auto 额度
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
