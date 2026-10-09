"""server/payment_core.py — 支付方式无关的支付内核（纯逻辑，不依赖 app / 网络）。

职责：
- PaymentProvider 抽象：create_order / query_order / handle_notify。
- 订单本地存储（每个订单一个 JSON 文件），状态机 PENDING→PAID。
- MockProvider：返回虚拟二维码 + 开发用 simulate_paid（不碰真钱，闭环测试用）。
- AlipayProvider / WeChatProvider：真实通道占位（需商户密钥，由配置注入，TODO）。
- PaymentService：编排 创建订单 → 查询 → 付款成功后发放权益（grant 回调由上层注入）。

本模块刻意不 import app，便于在无沙盒守卫环境下直接单测。真实通道接入时，
只需在 AlipayProvider/WeChatProvider 内实现三个方法并注入商户密钥即可，其余不变。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

# V1 方案三轨套餐 → 价格（元）。原 pay_server.PRICE_MAP 的本地真源。
# ⚠️ 本表只是**兜底**：真实下单金额由注入的 plans_fn（membership.effective_pay_plans）
#    决定，即「超管后台改价 → 下单金额跟随」。本表用于无注入时的独立单测。
# grant 字段 = 会员引擎 activate() 接受的 code（与 plan_code 一致）。
PAY_PLANS: dict[str, dict] = {
    "download_1day":      {"price": 1.90,  "name": "VIP会员·1天",   "grant": "download_1day"},
    "download_3day":      {"price": 4.90,  "name": "VIP会员·3天",   "grant": "download_3day"},
    "download_7day":      {"price": 9.90,  "name": "VIP会员·7天",   "grant": "download_7day"},
    "download_month":     {"price": 29.80, "name": "VIP会员·月",    "grant": "download_month"},
    "download_half_year": {"price": 99.90, "name": "VIP会员·半年",  "grant": "download_half_year"},
    "download_year":      {"price": 179.00, "name": "VIP会员·年",    "grant": "download_year"},
    "ai_5500":            {"price": 49.90, "name": "AI会员·5500积分", "grant": "ai_5500"},
    "ai_15000":           {"price": 99.90, "name": "AI会员·15000积分","grant": "ai_15000"},
    "credits_5000":       {"price": 50.00, "name": "积分包·5000",     "grant": "credits_5000"},
    "credits_15000":      {"price": 99.00, "name": "积分包·15000",    "grant": "credits_15000"},
}

ORDER_PREFIX = "VDL"


class OrderError(Exception):
    pass


class PaymentProvider:
    name = "base"

    def create_order(self, plan_code: str, amount: float, order_id: str,
                     user_id: str) -> dict:
        raise NotImplementedError

    def query_order(self, order_id: str) -> dict:
        raise NotImplementedError

    def handle_notify(self, payload: dict) -> dict:
        raise NotImplementedError


class MockProvider(PaymentProvider):
    """开发/测试用：返回虚拟二维码；simulate_paid 把订单置为已付（不连真实支付）。"""

    name = "mock"

    def __init__(self) -> None:
        self._paid: set[str] = set()

    def create_order(self, plan_code, amount, order_id, user_id):
        return {
            "ok": True,
            "order_id": order_id,
            "qr_content": f"mock://pay/{order_id}",
            "amount": amount,
            "plan_code": plan_code,
            "status": "PENDING",
        }

    def query_order(self, order_id):
        return {
            "ok": True,
            "order_id": order_id,
            "status": "PAID" if order_id in self._paid else "PENDING",
        }

    def simulate_paid(self, order_id: str) -> None:
        self._paid.add(order_id)

    def handle_notify(self, payload: dict) -> dict:
        oid = payload.get("order_id") or payload.get("out_trade_no")
        if oid:
            self._paid.add(oid)
        return {"ok": True, "order_id": oid}


class AlipayProvider(PaymentProvider):
    """支付宝当面付（待接入）。需配置：商户 PID / 应用私钥 / 支付宝公钥 / 回调地址。"""

    name = "alipay"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg or {}

    def create_order(self, plan_code, amount, order_id, user_id):
        # TODO: 调 alipay.trade.precreate，返回 qr_code（放进 qr_content）
        raise NotImplementedError("支付宝通道待接入：需配置商户 PID / 应用私钥 / 支付宝公钥")

    def query_order(self, order_id):
        raise NotImplementedError("支付宝通道待接入")

    def handle_notify(self, payload):
        raise NotImplementedError("支付宝通道待接入")


class WeChatProvider(PaymentProvider):
    """微信支付 Native 扫码（待接入）。需配置：商户号 / APIv3 密钥 / 证书 / 回调地址。"""

    name = "wechat"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg or {}

    def create_order(self, plan_code, amount, order_id, user_id):
        # TODO: 调 /v3/pay/transactions/native，返回 code_url（放进 qr_content）
        raise NotImplementedError("微信支付通道待接入：需配置商户号 / APIv3 密钥 / 证书")

    def query_order(self, order_id):
        raise NotImplementedError("微信支付通道待接入")

    def handle_notify(self, payload):
        raise NotImplementedError("微信支付通道待接入")


def get_provider(name: str, cfg: Optional[dict] = None) -> PaymentProvider:
    if name == "mock":
        return MockProvider()
    if name == "alipay":
        return AlipayProvider(cfg or {})
    if name == "wechat":
        return WeChatProvider(cfg or {})
    raise OrderError(f"未知支付通道: {name}")


class OrderStore:
    """订单持久化：每单一个 JSON 文件，单文件增删（非 rmtree，避免批量删守卫）。"""

    def __init__(self, base_dir: Path) -> None:
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, oid: str) -> Path:
        return self.base / f"{oid}.json"

    def save(self, order: dict) -> None:
        self._path(order["order_id"]).write_text(
            json.dumps(order, ensure_ascii=False), encoding="utf-8")

    def get(self, oid: str) -> Optional[dict]:
        p = self._path(oid)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def list_by_user(self, user_id: str) -> list[dict]:
        out: list[dict] = []
        for f in self.base.glob("*.json"):
            try:
                o = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if o.get("user_id") == user_id:
                out.append(o)
        out.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return out

    def delete(self, oid: str) -> None:
        try:
            self._path(oid).unlink(missing_ok=True)
        except OSError:
            pass


class PaymentService:
    def __init__(self, store: OrderStore, provider: PaymentProvider,
                 plans: dict = PAY_PLANS,
                 plans_fn: Optional[Callable[[], dict]] = None) -> None:
        self.store = store
        self.provider = provider
        self._default_plans = plans
        self._plans_fn = plans_fn

    @property
    def plans(self) -> dict:
        """生效套餐表：优先 plans_fn（会员引擎的覆盖层真源），异常回落静态表。

        🔴 2026-09-30：下单金额此前只读硬编码 PAY_PLANS，超管在后台改的价格
        不影响真实扣款。改由上层注入 plans_fn=membership.effective_pay_plans，
        使「展示价 / 扣款价 / 发放天数」三者同源。
        """
        fn = self._plans_fn
        if fn is not None:
            try:
                p = fn()
                if isinstance(p, dict) and p:
                    return p
            except Exception:  # noqa: BLE001 —— 覆盖层损坏时绝不能拦死下单
                pass
        return self._default_plans

    def create(self, user_id: str, plan_code: str) -> dict:
        plans = self.plans            # 取一次，避免属性被重复求值
        if plan_code not in plans:
            raise OrderError(f"未知套餐: {plan_code}")
        plan = plans[plan_code]
        oid = f"{ORDER_PREFIX}{uuid.uuid4().hex[:16].upper()}"
        order = {
            "order_id": oid,
            "user_id": user_id,
            "plan_code": plan_code,
            "plan_name": plan["name"],
            "amount": plan["price"],
            "status": "PENDING",
            "created_at": time.time(),
            "granted": False,
            "granted_at": 0.0,
        }
        r = self.provider.create_order(plan_code, plan["price"], oid, user_id)
        if not r.get("ok"):
            raise OrderError(r.get("error") or "下单失败")
        order["qr_content"] = r.get("qr_content")
        order["pay_url"] = r.get("pay_url")
        self.store.save(order)
        return self._public(order)

    def query(self, order_id: str,
              grant_fn: Optional[Callable[[str, str], dict]] = None) -> dict:
        order = self.store.get(order_id)
        if not order:
            raise OrderError("订单不存在")
        if order["status"] != "PAID":
            r = self.provider.query_order(order_id)
            if r.get("status") == "PAID":
                order["status"] = "PAID"
                order["paid_at"] = time.time()
                self.store.save(order)
                if not order.get("granted") and grant_fn:
                    grant_fn(order["plan_code"], order_id)
                    order["granted"] = True
                    order["granted_at"] = time.time()
                    self.store.save(order)
        return self._public(order)

    def notify(self, payload: dict,
               grant_fn: Optional[Callable[[str, str], dict]] = None) -> dict:
        res = self.provider.handle_notify(payload)
        oid = res.get("order_id")
        if oid:
            try:
                self.query(oid, grant_fn=grant_fn)
            except OrderError:
                pass
        return res

    def simulate_paid(self, order_id: str,
                      grant_fn: Optional[Callable[[str, str], dict]] = None) -> dict:
        if not isinstance(self.provider, MockProvider):
            raise OrderError("仅 mock 通道支持模拟付款")
        self.provider.simulate_paid(order_id)
        return self.query(order_id, grant_fn=grant_fn)

    @staticmethod
    def _public(order: dict) -> dict:
        return {
            "ok": True,
            "order_id": order["order_id"],
            "plan_code": order["plan_code"],
            "plan_name": order.get("plan_name"),
            "amount": order["amount"],
            "status": order["status"],
            "qr_content": order.get("qr_content"),
            "pay_url": order.get("pay_url"),
            "granted": order.get("granted", False),
            "created_at": order.get("created_at"),
        }
