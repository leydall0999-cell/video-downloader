"""server/routers/admin.py — 后台管理面板 API（/api/admin/*）。

全部接口需管理员 token（POST /api/admin/login 获取的 admin token），经
Authorization: Bearer <admin_token> 携带。无 token / token 失效返回 401。

功能模块：用户管理 / 会员管理 / 使用统计 / 系统配置。
底层数据见 server/admin_store.py（独立于用户账号体系，口令单独存储于 admin.json）。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()

from admin_store import (
    verify_admin_password,
    issue_admin_token,
    verify_admin_token,
    change_admin_password,
    list_users,
    set_user_disabled,
    reset_user_password,
    list_memberships,
    grant_membership,
    adjust_credits,
    usage_stats,
    system_config,
    reset_stats,
    save_smtp_accounts,
    save_plan_overrides,
)


def _admin_token(request: Request) -> Optional[str]:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return auth[7:].strip() or None


def require_admin(request: Request) -> None:
    tok = _admin_token(request)
    if not tok or not verify_admin_token(tok):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="未授权：需要管理员登录")


@router.post("/api/admin/login")
def admin_login(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    pw = str(payload.get("password") or "")
    if not verify_admin_password(pw):
        return {"ok": False, "error": "管理员口令错误"}
    return {"ok": True, "admin_token": issue_admin_token()}


@router.post("/api/admin/change-password")
def admin_change_password(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_admin(request)
    old_pw = str(payload.get("old_password") or "")
    new_pw = str(payload.get("new_password") or "")
    return change_admin_password(old_pw, new_pw)


@router.get("/api/admin/users")
def admin_list_users(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    return {"ok": True, "users": list_users(), "total": len(list_users())}


@router.post("/api/admin/users/{user_id}/disable")
def admin_disable_user(user_id: str, request: Request = None) -> dict[str, Any]:
    require_admin(request)
    return set_user_disabled(user_id, True)


@router.post("/api/admin/users/{user_id}/enable")
def admin_enable_user(user_id: str, request: Request = None) -> dict[str, Any]:
    require_admin(request)
    return set_user_disabled(user_id, False)


@router.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: str, payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_admin(request)
    new_pw = str(payload.get("new_password") or "")
    return reset_user_password(user_id, new_pw)


@router.get("/api/admin/memberships")
def admin_list_memberships(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    items = list_memberships()
    return {"ok": True, "memberships": items, "total": len(items)}


@router.post("/api/admin/memberships/grant")
def admin_grant(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_admin(request)
    user_id = str(payload.get("user_id") or "")
    code = str(payload.get("code") or "").strip()
    if not user_id or not code:
        return {"ok": False, "error": "user_id 与 code 必填"}
    return grant_membership(user_id, code)


@router.post("/api/admin/memberships/credits")
def admin_credits(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_admin(request)
    user_id = str(payload.get("user_id") or "")
    try:
        delta = int(payload.get("delta"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "delta 必须为整数"}
    return adjust_credits(user_id, delta)


@router.get("/api/admin/stats")
def admin_stats(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    return {"ok": True, **usage_stats()}


@router.post("/api/admin/stats/reset")
def admin_stats_reset(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    return reset_stats()


@router.get("/api/admin/config")
def admin_config(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    return {"ok": True, **system_config()}


@router.post("/api/admin/config/smtp")
def admin_config_smtp(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_admin(request)
    accounts = payload.get("accounts")
    if not isinstance(accounts, list):
        return {"ok": False, "error": "accounts 必须为数组"}
    return save_smtp_accounts(accounts)


@router.post("/api/admin/config/plans")
def admin_config_plans(payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_admin(request)
    return save_plan_overrides(payload or {})
