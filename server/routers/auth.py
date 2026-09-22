"""server/routers/auth.py — VDL 本地账号（/api/auth/*，A1）。

注册/登录/查询当前用户/本地重置密码（验证码流程）。token 为 HMAC 签名的无状态 Bearer，
前端存于 localStorage('vdl_auth_token')，后续请求经 Authorization: Bearer <token> 携带。

找回密码流程：先 POST /api/auth/reset-code 获取验证码（dev 模式**仅本机**展示 / smtp 真实投递），
再 POST /api/auth/reset 携带 code 完成改密。

🔴 dev 模式下验证码**只回传给本机调用方**（桌面 App 的界面走 127.0.0.1，UX 不变）。公网部署
若因缺少 `smtp.json` 落到 dev，把验证码写进响应体＝把任意账号的改密权交给调用方（账号接管），
故按调用方地址卡死；网页版要能重置密码就得配 `smtp.json`（模式自动切 smtp），而不是靠回传。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()

from stats import record_event


# 头像上传/读取相关依赖（懒加载，避免模块顶层循环 import）
def _avatar_helpers():
    from auth_store import set_user_avatar, user_avatar_url, _avatar_path
    return set_user_avatar, user_avatar_url, _avatar_path

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


# 本机回环地址：桌面 App 的前端固定访问 127.0.0.1:8321，故其调用方恒为其中之一。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback(request: Request) -> bool:
    """判断调用方是否来自本机。

    ⚠️ 本判定依赖「反代后的 `request.client.host` 是真实客户端 IP」这一**部署侧前提**：
    实测生产 VPS 的 `vdl-web` 访问日志为真实公网 IP（uvicorn 采纳了 nginx 的
    `X-Forwarded-For`），故公网请求不会被误判为本机。该前提**无法在离线测试里钉住**
    （属 nginx 配置），改反代/换端口后必须复核一次 —— 见 skill `vdl-build-release`。
    """
    try:
        client = request.client
        host = (client.host if client else "") or ""
    except Exception:  # noqa: BLE001 — 取不到就当公网，宁可少回传
        return False
    return host in _LOOPBACK_HOSTS


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
def auth_login(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ident = str(payload.get("identifier") or "").strip().lower()
    pw = str(payload.get("password") or "")
    if not ident or not pw:
        return {"ok": False, "error": "请输入账号和密码"}
    from auth_store import (authenticate, issue_token, ensure_superusers,
                            user_is_admin, user_exists)
    uid = authenticate(ident, pw)
    if not uid:
        # 🔴 2026-09-22 实测缺陷：此前统一回「账号或密码错误」，桌面端拿这个文案
        #    又去和云端比对，最终把云端的「账号不存在」显示给用户——手机号老账号
        #    只存在于本机（云端当年 register 拒收非邮箱），于是用户明明账号在、
        #    只是密码打错，却被提示「账号不存在，请先注册」。
        #    本机账号表才是权威，故在本机回环时区分两种失败，让提示与事实一致。
        #    公网（网页版）保持模糊文案，避免账号枚举。
        if _is_loopback(request) and user_exists(ident):
            return {"ok": False, "error": "密码不正确", "code": "BAD_PASSWORD"}
        if _is_loopback(request):
            return {"ok": False, "error": "账号不存在，请先注册", "code": "NO_ACCOUNT"}
        return {"ok": False, "error": "账号或密码错误", "code": "BAD_CREDENTIALS"}
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
def account_profile(request: Request, usage_period: str = "today") -> dict[str, Any]:
    """个人中心聚合数据：身份信息 + 会员有效时间 + 使用记录 + 购买记录 + 积分消耗记录。

    usage_period: today | 3d | 7d | month
    """
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录", "code": "NO_AUTH"}
    if usage_period not in ("today", "3d", "7d", "month"):
        usage_period = "today"
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
    from auth_store import user_avatar_url
    from membership import feature_usage_status
    return {
        "ok": True,
        "user_id": uid,
        "identifier": ident,
        "is_admin": bool(user_is_admin(uid)),
        "created_at": created_at,
        "avatar_url": user_avatar_url(uid),
        "membership": st,
        "purchases": purchases,
        "credit_history": credits,
        "usage": st.get("daily_usage", {}),
        "usage_period": usage_period,
        "usage_features": feature_usage_status(store, period=usage_period),
    }


@router.post("/api/account/avatar")
def account_avatar_upload(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """上传/更新当前用户头像。payload 支持 base64 data URL 或纯 base64 字符串。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录", "code": "NO_AUTH"}
    raw = payload.get("image") or ""
    if not raw:
        return {"ok": False, "error": "请选择图片"}
    import base64
    data = raw
    if "," in raw:
        data = raw.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(data)
    except Exception:
        return {"ok": False, "error": "图片解码失败，请重新选择"}
    set_user_avatar, _, _ = _avatar_helpers()
    return set_user_avatar(uid, image_bytes)


@router.get("/api/account/avatar/{user_id}")
def account_avatar_get(request: Request, user_id: str) -> Any:
    """读取指定用户头像。允许任何人读取（头像为公开资源），但文件必须存在。"""
    import base64
    import mimetypes
    from fastapi.responses import FileResponse
    _, _, _avatar_path = _avatar_helpers()
    p = _avatar_path(user_id)
    if not p.exists():
        return {"ok": False, "error": "未设置头像", "code": "NOT_FOUND"}
    ctype = mimetypes.guess_type(str(p))[0] or "image/png"
    return FileResponse(str(p), media_type=ctype)


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


@router.post("/api/account/deactivate")
def account_deactivate(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """注销当前登录账号（软删除），注销后不可再用该邮箱/手机号登录或重新注册。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录", "code": "NO_AUTH"}
    from auth_store import user_identifier, deactivate_user
    ident = user_identifier(uid)
    result = deactivate_user(uid)
    if result.get("ok"):
        record_event("deactivate", {"identifier": ident})
    return result


@router.post("/api/auth/reset-code")
def auth_reset_code(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
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
    ident_is_email = "@" in ident
    delivery_failed = False
    if code:
        try:
            deliver_reset_code(ident, code)
        except Exception as e:  # noqa: BLE001
            logging.getLogger("vdl.auth").error("投递验证码失败: %s", e)
            delivery_failed = True
            # 本机（桌面 App）如实报错，用户能看到真问题并自助处理。
            # 🔴 2026-09-22 实测缺陷：投递模式是 smtp，而 SMTP 只发邮箱 —— 手机号
            #    必然走到这里。若照旧直接回「发送失败」，**忘记密码的手机号用户就
            #    永久卡在登录页**（本次报障的 15014313254 正是这种形态）。短信网关
            #    （sms 模式）尚未接入，故手机号在本机回环时改走下方自助回显；
            #    邮箱投递失败仍如实报错（那是配置问题，不该被顺手放过）。
            if ident_is_email and _is_loopback(request):
                return {"ok": False, "error": "验证码发送失败，请检查邮件服务配置"}
            # 公网不回错误体：与「账号不存在」保持同一响应形状，防账号枚举。
    # 无论账号是否存在都返回 ok（防账号枚举）。
    # 🔴 全文件**唯一**的验证码回传点，必须同时受「本机回环」与「投放渠道」约束：
    #    · dev 模式 + 本机 → 桌面 App 本地调试（原有语义）
    #    · 手机号投递失败 + 本机 → 自助找回（sms 未接入，否则用户永久进不来）
    #    公网部署一旦因缺少 smtp.json 落到 dev，回传验证码＝调用方可直接改任意账号
    #    密码（账号接管），故 _is_loopback 是硬前提，不可放宽。
    dev_code = code if (code and _is_loopback(request)
                        and (_send_mode() == "dev" or delivery_failed)) else None
    # self_serve：这次回传是因为**投递渠道走不通**（手机号 + sms 未接入），
    # 而不是 dev 调试模式——前端据此换一句人话，别让用户以为「这是个测试功能」。
    return {"ok": True, "dev_code": dev_code, "expires_in": 300,
            "self_serve": bool(dev_code and delivery_failed)}


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
