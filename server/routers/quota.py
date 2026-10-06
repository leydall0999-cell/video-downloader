"""server/routers/quota.py — 免费用户配额闸门 HTTP 层。

对应设计文档 docs/llm-cloud-fallback-design.md §12：
- 终身云端事件 3 次（不可恢复）
- 每日 auto 运行 1 次（自然日重置）
- 单条上传视频 ≤ 30 分钟（免费用户；会员不限）
会员解除全部限制。

会员判定复用 app.current_member_store（与 /api/member 共用状态文件）。
配额状态持久化在 ~/.video-downloader/quota.json（与 membership 同目录），
由 pipeline 侧 quota.py 与本文共享同一 JSON schema。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Request

router = APIRouter()


def _is_member(request: Optional[Request] = None) -> bool:
    """当前用户是否为会员（下载会员或 AI 会员任一活跃即视为会员）。"""
    try:
        import app as _app
        store = _app.current_member_store(request) if request is not None else _app.member_store
        st = store.status()
        return bool(st.get("download_member", {}).get("active")) or \
               bool(st.get("ai_member", {}).get("active"))
    except Exception:
        return False


def _cloud_token(request: Optional[Request] = None) -> str:
    """取当前用户的云端账号 token（供 QuotaManager 做跨端原子记账）。

    🔴 2026-10-06：免费云端额度改为账号级中心记账后，这里是「本机身份 → 云端
    身份」的桥。拿不到 token（未登录云端 / 本机专属账号）时返回空串，
    QuotaManager 会 fail-open 沿用本机计数 —— 与旧口径一致，不误伤。

    必须用 `current_member_store(request)` 而不是全局 `app.member_store`：
    已登录本地账号时前者返回 per-user store（带该用户云端 token），
    后者是云端账号写入的全局 store（可能是别人的身份）。参见
    `membership.MembershipStore._cloud_token` 的同源回落逻辑。
    """
    try:
        import app as _app
        store = _app.current_member_store(request) if request is not None else _app.member_store
        return str(store._cloud_token() or "")
    except Exception:
        return ""


def get_quota_manager(request: Optional[Request] = None):
    """构造注入会员判定 + 云端 token 的 QuotaManager。"""
    from quota import QuotaManager
    return QuotaManager(is_member_fn=lambda: _is_member(request),
                        token_fn=lambda: _cloud_token(request))


# ── 本机引擎就绪探测（预检要用）──────────────────────────────────────── #
_LOCAL_READY_CACHE: dict = {"ts": 0.0, "val": False}
_LOCAL_READY_TTL = 60.0   # 秒；探测含 subprocess，缓存避免预检/面板高频重复调用


def _local_engine_ready(engine: str = "") -> bool:
    """本机 AI 引擎（MLX）当前是否可用：运行时找得到 + 至少一个可用权重目录。

    engine='cloud'（用户明确选纯云端）时本机能力不参与判定，直接返回 False
    —— 调用方据此把这条任务判为「需要云端」。探测只做 find_spec 与目录扫描、
    绝不加载模型，结果缓存 60 秒。
    """
    global _LOCAL_READY_CACHE
    eng = (engine or "").strip().lower()
    if eng == "cloud":
        return False
    now = time.time()
    if now - float(_LOCAL_READY_CACHE.get("ts") or 0) < _LOCAL_READY_TTL:
        return bool(_LOCAL_READY_CACHE.get("val"))
    ok = False
    try:
        import llm_config
        cfg = llm_config.get_llm_config()
        if not eng:
            eng = str(cfg.get("engine") or "auto").strip().lower()
        if eng != "cloud":
            rt = llm_config.local_runtime_status(cfg.get("mlx_python") or "")
            models = llm_config.list_local_models()
            ok = bool(rt.get("available")) and bool(models)
    except Exception:
        ok = False
    _LOCAL_READY_CACHE = {"ts": now, "val": ok}
    return ok


def resolve_engine(request: Optional[Request] = None) -> str:
    """当前生效的解说引擎档位（auto / cloud）。读不到时按 auto 处理。"""
    try:
        import llm_config
        eng = str(llm_config.get_llm_config().get("engine") or "auto").strip().lower()
    except Exception:
        eng = "auto"
    # 历史配置里可能残留 mlx / ollama（旧版三档），统一归一到 auto（本机优先）
    return "cloud" if eng == "cloud" else "auto"


def precheck_commentary(
    request: Optional[Request] = None,
    duration_sec: float = 0.0,
    engine: str = "",
) -> dict[str, Any]:
    """解说任务前置预检（返回结构化结论，不抛异常；供「选完文件立刻提示」用）。"""
    qm = get_quota_manager(request)
    eng = (engine or "").strip().lower() or resolve_engine(request)
    res = qm.precheck_commentary(
        duration_sec, local_engine_ready=_local_engine_ready(eng), engine=eng
    )
    st = qm.status()
    res["engine"] = eng
    res["is_member"] = st["is_member"]
    res["free_max_duration_sec"] = st["free_max_duration_sec"]
    res["lifetime_cloud_remaining"] = st["lifetime_cloud_remaining"]
    return res


def precheck_or_raise(
    request: Optional[Request] = None,
    duration_sec: float = 0.0,
    engine: str = "",
    vision: bool = False,
) -> dict[str, Any]:
    """前置预检：不通过则抛 403，detail 为结构化对象（message/hint/category/subscribe）。

    统一改造点：**所有**会启动解说任务的入口都必须先过这里，而不是等任务跑完
    才因额度不足失败（那会白等十几分钟并产出废片）。

    🔴 2026-10-05：在此**追加 AI 积分扣费**（解说此前完全无积分门禁，是最贵的功能）。
    扣费点选这里而不是各个 endpoint，因为本函数已是全部解说入口的统一收口 ——
    新增入口只要照旧调 `precheck_or_raise` 就自动被计费覆盖，不会漏。

    扣哪一档取决于实际会跑哪个引擎（`llm_script.py:2364` 会在长片时把
    auto 自动切成 cloud，所以长片按云端价）：
      - 本机 MLX          → `commentary_local_mlx`
      - 云端网关 / 大模型  → `commentary_llm`
    `vision=True`（用户主动开「画面理解」）时**额外**扣 `commentary_vision`。
    不足时抛 **402 + MEMBER_QUOTA|**（与会员墙其余部分同契约）。
    """
    res = precheck_commentary(request, duration_sec, engine=engine)
    if not res.get("allowed"):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail={
                "message": res.get("reason") or "当前无法开始这次解说",
                "hint": res.get("hint") or "",
                "category": "quota",
                "code": res.get("code") or "denied",
                "subscribe": True,
            },
        )
    charge_commentary_credits(request, res.get("engine") or engine, duration_sec)
    # 🔴 2026-10-06 补漏：`commentary_vision`（解说画面理解，50 积分）此前
    # **完全没有扣费点** —— 用户勾了「画面理解」会真的去调多模态模型
    # （单次真实成本 ¥0.24，全表最贵）却不扣任何积分，等于白嫖。
    # 扣在这里而不是管线里：① 本函数已是全部解说入口的统一收口，一处覆盖
    # 4 个入口（本地拖拽 / 上传 / 批量 / 单任务），新增入口照旧调它就自动被
    # 覆盖；② 用户定档是「按次定价」，任务入口收一次即一份，与内部调用几次无关。
    if vision:
        _charge_optional(request, "commentary_vision", "解说画面理解")
    return res


def _charge_optional(request: Optional[Request], op: str, label: str) -> None:
    """按需扣一项积分；不足抛 402（与主线同样的契约，前端统一弹会员中心）。"""
    import membership as _mem
    try:
        store = app_module().current_member_store(request) if request is not None \
            else app_module().member_store
    except Exception:
        return
    msg = _mem.gate_message(store, op, reason=op)
    if msg:
        from fastapi import HTTPException
        raise HTTPException(status_code=402, detail={
            "message": msg, "hint": f"{label}会消耗 AI 积分",
            "category": "quota", "code": "MEMBER_QUOTA", "subscribe": True,
        })


def app_module():
    """延迟 import app（避免 routers 层在导入期就绑死 app 模块）。"""
    import app as _a
    return _a


def _commentary_credit_op(engine: str, duration_sec: float) -> str:
    """解说这次该按哪个 op 计费（长片会被自动转云端 → 按云端价）。"""
    eng = (engine or "").strip().lower()
    local_ready = _local_engine_ready(eng)
    # auto 且本机就绪 → 通常跑本机；但长片会被 llm_script.py:2364 切云端。
    # 这里按「本机就绪且片子不长」判本机，长片按云端（宁多收不少收）。
    if local_ready and eng != "cloud":
        try:
            from llm_script_limits import LOCAL_MAX_VIDEO_SEC  # type: ignore
            long_clip = float(duration_sec or 0) > float(LOCAL_MAX_VIDEO_SEC)
        except Exception:
            long_clip = float(duration_sec or 0) > 1800.0
        if not long_clip:
            return "commentary_local_mlx"
    return "commentary_llm"


def charge_commentary_credits(
    request: Optional[Request] = None,
    engine: str = "",
    duration_sec: float = 0.0,
) -> dict[str, Any]:
    """按实际引擎扣 AI 积分；不足抛 402。返回扣费结果供调用方忽略。"""
    import app as _app
    from membership import credit_cost, gate_message
    op = _commentary_credit_op(engine, duration_sec)
    store = _app.current_member_store(request) if request is not None else _app.member_store
    if int(credit_cost(op)) <= 0:
        return {"ok": True, "spent": 0, "free": True, "op": op}
    msg = gate_message(store, op, reason=f"commentary:{op}")
    if msg:
        from fastapi import HTTPException
        raise HTTPException(status_code=402,
                            detail=f"MEMBER_QUOTA|{msg}")
    return {"ok": True, "op": op}


def assert_upload_allowed(request: Optional[Request], duration_sec: float,
                          vision: bool = False) -> None:
    """上传前服务端校验（兼容旧调用）：等价于 precheck_or_raise。

    历史上这里只判「时长 > 30 分钟」，漏了「时长合法但必须走云端、而云端额度
    已耗尽」这一致命分支（用户实测的废片场景就是后者）。现统一收口到预检。

    `vision` 透传给预检：上传入口勾了「画面理解」同样要扣 `commentary_vision`
    （这两个入口不走 `precheck_or_raise`，不传会漏扣）。
    """
    precheck_or_raise(request, duration_sec, vision=vision)


@router.get("/api/quota/status")
def quota_status(request: Request) -> dict[str, Any]:
    """返回配额状态，供设置页展示「剩余免费云端 X/3、今日 auto Y/1」。"""
    return get_quota_manager(request).status()
