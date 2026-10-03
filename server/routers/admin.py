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

import time
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()

from admin_store import (
    list_users,
    set_user_disabled,
    reset_user_password,
    delete_user,
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


@router.post("/api/admin/users/{user_id}/delete")
def admin_delete_user(user_id: str, request: Request = None) -> dict[str, Any]:
    """删除账号（软删除）：不可删自己；超管账号在 admin_store.delete_user 里拦。"""
    require_admin(request)
    if user_id == get_current_user_id(request):
        return {"ok": False, "error": "不能删除当前登录的账号"}
    return delete_user(user_id)


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
    # pool: "ai"=AI 订阅积分 / "permanent"=永久积分；缺省 auto=旧语义（正充永久，负先 AI 后永久）
    pool = str(payload.get("pool") or "auto").strip() or "auto"
    return adjust_credits(user_id, delta, pool)


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
    """火山引擎账户卡（信息与实际链路对齐，2026-09-25 校正）：

    - 云端抠图（说扣什么）：visual.volcengineapi.com，SigV4 签名，用 **AK/SK**。
    - 抠图增强 / AI 画质增强：AI MediaKit，用 **Bearer Key**（mediakit_api_key）。
    - 云端去水印：**不依赖火山**（本地 LaMa 精修链路），旧卡片把它挂在火山名下是旧信息。
    """
    info = {
        "id": "volcengine",
        "name": "火山引擎 Volcengine（云端抠图 / 画质增强）",
        "provider": "volcengine",
        "model": "visual · mediakit",
        "modules": [],
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
        from cloud_matting_config import get_cloud_matting_config
        cfg = get_cloud_matting_config()
        ak = (cfg.get("access_key") or "").strip()
        sk = (cfg.get("secret_key") or "").strip()
        mk = (cfg.get("mediakit_api_key") or "").strip()
        enabled = bool(cfg.get("enabled"))
        visual_ready = bool(ak and sk)
        mk_ready = bool(mk)
        info["account"] = _mask(ak) if ak else "(未配置 AccessKey)"
        # 模块清单带实时就绪标记，用户一眼看到哪条链路可用
        info["modules"] = [
            f"云端抠图 / 说扣什么（visual · AK/SK）{'✓ 已配置' if visual_ready else '✗ 未配置 AK/SK'}",
            f"抠图增强 / AI 画质增强（AI MediaKit · Bearer Key）{'✓ 已配置' if mk_ready else '✗ 未配置 Key'}",
        ]
        if not enabled:
            info["status"] = "disabled"
            info["note"] = "云端抠图开关未启用（抠图设置 → ☁️ 云端抠图）；启用后「说扣什么」走火山像素级。"
        elif visual_ready:
            info["status"] = "enabled"
            info["note"] = ("云端抠图已就绪；MediaKit 增强" + ("已就绪。" if mk_ready else "未配 Bearer Key，仅画质增强/增强抠图回退 visual/本地。"))
        else:
            info["status"] = "no_api_key"
            info["note"] = "已启用但缺 AK/SK：云端抠图不可用，将回退本地 SAM/MODNet。"
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


# ── 用户使用详情（2026-10-03）───────────────────────────────────────────────
# 动机：用户反馈「用不了/很卡」时，管理员需要能看到他真实的权益、配额消耗、
# 激活与购买历史、客服原文（含自动附加的诊断日志），而不是靠猜。
# 纯函数 _usage_bundle 组装 → 便于离线测试；接口只做鉴权 + 拼装。

_USAGE_DAYS = 14          # 使用历史回看天数
_USAGE_MSG_PREVIEW = 160  # 客服消息摘要字数


def _usage_bundle(uid: str, ident: str, disabled: bool, is_admin: bool,
                  created_at: Any, store: Any) -> dict[str, Any]:
    """把一个用户的「使用情况」拼成一份可直接渲染的结构（纯函数，不碰网络）。"""
    import time as _t

    now = _t.time()
    try:
        st = store.status()
    except Exception:  # noqa: BLE001 — 状态读失败不该让整个面板 500
        st = {}

    # ---- 使用历史（按天，末 N 天；新的在前）----
    try:
        hist = dict(getattr(store, "_state", {}).get("usage_history") or {})
    except Exception:  # noqa: BLE001
        hist = {}
    def _counts(row: Any) -> dict[str, int]:
        """只取「资源名 → 次数」，跳过 date 这类非数值字段。"""
        out: dict[str, int] = {}
        for k, v in (row or {}).items():
            try:
                n = int(v or 0)
            except (TypeError, ValueError):
                continue        # date="2026-10-03" 之类，不是计数
            if n > 0:
                out[str(k)] = n
        return out

    days = []
    for day in sorted(hist.keys(), reverse=True)[:_USAGE_DAYS]:
        items = _counts(hist.get(day))
        days.append({"date": day, "total": sum(items.values()), "items": items})
    today_row = (getattr(store, "_state", {}) or {}).get("daily_usage") or {}
    today_items = _counts(today_row)
    today = next((d for d in days if d["date"] == _t.strftime("%Y-%m-%d", _t.localtime(now))), None)

    # ---- 激活 / 购买历史（倒序）----
    meta = (getattr(store, "_state", {}) or {}).get("meta") or {}
    acts = []
    for h in list(meta.get("history") or [])[-50:][::-1]:
        acts.append({
            "code": str(h.get("code") or ""),
            "type": str(h.get("type") or ""),
            "via": str(h.get("via") or ""),
            "at": float(h.get("at") or 0),
        })

    return {
        "user_id": uid,
        "identifier": ident,
        "disabled": bool(disabled),
        "is_admin": bool(is_admin),
        "created_at": created_at,
        "membership": st,
        "today": {"date": today_row.get("date") or _t.strftime("%Y-%m-%d"),
                  "items": today_items,
                  "total": int(sum(today_items.values()))},
        "usage_days": days,
        "usage_summary": {
            "days": len(days),
            "active_days": sum(1 for d in days if d["total"] > 0),
            "total": sum(d["total"] for d in days),
        },
        "activations": acts,
        "device_fp": str(meta.get("device_fp") or ""),
        "activated_at": float(meta.get("activated_at") or 0),
        "account_bound_at": float(meta.get("account_bound_at") or 0),
    }


def _usage_support(uid: str) -> dict[str, Any]:
    """该用户的客服会话（排障关键：用户原文 + 错误上报时自动附加的诊断日志）。"""
    try:
        from routers.support import _read_threads
        threads = [t for t in _read_threads() if t.get("user_id") == uid]
    except Exception:  # noqa: BLE001
        threads = []
    threads.sort(key=lambda t: t.get("updated_at") or 0, reverse=True)
    out = []
    for t in threads[:5]:
        msgs = t.get("messages") or []
        last_user = ""
        for m in msgs:
            if m.get("role") == "user":
                last_user = str(m.get("text") or "")
        out.append({
            "id": t.get("id"),
            "status": t.get("status"),
            "msg_count": len(msgs),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            "last_user_text": last_user[:_USAGE_MSG_PREVIEW],
            "has_diagnostics": "[错误上报]" in last_user,
        })
    return {"threads": out, "total_threads": len(threads)}


@router.get("/api/admin/users/{user_id}/usage")
def admin_user_usage(user_id: str, request: Request = None) -> dict[str, Any]:
    """超级管理员：查看单个用户的完整使用情况（排障用）。

    含：账号档案 / 会员与积分 / 今日与近 14 天配额消耗 / 激活购买历史 /
    客服会话原文 / 云端授权状态（可拉时）。
    """
    require_admin(request)
    import auth_store
    from user_membership import get_user_store

    user = next((u for u in auth_store._load_users().get("users", [])
                 if u.get("user_id") == user_id and not u.get("deleted_at")), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    store = get_user_store(user_id)
    bundle = _usage_bundle(
        user_id,
        str(user.get("identifier") or user_id),
        bool(user.get("disabled")),
        bool(user.get("is_admin")),
        user.get("created_at"),
        store,
    )
    bundle["support"] = _usage_support(user_id)
    bundle["cloud"] = _usage_cloud(user.get("identifier") or "")
    return {"ok": True, "usage": bundle}


def _usage_cloud(identifier: str) -> dict[str, Any]:
    """云端授权状态（设备数 / 最近心跳 / 额度）。拉不到就降级，不影响本机数据。"""
    ident = str(identifier or "").strip()
    if not ident:
        return {"ok": False, "reason": "无账号标识"}
    try:
        import json as _json
        import urllib.request as _rq
        from admin_store import _license_admin_token
        from license_client import license_base
        token = _license_admin_token()
        if not token:
            return {"ok": False, "reason": "未配置授权中心管理员令牌"}
        url = f"{str(license_base() or '').rstrip('/')}/api/license/usage"
        body = _json.dumps({"token": token, "email": ident}).encode("utf-8")
        req = _rq.Request(url, data=body, method="POST",
                          headers={"Content-Type": "application/json"})
        with _rq.urlopen(req, timeout=6) as resp:
            data = _json.loads(resp.read().decode("utf-8") or "{}")
        if isinstance(data, dict) and data.get("ok") is False:
            return {"ok": False, "reason": str(data.get("error") or "云端无记录")}
        return {"ok": True, "data": data}
    except Exception as e:  # noqa: BLE001 — 云端不可达只提示，不阻塞
        return {"ok": False, "reason": f"云端不可达：{e}"}


# ── 套餐档位快速上下架（2026-10-03）─────────────────────────────────────────
# 场景：某档要临时停售（活动结束 / 出问题），运营不该先去「⚙ 营销」里找复选框
# 再点保存——那个保存是全表单提交，会把其它未保存的编辑一起带上去。
# 所以单开一个端点：只改这一档的 on_sale，走字段级合并，并同步云端。
# 注意：前端「下架」不是删除 —— 已购买用户的权益不受影响，前台该档隐藏且无法下单。

_SALE_TABLES = ("download_plans", "ai_plans", "credit_packs")


def _find_plan_table(code: str) -> str:
    """按 code 在本机覆盖层里找归属表；找不到就回退到内置常量所在的表。"""
    from membership import load_plan_overrides
    ov = load_plan_overrides() or {}
    for t in _SALE_TABLES:
        if code in (ov.get(t) or {}):
            return t
    from membership import AI_PLANS, CREDIT_PACKS, DOWNLOAD_PLANS
    if code in DOWNLOAD_PLANS:
        return "download_plans"
    if code in AI_PLANS:
        return "ai_plans"
    if code in CREDIT_PACKS:
        return "credit_packs"
    return ""


@router.post("/api/admin/plans/{code}/sale")
def admin_plan_sale(code: str, payload: dict[str, Any] = Body(...),
                    request: Request = None) -> dict[str, Any]:
    """超级管理员：单个档位快速上架 / 下架（不落其它表单改动）。"""
    require_admin(request)
    from admin_store import save_plan_overrides as _save
    import membership as M

    table = str(payload.get("table") or "") or _find_plan_table(code)
    if table not in _SALE_TABLES:
        return {"ok": False, "error": f"未知套餐类别：{table or '（无法定位该档）'}"}
    on_sale = bool(payload.get("on_sale", True))
    try:
        _save({table: {code: {"on_sale": on_sale}}})
    except OSError as e:  # noqa: BLE001
        return {"ok": False, "error": f"写入失败：{e}"}
    # 下架要立刻同步云端：网页版与真实收款也读那份表，否则网页还能卖下架的档
    cloud = M.push_plans_to_cloud()
    st = M.plan_sales_state(M.effective_plans().get(table, {}).get(code) or {}, now=time.time())
    return {"ok": True, "code": code, "table": table, "on_sale": on_sale,
            "state": st, "cloud": cloud}
