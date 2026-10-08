# -*- coding: utf-8 -*-
"""守卫：网页版支付「真通道转发」（/api/cloud/pay/create|query）。

背景（2026-10-09 用户截图）：线上网页版（hanyuxz.top）点「立即开通」只弹
「订单已创建 · 支付通道尚未开通（模拟通道，未产生扣款）」+ 裂图 —— web 后端的
/api/cloud/pay/* 一直走 payment_core 的 mock 通道（没有 qr_png），从未接到真实支付服务。

本守卫钉住修复后的契约：
  1) 真通道 = 后端**转发**（带该账号云端令牌）到 VPS 支付服务（虎皮椒 / 支付宝）；
     令牌只存在于后端，前端拿不到、也不该拿到（前端直连会 401 BAD_TOKEN）。
  2) 未登录 / 无云端令牌时**必须明确拒绝**，而不是造一个付不了的 mock 订单。
  3) 轮询到 PAID 时**必须**拉一次权威快照把权益落到本机（否则要等下次心跳才看到开通）。
  4) 离线 / 开发（VDL_CLOUD_LINK=0）仍回落 mock，不碰真钱。
  5) 前端按 qr 原始内容切换通道文案（虎皮椒是微信码，写死「支付宝」会与码对不上）。

全程离线：license_client 的 pay_* 替换为内存假实现（真模块属性替换，与调用点一致）。
运行：cd server && python tests/test_pay_gateway_web.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

# ⚠️ 必须在 import 任何 server 模块之前把 HOME / 数据目录指到临时目录，
#    否则会员库、订单库会落到真机上。
_TMP = tempfile.mkdtemp(prefix="vdl-payweb-")
os.environ["HOME"] = _TMP
os.environ["VDL_DATA_DIR"] = _TMP
os.environ["VDL_CLOUD_LINK"] = "0"      # 离线；真通道用例自行打开 _remote_enabled
os.environ["VDL_PLANS_CLOUD"] = "0"     # 价格真源关掉，避免联网

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))

import license_client            # noqa: E402
import routers.payment as pay    # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  \u2705 " if cond else "  \u274c ") + name)
    if not cond:
        FAILS.append(name)


class FakeReq:
    """最小 Request 替身：只需 headers.get('user-agent')。"""

    def __init__(self, ua: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"):
        self.headers = {"user-agent": ua}


class FakeStore:
    """最小会员 store 替身：cloud_session() 交回该账号云端令牌。"""

    def __init__(self, token: str = "", email: str = "probe@vdl.test"):
        self._sess = {"email": email, "token": token, "fp": "fp-test", "name": "测试机"}

    def cloud_session(self):
        return dict(self._sess)


class Harness:
    """安装转发链路的内存替身，并记录调用。"""

    def __init__(self):
        self.create_calls: list[dict] = []
        self.query_calls: list[dict] = []
        self.refresh_calls: list[int] = []
        self.create_result = {
            "ok": True, "order_id": "VDLP20261009TEST", "mode": "xunhupay",
            "qr_png": "data:image/png;base64,AAAA", "qr": "weixin://wxpay/bizpayurl?pr=abc",
            "pay_url": "", "amount": "0.10", "plan_code": "download_1day",
        }
        self.query_result = {"ok": True, "status": "PENDING", "order_id": "VDLP20261009TEST"}
        self.create_exc: Exception | None = None
        self.query_exc: Exception | None = None

    def install(self, *, remote: bool = True, token: str = "tok-cloud-1") -> "Harness":
        def _create(t, plan, client="", base_url="", **kw):
            self.create_calls.append(
                {"token": t, "plan_code": plan, "client": client, "base_url": base_url})
            if self.create_exc:
                raise self.create_exc
            return dict(self.create_result)

        def _query(oid, base_url="", **kw):
            self.query_calls.append({"order_id": oid, "base_url": base_url})
            if self.query_exc:
                raise self.query_exc
            return dict(self.query_result)

        license_client.pay_create_remote = _create
        license_client.pay_query_remote = _query
        pay._remote_enabled = lambda: remote
        pay._require_user = lambda request: "u_test"
        pay._store = lambda request: FakeStore(token=token)
        pay._refresh_authority = lambda request: self.refresh_calls.append(1)
        os.environ["VDL_PAY_BASE"] = "http://127.0.0.1:8903"
        return self


# ── 下单 ──────────────────────────────────────────────────────────────────── #
def test_create_requires_plan_code() -> None:
    print("\n[A] 下单入参校验")
    h = Harness().install()
    r = pay.pay_create({"plan_code": ""}, FakeReq())
    check("缺套餐 → NO_PLAN，且不打支付服务", r.get("code") == "NO_PLAN" and not h.create_calls)


def test_create_requires_login() -> None:
    h = Harness().install()
    pay._require_user = lambda request: None
    r = pay.pay_create({"plan_code": "download_1day"}, FakeReq())
    check("未登录 → NO_AUTH，且不打支付服务", r.get("code") == "NO_AUTH" and not h.create_calls)


def test_create_requires_cloud_token() -> None:
    h = Harness().install(token="")            # 已登录，但该账号没有云端令牌
    r = pay.pay_create({"plan_code": "download_1day"}, FakeReq())
    check("已登录但无云端令牌 → NO_CLOUD_TOKEN（明确拒绝，不造付不了的 mock 单）",
          r.get("ok") is False and r.get("code") == "NO_CLOUD_TOKEN" and not h.create_calls)


def test_create_forwards_token_plan_client() -> None:
    print("\n[B] 下单转发（令牌 / 套餐 / 客户端 / 基址 / 透传）")
    h = Harness().install(token="tok-cloud-1")
    r = pay.pay_create({"plan_code": "download_1day"},
                       FakeReq("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile"))
    c = h.create_calls[0] if h.create_calls else {}
    check("下单转发到 VPS 支付服务（1 次）", len(h.create_calls) == 1)
    check("转发携带**云端令牌**（不是前端会话令牌）", c.get("token") == "tok-cloud-1")
    check("转发携带 plan_code", c.get("plan_code") == "download_1day")
    check("移动端 UA → client=mobile", c.get("client") == "mobile")
    check("转发基址取 VDL_PAY_BASE（同机 127.0.0.1:8903）",
          c.get("base_url") == "http://127.0.0.1:8903")
    check("响应原样透传（含 qr_png / qr）",
          bool(r.get("ok")) and str(r.get("qr_png")).startswith("data:image/png")
          and str(r.get("qr")).startswith("weixin://"))
    check("补 plan_name 供前端展示（不是裸 code）",
          bool(r.get("plan_name")) and r.get("plan_name") != "download_1day")


def test_create_desktop_ua() -> None:
    h = Harness().install()
    pay.pay_create({"plan_code": "download_3day"}, FakeReq("Mozilla/5.0 (Windows NT 10.0)"))
    c = h.create_calls[0] if h.create_calls else {}
    check("桌面 UA → client=desktop", c.get("client") == "desktop")


def test_create_wraps_gateway_error() -> None:
    h = Harness().install()
    h.create_exc = RuntimeError("connection refused")
    r = pay.pay_create({"plan_code": "download_1day"}, FakeReq())
    check("支付服务异常 → PAY_GATEWAY_ERROR（不把异常抛给用户）",
          r.get("ok") is False and r.get("code") == "PAY_GATEWAY_ERROR")


# ── 轮询 ──────────────────────────────────────────────────────────────────── #
def test_query_requires_order_id() -> None:
    print("\n[C] 轮询入参校验与权益落地")
    h = Harness().install()
    r = pay.pay_query({"order_id": ""}, FakeReq())
    check("缺订单号 → NO_ORDER，且不打支付服务", r.get("code") == "NO_ORDER" and not h.query_calls)


def test_query_paid_refreshes_authority() -> None:
    h = Harness().install()
    h.query_result = {"ok": True, "status": "PAID", "order_id": "VDLP20261009TEST"}
    r = pay.pay_query({"order_id": "VDLP20261009TEST"}, FakeReq())
    check("PAID → 拉一次权威快照落地权益（否则要等下次心跳）", len(h.refresh_calls) == 1)
    check("PAID 状态透传前端", bool(r.get("ok")) and r.get("status") == "PAID")


def test_query_pending_does_not_refresh() -> None:
    h = Harness().install()
    h.query_result = {"ok": True, "status": "PENDING", "order_id": "VDLP20261009TEST"}
    pay.pay_query({"order_id": "VDLP20261009TEST"}, FakeReq())
    check("PENDING → 不拉权益快照（前端 2.5s 一次轮询，别把云端打爆）", not h.refresh_calls)


def test_query_gateway_error_is_wrapped() -> None:
    h = Harness().install()
    h.query_exc = RuntimeError("timeout")
    r = pay.pay_query({"order_id": "VDLP20261009TEST"}, FakeReq())
    check("轮询期支付服务异常 → PAY_GATEWAY_ERROR（前端静默重试）",
          r.get("ok") is False and r.get("code") == "PAY_GATEWAY_ERROR")


# ── 离线回落 ──────────────────────────────────────────────────────────────── #
def test_offline_falls_back_to_mock() -> None:
    print("\n[D] 离线 / 开发回落 mock（不碰真钱）")

    class FakeService:
        def create(self, uid, code):
            return {"ok": True, "order_id": "MOCK-1", "mode": "mock",
                    "amount": 1.99, "plan_code": code}

    h = Harness().install(remote=False)
    pay._service = lambda: FakeService()
    r = pay.pay_create({"plan_code": "download_1day"}, FakeReq())
    check("VDL_CLOUD_LINK 关 → 走本地 mock，且不碰真通道",
          r.get("mode") == "mock" and not h.create_calls)


# ── 前端通道文案 ──────────────────────────────────────────────────────────── #
def test_frontend_channel_label() -> None:
    print("\n[E] 前端通道文案（微信码不能写「支付宝」）")
    appjs = (REPO / "web" / "app.js").read_text(encoding="utf-8")
    html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    check("pfBuy 按 qr 原始内容判定通道（weixin://）", "indexOf('weixin://')" in appjs)
    check("按通道切扫码提示文案", "el.payTip.textContent" in appjs)
    check("按通道切二维码 alt", "el.payQr.alt" in appjs)
    check("HTML 不再写死「支付宝支付二维码」", "支付宝支付二维码" not in html)
    check("HTML 不再写死「请使用支付宝扫码付款」", "请使用支付宝扫码付款" not in html)
    check("无码时的诚实降级仍保留（不返回未接通道的假象）", "支付通道尚未开通" in appjs)


def main() -> int:
    test_create_requires_plan_code()
    test_create_requires_login()
    test_create_requires_cloud_token()
    test_create_forwards_token_plan_client()
    test_create_desktop_ua()
    test_create_wraps_gateway_error()
    test_query_requires_order_id()
    test_query_paid_refreshes_authority()
    test_query_pending_does_not_refresh()
    test_query_gateway_error_is_wrapped()
    test_offline_falls_back_to_mock()
    test_frontend_channel_label()
    print("\n" + "=" * 46)
    if FAILS:
        print("\u274c 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("\u2705 网页版支付转发守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
