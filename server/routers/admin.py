"""server/routers/admin.py — 后台管理面板 API（/api/admin/*）。

全部接口需「已登录 + 超级用户」权限：经 Authorization: Bearer <user_token> 携带
用户 token，并由 users.json 中 is_admin=True 的账号放行。非超级用户或缺失 token
一律 401。

超级用户名单由 server/auth_store.ensure_superusers 维护：admin.json 的 admin_identifiers
或环境变量 VDL_ADMIN_IDENTIFIER；两者均未配置时，首个注册的账号自动成为超级用户。
后台内可经 /api/admin/users/{id}/set-admin 提权 / 降权其他账号。

功能模块：用户管理 / 会员管理 / 使用统计 / 系统配置。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()

from admin_store import (
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
from user_membership import get_current_user_id
from auth_store import user_is_admin, set_user_admin


def require_admin(request: Request) -> None:
    from fastapi import HTTPException
    uid = get_current_user_id(request)
    if not uid or not user_is_admin(uid):
        raise HTTPException(status_code=401, detail="未授权：需要超级用户权限")


@router.get("/api/admin/users")
def admin_list_users(request: Request = None) -> dict[str, Any]:
    require_admin(request)
    return {"ok": True, "users": list_users(), "total": len(list_users())}


@router.post("/api/admin/users/{user_id}/set-admin")
def admin_set_admin(user_id: str, payload: dict[str, Any] = Body(...), request: Request = None) -> dict[str, Any]:
    require_admin(request)
    flag = bool(payload.get("is_admin", False))
    return set_user_admin(user_id, flag)


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
