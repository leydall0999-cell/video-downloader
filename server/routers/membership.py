"""server/routers/membership.py — VDL 会员（/api/member/*，B2 per-user）。

三轨会员引擎的 HTTP 层：套餐列表 / 状态查询 / 激活 / 积分花费 / 日配额使用。
会员状态按 user_id 分文件存储（见 user_membership.current_member_store）：
  - 已登录 → 该用户的 memberships/{user_id}.json；
  - 未登录 → 全局匿名 store（免费档配额共享，用于门禁降级）。
V1 只提供状态与账本能力，不接支付；激活仍为测试期通道（via=ui_test）。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()


def _store(request: Request):
    """解析当前请求的会员 store（按 token 用户；匿名回退全局）。"""
    import app
    return app.current_member_store(request)


def _require_user(request: Request) -> Optional[str]:
    import app
    return app.get_current_user_id(request)


@router.get("/api/member/plans")
def member_plans() -> dict[str, Any]:
    """套餐与权益表（供购买中心展示）。"""
    import app
    return app.member_store.plans()


@router.get("/api/member/status")
def member_status(request: Request) -> dict[str, Any]:
    """当前会员状态：下载/AI 双轨、积分余额、今日配额用量。兼容匿名（返回免费态）。

    已登录时先用**云端权威快照**校准本机权益（节流 60s，见 cloud_link.refresh_authority）：
    web 版与 App 从此看到的是同一份会员/积分；被挤下设备或账号被封禁也会即时降级。
    """
    store = _store(request)
    uid = _require_user(request)
    cloud: dict[str, Any] = {}
    if uid:
        try:
            import cloud_link
            cloud = cloud_link.refresh_authority(store)
        except Exception:  # noqa: BLE001 — 云端异常绝不能拖垮状态查询
            cloud = {"ok": False, "reason": "error"}
    s = store.status()
    s["anonymous"] = uid is None
    if cloud:
        s["cloud"] = {"ok": bool(cloud.get("ok")),
                      "skipped": str(cloud.get("skipped") or ""),
                      "code": str(cloud.get("code") or ""),
                      "reason": str(cloud.get("reason") or "")}
    return s


@router.post("/api/member/activate")
def member_activate(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """套餐 code 直激活（**仅超级管理员补单用**）。payload: {"code": "download_year"}。

    🔒 2026-10-04 对齐桌面端 1.0.36（2026-09-25 已收口，网页端当时漏改）：
    此前本路由只判「是否登录」，于是**任意登录用户**都能把套餐 code（如
    `download_year` / `credits_5000`）当激活码提交，本地直接发放——一条 curl
    即可白嫖任意下载/AI 套餐或永久积分。虽然云端 `authority` 快照是覆盖式、
    下一次 `/api/member/status`（节流 15s）就会回滚，但那 15 秒窗口足够真实
    用掉额度，且云端 `redeem` 通道已于 2026-09-26 下线（`VDL_REDEEM_DISABLED=1`）
    ⇒ 用户既兑不了真卡密、又能白嫖，是纯负收益后门。
    现在与桌面端一致：非超管一律 `USE_REDEEM` 拒绝；普通用户走在线支付购买。
    """
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    from auth_store import user_is_admin
    if not user_is_admin(uid):
        return {"ok": False,
                "error": "套餐直激活仅限超级管理员；请通过会员中心购买",
                "code": "USE_REDEEM"}
    code = str(payload.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "缺少 code"}
    return _store(request).activate(code, via="admin_direct")


@router.post("/api/member/credits/spend")
def credits_spend(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """AI 积分花费。payload: {"amount": 100, "reason": "asr"}。需登录。"""
    if not _require_user(request):
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    try:
        amount = int(payload.get("amount", 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "amount 必须为整数"}
    reason = str(payload.get("reason") or "ai_usage")
    return _store(request).spend_credits(amount, reason=reason)


@router.get("/api/member/credits/balance")
def credits_balance(request: Request) -> dict[str, Any]:
    if not _require_user(request):
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    return _store(request).credits_balance()


@router.get("/api/member/quota/{resource}")
def quota_state(resource: str, request: Request) -> dict[str, Any]:
    if not _require_user(request):
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    return _store(request).quota_state(resource)


@router.post("/api/member/quota/use")
def quota_use(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """消耗下载类配额。payload: {"resource": "download", "n": 1}。需登录。

    2026-09-28 新增 `check_only: true`：只查不扣（返回 quota_state）。供海外（global）
    节点把 cn 签发的 Authorization 原样回派过来做**下载配额预检**（cn 是会员/配额权威，
    hk 本机验不了 token、也读不到 cn 的 store）——回派失败由调用方 fail-open。
    """
    resource = str(payload.get("resource") or "").strip()
    if not resource:
        return {"ok": False, "error": "缺少 resource"}
    try:
        n = int(payload.get("n", 1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "n 必须为整数"}
    if payload.get("check_only"):
        if not _require_user(request):
            return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
        return _store(request).quota_state(resource)
    if not _require_user(request):
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    return _store(request).use_daily(resource, n=n)
