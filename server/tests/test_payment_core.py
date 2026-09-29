"""payment_core 纯逻辑单测：不 import app，可无守卫直接跑。

覆盖：套餐价表 / 创建订单(状态 PENDING+二维码) / 查询未付仍 PENDING /
模拟付款→查询变 PAID 且权益发放回调被调用(plan_code 正确) / 未知套餐报错 /
真实通道占位尚未实现时优雅报错。
"""
import os
import sys
import tempfile

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import payment_core as pc


def _make(plan_code="download_year"):
    tmp = tempfile.mkdtemp(prefix="vdl_pay_")
    svc = pc.PaymentService(pc.OrderStore(tmp), pc.MockProvider())
    calls = []

    def grant(plan, oid):
        calls.append((plan, oid))
        return {"ok": True, "plan": plan}

    return svc, grant, calls, tmp


def test_plans_price_table():
    assert pc.PAY_PLANS["download_year"]["price"] == 179.00
    assert pc.PAY_PLANS["ai_15000"]["price"] == 99.90
    assert pc.PAY_PLANS["credits_15000"]["grant"] == "credits_15000"


def test_create_returns_pending_order_with_qr():
    svc, grant, calls, tmp = _make()
    order = svc.create("user_1", "download_year")
    assert order["ok"] is True
    assert order["status"] == "PENDING"
    assert order["amount"] == 179.00
    assert order["qr_content"].startswith("mock://pay/")
    assert order["order_id"].startswith("VDL")


def test_query_before_paid_stays_pending():
    svc, grant, calls, tmp = _make()
    o = svc.create("user_1", "download_month")
    q = svc.query(o["order_id"], grant_fn=grant)
    assert q["status"] == "PENDING"
    assert q["granted"] is False
    assert calls == []  # 未付款不发放


def test_simulate_paid_flips_to_paid_and_grants():
    svc, grant, calls, tmp = _make("download_year")
    o = svc.create("user_1", "download_year")
    res = svc.simulate_paid(o["order_id"], grant_fn=grant)
    assert res["status"] == "PAID"
    assert res["granted"] is True
    # 权益发放回调收到正确的 plan_code + 同一订单号
    assert calls == [("download_year", o["order_id"])]


def test_unknown_plan_rejected():
    svc, grant, calls, tmp = _make()
    try:
        svc.create("user_1", "not_a_plan")
        assert False, "应抛 OrderError"
    except pc.OrderError:
        pass


def test_real_channel_not_implemented_yet():
    svc = pc.PaymentService(pc.OrderStore(tempfile.mkdtemp(prefix="vdl_pay_")),
                            pc.get_provider("alipay", {}))
    try:
        svc.create("user_1", "download_year")
        assert False, "支付宝通道未接入应报错"
    except NotImplementedError:
        pass


def test_order_persisted_and_queryable():
    svc, grant, calls, tmp = _make("ai_5500")
    o = svc.create("user_2", "ai_5500")
    # 新 service 实例（同存储目录）应能查到同一订单
    svc2 = pc.PaymentService(pc.OrderStore(tmp), pc.MockProvider())
    found = svc2.store.get(o["order_id"])
    assert found is not None
    assert found["plan_code"] == "ai_5500"
    assert found["user_id"] == "user_2"


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"✅ {fn.__name__}")
    print("ALL PAYMENT CORE TESTS PASSED")
