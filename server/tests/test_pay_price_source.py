# -*- coding: utf-8 -*-
"""守卫：支付服务（deploy/pay_server.py）的「有效价 / 可购性」与 App 侧同源。

背景（2026-10-09）：用户报「前端展示价 ≠ 实收价」（3 天档展示 ¥2.99、实收 ¥4.90，
属多收）。根因是 pay_server 用硬编码 PRICE_MAP 收款。修复后付额与可购性都改为
向授权中心取，并**在 pay_server 内重算**秒杀/活动窗口（pay_server 是独立进程，
无法 import App 的 membership）。

本测试钉住这条「规则复刻」不漂移：对同一份 plan 覆盖，两侧必须给出
**相同的有效价、可购性、不可购原因、is_flash**。任一侧单改规则 → 本测试红。

另钉住：plan_quote 的云端不可达回落路径（source=local、视为可购）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
SERVER = HERE.parent
REPO = SERVER.parent

_tmp = tempfile.mkdtemp(prefix="vdl_payprice_")
os.environ["VDL_DATA_DIR"] = _tmp
os.environ["VDL_CLOUD_LINK"] = "0"
os.environ["VDL_PLANS_CLOUD"] = "0"
sys.path.insert(0, str(SERVER))

import membership as M                                       # noqa: E402

# pay_server 是独立可执行脚本（不进 App 包），用 spec 直接加载其模块。
_spec = importlib.util.spec_from_file_location(
    "vdl_pay_server_probe", str(REPO / "deploy" / "pay_server.py"))
PS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(PS)

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  ✅ " + name)
    else:
        print("  ❌ " + name + (("  | " + detail) if detail else ""))
        FAILS.append(name)


NOW = 1_700_000_000.0
CASES = [
    # (名称, plan 覆盖)
    ("常态", {"price_cny": 2.99}),
    ("秒杀窗口内", {"price_cny": 1.99, "mode": "flash_sale", "flash_price": 0.99,
                "flash_start": NOW - 100, "flash_end": NOW + 100}),
    ("秒杀窗口边界(起)", {"price_cny": 1.99, "mode": "flash_sale", "flash_price": 0.99,
                   "flash_start": NOW, "flash_end": NOW + 100}),
    ("秒杀未开始", {"price_cny": 1.99, "mode": "flash_sale", "flash_price": 0.99,
                "flash_start": NOW + 100, "flash_end": NOW + 200}),
    ("秒杀已结束", {"price_cny": 1.99, "mode": "flash_sale", "flash_price": 0.99,
                "flash_start": NOW - 200, "flash_end": NOW - 100}),
    ("秒杀价缺省=0", {"price_cny": 1.99, "mode": "flash_sale", "flash_price": 0,
                 "flash_start": NOW - 100, "flash_end": NOW + 100}),
    # in_flash 只看窗口+flash_price>0，不看 mode（两侧都如此）——这条正是易漂移点
    ("非秒杀模式但窗内", {"price_cny": 5.99, "mode": "normal", "flash_price": 3.99,
                    "flash_start": NOW - 10, "flash_end": NOW + 10}),
    ("下架", {"price_cny": 2.99, "on_sale": False}),
    ("活动未开始", {"price_cny": 2.99, "start_at": NOW + 60}),
    ("活动已结束", {"price_cny": 2.99, "end_at": NOW - 60}),
    ("售罄", {"price_cny": 2.99, "stock": 10, "sold": 10}),
    ("限量未售罄", {"price_cny": 2.99, "stock": 10, "sold": 9}),
]


def test_rules_agree() -> None:
    print("[1] pay_server._sales_state 与 membership.plan_sales_state 逐例一致")
    for name, plan in CASES:
        mine = PS._sales_state(dict(plan), NOW)
        ref = M.plan_sales_state(dict(plan), now=NOW)
        same = (abs(mine["price"] - ref["price"]) < 1e-6
                and mine["buyable"] == ref["buyable"]
                and mine["reason"] == ref["reason"]
                and mine["is_flash"] == ref["is_flash"])
        check(f"{name}：price={ref['price']:.2f} buyable={ref['buyable']}", same,
              f"pay_server={mine}")


def test_flash_price_used() -> None:
    print("[2] 秒杀窗口内实收 = flash_price（防多收），窗口外回 price_cny")
    hot = PS._sales_state(
        {"price_cny": 1.99, "mode": "flash_sale", "flash_price": 0.99,
         "flash_start": NOW - 10, "flash_end": NOW + 10}, NOW)
    cold = PS._sales_state(
        {"price_cny": 1.99, "mode": "flash_sale", "flash_price": 0.99,
         "flash_start": NOW - 200, "flash_end": NOW - 100}, NOW)
    check("窗口内 price=0.99", hot["price"] == 0.99 and hot["is_flash"] is True,
          str(hot))
    check("窗口外 price=1.99", cold["price"] == 1.99 and cold["is_flash"] is False,
          str(cold))


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._b = body

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_plan_quote_cloud_and_fallback() -> None:
    print("[3] plan_quote：云端可达取有效价；不可达回落本地 PRICE_MAP")
    payload = {"ok": True, "plans": {"download_plans": {
        "download_3day": {"price_cny": 2.99, "mode": "flash_sale",
                          "flash_price": 1.50,
                          "flash_start": "0", "flash_end": "0"},
        "download_7day": {"price_cny": 5.99, "on_sale": False},
    }}}

    def _fake(req, timeout=6):   # noqa: ARG001
        return _Resp(json.dumps(payload).encode("utf-8"))

    _real_urlopen = urllib.request.urlopen
    urllib.request.urlopen = _fake
    PS._PLAN_CACHE["at"] = 0.0
    try:
        q = PS.plan_price("download_3day")
        check("云端 3 天档取 price_cny=2.99", q == "2.99", q)
        off = PS.plan_quote("download_7day")
        check("云端 7 天档已下架 → buyable=False", off["buyable"] is False, str(off))
        check("下架原因带出", off["reason"] == "已下架", str(off))
        unknown = PS.plan_price("download_year")
        # 张 assertion 只钉「机制」：无云端覆盖 → 取本地 PRICE_MAP 的兜底值。
        # 不写死数字：内建默认价 = App membership.DOWNLOAD_PLANS（同为 179.00），
        # 云端覆盖（159.00）是管理员改价、优先级更高；改内建价属另一决策，不在此钉。
        check("无云端覆盖 → 回落本地 PRICE_MAP 兜底",
              unknown == PS.PRICE_MAP["download_year"]["price"], unknown)
        loc = PS.plan_quote("download_year")
        check("回落路径视为可购（source=local）",
              loc["source"] == "local" and loc["buyable"] is True, str(loc))

        # 云端不可达：应保留上次缓存（仍是云端结果），不要误回落拿错价
        def _boom(req, timeout=6):
            raise OSError("network down")
        urllib.request.urlopen = _boom
        PS._PLAN_CACHE["at"] = 0.0
        check("云端不可达 → 保留缓存不误改价", PS.plan_price("download_3day") == "2.99")
    finally:
        urllib.request.urlopen = _real_urlopen
        PS._PLAN_CACHE["at"] = 0.0
        PS._PLAN_CACHE["map"] = {}


def test_unknown_code_not_sellable() -> None:
    print("[4] 未知 code：本地兜底价 0.00（handler 另有 PRICE_MAP 白名单拦截）")
    check("未知 code → 0.00", PS.plan_price("no_such_plan") == "0.00")


def main() -> int:
    print("=" * 46)
    print("支付服务价格同源守卫（deploy/pay_server.py ↔ server/membership.py）")
    print("=" * 46)
    test_rules_agree()
    test_flash_price_used()
    test_plan_quote_cloud_and_fallback()
    test_unknown_code_not_sellable()
    print("=" * 46)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 支付服务价格同源守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
