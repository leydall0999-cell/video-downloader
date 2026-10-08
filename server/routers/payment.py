"""server/routers/payment.py — 支付 REST 层（/api/cloud/pay/*）。

委托 payment_core 完成通道无关的下单 / 查询 / 回调；付款成功后通过会员引擎
activate(plan_code, via="payment") 发放权益。真实通道（支付宝 / 微信）经环境变量
VDL_PAY_PROVIDER 切换，商户密钥由配置注入；开发期默认 mock 通道（不碰真钱）。

⚠️ 原 cloud_account.py 里的 /api/cloud/pay/* 已迁移到本文件，避免重复路由。
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()


def _order_dir() -> Any:
    """订单存放目录：统一走 auth_store._base_dir()（~/.video-downloader，支持
    VDL_DATA_DIR 隔离）。

    ⚠️ 不要回落 ~/.videodownloader（无短横线）：那是 cookie_pool / cloud_sync 的历史
    目录，且绕开 VDL_DATA_DIR —— 会让离线测试写脏真实家目录。app 上也没有 DATA_DIR
    属性，getattr 回落分支此前恒为死路。
    """
    from auth_store import _base_dir
    return _base_dir() / "pay_orders"


def _provider() -> Any:
    name = (os.environ.get("VDL_PAY_PROVIDER") or "mock").strip() or "mock"
    cfg: dict = {}
    if name in ("alipay", "wechat"):
        # TODO: 从 app.payment_config 读取对应商户密钥
        pass
    from payment_core import get_provider
    return get_provider(name, cfg)


def _service() -> Any:
    from payment_core import OrderStore, PaymentService
    # 下单金额走「生效套餐表」（超管后台改价即刻生效），而非 payment_core 的硬编码表。
    from membership import effective_pay_plans
    return PaymentService(OrderStore(_order_dir()), _provider(),
                          plans_fn=effective_pay_plans)


def _store(request: Request) -> Any:
    import app
    return app.current_member_store(request)


def _require_user(request: Request) -> Optional[str]:
    import app
    return app.get_current_user_id(request)


def _grant_with(request: Request, plan_code: str, order_id: str) -> dict:
    """付款成功 → 发放权益。失败 fail-open：订单状态已是 PAID，权益可后续补发。"""
    try:
        store = _store(request)
        return store.activate(plan_code, via="payment")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "order_id": order_id}


@router.post("/api/cloud/pay/create")
def pay_create(payload: dict[str, Any] = Body(...), request: Request = None) -> dict:
    """购买下单：返回二维码内容（qr_content），前端展示扫码。金额由 PAY_PLANS 决定。"""
    plan_code = str(payload.get("plan_code") or "")
    if not plan_code:
        return {"ok": False, "error": "缺少套餐", "code": "NO_PLAN"}
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    try:
        return _service().create(uid, plan_code)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "code": "CREATE_FAILED"}


@router.post("/api/cloud/pay/query")
def pay_query(payload: dict[str, Any] = Body(...), request: Request = None) -> dict:
    """订单状态轮询（前端轮询；付款成功自动发放权益）。"""
    order_id = str(payload.get("order_id") or "")
    if not order_id:
        return {"ok": False, "error": "缺少订单号"}
    try:
        return _service().query(
            order_id, grant_fn=lambda pc, oid: _grant_with(request, pc, oid))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.post("/api/cloud/pay/notify")
def pay_notify(payload: dict[str, Any] = Body(...), request: Request = None) -> dict:
    """支付渠道异步回调（支付宝 / 微信 notify）。校验后翻转订单并发行权益。"""
    try:
        return _service().notify(
            payload, grant_fn=lambda pc, oid: _grant_with(request, pc, oid))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.post("/api/cloud/pay/simulate_paid")
def pay_simulate(payload: dict[str, Any] = Body(...), request: Request = None) -> dict:
    """开发用：把 mock 订单置为已付并发行权益（真实通道不可用）。"""
    order_id = str(payload.get("order_id") or "")
    if not order_id:
        return {"ok": False, "error": "缺少订单号"}
    try:
        return _service().simulate_paid(
            order_id, grant_fn=lambda pc, oid: _grant_with(request, pc, oid))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ---- 真通道网关：/api/pay/*（前端默认入口）--------------------------------- #
# 🔴 2026-10-09 修复「点购买没反应」：
#   2aed1e4 把前端从「调本机 /api/cloud/pay/*」改成「直连公网 pay.hanyuxz.top」，
#   但**没有携带云端令牌** —— 支付服务 parse_token("") 直接 401 BAD_TOKEN，
#   前端只显示一句「登录态失效，请重新登录」，用户感知就是「点了没反应」。
#   令牌只存在于后端（明文 → 已迁移到系统 Keychain），前端拿不到、也不该拿到。
#   正解：前端 → 本机后端（凭 Authorization 认出用户）→ 后端取该账号云端令牌
#   → 转发 VPS 支付服务。对外路径与 VPS 保持一致（/api/pay/create|query），
#   前端只需把 PAY_API_BASE 置空即可复用，无需改第二处。
PAY_GATEWAY_BASE = os.environ.get("VDL_PAY_GATEWAY_BASE", "https://pay.hanyuxz.top")


def _cloud_token_of(request: Optional[Request]) -> str:
    """当前用户（或全局）云端账号令牌；未登录云端账号时返回空串。"""
    try:
        from routers.quota import _cloud_token as _quota_cloud_token
        return str(_quota_cloud_token(request) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


@router.post("/api/pay/create")
def pay_gateway_create(payload: dict[str, Any] = Body(...), request: Request = None) -> dict:
    """下单转发：带云端令牌调 VPS 支付服务（虎皮椒 / 支付宝）。"""
    plan_code = str(payload.get("plan_code") or "").strip()
    if not plan_code:
        return {"ok": False, "error": "缺少套餐", "code": "NO_PLAN"}
    token = _cloud_token_of(request)
    if not token:
        return {"ok": False, "error": "请先登录账号后再购买", "code": "NO_CLOUD_TOKEN"}
    client = str(payload.get("client") or "").strip().lower()
    if client not in ("desktop", "mobile"):
        client = ""
    try:
        import license_client
        r = license_client.pay_create_remote(token, plan_code, client=client,
                                             base_url=PAY_GATEWAY_BASE)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"支付服务不可用：{e}", "code": "PAY_GATEWAY_ERROR"}
    if not isinstance(r, dict):
        return {"ok": False, "error": "支付服务返回异常", "code": "PAY_GATEWAY_BAD_RESP"}
    return r


@router.post("/api/pay/query")
def pay_gateway_query(payload: dict[str, Any] = Body(...)) -> dict:
    """订单状态轮询转发（付款成功由 VPS 异步回调发货，前端只查状态）。"""
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        return {"ok": False, "error": "缺少订单号", "code": "NO_ORDER"}
    try:
        import license_client
        r = license_client.pay_query_remote(order_id, base_url=PAY_GATEWAY_BASE)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"支付服务不可用：{e}", "code": "PAY_GATEWAY_ERROR"}
    if not isinstance(r, dict):
        return {"ok": False, "error": "支付服务返回异常", "code": "PAY_GATEWAY_BAD_RESP"}
    return r
