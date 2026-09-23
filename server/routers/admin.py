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


# ── AI 大模型账户（2026-09-24 新增）────────────────────────────────────────
# 超级管理员面板用：汇总各 AI 提供方的「余额 / 使用模块 / 账号标识 / 充值入口」。
# DeepSeek 余额走网关实时取（Key 仅在服务端）；百炼/火山无通用余额 REST，
# 标记为 console（前端给出控制台/充值链接）。
import json
import os
import urllib.error
import urllib.request


def _gw_get(url: str, token: str) -> dict:
    """带 Bearer token 调网关，绕过本机代理（direct:// 语义），失败返回 ok=False。"""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=12) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _gateway_base() -> tuple:
    """返回 (base_url, token) 或 (None, None)。"""
    p = os.path.expanduser("~/.video-downloader/gateway_managed.json")
    if not os.path.exists(p):
        return None, None
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None, None
    url = (d.get("url") or "").replace("direct://", "", 1).rstrip("/")
    return url, d.get("token")


def _mask(s: str) -> str:
    s = str(s or "")
    return "****" if len(s) <= 8 else f"{s[:4]}\u2026{s[-4:]}"


def _deepseek_account() -> dict:
    info = {
        "id": "deepseek",
        "name": "DeepSeek（解说大模型）",
        "provider": "deepseek",
        "model": "",
        "modules": ["视频解说 / 解说词生成", "长片云端兜底 LLM"],
        "account": "",
        "status": "unknown",
        "balance": None,
        "balances": [],
        "currency": None,
        "is_available": None,
        "balance_source": "none",
        "recharge_url": "https://platform.deepseek.com/top_up",
        "console_url": "https://platform.deepseek.com",
        "note": "",
    }
    base, token = _gateway_base()
    if not base:
        info["note"] = "未找到网关配置（gateway_managed.json）"
        info["status"] = "no_gateway"
        return info
    # 模型名 + 健康
    try:
        h = _gw_get(base + "/health", token)
        if h and h.get("ok"):
            info["model"] = (h.get("models") or [""])[0]
        elif h:
            info["note"] = h.get("error", "")
    except Exception as e:
        info["note"] = f"网关查询失败：{e}"
    # 余额（实时）
    try:
        b = _gw_get(base + "/balance", token)
        if b and b.get("ok"):
            info["balance"] = b.get("balance")
            info["balances"] = b.get("balances") or []
            info["currency"] = b.get("currency")
            info["is_available"] = b.get("is_available")
            info["balance_source"] = "live"
            info["status"] = "ok" if b.get("is_available") else "insufficient"
            if not b.get("is_available"):
                info["note"] = "余额已耗尽/为负，云端解说调用会被拒，请尽快充值"
            info["account"] = f"网关令牌 {_mask(token)}（上游 Key 仅在服务端）"
        elif b:
            info["balance_source"] = "error"
            info["status"] = "error"
            info["note"] = b.get("error", "余额查询失败")
            info["account"] = f"网关令牌 {_mask(token)}"
    except Exception as e:
        info["status"] = "error"
        info["note"] = f"余额查询异常：{e}"
    return info


def _dashscope_account() -> dict:
    info = {
        "id": "dashscope",
        "name": "阿里百炼 DashScope（视觉 / VLM）",
        "provider": "dashscope",
        "model": "",
        "modules": ["视觉理解 / 图片 OCR", "抠图 VLM 自动分类"],
        "account": "",
        "status": "unknown",
        "balance": None,
        "balances": [],
        "currency": None,
        "is_available": None,
        "balance_source": "console",
        "recharge_url": "https://billing.console.aliyun.com/?#/account/balance",
        "console_url": "https://dashscope.console.aliyun.com/",
        "note": "余额请登录阿里云费用中心查看；有免费额度，中文 OCR 强。",
    }
    try:
        from vision_config import get_vision_config
        cfg = get_vision_config()
        key = (cfg.get("api_key") or "").strip()
        info["model"] = cfg.get("model") or ""
        info["account"] = _mask(key) if key else "(未配置 Key)"
        info["status"] = "configured" if key else "not_configured"
    except Exception as e:  # noqa: BLE001
        info["note"] = f"读取视觉配置失败：{e}"
        info["status"] = "error"
    return info


def _volcengine_account() -> dict:
    info = {
        "id": "volcengine",
        "name": "火山引擎 Volcengine（云端去水印 / 抠图）",
        "provider": "volcengine",
        "model": "mediakit / visual",
        "modules": ["云端去水印（mediakit）", "云端抠图（visual）"],
        "account": "",
        "status": "unknown",
        "balance": None,
        "balances": [],
        "currency": None,
        "is_available": None,
        "balance_source": "console",
        "recharge_url": "https://console.volcengine.com/wallet",
        "console_url": "https://console.volcengine.com/",
        "note": "按量计费，余额请登录火山控制台「费用中心」查看。",
    }
    try:
        from cloud_matting_config import (
            get_cloud_matting_config,
            is_cloud_matting_mediakit_ready,
        )
        cfg = get_cloud_matting_config()
        ak = (cfg.get("access_key") or "").strip()
        info["account"] = _mask(ak) if ak else "(未配置 AccessKey)"
        enabled = bool(cfg.get("enabled"))
        mediakit = is_cloud_matting_mediakit_ready()
        if enabled and mediakit:
            info["status"] = "enabled"
        elif not enabled:
            info["status"] = "disabled"
        else:
            info["status"] = "no_api_key"
    except Exception as e:  # noqa: BLE001
        info["note"] = f"读取火山配置失败：{e}"
        info["status"] = "error"
    return info


def _collect_ai_accounts() -> list:
    return [_deepseek_account(), _dashscope_account(), _volcengine_account()]


@router.get("/api/admin/ai/accounts")
def admin_ai_accounts(request: Request = None) -> dict[str, Any]:
    """超级管理员：汇总各 AI 提供方账户（余额 / 模块 / 账号 / 充值入口）。"""
    require_admin(request)
    return {"ok": True, "accounts": _collect_ai_accounts()}
