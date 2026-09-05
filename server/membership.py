"""VDL 会员引擎（V1，照搬 DataTool.vip 三轨结构）。

三轨：
  1) download_member  下载会员（30/180/365 天订阅；含下载类权益配额表）
  2) ai_member        AI 会员（月订阅 + 一次性积分池；自动捆绑下载会员权益）
  3) permanent_credits 永久积分包（纯按次计费，不与订阅绑定）

设计要点：
  - 纯标准库、零外部依赖（engine 不 import app / fastapi），可独立单测。
  - 时间与存储路径全部可注入（now_fn / path），测试无需 mock 系统时钟。
  - 激活续费顺延：expire_at = max(now, 当前到期) + 时长，不吞已有天数。
  - AI 会员激活时自动把下载会员权益覆盖到同一到期日（捆绑，无「纯 AI」档）。
  - 积分消耗顺序：先扣 AI 订阅积分（随会员到期清零），再扣永久积分。
  - 日配额惰性重置：daily_usage.date 非当日时自动清零重计。

V1 明确不做：真实支付、验签、功能门禁接入。仅提供引擎 + 状态机，供
/api/member 路由与后续功能模块调用。

参考文档：video-downloader-app/VDL_会员商业化_V1方案_2026-09-05.md
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# 套餐与权益常量表（唯一真源：VDL_会员商业化_V1方案_2026-09-05.md）
# --------------------------------------------------------------------------- #

# download member: 时长（天）、折算锚点
DOWNLOAD_PLANS: dict[str, dict[str, Any]] = {
    "download_month":      {"price_cny": 29.80,  "days": 30,   "label": "下载会员·月",    "saving": 0.0},
    "download_half_year":  {"price_cny": 99.90,  "days": 180,  "label": "下载会员·180天", "saving": 0.44},
    "download_year":       {"price_cny": 179.00, "days": 365,  "label": "下载会员·年",    "saving": 0.50, "best": True},
}

# AI member: 积分池大小
AI_PLANS: dict[str, dict[str, Any]] = {
    "ai_5500":   {"price_cny": 49.90, "days": 30, "credits": 5500,  "label": "AI 积分月会员"},
    "ai_15000":  {"price_cny": 99.90, "days": 30, "credits": 15000, "label": "AI 月会员", "best": True},
}

# 永久积分包
CREDIT_PACKS: dict[str, dict[str, Any]] = {
    "credits_5000":  {"price_cny": 50.00, "credits": 5000,  "label": "5000 积分包"},
    "credits_15000": {"price_cny": 99.00, "credits": 15000, "label": "15000 积分包", "best": True},
}

# 下载类权益的每日配额上限（会员档；免费档见 FREE_DAILY_LIMITS）
DAILY_QUOTA_LIMITS: dict[str, int] = {
    "resolve": 1000,          # 视频解析 / 日（会员）
    "original": 100,          # 插件原画解析 / 日（会员）
    "batch_material": 1000,   # 素材批量 / 日（会员）
    # 评论 / 数据 / 字幕批量：不限（不进 daily_usage 计配额）
}
# 免费档每日配额（2026-09-05 定稿：免费 resolve 10/日，原画/批量不开放）
FREE_DAILY_LIMITS: dict[str, int] = {
    "resolve": 10,
    "original": 0,            # 免费不开放原画
    "batch_material": 0,      # 免费不开放批量
}
UNLIMITED_QUOTA = ("comment", "data", "subtitle")

# 会员权益内免费的 AI 资源描述（供 plans/status 展示）
AI_FEATURES: list[str] = [
    "AI 字幕识别", "字幕提取", "视频总结", "图片翻译体验", "更多 AI 权益持续新增",
]

# --------------------------------------------------------------------------- #
# 状态文件
# --------------------------------------------------------------------------- #


def default_state_path() -> Path:
    """状态文件默认路径 ~/.video-downloader/membership.json（frozen 兼容）。"""
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    else:
        base = Path.home() / ".video-downloader"
    return base / "membership.json"


def _empty_state() -> dict[str, Any]:
    return {
        "download_member": {"active": False, "plan": None, "expire_at": 0.0},
        "ai_member": {"active": False, "plan": None, "expire_at": 0.0,
                      "grant_credits": 0, "credits_left": 0},
        "permanent_credits": {"total": 0, "packs": []},
        "daily_usage": {"date": "", "resolve": 0, "original": 0, "batch_material": 0},
        "meta": {"activated_at": 0.0, "history": []},
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return _empty_state()
    st = _empty_state()
    # 逐键合并，容忍旧/缺字段
    for k in ("download_member", "ai_member", "permanent_credits", "daily_usage", "meta"):
        if isinstance(data.get(k), dict):
            st[k].update(data[k])
    return st


def _save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        # 状态文件写失败不应让业务崩溃（降级为内存态）
        pass


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #

@dataclass
class MembershipStore:
    """VDL 会员状态机。线程外调用方需自行加锁（见 app 单例）。"""
    path: Optional[Path] = None
    now_fn: Callable[[], float] = field(default=time.time)
    _state: dict[str, Any] = field(default_factory=_empty_state, init=False)
    _loaded: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = default_state_path()

    # ---- 基础读写 ----
    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._state = _load_state(self.path)
            self._loaded = True

    def _persist(self) -> None:
        _save_state(self.path, self._state)

    def _now(self) -> float:
        return self.now_fn()

    # ---- 公开查询 ----
    def status(self) -> dict[str, Any]:
        """当前会员状态（惰性判定过期、惰性日切配额）。返回公开 dict。"""
        self._ensure_loaded()
        now = self._now()
        st = self._state
        dl = st["download_member"]
        ai = st["ai_member"]

        # 惰性过期
        if dl.get("active") and now >= float(dl.get("expire_at", 0)):
            dl["active"] = False
            dl["plan"] = None
            dl["expire_at"] = 0.0
        if ai.get("active") and now >= float(ai.get("expire_at", 0)):
            ai["active"] = False
            ai["plan"] = None
            ai["expire_at"] = 0.0
            ai["credits_left"] = 0  # AI 订阅积分到期清零

        # AI 会员捆绑下载权益：激活期内 download 视同可用到同一到期日
        dl_active = bool(dl.get("active")) or bool(ai.get("active"))
        dl_until = max(float(dl.get("expire_at", 0)), float(ai.get("expire_at", 0))) if (dl.get("active") or ai.get("active")) else 0.0

        # 惰性日切
        self._roll_daily(now)

        return {
            "download_member": {
                "active": dl_active,
                "plan": dl.get("plan") if dl.get("active") else (ai.get("plan") if ai.get("active") else None),
                "expire_at": dl_until,
                "source": "download" if dl.get("active") else ("ai_bundle" if ai.get("active") else None),
            },
            "ai_member": {
                "active": bool(ai.get("active")),
                "plan": ai.get("plan"),
                "expire_at": float(ai.get("expire_at", 0)),
                "credits_left": int(ai.get("credits_left", 0)),
                "grant_credits": int(ai.get("grant_credits", 0)),
            },
            "permanent_credits": int(st["permanent_credits"].get("total", 0)),
            "credits_total": int(ai.get("credits_left", 0)) + int(st["permanent_credits"].get("total", 0)),
            "daily_usage": dict(st["daily_usage"]),
        }

    def plans(self) -> dict[str, Any]:
        """套餐表（价格/时长/权益），供前端购买中心展示。"""
        return {
            "download_member": {
                "plans": DOWNLOAD_PLANS,
                "benefits": [
                    {"key": "resolve", "text": "视频解析 1000 次/日"},
                    {"key": "original", "text": "原画解析 100 次/日"},
                    {"key": "batch_material", "text": "批量下载素材 1000 条/日"},
                    {"key": "unlimited", "text": "评论/数据/字幕批量：不限"},
                    {"key": "speed", "text": "高速通道·全速不限速"},
                    {"key": "support", "text": "优先客服支持"},
                ],
            },
            "ai_member": {
                "plans": AI_PLANS,
                "bundle_note": "包含下载会员全部权益",
                "features": AI_FEATURES,
            },
            "credit_packs": CREDIT_PACKS,
            "currency": "CNY",
        }

    # ---- 激活 ----
    def activate(self, code: str, via: str = "test") -> dict[str, Any]:
        """按套餐/积分包 code 激活。续费顺延，AI 会员自动捆绑下载权益。"""
        self._ensure_loaded()
        now = self._now()
        st = self._state
        result: dict[str, Any] = {"ok": True, "code": code, "via": via}

        if code in DOWNLOAD_PLANS:
            info = DOWNLOAD_PLANS[code]
            cur = float(st["download_member"].get("expire_at", 0) or 0)
            new_exp = max(now, cur) + info["days"] * 86400
            st["download_member"].update({"active": True, "plan": code, "expire_at": new_exp})
            result.update({"kind": "download_member", "expire_at": new_exp})
        elif code in AI_PLANS:
            info = AI_PLANS[code]
            cur = float(st["ai_member"].get("expire_at", 0) or 0)
            new_exp = max(now, cur) + info["days"] * 86400
            st["ai_member"].update({
                "active": True, "plan": code, "expire_at": new_exp,
                "grant_credits": int(info["credits"]), "credits_left": int(info["credits"]),
            })
            # 捆绑：下载权益覆盖到 AI 到期（不置 active 标志，status 负责推导 source）
            dl_exp = float(st["download_member"].get("expire_at", 0) or 0)
            if dl_exp < new_exp:
                st["download_member"]["expire_at"] = new_exp
            result.update({"kind": "ai_member", "expire_at": new_exp,
                           "credits_granted": int(info["credits"])})
        elif code in CREDIT_PACKS:
            info = CREDIT_PACKS[code]
            amt = int(info["credits"])
            st["permanent_credits"]["total"] = int(st["permanent_credits"].get("total", 0)) + amt
            st["permanent_credits"].setdefault("packs", []).append({
                "pack": code, "amount": amt, "bought_at": now,
            })
            result.update({"kind": "credit_pack", "credits_added": amt})
        else:
            return {"ok": False, "error": f"未知套餐 code: {code}"}

        if not st["meta"].get("activated_at"):
            st["meta"]["activated_at"] = now
        st["meta"].setdefault("history", []).append({
            "code": code, "via": via, "at": now,
        })
        st["meta"]["history"] = st["meta"]["history"][-200:]  # 只留最近 200 条
        self._persist()
        return result

    # ---- 积分 ----
    def spend_credits(self, amount: int, reason: str = "ai_usage") -> dict[str, Any]:
        """消耗积分：先 AI 订阅积分（快过期），后永久积分。不足则拒绝。"""
        if amount <= 0:
            return {"ok": False, "error": "amount 必须为正"}
        self._ensure_loaded()
        st = self._state
        ai = st["ai_member"]
        ai_left = int(ai.get("credits_left", 0)) if ai.get("active") else 0
        perm_total = int(st["permanent_credits"].get("total", 0))
        if ai_left + perm_total < amount:
            return {"ok": False, "error": f"积分不足：需要 {amount}，当前 {ai_left + perm_total}"}

        remaining = amount
        # 1) AI 订阅积分
        if remaining > 0 and ai_left > 0:
            take = min(ai_left, remaining)
            ai["credits_left"] = ai_left - take
            remaining -= take
        # 2) 永久积分
        if remaining > 0:
            perm_total -= remaining
            st["permanent_credits"]["total"] = perm_total
            remaining = 0
        self._persist()
        return {"ok": True, "spent": amount, "reason": reason,
                "credits_left": self.status()["credits_total"]}

    def credits_balance(self) -> dict[str, int]:
        s = self.status()
        return {"ai_subscription": s["ai_member"]["credits_left"],
                "permanent": s["permanent_credits"],
                "total": s["credits_total"]}

    # ---- 每日配额 ----
    def _is_download_active(self) -> bool:
        """下载权益是否活跃（独立下载会员或 AI 会员捆绑）。"""
        st = self._state
        return bool(st["download_member"].get("active")) or bool(st["ai_member"].get("active"))

    def _roll_daily(self, now: float) -> None:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        du = self._state["daily_usage"]
        if du.get("date") != day:
            du["date"] = day
            du["resolve"] = 0
            du["original"] = 0
            du["batch_material"] = 0

    def quota_state(self, resource: str) -> dict[str, Any]:
        """查询某资源的当日用量/上限（按当前档位：免费 or 会员）。unlimited 恒放行。"""
        self._ensure_loaded()
        self._roll_daily(self._now())
        if resource in UNLIMITED_QUOTA:
            return {"resource": resource, "allowed": True, "unlimited": True}
        is_member = self._is_download_active()
        limit_map = DAILY_QUOTA_LIMITS if is_member else FREE_DAILY_LIMITS
        limit = limit_map.get(resource)
        if limit is None and DAILY_QUOTA_LIMITS.get(resource) is None:
            # 未知资源：V1 不设卡（保守默认放行，避免误伤功能）
            return {"resource": resource, "allowed": True, "unknown": True}
        if limit is None:
            # 免费表未覆盖但会员表有（原画/批量）→ 免费额度为 0
            limit = 0
        used = int(self._state["daily_usage"].get(resource, 0))
        return {"resource": resource, "limit": limit, "used": used,
                "remaining": max(0, limit - used),
                "allowed": used < limit,
                "tier": "member" if is_member else "free",
                "member_limit": DAILY_QUOTA_LIMITS.get(resource),
                "free_limit": FREE_DAILY_LIMITS.get(resource, 0)}

    def use_daily(self, resource: str, n: int = 1) -> dict[str, Any]:
        """消耗下载类配额（免费档 resolve 10/日；会员档按表）。超限返回 ok=False。"""
        if n <= 0:
            return {"ok": False, "error": "n 必须为正"}
        q = self.quota_state(resource)
        if q.get("unlimited") or q.get("unknown"):
            return {"ok": True, "resource": resource, "unlimited": q.get("unlimited", False)}
        if not q["allowed"]:
            if q.get("tier") == "free":
                return {"ok": False, "error": f"今日免费解析额度已用尽（{q['limit']}/日）— 开通下载会员可解锁 {q.get('member_limit', 0)} 次/日",
                        "resource": resource, "code": "MEMBER_QUOTA"}
            return {"ok": False, "error": f"{resource} 今日配额已用尽（{q['limit']}/日）", "resource": resource,
                    "code": "MEMBER_QUOTA"}
        used = int(self._state["daily_usage"].get(resource, 0))
        new_used = used + n
        if new_used > q["limit"]:
            return {"ok": False, "error": f"超出 {resource} 日配额上限 {q['limit']}",
                    "resource": resource, "code": "MEMBER_QUOTA"}
        self._state["daily_usage"][resource] = new_used
        self._persist()
        return {"ok": True, "resource": resource, "used": new_used,
                "remaining": q["limit"] - new_used}
