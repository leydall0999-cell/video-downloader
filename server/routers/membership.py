"""server/routers/membership.py — VDL 会员（/api/member/*）。

三轨会员引擎的 HTTP 层：套餐列表 / 状态查询 / 激活 / 积分花费 / 日配额使用。
V1 只提供状态与账本能力，不接支付、不加功能门禁（现有下载/去水印/抠图行为零变化）。

说明：
  - 引擎 membership.MembershipStore 为本模块单例（app 端同样 import 本 store 可共享）。
  - 激活为测试期通道（via=test），真实支付/验签在 V1 后接入。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body

from membership import MembershipStore

router = APIRouter()
store = MembershipStore()


@router.get("/api/member/plans")
def member_plans() -> dict[str, Any]:
    """套餐与权益表（供购买中心展示）。"""
    return store.plans()


@router.get("/api/member/status")
def member_status() -> dict[str, Any]:
    """当前会员状态：下载/AI 双轨、积分余额、今日配额用量。"""
    return store.status()


@router.post("/api/member/activate")
def member_activate(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """激活/续费。payload: {"code": "download_year"|"ai_5500"|"credits_5000", "via": "test"}"""
    code = str(payload.get("code") or "").strip()
    via = str(payload.get("via") or "test").strip() or "test"
    if not code:
        return {"ok": False, "error": "缺少 code"}
    return store.activate(code, via=via)


@router.post("/api/member/credits/spend")
def credits_spend(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """AI 积分花费。payload: {"amount": 100, "reason": "asr"}"""
    try:
        amount = int(payload.get("amount", 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "amount 必须为整数"}
    reason = str(payload.get("reason") or "ai_usage")
    return store.spend_credits(amount, reason=reason)


@router.get("/api/member/credits/balance")
def credits_balance() -> dict[str, Any]:
    """积分余额（AI 订阅积分 / 永久积分 / 合计）。"""
    return store.credits_balance()


@router.get("/api/member/quota/{resource}")
def quota_state(resource: str) -> dict[str, Any]:
    """查询某下载类资源当日配额用量与剩余。"""
    return store.quota_state(resource)


@router.post("/api/member/quota/use")
def quota_use(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """消耗下载类配额。payload: {"resource": "resolve", "n": 1}"""
    resource = str(payload.get("resource") or "").strip()
    if not resource:
        return {"ok": False, "error": "缺少 resource"}
    try:
        n = int(payload.get("n", 1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "n 必须为整数"}
    return store.use_daily(resource, n=n)
