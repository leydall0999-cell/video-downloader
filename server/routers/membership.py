"""server/routers/membership.py — VDL 会员（/api/member/*，B2 per-user）。

三轨会员引擎的 HTTP 层：套餐列表 / 状态查询 / 激活 / 积分花费 / 日配额使用。
会员状态按 user_id 分文件存储（见 user_membership.current_member_store）：
  - 已登录 → 该用户的 memberships/{user_id}.json；
  - 未登录 → 全局匿名 store（免费档配额共享，用于门禁降级）。
V1 只提供状态与账本能力，不接支付；激活仍为测试期通道（via=ui_test）。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()


def _store(request: Request):
    """解析当前请求的会员 store（按 token 用户；匿名回退全局）。"""
    import app
    return app.current_member_store(request)


def _require_user(request: Request) -> Optional[str]:
    import app
    return app.get_current_user_id(request)


@router.get("/api/member/plans")
def member_plans() -> dict[str, Any]:
    """套餐与权益表（供购买中心展示）。"""
    import app
    return app.member_store.plans()


@router.get("/api/member/status")
def member_status(request: Request) -> dict[str, Any]:
    """当前会员状态：下载/AI 双轨、积分余额、今日配额用量。兼容匿名（返回免费态）。"""
    _maybe_sync(_store(request))
    s = _store(request).status()
    s["anonymous"] = _require_user(request) is None
    return s


# 账号制：由 cloud_account 决定是否该再去云端核一次（内部按 last_sync 节流，
# 默认 30 分钟一次），断网不改本地权益（fail-open）。
def _maybe_sync(store) -> None:
    try:
        from routers.cloud_account import maybe_sync_account
        maybe_sync_account(store)
    except Exception:
        pass


@router.post("/api/member/activate")
def member_activate(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """激活/续费。payload: {"code": "download_year"|"ai_15000"|"credits_5000"}。需登录。

    🔒 P2 收口：套餐 code 直激活是**本机调试后门**，仅限回环请求（桌面 App 本机）。
    远程请求一律要求走 /api/member/redeem 卡密通道 —— 否则任何登录用户
    都能白嫖任意套餐（V1 时期的洞）。
    """
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    client_ip = (request.client.host if request.client else "") or ""
    if client_ip not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "请使用卡密激活（购买卡密后在本面板输入兑换）",
                "code": "USE_REDEEM"}
    code = str(payload.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "缺少 code"}
    return _store(request).activate(code, via="ui_test")


@router.post("/api/member/redeem")
def member_redeem(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """卡密充值（兼容入口，实际走 /api/cloud/redeem）。

    账号制改动：卡密不再绑机器，而是充到当前登录的云端账号；换机重装只要重新登录，
    不需要人工解绑。参数兼容旧的 license_code 字段。
    """
    from routers.cloud_account import cloud_redeem
    code = str(payload.get("code") or payload.get("license_code") or "").strip()
    return cloud_redeem({"code": code})


@router.post("/api/member/credits/spend")
def credits_spend(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """AI 积分花费。payload: {"amount": 100, "reason": "asr"}。需登录。"""
    if not _require_user(request):
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    try:
        amount = int(payload.get("amount", 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "amount 必须为整数"}
    reason = str(payload.get("reason") or "ai_usage")
    return _store(request).spend_credits(amount, reason=reason)


@router.get("/api/member/credits/balance")
def credits_balance(request: Request) -> dict[str, Any]:
    if not _require_user(request):
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    return _store(request).credits_balance()


@router.get("/api/member/quota/{resource}")
def quota_state(resource: str, request: Request) -> dict[str, Any]:
    if not _require_user(request):
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    return _store(request).quota_state(resource)


@router.post("/api/member/quota/use")
def quota_use(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """消耗下载类配额。payload: {"resource": "download", "n": 1}。需登录。"""
    if not _require_user(request):
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    resource = str(payload.get("resource") or "").strip()
    if not resource:
        return {"ok": False, "error": "缺少 resource"}
    try:
        n = int(payload.get("n", 1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "n 必须为整数"}
    return _store(request).use_daily(resource, n=n)
