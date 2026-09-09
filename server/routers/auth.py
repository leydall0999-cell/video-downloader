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


def _require_user(request: Request) -> Optional[str]:
    """解析已登录用户 user_id（未登录返回 None）。"""
    import app as _app
    return _app.get_current_user_id(request)


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
    from auth_store import create_user, issue_token, ensure_superusers, user_is_admin
    uid = create_user(ident, pw)
    if not uid:
        return {"ok": False, "error": "该账号已注册，请直接登录"}
    ensure_superusers()
    record_event("register", {"identifier": ident})
    return {"ok": True, "token": issue_token(uid), "user_id": uid, "identifier": ident,
            "is_admin": bool(user_is_admin(uid))}


@router.post("/api/auth/login")
def auth_login(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ident = str(payload.get("identifier") or "").strip().lower()
    pw = str(payload.get("password") or "")
    if not ident or not pw:
        return {"ok": False, "error": "请输入账号和密码"}
    from auth_store import authenticate, issue_token, ensure_superusers, user_is_admin
    uid = authenticate(ident, pw)
    if not uid:
        return {"ok": False, "error": "账号或密码错误"}
    ensure_superusers()
    record_event("login", {"identifier": ident})
    return {"ok": True, "token": issue_token(uid), "user_id": uid, "identifier": ident,
            "is_admin": bool(user_is_admin(uid))}


@router.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    uid = _bearer(request)
    if not uid:
        return {"ok": False, "error": "未登录", "code": "NO_AUTH"}
    from auth_store import user_identifier, user_is_admin
    return {"ok": True, "user_id": uid, "identifier": user_identifier(uid) or uid,
            "is_admin": bool(user_is_admin(uid))}


@router.get("/api/account/profile")
def account_profile(request: Request) -> dict[str, Any]:
    """个人中心聚合数据：身份信息 + 会员有效时间 + 使用记录 + 购买记录 + 积分消耗记录。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录", "code": "NO_AUTH"}
    import user_membership
    from auth_store import user_identifier, user_is_admin, _load_users
    ident = user_identifier(uid) or uid
    store = user_membership.get_user_store(uid)
    st = store.status()
    raw_hist = (store._state.get("meta", {}) or {}).get("history", [])
    purchases: list[dict] = []
    credits: list[dict] = []
    for h in raw_hist:
        t = h.get("type")
        if t == "spend" or t == "admin_adjust":
            credits.append(dict(h))
        else:  # None / "activate" → 购买/激活
            purchases.append(dict(h))
    purchases.sort(key=lambda x: x.get("at", 0), reverse=True)
    credits.sort(key=lambda x: x.get("at", 0), reverse=True)
    # 积分流水补充变动值与变动后余额（从当前余额倒推，保证与现状一致）
    balance = int(st.get("credits_total", 0))
    for h in credits:
        if h.get("type") == "spend":
            delta = -int(h.get("amount", 0))
        elif h.get("type") == "admin_adjust":
            code = str(h.get("code", ""))
            try:
                delta = int(code.split(":", 1)[1]) if ":" in code else 0
            except ValueError:
                delta = 0
        else:
            delta = 0
        h["delta"] = delta
        h.setdefault("balance_after", balance)
        balance -= delta
    # 注册时间
    created_at = None
    data = _load_users()
    u = next((x for x in data.get("users", []) if x.get("user_id") == uid), None)
    if u:
        created_at = u.get("created_at")
    return {
        "ok": True,
        "user_id": uid,
        "identifier": ident,
        "is_admin": bool(user_is_admin(uid)),
        "created_at": created_at,
        "membership": st,
        "purchases": purchases,
        "credit_history": credits,
        "usage": st.get("daily_usage", {}),
    }


@router.post("/api/auth/change-password")
def auth_change_password(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """本人凭当前密码修改密码（需登录）。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录", "code": "NO_AUTH"}
    cur = str(payload.get("current_password") or "")
    new = str(payload.get("new_password") or "")
    if not cur or not new:
        return {"ok": False, "error": "请输入当前密码和新密码"}
    if len(new) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    from auth_store import user_identifier, authenticate, reset_password
    ident = user_identifier(uid)
    if not ident:
        return {"ok": False, "error": "账号不存在"}
    if not authenticate(ident, cur):
        return {"ok": False, "error": "当前密码错误"}
    reset_password(ident, new)
    record_event("change_pw", {"identifier": ident})
    return {"ok": True}


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
