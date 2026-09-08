"""server/routers/auth.py — VDL 本地账号（/api/auth/*，A1）。

注册/登录/查询当前用户。token 为 HMAC 签名的无状态 Bearer，前端存于
localStorage('vdl_auth_token')，后续请求经 Authorization: Bearer <token> 携带。

V1 不做邮箱/手机验证、不做密码找回（本地账号，凭据本机可控）。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()


def _auth_user_id(request: Request) -> Optional[str]:
    return request.headers.get("Authorization", "")


def _bearer(request: Request) -> Optional[str]:
    from auth_store import token_from_header
    return token_from_header(request.headers.get("Authorization"))


@router.post("/api/auth/register")
def auth_register(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ident = str(payload.get("identifier") or "").strip().lower()
    pw = str(payload.get("password") or "")
    if not ident:
        return {"ok": False, "error": "请输入邮箱或手机号"}
    if "@" not in ident and not ident.startswith("+"):
        return {"ok": False, "error": "账号需为邮箱（含@）或手机号（以+开头）"}
    if len(pw) < 6:
        return {"ok": False, "error": "密码至少 6 位"}
    from auth_store import create_user, issue_token
    uid = create_user(ident, pw)
    if not uid:
        return {"ok": False, "error": "该账号已注册，请直接登录"}
    return {"ok": True, "token": issue_token(uid), "user_id": uid, "identifier": ident}


@router.post("/api/auth/login")
def auth_login(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ident = str(payload.get("identifier") or "").strip().lower()
    pw = str(payload.get("password") or "")
    if not ident or not pw:
        return {"ok": False, "error": "请输入账号和密码"}
    from auth_store import authenticate, issue_token
    uid = authenticate(ident, pw)
    if not uid:
        return {"ok": False, "error": "账号或密码错误"}
    return {"ok": True, "token": issue_token(uid), "user_id": uid, "identifier": ident}


@router.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    uid = _bearer(request)
    if not uid:
        return {"ok": False, "error": "未登录", "code": "NO_AUTH"}
    from auth_store import user_identifier
    return {"ok": True, "user_id": uid, "identifier": user_identifier(uid) or uid}
