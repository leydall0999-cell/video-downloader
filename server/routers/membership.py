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
    """当前会员状态：下载/AI 双轨、积分余额、今日配额用量。兼容匿名（返回免费态）。"""
    _maybe_check_license_revoked(_store(request))
    s = _store(request).status()
    s["anonymous"] = _require_user(request) is None
    return s


# 卡密作废校验：每进程最多一次（首次 status 时），fail-open（断网不锁会员）
_license_check_done = [False]


def _maybe_check_license_revoked(store) -> None:
    if _license_check_done[0]:
        return
    _license_check_done[0] = True
    try:
        store._ensure_loaded()
        meta = store._state.get("meta") or {}
        code = str(meta.get("license_code") or "").strip()
        if not code:
            return
        import device_id
        import license_client
        fp, _strong = device_id.fingerprint()
        r = license_client.check_remote(code, fp)
        if r.get("known") and r.get("status") == "revoked":
            store.set_license_revoked(True)
    except Exception:
        pass  # 网络异常/指纹不可用：保持现状（宽限语义）


@router.post("/api/member/activate")
def member_activate(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """激活/续费。payload: {"code": "download_year"|"ai_15000"|"credits_5000"}。需登录。

    🔒 P2 收口：套餐 code 直激活是**本机调试后门**，仅限回环请求（桌面 App 本机）。
    远程请求一律要求走 /api/member/redeem 卡密通道 —— 否则任何登录用户
    都能白嫖任意套餐（V1 时期的洞）。
    """
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    client_ip = (request.client.host if request.client else "") or ""
    if client_ip not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "请使用卡密激活（购买卡密后在本面板输入兑换）",
                "code": "USE_REDEEM"}
    code = str(payload.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "缺少 code"}
    return _store(request).activate(code, via="ui_test")


@router.post("/api/member/redeem")
def member_redeem(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """卡密激活（P2 一机一码主通道）。payload: {"license_code": "VDL-..."}。

    流程：卡密+设备指纹 → 云端授权中心裁决（验签/查重/绑定）→ 成功后本地
    按返回的 plan_code 激活会员并记录绑定。云端业务失败原样透传错误；
    网络不通返回可读提示（激活必须联网，已激活权益不受影响）。
    """
    import device_id
    import license_client

    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    code = str(payload.get("license_code") or "").strip()
    if not code:
        return {"ok": False, "error": "请输入卡密"}
    try:
        fp, strong = device_id.fingerprint()
    except Exception:
        return {"ok": False, "error": "无法取得设备标识，请重启 App 后重试"}
    if not strong:
        return {"ok": False, "error": "本机无法生成稳定设备标识，暂不支持卡密激活"}
    try:
        r = license_client.redeem_remote(code, fp)
    except license_client.LicenseCloudError as e:
        return {"ok": False, "error": f"{e}（激活需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "卡密无效",
                "code": r.get("code") or "REJECTED"}
    store = _store(request)
    res = store.activate(r["plan_code"], via="license",
                         device_fp=fp, license_code=code.strip())
    if not res.get("ok"):
        return res
    res["license_bound"] = True
    return res


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
    """消耗下载类配额。payload: {"resource": "download", "n": 1}。需登录。"""
    if not _require_user(request):
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    resource = str(payload.get("resource") or "").strip()
    if not resource:
        return {"ok": False, "error": "缺少 resource"}
    try:
        n = int(payload.get("n", 1))
    except (TypeError, ValueError):
        return {"ok": False, "error": "n 必须为整数"}
    return _store(request).use_daily(resource, n=n)
