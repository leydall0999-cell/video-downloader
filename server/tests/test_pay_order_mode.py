#!/usr/bin/env python3
"""支付下单产品分流守卫（deploy/pay_server.py::_place_order）。

为什么必须有这个测试
--------------------
本应用（appid 2021007104663693）**没有当面付资质**，可用的是「电脑网站支付
(page.pay) / 手机网站支付(wap.pay)」。而这两个接口是**纯本地签名** —— 本地
永远能拿到 URL，「签名成功 ≠ 可支付」；真实可用性只有用户打开收银台才暴露
（2026-10-08 实测：打开收银台报 insufficient-isv-permissions）。

因此下单分流必须满足：
  * auto：先试当面付（出二维码，UX 最好）；遇权限不足自动回退网页支付；
  * 桌面回退 page、移动回退 wap（client/UA 决定）；
  * 全部候选失败 → 抛错，**绝不静默返回空二维码**（否则前端弹一个扫不出来的码，
    用户点半天没反应却看不到任何原因）。

背景：`deploy/` 此前无任何测试覆盖，导致 3 个致命 bug（漏 import secrets、
notify_url 属性名错、GRANT_URL 走公网回环）全部溜到线上。本用例与
test_static_undefined_names.py 一起，构成 deploy/ 的第一道回归网。

不联网、不依赖 alipay SDK：用假 AliPay 对象断言**调用序列**与返回形状。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PAY_SRV = REPO / "deploy" / "pay_server.py"

_GW = "https://openapi.alipay.com/gateway.do"


def _load_module():
    """独立加载 pay_server，数据目录指向临时目录（避免污染 ~/.vdl-license）。"""
    os.environ.setdefault("VDL_LICENSE_DATA", tempfile.mkdtemp(prefix="vdl_paytest_"))
    spec = importlib.util.spec_from_file_location("vdl_pay_server", str(PAY_SRV))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._RETURN_URL = "https://pay.hanyuxz.top/api/pay/return"
    return mod


class FakeAliPay:
    """记录调用序列的假客户端；face2face_ok 决定当面付是否可用。"""

    def __init__(self, face2face_ok=False, page_ok=True, wap_ok=True):
        self._gateway = _GW
        self.face2face_ok, self.page_ok, self.wap_ok = face2face_ok, page_ok, wap_ok
        self.calls: list[str] = []
        self.return_urls: list = []

    def api_alipay_trade_precreate(self, subject, out_trade_no, total_amount):
        self.calls.append("precreate")
        if self.face2face_ok:
            return {"code": "10000", "msg": "Success", "qr_code": "https://qr.alipay.com/abc"}
        return {"code": "40006", "sub_code": "isv.insufficient-isv-permissions",
                "sub_msg": "接口调用权限不足"}

    def api_alipay_trade_page_pay(self, subject, out_trade_no, total_amount, return_url=None):
        self.calls.append("page")
        self.return_urls.append(return_url)
        if not self.page_ok:
            raise RuntimeError("page denied")
        return "method=alipay.trade.page.pay&biz_content=xx"

    def api_alipay_trade_wap_pay(self, subject, out_trade_no, total_amount, return_url=None):
        self.calls.append("wap")
        self.return_urls.append(return_url)
        if not self.wap_ok:
            raise RuntimeError("wap denied")
        return "method=alipay.trade.wap.pay&biz_content=xx"


def main() -> int:
    if not PAY_SRV.exists():
        print("❌ 找不到 %s" % PAY_SRV)
        return 1
    mod = _load_module()
    fails: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        print(("  ✓ " if cond else "  ✗ ") + name + (("  [%s]" % extra) if extra else ""))
        if not cond:
            fails.append(name)

    def place(mode: str, client: str, fake: FakeAliPay):
        mod.PAY_MODE = mode
        try:
            return mod._place_order(fake, "测试套餐", "VDLP1", "29.80", client), None
        except Exception as exc:  # noqa: BLE001
            return None, exc

    print("▶ _place_order 产品分流（当面付 / 电脑网站支付 / 手机网站支付）")

    # 1) auto + 桌面 + 无当面付资质 → 回退 page（这是本应用的真实工况）
    f = FakeAliPay(face2face_ok=False)
    res, _ = place("auto", "desktop", f)
    check("无当面付资质 → 回退电脑网站支付", bool(res) and res["mode"] == "page", str(res))
    check("pay_url 指向支付宝网关", bool(res) and res["pay_url"].startswith(_GW + "?"))
    check("调用序列 = precreate→page", f.calls == ["precreate", "page"], str(f.calls))
    check("return_url 已透传（付完回跳落地页）",
          f.return_urls and f.return_urls[0] == mod._RETURN_URL, str(f.return_urls))

    # 2) auto + 移动 → 回退 wap
    f = FakeAliPay(face2face_ok=False)
    res, _ = place("auto", "mobile", f)
    check("移动端 → 回退手机网站支付", bool(res) and res["mode"] == "wap", str(res))
    check("调用序列 = precreate→wap", f.calls == ["precreate", "wap"], str(f.calls))

    # 3) auto + 当面付可用 → 直接用 face2face 出二维码（不打多余请求）
    f = FakeAliPay(face2face_ok=True)
    res, _ = place("auto", "desktop", f)
    check("当面付可用 → 出 qr_code 且不用 pay_url",
          bool(res) and res["mode"] == "face2face" and res["qr_code"] and not res["pay_url"],
          str(res))
    check("只调 precreate（不浪费一次网页支付签名）", f.calls == ["precreate"], str(f.calls))

    # 4) 强制 PAY_MODE=page → 即便当面付可用也不走它
    f = FakeAliPay(face2face_ok=True)
    res, _ = place("page", "desktop", f)
    check("强制 page 不被 auto 覆盖", bool(res) and res["mode"] == "page" and f.calls == ["page"],
          str(res))

    # 5) 全部不可用 → 抛错（含候选摘要），绝不返回空二维码
    f = FakeAliPay(face2face_ok=False, page_ok=False)
    res, exc = place("auto", "desktop", f)
    check("全部产品失败 → 抛 RuntimeError", isinstance(exc, RuntimeError),
          type(exc).__name__ if exc else "无异常")
    check("错误信息含各候选原因", exc is not None and "insufficient" in str(exc),
          str(exc)[:80])
    check("失败时不返回结果（不会静默弹空码）", res is None)

    print()
    if fails:
        print("❌ 失败 %d 项：%s" % (len(fails), "；".join(fails)))
        return 1
    print("✅ 支付产品分流全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
