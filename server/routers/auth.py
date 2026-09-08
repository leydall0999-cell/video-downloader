"""server/routers/auth.py — VDL 本地账号（/api/auth/*，A1）。

注册/登录/查询当前用户/本地重置密码（验证码流程）。token 为 HMAC 签名的无状态 Bearer，
前端存于 localStorage('vdl_auth_token')，后续请求经 Authorization: Bearer <token> 携带。

找回密码流程：先 POST /api/auth/reset-code 获取验证码（dev 模式本地展示 / smtp 真实投递），
再 POST /api/auth/reset 携带 code 完成改密。V1 验证码投递为 dev 模式，后续可切 smtp/sms。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()

from stats import record_event

# 邮箱：标准格式校验，支持 QQ 邮箱（@qq.com/@foxmail.com）、Google 邮箱
# （@gmail.com/@googlemail.com）及其常见变体（用户名含 . + % - 等）。
# 不限制特定域名，所有合法邮箱均可通过。
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# 手机号：支持中国大陆 11 位（1[3-9]...）或 E.164 国际格式（+ 开头）
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


def _is_valid_identifier(ident: str) -> bool:
    """账号为合法邮箱（支持 QQ/Google 等）或有效手机号（不强求 + 开头）。"""
    if "@" in ident:
        return bool(_EMAIL_RE.match(ident))
    return bool(_PHONE_RE.match(ident) or _E164_RE.match(ident))


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
    if not _is_valid_identifier(ident):
        return {"ok": False, "error": "账号需为有效邮箱（支持 QQ/Google 邮箱）或手机号"}
    if len(pw) < 6:
        return {"ok": False, "error": "密码至少 6 位"}
    from auth_store import create_user, issue_token
    uid = create_user(ident, pw)
    if not uid:
        return {"ok": False, "error": "该账号已注册，请直接登录"}
    record_event("register", {"identifier": ident})
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
    record_event("login", {"identifier": ident})
    return {"ok": True, "token": issue_token(uid), "user_id": uid, "identifier": ident}


@router.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    uid = _bearer(request)
    if not uid:
        return {"ok": False, "error": "未登录", "code": "NO_AUTH"}
    from auth_store import user_identifier
    return {"ok": True, "user_id": uid, "identifier": user_identifier(uid) or uid}


@router.post("/api/auth/reset-code")
def auth_reset_code(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ident = str(payload.get("identifier") or "").strip().lower()
    if not ident:
        return {"ok": False, "error": "请输入邮箱或手机号"}
    if not _is_valid_identifier(ident):
        return {"ok": False, "error": "账号需为有效邮箱（支持 QQ/Google 邮箱）或手机号"}
    from auth_store import (
        generate_reset_code,
        reset_code_cooldown_ok,
        deliver_reset_code,
        _send_mode,
    )
    if not reset_code_cooldown_ok(ident):
        return {"ok": False, "error": "验证码已发送，请稍后再试（60 秒冷却）"}
    code = generate_reset_code(ident)
    if code:
        try:
            deliver_reset_code(ident, code)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("vdl.auth").error("投递验证码失败: %s", e)
            return {"ok": False, "error": "验证码发送失败，请检查邮件服务配置"}
    # 无论账号是否存在都返回 ok（防账号枚举）；dev 模式附带 dev_code 便于本地测试
    dev_code = code if _send_mode() == "dev" else None
    return {"ok": True, "dev_code": dev_code, "expires_in": 300}


@router.post("/api/auth/reset")
def auth_reset(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ident = str(payload.get("identifier") or "").strip().lower()
    pw = str(payload.get("password") or "")
    code = str(payload.get("code") or "").strip()
    if not ident:
        return {"ok": False, "error": "请输入邮箱或手机号"}
    if not _is_valid_identifier(ident):
        return {"ok": False, "error": "账号需为有效邮箱（支持 QQ/Google 邮箱）或手机号"}
    if not code:
        return {"ok": False, "error": "请输入验证码"}
    if len(pw) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    from auth_store import verify_reset_code, reset_password
    if not verify_reset_code(ident, code):
        return {"ok": False, "error": "验证码错误或已过期"}
    if not reset_password(ident, pw):
        return {"ok": False, "error": "该账号不存在，无法重置"}
    record_event("reset_pw", {"identifier": ident})
    return {"ok": True}
