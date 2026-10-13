"""server/routers/payment.py — 支付 REST 层（/api/cloud/pay/*）。

🔴 真通道（2026-10-09 接线）：线上网页版本走**转发** —— 本层凭当前用户的云端令牌，
把下单 / 查询转发给同机 VPS 支付服务（`VDL_PAY_BASE`，同机部署指向 127.0.0.1:8903，
虎皮椒 / 支付宝）。付款成功后由支付服务异步回调授权中心发货；本层在轮询到 PAID 时
再拉一次权威快照，把权益**立即**落到本机（否则用户要等下一次心跳才看到开通）。

本地 `payment_core` 的 mock 通道**仅在离线 / 开发**（`VDL_CLOUD_LINK=0`）时启用，
不碰真钱；`/api/cloud/pay/simulate_paid` 亦只对 mock 通道有效。

⚠️ 原 cloud_account.py 里的 /api/cloud/pay/* 已迁移到本文件，避免重复路由。
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter()


def _order_dir() -> Any:
    import app
    base = getattr(app, "DATA_DIR", None) or app.Path(
        os.path.expanduser("~/.videodownloader"))
    return base / "pay_orders"


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


# ---- 旧 mock 通道（/api/cloud/pay/*）的双重开关（安全审计 2026-10-13，F-01）----- #
# 🔴 缺陷：原实现里这四个端点**既不要求登录也不检查 mock 开关**，
#   `MockProvider.simulate_paid` 对任意 order_id 都返回成功；且 `_grant_with` 用的是
#   **请求者**的 store、与订单归属无关 ⇒ 拿到他人 order_id 即可把权益发到自己账上
#   （会员 / 积分是计费资产）。
#
# 修复（与桌面端 app 仓 P0-3 同口径）：旧 mock 通道需**同时**满足两个条件才可用，
# 缺一即 404（不暴露该端点存在）：
#   ① 环境变量 VDL_PAY_MOCK=1（默认关）——生产不设即整条链路关闭；
#   ② 请求来自**本机直连**（环回且未携带任何代理头）——防误开开关的服务器被公网白嫖。
# 真正的线上收款走 /api/pay/*（转发 VPS 支付服务），与本开关无关。
def _mock_pay_allowed(request: Optional[Request]) -> bool:
    if (os.environ.get("VDL_PAY_MOCK") or "").strip() != "1":
        return False
    if request is None:
        return False
    try:
        import app
        return bool(app._is_local_direct(request))
    except Exception:  # noqa: BLE001 —— 判定失败一律当公网，宁严勿宽
        return False


def _require_mock_pay(request: Optional[Request]) -> None:
    if not _mock_pay_allowed(request):
        raise HTTPException(status_code=404, detail="Not Found")


def _grant_with(request: Request, plan_code: str, order_id: str) -> dict:
    """付款成功 → 发放权益。

    安全审计 2026-10-13（F-01）：改为按**订单记录里的 user_id** 发放
    （`get_user_store(owner)`），而不是回调 / 轮询**请求者**的 store ——
    否则「谁触发回调，权益就发给谁」，拿到他人 order_id 即可把权益发到自己账上；
    同时把 order_id 作为幂等键传给 activate，同一笔支付重复回调只发放一次
    （见 MembershipStore.activate 的 order_id 幂等）。

    失败仍 fail-open：订单状态已是 PAID，权益可后续补发。
    """
    try:
        order = _service().store.get(order_id) or {}
        owner_uid = str(order.get("user_id") or "").strip()
        if not owner_uid:
            return {"ok": False, "error": "订单缺少归属账号，拒绝发放",
                    "order_id": order_id, "code": "ORDER_NO_OWNER"}
        from user_membership import get_user_store
        store = get_user_store(owner_uid)
        return store.activate(plan_code, via="payment", order_id=order_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "order_id": order_id}


# ── 真通道转发（线上网页版）──────────────────────────────────────────────── #
# 为什么必须**由后端转发**而不能让前端直连支付服务：云端令牌只存在于后端
# （`store.cloud_session()["token"]`），前端拿不到、也不该拿到；前端直连会因
# 支付服务 `parse_token("")` 直接 401 BAD_TOKEN（这正是桌面端「点购买没反应」
# 的成因）。网页版与支付服务**同机部署** ⇒ 直连 127.0.0.1:8903，不经公网回环。

def _pay_base() -> str:
    """VPS 支付服务基址。`VDL_PAY_BASE` 可覆盖（同机部署指向 127.0.0.1:8903）。"""
    import license_client
    return (os.environ.get("VDL_PAY_BASE") or "").strip() or license_client.PAY_BASE


def _remote_enabled() -> bool:
    """真通道可用 ⇔ 云端联动开着；离线 / 开发时回落 mock，不碰真钱。"""
    try:
        import cloud_link
        return bool(cloud_link.link_enabled())
    except Exception:  # noqa: BLE001
        return True


def _cloud_token_of(request: Optional[Request]) -> str:
    """当前用户云端账号令牌；未登录云端账号 / 纯本机账号时返回空串。"""
    try:
        store = _store(request)
    except Exception:  # noqa: BLE001
        return ""
    try:
        return str((store.cloud_session() or {}).get("token") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _client_hint(request: Optional[Request]) -> str:
    """桌面 / 移动提示（虎皮椒渠道不影响结果；支付宝 page/wap 用它分流）。"""
    try:
        ua = str(request.headers.get("user-agent") or "").lower()
    except Exception:  # noqa: BLE001
        return "desktop"
    mobile = ("iphone", "ipad", "android", "mobile", "micromessenger")
    return "mobile" if any(k in ua for k in mobile) else "desktop"


def _plan_name(plan_code: str) -> str:
    """套餐显示名（与前端价目同源）；取不到回落 code，绝不报错。"""
    try:
        from membership import effective_pay_plans
        return str((effective_pay_plans().get(plan_code) or {}).get("name") or plan_code)
    except Exception:  # noqa: BLE001
        return plan_code


def _refresh_authority(request: Optional[Request]) -> None:
    """付款成功后拉一次云端权威快照，把权益落到本机（best-effort，失败不挡轮询）。"""
    try:
        import cloud_link
        cloud_link.refresh_authority(_store(request), force=True)
    except Exception:  # noqa: BLE001
        pass


@router.post("/api/cloud/pay/create")
def pay_create(payload: dict[str, Any] = Body(...), request: Request = None) -> dict:
    """购买下单：真通道转发 VPS 支付服务，返回二维码（qr_png）供前端展示。

    金额以**授权中心**为单一真源（支付服务侧解析常态价 / 秒杀价），前端不参与定价。
    离线 / 开发（VDL_CLOUD_LINK=0）回落本地 mock 通道。
    """
    plan_code = str(payload.get("plan_code") or "").strip()
    if not plan_code:
        return {"ok": False, "error": "缺少套餐", "code": "NO_PLAN"}
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}

    if _remote_enabled():
        token = _cloud_token_of(request)
        if not token:
            return {"ok": False, "code": "NO_CLOUD_TOKEN",
                    "error": "请先登录云端账号后再购买（当前为本机 / 离线账号）"}
        try:
            import license_client
            r = license_client.pay_create_remote(
                token, plan_code, client=_client_hint(request), base_url=_pay_base())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "code": "PAY_GATEWAY_ERROR",
                    "error": f"支付服务暂时不可用，请稍后重试（{e}）"}
        if not isinstance(r, dict):
            return {"ok": False, "error": "支付服务返回异常", "code": "PAY_GATEWAY_BAD_RESP"}
        if r.get("ok"):
            r.setdefault("plan_name", _plan_name(plan_code))
        return r

    # —— 离线 / 开发：本地 mock 通道（不碰真钱；受双重开关保护，见 F-01）——
    _require_mock_pay(request)
    try:
        return _service().create(uid, plan_code)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "code": "CREATE_FAILED"}


@router.post("/api/cloud/pay/query")
def pay_query(payload: dict[str, Any] = Body(...), request: Request = None) -> dict:
    """订单状态轮询。真通道查 VPS；轮询到 PAID 时拉一次权威快照落地权益。"""
    order_id = str(payload.get("order_id") or "").strip()
    if not order_id:
        return {"ok": False, "error": "缺少订单号", "code": "NO_ORDER"}

    if _remote_enabled():
        try:
            import license_client
            r = license_client.pay_query_remote(order_id, base_url=_pay_base())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "code": "PAY_GATEWAY_ERROR",
                    "error": f"支付服务暂时不可用（{e}）"}
        if not isinstance(r, dict):
            return {"ok": False, "error": "支付服务返回异常", "code": "PAY_GATEWAY_BAD_RESP"}
        if r.get("ok") and str(r.get("status") or "").upper() == "PAID":
            _refresh_authority(request)
        return r

    # —— 离线 / 开发：本地 mock 通道（受双重开关保护，见 F-01）——
    _require_mock_pay(request)
    try:
        return _service().query(
            order_id, grant_fn=lambda pc, oid: _grant_with(request, pc, oid))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.post("/api/cloud/pay/notify")
def pay_notify(payload: dict[str, Any] = Body(...), request: Request = None) -> dict:
    """支付渠道异步回调（支付宝 / 微信 notify）。校验后翻转订单并发行权益。

    安全审计 2026-10-13（F-01）：该端点直接发放权益，必须受 mock 双重开关保护
    （默认全关）；权益按订单归属发放 + order_id 幂等（见 `_grant_with`）。
    """
    _require_mock_pay(request)
    try:
        return _service().notify(
            payload, grant_fn=lambda pc, oid: _grant_with(request, pc, oid))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@router.post("/api/cloud/pay/simulate_paid")
def pay_simulate(payload: dict[str, Any] = Body(...), request: Request = None) -> dict:
    """开发用：把 mock 订单置为已付并发行权益（真实通道不可用）。

    安全审计 2026-10-13（F-01）：这是「白拿会员」的直接入口，必须受双重开关保护
    （默认全关）。
    """
    _require_mock_pay(request)
    order_id = str(payload.get("order_id") or "")
    if not order_id:
        return {"ok": False, "error": "缺少订单号"}
    try:
        return _service().simulate_paid(
            order_id, grant_fn=lambda pc, oid: _grant_with(request, pc, oid))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
