"""离线测试：本机支付网关转发（`/api/pay/*`）—— 2026-10-09「点购买没反应」的回归守卫。

背景（真实故障，不是假想）：
  2aed1e4 把前端从「调本机 /api/cloud/pay/*」改成「直连公网 pay.hanyuxz.top」，
  但请求体里**没有云端令牌** —— 支付服务 `parse_token("")` 必然 401 BAD_TOKEN，
  前端只弹一句「登录态失效」，用户感知就是「点购买没反应」。
  云端令牌只存在于后端凭据库（明文 → 已迁移到系统 Keychain），前端拿不到、也不该拿，
  所以正确链路是「前端 → 本机后端（凭 Authorization 认出用户）→ 后端带令牌转发 VPS」。

本文件守三件事：
  A. 网关路由存在，且**真的把云端令牌**交给转发函数（不是空串、不是本地会话令牌）；
  B. 未登录云端账号 / 缺套餐 / 上游异常都有**明确可读 error**（不再静默无反应）；
  C. 前端 `PAY_API_BASE` 默认**不得**指向公网（防再次改回直连而丢掉令牌）。

不连真实网络：mock 掉 `license_client.pay_create_remote` / `pay_query_remote`。
"""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

REPO = os.path.dirname(SERVER)

CLOUD_TOKEN = "MTU2MzgzMTk5NTZ8MTc5MTQ4MTY5OHwxNzkxNDgzNDk4fGFiY2RlZjEyMzQ1Njc4OTBhYmNkZWY12"


def _client():
    """挂载**真实** payment router 的 TestClient（只 mock 登录态与出网）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import payment
    app = FastAPI()
    app.include_router(payment.router)
    return TestClient(app), payment


def _fake_create(token, plan_code, client="", **kw):
    _fake_create.seen = {"token": token, "plan_code": plan_code,
                         "client": client, "base_url": kw.get("base_url")}
    return {"ok": True, "order_id": "VDLP20261009014818e847", "mode": "xunhupay",
            "qr_png": "data:image/png;base64,AAAA", "qr": "weixin://wxpay/bizpayurl?pr=TEST",
            "pay_url": "https://api.xunhupay.com/payments/wechat/index?id=1",
            "amount": "1.90", "plan_code": plan_code}


def _fake_query(order_id, **kw):
    _fake_query.seen = {"order_id": order_id, "base_url": kw.get("base_url")}
    return {"ok": True, "order_id": order_id, "status": "PENDING"}


def test_create_requires_plan_code():
    c, _pay = _client()
    r = c.post("/api/pay/create", json={}).json()
    assert r.get("ok") is False and r.get("code") == "NO_PLAN", r


def test_create_requires_cloud_token():
    """未登录云端账号时必须**明确报错**，而不是一路静默。"""
    c, pay = _client()
    with mock.patch.object(pay, "_cloud_token_of", return_value=""):
        r = c.post("/api/pay/create", json={"plan_code": "download_1day"}).json()
    assert r.get("ok") is False and r.get("code") == "NO_CLOUD_TOKEN", r
    assert r.get("error"), "必须给出可读错误文案（前端会直接展示）"


def test_create_forwards_cloud_token_and_passthrough():
    """核心回归：云端令牌必须真的传给转发函数，返回值原样透传（含原生码）。"""
    c, pay = _client()
    import license_client
    with mock.patch.object(pay, "_cloud_token_of", return_value=CLOUD_TOKEN), \
         mock.patch.object(license_client, "pay_create_remote", side_effect=_fake_create):
        r = c.post("/api/pay/create",
                   json={"plan_code": "download_1day", "client": "desktop"}).json()
    assert r.get("ok") is True, r
    assert str(r.get("qr") or "").startswith("weixin://"), r
    assert r.get("qr_png"), "二维码要给到前端"
    assert r.get("pay_url"), "收银台兜底地址要给到前端"
    seen = _fake_create.seen
    assert seen["token"] == CLOUD_TOKEN, "必须交出云端令牌，不能为空、不能用本地会话令牌"
    assert seen["plan_code"] == "download_1day"
    assert seen["client"] == "desktop"


def test_create_wraps_upstream_error():
    c, pay = _client()
    import license_client
    with mock.patch.object(pay, "_cloud_token_of", return_value=CLOUD_TOKEN), \
         mock.patch.object(license_client, "pay_create_remote",
                           side_effect=RuntimeError("boom")):
        r = c.post("/api/pay/create", json={"plan_code": "download_1day"}).json()
    assert r.get("ok") is False and r.get("code") == "PAY_GATEWAY_ERROR", r
    assert "boom" in str(r.get("error") or ""), r


def test_create_rejects_non_dict_response():
    c, pay = _client()
    import license_client
    with mock.patch.object(pay, "_cloud_token_of", return_value=CLOUD_TOKEN), \
         mock.patch.object(license_client, "pay_create_remote", return_value=None):
        r = c.post("/api/pay/create", json={"plan_code": "download_1day"}).json()
    assert r.get("ok") is False and r.get("code") == "PAY_GATEWAY_BAD_RESP", r


def test_query_requires_order_id():
    c, _pay = _client()
    r = c.post("/api/pay/query", json={}).json()
    assert r.get("ok") is False and r.get("code") == "NO_ORDER", r


def test_query_forwards_order_id():
    c, _pay = _client()
    import license_client
    with mock.patch.object(license_client, "pay_query_remote", side_effect=_fake_query):
        r = c.post("/api/pay/query", json={"order_id": "VDLP1"}).json()
    assert r.get("ok") is True and r.get("status") == "PENDING", r
    assert _fake_query.seen["order_id"] == "VDLP1"


def test_frontend_default_gateway_is_local_not_public():
    """源码口径守卫：默认入口必须是本机后端，绝不能退回「直连公网」。

    直连公网 = 请求里没有云端令牌 = 必然 401（就是本次故障）。允许经
    window.VDL_PAY_API_BASE 显式覆盖，但**默认值**必须是空（同源本机）。
    """
    appjs = open(os.path.join(REPO, "web", "app.js"), encoding="utf-8").read()
    assert "window.VDL_PAY_API_BASE || ''" in appjs, "PAY_API_BASE 默认值被改成非本机入口"
    assert "|| 'https://pay.hanyuxz.top'" not in appjs, \
        "又退回直连公网了 —— 请求不带云端令牌，必然 401（见本文件头部说明）"
    assert "'/api/pay/create'" in appjs and "'/api/pay/query'" in appjs, \
        "前端必须调用本机后端的同名网关路由"
    # 失败必须可见：订单提示条在弹窗底部，用户常看不到，需要同时弹 toast
    assert "showToast('❌ ' + _msg)" in appjs, "下单失败必须同时弹 toast，否则又是「点了没反应」"


def test_frontend_pay_layer_above_member_dialog():
    """源码口径守卫：支付层必须能盖住会员中心（top-layer 遮挡，2026-10-09 第二层根因）。

    `<dialog showModal()>` 会进入浏览器 **top layer**，渲染在一切 z-index 之上；而支付层
    只是 appendChild 到 body 的普通 div（z-index:9999、铺满视口也无效）⇒ 若下单后不先
    关掉 dialog，二维码被整块压在下面，用户「什么都看不到」。
    实测：elementFromPoint(视口中心) 命中的仍是会员弹窗里的 `.member-buy` 按钮。
    """
    appjs = open(os.path.join(REPO, "web", "app.js"), encoding="utf-8").read()
    i_pay = appjs.index("async function payCreate")
    i_open = appjs.index("function openPayModal", i_pay)
    body = appjs[i_pay:i_open]
    assert "el.memberModal.close()" in body, \
        "payCreate 必须先关掉会员中心 dialog，否则支付层被 top layer 压住（用户看不到二维码）"
    assert body.index("el.memberModal.close()") < body.index("openPayModal(r)"), \
        "关 dialog 必须在 openPayModal 之前；顺序反了同样看不到二维码"
    # 关闭支付层后应把会员中心还回来（点「关闭」不该被弹回主界面）
    assert "function closePayModal(restoreMemberCenter)" in appjs, \
        "closePayModal 需要 restoreMemberCenter 开关（openPayModal 内部清理必须传假值，否则死循环式遮挡）"
    assert "closePayModal(true)" in appjs, "用户主动关闭 / 支付成功时应还原会员中心"
    assert "setTimeout(closePayModal, 1200)" not in appjs, \
        "setTimeout 直接传 closePayModal 会把定时器参数当开关；须写 () => closePayModal(true)"


if __name__ == "__main__":
    test_create_requires_plan_code()
    test_create_requires_cloud_token()
    test_create_forwards_cloud_token_and_passthrough()
    test_create_wraps_upstream_error()
    test_create_rejects_non_dict_response()
    test_query_requires_order_id()
    test_query_forwards_order_id()
    test_frontend_default_gateway_is_local_not_public()
    test_frontend_pay_layer_above_member_dialog()
    print("ALL PAY GATEWAY TESTS PASSED")
