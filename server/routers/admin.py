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
    out = save_plan_overrides(payload or {})
    # 🔴 2026-10-06 诚实回报：云端终身免费次数的**放行判定在授权中心**，而中心写入
    #   端点 /api/license/free_quota_set 要求**桌面端的 license_admin 令牌** ——
    #   网页版没有这套令牌（只有本站 superuser），因此这里**无法**下发覆盖。
    #   与其静默返回 ok 让管理员以为改了两端，不如如实说明「只改了本机缓存」。
    #   要改云端放行上限，请到桌面端 App 后台保存（那里会下发中心）。
    if isinstance(payload, dict) and "free_quota" in payload:
        out = dict(out)
        out["cloud_free_quota"] = {
            "ok": False,
            "reason": "web_admin_cannot_push_center",
            "hint": "云端放行上限以授权中心为准，请在桌面端 App 后台修改并保存",
        }
    return out


# ── AI 积分成本 / 免费试用策略（2026-10-05 与桌面端拉齐）─────────────────────
# 这两张表都写在 plans.json 覆盖层，改完即时生效、不需要重启服务。
# 网页版与桌面端**同一套语义**：各功能的积分单价由 plans.json 逐项覆盖，
# `free_trial` 控制「账号首次使用免费一次」。


@router.get("/api/admin/ai/credit-costs")
def admin_ai_credit_costs(request: Request = None) -> dict[str, Any]:
    """超级管理员：读取各 AI 功能的积分单价（**不含任何凭据/密钥**）。

    与桌面端同端点、同返回结构，前端两侧可以复用同一份渲染代码。
    """
    require_admin(request)
    from membership import AI_CREDIT_COSTS, credit_cost_table, free_trial_policy
    return {
        "ok": True,
        "costs": credit_cost_table(),
        "registered": len(AI_CREDIT_COSTS),
        # 免费用户「首次体验」策略（2026-10-05 晚定档：账号首次使用免费一次）
        "free_trial": free_trial_policy(),
        "trial_modes": ["once", "per_op", "off"],
        "policy": {
            "scope": "云端算力 + 服务端算力都计费（2026-10-05 定档）",
            "granularity": "按「次」定价：一次操作内发生多次模型调用只扣一份，重试成本含在单价里",
            # 🔴 网页版特有的成本性质，必须让管理员知道：
            "local_note": "网页版的去水印（LaMa）与字幕提取跑在**服务端 ECS**上，"
                          "占的是服务器 CPU，不像桌面端那样「占用的是用户自己的机器」。",
            "trial": "只要是首次使用的账号，其第一次使用 AI 功能免费一次；"
                     "已经在 AI 会员有效期内或有足够积分的用户不消耗名额（先花自己的积分）。",
        },
    }


@router.post("/api/admin/ai/credit-costs")
def admin_ai_set_credit_costs(
    request: Request = None,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """超级管理员：调整各功能的积分单价 / 免费试用策略。

    payload: `{"costs": {"commentary_llm": 60, ...}, "reset": ["op", ...],
               "free_trial": {"enabled": true, "mode": "once"|"per_op"|"off", ...}}`
      · 只提交要改的项，其余保持不变（不覆盖别人的改动）。
      · 传 `0` = 该功能免费（显式免费，不再依赖「表外默认 0」）。
      · `reset` 列出要恢复代码默认价的 op。
      · **op 必须已在 `AI_CREDIT_COSTS` 中登记**，未知 op 直接报错 ——
        防止打错字导致配置无效（且避免把 typo 写进 plans.json 变成死配置）。
    """
    require_admin(request)
    from fastapi import HTTPException
    from membership import (AI_CREDIT_COSTS, credit_cost_table, free_trial_policy,
                            load_plan_overrides, save_plan_overrides)

    costs_in = payload.get("costs")
    if costs_in is not None and not isinstance(costs_in, dict):
        raise HTTPException(status_code=400, detail="costs 必须是对象 {op: 积分}")

    current = dict(load_plan_overrides().get("credit_costs") or {})
    unknown = [k for k in (costs_in or {}) if k not in AI_CREDIT_COSTS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"未登记的计费项：{unknown}（可用项：{sorted(AI_CREDIT_COSTS)}）")

    for op, v in (costs_in or {}).items():
        try:
            val = int(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{op} 的积分必须是整数，收到 {v!r}")
        if val < 0:
            raise HTTPException(status_code=400, detail=f"{op} 的积分不能为负")
        current[op] = val

    reset = payload.get("reset") or []
    if isinstance(reset, list):
        for op in reset:
            if op in AI_CREDIT_COSTS:
                # 🔴 用 `None` 标记删除，**不能**在 Python 侧先 pop 掉再提交：
                #   `save_plan_overrides` 的合并语义是「表内逐条合并、条目值为 null
                #   视为删除」。若提交时少了这个键，旧值会原样留在 plans.json ——
                #   表现为「点了恢复默认，价没变」（桌面端实测踩到）。
                current[op] = None

    trial_in = payload.get("free_trial")
    if trial_in is not None and not isinstance(trial_in, dict):
        raise HTTPException(status_code=400, detail="free_trial 必须是对象")
    if costs_in is None and not reset and not trial_in:
        raise HTTPException(status_code=400, detail="没有要保存的改动")

    # 一次落盘两张表（同一个 plans.json），避免中间态被别人读到
    patch: dict[str, Any] = {"credit_costs": current}
    if isinstance(trial_in, dict) and trial_in:
        patch["free_trial"] = _validate_free_trial(trial_in, AI_CREDIT_COSTS)
    save_plan_overrides(patch)
    return {"ok": True, "costs": credit_cost_table(),
            "free_trial": free_trial_policy()}


def _validate_free_trial(trial_in: dict[str, Any], known_ops: dict) -> dict[str, Any]:
    """校验并归一 `free_trial` 载荷：只留认识的键，非法值直接 400。

    🔴 只认识 `DEFAULT_FREE_TRIAL_POLICY` 里的键 —— 后台把 `per_op` 拼错成
    `per-op` 之类，写进 plans.json 会造成**整条策略静默失效**（读表时收敛回默认值），
    管理员看着「明明配了」却不起效，属于最难查的一类事故，所以宁可当场报错。

    ⚠️ 恢复默认必须传 None（不能用 Python 侧 pop）：合并语义见上方 `reset` 注释。
    """
    from fastapi import HTTPException as _HE
    from membership import DEFAULT_FREE_TRIAL_POLICY, _TRIAL_MODES
    out: dict[str, Any] = {}
    unknown = [k for k in trial_in if k not in DEFAULT_FREE_TRIAL_POLICY]
    if unknown:
        raise _HE(status_code=400,
                  detail=f"未知的试用策略字段：{unknown}（可用：{sorted(DEFAULT_FREE_TRIAL_POLICY)}）")
    for k, v in trial_in.items():
        if v is None:
            out[k] = None            # 显式删除 ⇒ 回退代码默认值
            continue
        if k in ("enabled", "members_too"):
            if not isinstance(v, bool):
                raise _HE(status_code=400, detail=f"{k} 必须是 true/false")
            out[k] = v
        elif k == "mode":
            mode = str(v).strip().lower()
            if mode not in _TRIAL_MODES:
                raise _HE(status_code=400,
                          detail=f"未知的试用口径：{mode}（可选：{list(_TRIAL_MODES)}）")
            out[k] = mode
        elif k == "exclude":
            if not isinstance(v, (list, tuple)):
                raise _HE(status_code=400, detail="exclude 必须是数组 [op, ...]")
            bad = [x for x in v if str(x) not in known_ops]
            if bad:
                raise _HE(status_code=400,
                          detail=f"exclude 里有未登记的计费项：{bad}（可用项：{sorted(known_ops)}）")
            out[k] = [str(x) for x in v]
        elif k == "max_cost":
            try:
                mc = int(v)
            except (TypeError, ValueError):
                raise _HE(status_code=400, detail=f"max_cost 必须是整数，收到 {v!r}")
            if mc < 0:
                raise _HE(status_code=400, detail="max_cost 不能为负")
            out[k] = mc
        else:  # pragma: no cover — 上面已经过一遍键名白名单
            out[k] = v
    return out
