"""server/routers/webstats.py — 网页版**访客统计**端点（/api/webstats/*）。

  POST /api/webstats/track    前端埋点上报（page_view / resolve_ok / resolve_fail /
                              download_done / member_click / desktop_click / promo_click …）
  GET  /api/webstats/summary  最近 N 天汇总（PV/UV/事件/来源/设备），仅超管可见

设计要点：
  - IP 只用于派生 UV 哈希，**绝不落库明文**（web_stats.record 内部单向哈希）；
    CF 走 CF-Connecting-IP，nginx 反代取 X-Forwarded-For 最左。
  - track 是公开提交端点 → 做秒级节流（同 IP 每 10s 至多 20 次），防刷爆存储；
    属于「提交类」端点，限流只影响统计完整性，不影响任何业务功能。
  - 全程 try/except 静默：埋点失败绝不能影响主流程或返回 5xx。
  - summary 走 admin.require_admin（Bearer token + is_admin），与后台面板同一套鉴权。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter, Body, Query, Request

import web_stats
from routers.admin import require_admin

router = APIRouter()

_KIND_MAXLEN = 32
_REF_MAXLEN = 512

# —— 节流（防刷）：ip -> 窗口内上报时间戳列表 ——
_bucket_lock = threading.Lock()
_buckets: dict = {}
_WINDOW = 10.0        # 统计窗口（秒）
_MAX_IN_WINDOW = 20   # 窗口内同 IP 最多接受次数
_MAX_BUCKETS = 5000   # 内存上限，超出整体清空


def _throttle_ok(ip: str, now: float) -> bool:
    key = ip or "-"
    with _bucket_lock:
        if len(_buckets) > _MAX_BUCKETS:
            _buckets.clear()
        arr = _buckets.setdefault(key, [])
        arr[:] = [t for t in arr if now - t < _WINDOW]
        if len(arr) >= _MAX_IN_WINDOW:
            return False
        arr.append(now)
        return True


def _client_ip(request: Request = None) -> str:
    """真实客户端 IP：CF → nginx 反代 → 直连。仅用于派生哈希，不落库。"""
    if request is None:
        return ""
    h = request.headers
    v = (h.get("cf-connecting-ip") or "").strip()
    if v:
        return v[:64]
    v = (h.get("x-forwarded-for") or "").strip()
    if v:
        return v.split(",")[0].strip()[:64]
    v = (h.get("x-real-ip") or "").strip()
    if v:
        return v[:64]
    return ((request.client.host if request.client else "") or "")[:64]


@router.post("/api/webstats/track")
def track(payload: dict = Body(default=None), request: Request = None) -> dict:
    """前端埋点上报。任何异常一律吞掉并返回 ok:false，绝不抛 5xx。"""
    try:
        body = payload if isinstance(payload, dict) else {}
        kind = str(body.get("kind") or "")[:_KIND_MAXLEN]
        ref = str(body.get("ref") or "")[:_REF_MAXLEN]
        ip = _client_ip(request)
        if not _throttle_ok(ip, time.time()):
            return {"ok": False, "dropped": "throttled"}
        own = ""
        if request is not None:
            own = (request.headers.get("host") or "").split(":")[0].strip().lower()
        ua = (request.headers.get("user-agent") or "") if request is not None else ""
        ok = web_stats.record(kind, ip=ip, ua=ua, ref=ref, own_host=own)
        return {"ok": bool(ok)}
    except Exception:  # noqa: BLE001
        return {"ok": False, "dropped": "error"}


@router.get("/api/webstats/summary")
def summary(days: int = Query(7, ge=1, le=90), request: Request = None) -> Any:
    """最近 N 天网页访客汇总。仅超级用户可见。"""
    require_admin(request)
    return web_stats.summary(days)
