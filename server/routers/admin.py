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

import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()

# 静态扫描用：server/ 根目录与解说管线 scripts/ 目录（不在则跳过该根）。
# 🔴 绝不在源码里硬编码某台机器的绝对路径：开发机跑得通、部署到 ECS 就找不到
# （构建产物里也没有 WorkBuddy/问问题 这层目录）。优先读环境变量
# VDL_PIPELINE_DIR（build_mac.sh 用的就是 COMMENTARY_PIPELINE_DIR），其次找
# 同级 ../commentary-pipeline/scripts（源码仓布局）。
_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
_PIPELINE_DIR = ""
for _c in (os.environ.get("VDL_PIPELINE_DIR", "").strip(),
           os.environ.get("COMMENTARY_PIPELINE_DIR", "").strip()):
    if _c and Path(_c).is_dir():
        _PIPELINE_DIR = str(Path(_c).resolve())
        break
if not _PIPELINE_DIR:
    _guess = Path(_SERVER_DIR).parent / "commentary-pipeline" / "scripts"
    if _guess.is_dir():
        _PIPELINE_DIR = str(_guess)

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


# 凭据来源的中文说法（面板展示用；2026-10-05 探测式改造引入）。
# env=环境变量下发 / managed=管理员受管配置 / user=用户自己填的（我们已不再要求）
_SOURCE_CN = {"env": "环境变量", "managed": "管理员下发", "user": "本机文件"}


# 🔴 2026-10-06：后台「免费额度」分栏要用它标出「配了价但根本没扣费」的功能。
# 起因是盘点时发现 `commentary_vision`（单次真实成本 ¥0.24，全表最贵）、
# `voice_clone`、`matting_cloud_enhance` 三项在业务代码里只有后台能配价、
# 没有任何地方真正调 gate —— 也就是**现在完全免费、随便用**，属财务漏洞。
# 前端据此打「⚠ 未接入」角标，避免管理员以为配了价就等于在收钱。
# 判定不靠人工维护清单（会过期），而是每次请求时静态扫一遍真实调用点。
# 真正出现「op 作为计费参数」的调用形态（_credit_gate / gate_message / spend_for）
# 🔴 判定「这个 op 真在扣费」的实现（2026-10-06 两次返工后定稿）：
#
# 前两次都栽在正则上 —— ① 只认 `_credit_gate(op)`，把 `_cb("op")` 回调和
# `return "op"` 误判成「没接入」；② 放宽成多行匹配后，`[^)]{0,200}?` 又在
# `gate_message(\n  store,\n  "op",\n)` 这种多行实参上失配，把明明在扣费的
# matting_vision / subtitle_asr / local_matting_ai 误判成「没接入」。
# **误报成财务漏洞比不报更坏**（会去改本来正确的代码），所以彻底不用正则。
#
# 现在只做一件事：在**业务源码**里找该 op 的字符串字面量出现位置，记为
# 「候选调用点」；是否真扣费由 `_COST_GATE_SITES` 里人工登记的文件名确认。
# 人工登记的那张表由守卫测试 test_ai_credit_costs.py 双向核对（新登记的 op
# 必须真有调用点，成本表里新增却没登记的会红），不会静默腐烂。
_COST_GATE_SITES: dict[str, tuple[str, ...]] = {
    # 有真实扣费拦截点的 op → 出现该字面量的业务文件
    "matting_cloud": ("routers/matting.py",),
    "matting_vision": ("routers/matting.py",),
    "local_matting_ai": ("routers/matting.py",),
    "commentary_llm": ("routers/quota.py",),
    "commentary_local_mlx": ("routers/quota.py",),
    "subtitle_asr": ("routers/subtitle.py",),
    "subtitle_translate": ("routers/subtitles.py",),
    "dewatermark_ai": ("routers/dewatermark.py",),
    # ⚠️ 下面三项**故意不登记**：全仓搜索连注释都搜不到 op 字面量，
    # 说明它们只有后台能配价、业务代码从不扣费 ⇒ 现在完全免费随便用。
    # 前端会据此打「⚠ 未接入扣费」角标。补上拦截点后从这里删掉并加进上面那张表。
    # "matting_cloud_enhance": (),
    # "commentary_vision": (),
    # "voice_clone": (),
}
def _has_cost_gate(op: str) -> bool:
    """该 op 是否真有扣费拦截点。

    查 `_COST_GATE_SITES` 登记表，并**顺带校验**登记的文件里确实出现了该 op 的
    字面量 —— 双重保险：登记表过期（功能挪了文件）会退化成 False，而不是继续
    报一个假的「在扣费」。
    """
    sites = _COST_GATE_SITES.get(op)
    if not sites:
        return False
    for rel in sites:
        p = os.path.join(_SERVER_DIR, rel)
        try:
            with open(p, encoding="utf-8", errors="ignore") as fh:
                if f'"{op}"' in fh.read() or f"'{op}'" in fh.read():
                    return True
        except OSError:
            continue
    return False


def _cloud_quota_limits() -> tuple[int, int]:
    """云端免费额度上限（终身 / 每日 auto），读 quota.py 的常量做单一真源。"""
    try:
        from quota import LIFETIME_CLOUD_EVENTS, DAILY_AUTO_RUNS
        return int(LIFETIME_CLOUD_EVENTS), int(DAILY_AUTO_RUNS)
    except Exception:
        return 3, 1


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


# ── AI 积分成本配置（2026-10-05 新增）────────────────────────────────────────
# 用户定档：「所有 Key 由后台配好」+「每个功能每次消耗多少积分要能在后台配」。
# 这两个接口把 `membership.AI_CREDIT_COSTS` 的运行时生效价暴露出来并允许改价。
#
# 优先级：这里写入的 `credit_costs`（plans.json 覆盖层）**高于**代码默认表。
# GET 只返回成本数字与展示字段，**不含任何凭据**。
@router.get("/api/admin/ai/credit-costs")
def admin_ai_credit_costs(request: Request = None) -> dict[str, Any]:
    """超级管理员：读取 AI 积分成本表（生效价 + 代码默认价 + 真实调用点）。"""
    require_admin(request)
    from membership import (AI_CREDIT_COSTS, FEATURE_USAGE_DEFS,
                            credit_cost_table, free_trial_policy)
    pol = free_trial_policy()
    try:
        from membership import load_plan_overrides
        ov = (load_plan_overrides().get("free_quota") or {})
    except Exception:
        ov = {}
    return {
        "ok": True,
        "costs": credit_cost_table(),
        "registered": len(AI_CREDIT_COSTS),
        # 🔴 2026-10-06 用户要求「所有功能都列出来（含纯免费的）」：这里是后台
        # 「系统配置 → 套餐与积分成本 → 免费额度」分栏的数据源。把两套东西
        # 一起返回，前端分两组渲染：
        #   · 纯免费功能（FEATURE_USAGE_DEFS）：不消耗积分，走**日配额**（免费 N 次/日）
        #   · 消耗积分的功能（AI_CREDIT_COSTS）：扣积分，可用**首次体验**免费名额
        # 两者口径不同，混在一起会让人以为「下载 10/日」和「首次体验 1 次」是一回事。
        "free_quota": {
            "daily_features": [dict(x) for x in FEATURE_USAGE_DEFS],
            "credit_features": [
                {
                    "op": op,
                    "name": cfg.get("name") or op,
                    "where": cfg.get("where") or "",
                    "real_cost": cfg.get("real_cost") or "",
                    "note": cfg.get("note") or "",
                    # 有没有真实的计费拦截点（无 = 现在完全免费随便用，属财务漏洞）
                    "has_gate": _has_cost_gate(op),
                }
                for op, cfg in AI_CREDIT_COSTS.items()
            ],
            "overrides": ov if isinstance(ov, dict) else {},
            "limits": {
                "cloud_lifetime": _cloud_quota_limits()[0],
                "cloud_daily_auto": _cloud_quota_limits()[1],
            },
        },
        # 免费用户「首次体验」策略（2026-10-05 晚用户定档：账号首次使用免费一次）。
        # 与 `credit_costs` 同处 plans.json 覆盖层，改完即时生效、不需要重新打包。
        "free_trial": pol,
        "trial_modes": ["per_op", "once", "off"],
        # 计费口径说明，前端要如实展示给管理员（这是产品定档，不是实现细节）
        "policy": {
            "scope": "云端算力 + 本机重算力都计费（2026-10-05 用户定档）",
            "granularity": "按「次」定价：一次操作内发生多次模型调用只扣一份，重试成本含在单价里",
            "local_note": "本机功能（字幕提取 / 去水印 / 本地抠图 / 本机大模型解说 / 声音克隆）同样计费，"
                          "因为它们真实占用用户的 CPU 与内存。",
            "trial": "只要是首次使用的账号，其第一次使用 AI 功能免费一次（2026-10-05 用户定档）；"
                     "已经在 AI 会员有效期内或有足够积分的用户不消耗名额（先花自己的积分）。",
        },
    }


@router.post("/api/admin/ai/credit-costs")
def admin_ai_set_credit_costs(
    request: Request = None,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """超级管理员：调整各功能的积分单价。

    payload: `{"costs": {"matting_cloud": 60, ...}, "reset": ["op", ...],
               "free_trial": {"enabled": true, "mode": "once"|"per_op"|"off", ...}}`
      · 只提交要改的项，其余保持不变（不覆盖别人的改动）。
      · 传 `0` = 该功能免费（显式免费，不再依赖「表外默认 0」）。
      · `reset` 列出要恢复代码默认价的 op。
      · **op 必须已在 `AI_CREDIT_COSTS` 中登记**，未知 op 直接报错 ——
        防止打错字导致配置无效（且避免把 typo 写进 plans.json 变成死配置）。

    `free_trial` 是免费用户「首次体验」策略（2026-10-05 晚定档：账号首次使用免费一次）。
    同样按字段合并，**只传要改的键**（比如只切 `enabled`）即可，其余保持原值。
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
                #   `membership.save_plan_overrides` 的合并语义是「表内逐条合并、
                #   条目值为 null 视为删除」（2026-09-24 起）。若提交时少了这个键，
                #   旧值会原样留在 plans.json —— 表现为「点了恢复默认，价没变」（实测踩到）。
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
        if k == "enabled" or k == "members_too":
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
    """返回 (base_url, token) 或 (None, None)。

    🔴 2026-10-05：原先这里**硬编码 `~/.video-downloader/gateway_managed.json`**
    并重复实现了一遍 url 归一化（`replace("direct://")` + `rstrip("/")`），绕过了
    `gateway_config` 的 `VDL_HOME` 覆盖与三级优先级 —— 于是：
      · 测试/隔离实例里 `VDL_HOME` 指临时目录，本函数却仍读家目录 ⇒ 恒判「无网关」；
      · `save_managed_gateway()` 明明写成功了，面板还是显示未配置。
    改为直接调 `gateway_config.get_gateway_config()`，单一真源。
    """
    try:
        from gateway_config import get_gateway_config, _strip_direct
        cfg = get_gateway_config()
    except Exception:
        return None, None
    url = _strip_direct(str(cfg.get("url") or ""))
    return (url or None), (cfg.get("token") or None)


def _mask(s: str) -> str:
    s = str(s or "")
    return "****" if len(s) <= 8 else f"{s[:4]}\u2026{s[-4:]}"


def _deepseek_account() -> dict:
    info = {
        "id": "deepseek",
        "name": "DeepSeek（解说大模型）",
        "provider": "deepseek",
        "model": "",
        # 🔴 2026-10-05 原为硬编码两条文案（"modules": ["视频解说 / 解说词生成", …]），
        # 在没配网关 / 余额耗尽时仍显示成可用。改为空，末尾按真实就绪态填充。
        "modules": [],
        "account": "",
        "status": "unknown",
        "balance": None,
        "balances": [],
        "currency": None,
        "is_available": None,
        "balance_source": "none",
        "modules": [],      # 探测式填充，见函数末尾
        "recharge_url": "https://platform.deepseek.com/top_up",
        "console_url": "https://platform.deepseek.com",
        "note": "",
    }
    base, token = _gateway_base()
    if not base:
        info["note"] = "未找到网关配置（gateway_managed.json）"
        info["status"] = "no_gateway"
        info["modules"] = [
            "✗ 未配置网关：视频解说 / 解说词生成不可用",
            "✗ 未配置网关：长片云端兜底 LLM 不可用",
        ]
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
    # 🔴 2026-10-05 模块清单改为**探测式**（原先是硬编码文案，见下方说明）。
    #   两条模块的真实落点都已核实：
    #     · 视频解说 / 解说词生成 → commentary-pipeline/scripts/llm_script.py:157
    #       `_call_llm()`，LLM_API_KEY 硬依赖（缺失即报错）。
    #     · 长片云端兜底 LLM     → llm_script.py:2364
    #       `_local_capability_exceeded(total_duration, len(transcript))`，
    #       视频 >30min（VDL_LOCAL_MAX_VIDEO_SEC）或转写稿 >2.6 万字时，把本次任务
    #       的 LLM_ENGINE 由 auto 切成 cloud（长稿上本机 3B 模型会退化成复读机）。
    #   硬编码文案会在「没配网关 / 余额耗尽」时仍显示成可用 —— 与本轮
    #   「不显示做不到的事」定档冲突，故按真实就绪态标注。
    _gw_ok = bool(token) and info.get("status") in ("ok", "unknown", "insufficient")
    _flag = "✓" if _gw_ok else "✗"
    _suffix = "" if _gw_ok else " —— 不可用"
    info["modules"] = [
        f"{_flag} 视频解说 / 解说词生成{_suffix}",
        f"{_flag} 长片云端兜底 LLM（视频 >30 分钟自动转云端）{_suffix}",
    ]
    return info


def _dashscope_account() -> dict:
    info = {
        "id": "dashscope",
        "name": "阿里百炼 DashScope（视觉 / VLM）",
        "provider": "dashscope",
        "model": "",
        "modules": [],      # 探测式填充，见下方 try 块
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
        from vision_config import get_vision_config, managed_status as _vmg
        cfg = get_vision_config()
        key = (cfg.get("api_key") or "").strip()
        info["model"] = cfg.get("model") or ""
        info["account"] = _mask(key) if key else "(未配置 Key)"
        info["status"] = "configured" if key else "not_configured"
        # 🔴 2026-10-05 探测式：区分「有云端 Key」与「走本机回退」两种能力档位。
        #   provider=auto 且无 Key 时并非不可用 —— 管线会回退本机离线 OCR
        #   （`vision_analysis.py` 文档 + process.py:547 的优雅降级分支），
        #   所以文案不能说成「不可用」，要说清「当前走哪一档」。
        mg = _vmg()
        has_key = bool(key) or bool(mg.get("configured"))
        source = mg.get("source", "user")
        if has_key:
            info["modules"] = [
                f"✓ 视觉理解 / 图片 OCR（Key 来源：{_SOURCE_CN.get(source, source)}）",
                "✓ 抠图 VLM 自动分类（AI 智能识别）",
            ]
        else:
            info["modules"] = [
                "○ 视觉理解 / 图片 OCR —— 未下发云端 Key，自动档回退本机离线识别",
                "○ 抠图 VLM 自动分类 —— 未下发云端 Key，自动档回退本机引擎",
            ]
            info["note"] = ("凭据由管理员在本面板下发；未下发时 Mac 上自动走本机离线 OCR"
                            "（免费、无需 Key），不会报错。")
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
            # 🔴 2026-10-05：原先只说「MediaKit 增强已就绪 / 未配…回退」，
            # 顶部状态标签却是「已启用」—— 用户会以为画质增强也能用。补明确说明。
            info["note"] = ("云端抠图已就绪。" + (
                "AI 画质增强 / 增强抠图已就绪。"
                if mk_ready else
                "⚠️ AI 画质增强 / 增强抠图**不可用**（缺 MediaKit Bearer Key），"
                "目前会回退 visual/本地处理 —— 右上角「已启用」仅代表云端抠图。"))
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


# ── AI 凭据下发（2026-10-05）──────────────────────────────────────────────────
# 背景：产品定档「所有 Key 都由后台配好，用户不需要自己填」。用户界面上的
# Key 输入框已全部隐藏，但**此前没有任何下发通道** —— 云端抠图的 AK/SK 只能
# 手工写进 `cloud_matting.json`：换机/重装/分发即丢，新用户机器上根本没有，
# 界面却显示「管理员已配置」。这是本组接口要补的最后一环。
#
# 🔴 安全约定（务必保持）：
#   1. 三个端点都只接受「已登录 + 超级用户」（require_admin）。
#   2. **GET 绝不返回明文 Key**，只返回 `*_masked`（前 6 + 末 4）与就绪布尔。
#   3. 写入走 *_managed.json（0600），用户文件改不动它。
@router.get("/api/admin/ai/managed")
def admin_ai_managed(request: Request = None) -> dict[str, Any]:
    """超级管理员：读取三家的受管凭据状态（**永不返回明文**）。"""
    require_admin(request)
    from cloud_matting_config import managed_status as mat_managed
    from vision_config import managed_status as vis_managed
    from gateway_config import gateway_status
    gw = gateway_status()
    return {
        "ok": True,
        "volcengine": mat_managed(),
        "dashscope": vis_managed(),
        "deepseek": {
            "configured": bool(gw.get("enabled") and gw.get("has_token")),
            "source": gw.get("source", ""),
            "url": gw.get("url", ""),
            "token_masked": gw.get("token_masked", ""),
        },
    }


@router.post("/api/admin/ai/managed/volcengine")
def admin_ai_set_volcengine(
    request: Request = None,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """超级管理员：下发云端抠图（火山）凭据到受管配置。

    空字符串的字段**保持原值不变**（便于前端只提交改了的那几项，不会把没填
    的 Key 抹掉）；显式传 `"__clear__"` 才清空。
    """
    require_admin(request)
    from cloud_matting_config import get_cloud_matting_config, save_managed_config
    cur = get_cloud_matting_config()

    def _pick(new: str, old: str) -> str:
        new = str(new or "").strip()
        if new == "__clear__":
            return ""
        return new or old

    save_managed_config({
        "access_key": _pick(payload.get("access_key"), cur.get("access_key", "")),
        "secret_key": _pick(payload.get("secret_key"), cur.get("secret_key", "")),
        "mediakit_api_key": _pick(payload.get("mediakit_api_key"), cur.get("mediakit_api_key", "")),
        "enhance_version": str(payload.get("enhance_version") or cur.get("enhance_version") or "professional"),
        "enabled": bool(payload.get("enabled", True)),
    })
    return {"ok": True, "status": admin_ai_managed(request)["volcengine"]}


@router.post("/api/admin/ai/managed/dashscope")
def admin_ai_set_dashscope(
    request: Request = None,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """超级管理员：下发视觉理解（DashScope / 任意 OpenAI 兼容多模态）凭据。"""
    require_admin(request)
    from vision_config import get_vision_config, save_managed_vision_config
    cur = get_vision_config()

    def _pick(new: str, old: str) -> str:
        new = str(new or "").strip()
        if new == "__clear__":
            return ""
        return new or old

    save_managed_vision_config({
        "provider": str(payload.get("provider") or cur.get("provider") or "auto"),
        "api_key": _pick(payload.get("api_key"), cur.get("api_key", "")),
        "base_url": _pick(payload.get("base_url"), cur.get("base_url", "")),
        "model": str(payload.get("model") or cur.get("model") or ""),
    })
    return {"ok": True, "status": admin_ai_managed(request)["dashscope"]}


@router.post("/api/admin/ai/managed/deepseek")
def admin_ai_set_deepseek(
    request: Request = None,
    payload: dict[str, Any] = Body(default={}),
) -> dict[str, Any]:
    """超级管理员：下发云端网关（解说大模型）配置。

    🔴 这里下发的是**网关地址 + 令牌**，不是 DeepSeek 官方 Key —— 真实 Key 只留在
    ECS 网关侧（`upstream.json`），本机永远不接触上游凭据。
    """
    require_admin(request)
    from gateway_config import get_gateway_config, save_managed_gateway
    cur = get_gateway_config()

    def _pick(new: str, old: str) -> str:
        new = str(new or "").strip()
        if new == "__clear__":
            return ""
        return new or old

    save_managed_gateway({
        "url": _pick(payload.get("url"), cur.get("url", "")),
        "token": _pick(payload.get("token"), cur.get("token", "")),
        "enabled": bool(payload.get("enabled", True)),
    })
    return {"ok": True, "status": admin_ai_managed(request)["deepseek"]}


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

# 前端界面分段 id → 真实表名。前端历史上传过分段 id（dl/ai/cp）导致
# 「未知套餐类别：dl」整条下架链路不可用，这里做兼容：新旧前端都能用。
_SALE_TABLE_ALIAS = {
    "dl": "download_plans", "download": "download_plans", "download_member": "download_plans",
    "ai": "ai_plans", "ai_member": "ai_plans",
    "cp": "credit_packs", "packs": "credit_packs", "credits": "credit_packs",
    "cost": "",  # AI 积分成本不是可售档位
}


def _norm_plan_table(value: str) -> str:
    v = (value or "").strip()
    if v in _SALE_TABLES:
        return v
    return _SALE_TABLE_ALIAS.get(v, "")


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

    # 归属表以「按 code 定位」为权威：客户端传错表名（曾出现把分段 id 'dl' 当表名，
    # 也有传入 'ai' 却操作下载档的情况）时若盲信客户端，会把 on_sale 写进错误的表 ——
    # 表现为「提示已下架/上架，前台却没变化」。客户端 table 只在定位不到时作兜底。
    table = _find_plan_table(code) or _norm_plan_table(str(payload.get("table") or ""))
    if table not in _SALE_TABLES:
        return {"ok": False, "error": f"未知套餐类别：{payload.get('table') or '（无法定位该档）'}"}
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
