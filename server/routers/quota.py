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


def get_quota_manager(request: Optional[Request] = None):
    """构造注入会员判定的 QuotaManager。"""
    from quota import QuotaManager
    return QuotaManager(is_member_fn=lambda: _is_member(request))


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
) -> dict[str, Any]:
    """前置预检：不通过则抛 403，detail 为结构化对象（message/hint/category/subscribe）。

    统一改造点：**所有**会启动解说任务的入口都必须先过这里，而不是等任务跑完
    才因额度不足失败（那会白等十几分钟并产出废片）。
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
    return res


def assert_upload_allowed(request: Optional[Request], duration_sec: float) -> None:
    """上传前服务端校验（兼容旧调用）：等价于 precheck_or_raise。

    历史上这里只判「时长 > 30 分钟」，漏了「时长合法但必须走云端、而云端额度
    已耗尽」这一致命分支（用户实测的废片场景就是后者）。现统一收口到预检。
    """
    precheck_or_raise(request, duration_sec)


@router.get("/api/quota/status")
def quota_status(request: Request) -> dict[str, Any]:
    """返回配额状态，供设置页展示「剩余免费云端 X/3、今日 auto Y/1」。"""
    return get_quota_manager(request).status()
