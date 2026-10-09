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

# 授权中心（deploy/license_server.py）同样加载：用于钉「活动档判定两侧同口径」。
# 该文件模块级只有常量赋值（服务在 __main__ 里才启动），加载无副作用。
_lspec = importlib.util.spec_from_file_location(
    "vdl_license_probe", str(REPO / "deploy" / "license_server.py"))
LIC = importlib.util.module_from_spec(_lspec)
_lspec.loader.exec_module(LIC)

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


def test_activity_scope_agrees() -> None:
    """🔴 2026-10-09 客诉「活动每人限购一份没有生效」。

    现场：download_1day 的 `price_cny = flash_price = 0.10`（管理员没把价格恢复），
    秒杀窗口 00:27–02:35。窗口过后仍按 ¥0.10 卖，却因 `is_flash=False` 被判
    「非活动」⇒ 活动档默认限购 1 失效 ⇒ 已购账号可无限次重复下单
    （实测 `/api/pay/create` 返回 200 出码；`plan_usage` 回 `limit=0, allowed=true`）。

    修法：把「限购用的活动档判定」与「价格用的 is_flash」分开，新增 `is_activity`：
    窗口内算活动；**窗口外若秒杀价未回升到常态价之上**（用户看到的仍是活动价）
    也算。本用例同时钉住**收款侧 pay_server 与授权中心硬闸 license_server 的口径
    必须一致** —— 否则会出现「下单被拦 / 发货放行」的分叉。
    """
    print("[5] 活动档判定：窗口外价格未回升仍算活动（收款侧 ↔ 授权中心同口径）")
    cases = [
        ("窗口内(0.1/0.1)",
         {"price_cny": 0.10, "flash_price": 0.10,
          "flash_start": NOW - 10, "flash_end": NOW + 10}, True),
        ("窗口外·价格未回升(0.1/0.1)  ← 本次客诉场景",
         {"price_cny": 0.10, "flash_price": 0.10,
          "flash_start": NOW - 200, "flash_end": NOW - 100}, True),
        ("窗口外·价格已回升(0.1/1.9)",
         {"price_cny": 1.90, "flash_price": 0.10,
          "flash_start": NOW - 200, "flash_end": NOW - 100}, False),
        ("窗口内(0.1/1.9)",
         {"price_cny": 1.90, "flash_price": 0.10,
          "flash_start": NOW - 10, "flash_end": NOW + 10}, True),
        ("无秒杀价",
         {"price_cny": 1.90, "flash_price": 0,
          "flash_start": NOW - 10, "flash_end": NOW + 10}, False),
        ("无窗口·价未回升",
         {"price_cny": 0.10, "flash_price": 0.10}, True),
        ("无窗口·价已回升",
         {"price_cny": 1.90, "flash_price": 0.10}, False),
    ]
    for name, plan, want in cases:
        pay = bool(PS._sales_state(dict(plan), NOW).get("is_activity"))
        lic = bool(LIC._spec_in_activity(dict(plan), NOW))
        check(f"{name}：期望={want}", pay == want and lic == want,
              f"pay_server={pay} license={lic} want={want}")
    # 源码口径：plan_quote 必须取 _sales_state.is_activity，不许退回 is_flash
    src = (REPO / "deploy" / "pay_server.py").read_text(encoding="utf-8")
    check('plan_quote 取 _sales_state.is_activity（不是 is_flash）',
          'bool(st.get("is_activity"))' in src)

    # 端到端：注入云端覆盖表 → plan_quote() 的 is_activity 必须是新口径。
    # ⚠️ plan_quote 内部用 time.time()，故这里的窗口要以**真实当前时刻**为基准
    #    （上面那组用固定 NOW，只喂 _sales_state / _spec_in_activity）。
    #    这一组才是真正能抓住「有人把 plan_quote 改回 is_flash」的断言。
    live = time.time()

    def _mock(plan: dict) -> object:
        payload = {"ok": True, "plans": {"download_plans": {"download_1day": plan}}}

        def _fake(req, timeout=6):   # noqa: ARG001
            return _Resp(json.dumps(payload).encode("utf-8"))
        return _fake

    live_cases = [
        ("窗口内", {"price_cny": 0.10, "flash_price": 0.10,
                 "flash_start": live - 10, "flash_end": live + 10}, True),
        ("窗口外·价格未回升", {"price_cny": 0.10, "flash_price": 0.10,
                       "flash_start": live - 200, "flash_end": live - 100}, True),
        ("窗口外·价格已回升", {"price_cny": 1.90, "flash_price": 0.10,
                       "flash_start": live - 200, "flash_end": live - 100}, False),
    ]
    _real = urllib.request.urlopen
    try:
        for name, plan, want in live_cases:
            urllib.request.urlopen = _mock(plan)
            PS._PLAN_CACHE["at"] = 0.0
            got = PS.plan_quote("download_1day", force=True)["is_activity"]
            check(f"plan_quote 端到端 {name}：期望 is_activity={want}", got == want,
                  f"got={got}")
    finally:
        urllib.request.urlopen = _real
        PS._PLAN_CACHE["at"] = 0.0
        PS._PLAN_CACHE["map"] = {}


def test_limit_reject_is_refund_not_regrant() -> None:
    """🔴 修好「活动限购」后暴露的连带问题：漏洞期遗留的 PENDING 单若被支付，
    授权中心硬闸会以 409 LIMIT_REACHED 拒绝发货。若这笔和普通发货故障一样标成
    `GRANT_FAILED`，对账会按「已收款未发货」告警**补发** —— 等于给已经超限的
    账号又发一次权益。必须单独标 `LIMIT_REJECTED` 并提示**退款**。
    """
    print("[7] 限购拒绝 → LIMIT_REJECTED（退款），不是 GRANT_FAILED（补发）")
    check("限购拒绝 → LIMIT_REJECTED",
          PS._grant_failure_status(
              'grant rejected: {"ok": false, "code": "LIMIT_REACHED"}') == "LIMIT_REJECTED")
    check("其他发货故障 → GRANT_FAILED（仍走补发）",
          PS._grant_failure_status("network down") == "GRANT_FAILED")
    _lic = (REPO / "deploy" / "license_server.py").read_text(encoding="utf-8")
    check("对账把 LIMIT_REJECTED 单列为退款告警",
          "limit_rejected_refund" in _lic
          and '"status") == "LIMIT_REJECTED"' in _lic)
    _seg = _lic[_lic.index("in_money = ["): _lic.index("in_money +=")]
    check("in_money 白名单不含 LIMIT_REJECTED（否则会误告警「请立即补发」）",
          "LIMIT_REJECTED" not in _seg)
    check("两个支付回调都走同一个判定函数",
          (REPO / "deploy" / "pay_server.py").read_text(encoding="utf-8")
          .count("_grant_failure_status(e)") == 2)


def test_unknown_code_not_sellable() -> None:
    print("[4] 未知 code：本地兜底价 0.00（handler 另有 PRICE_MAP 白名单拦截）")
    check("未知 code → 0.00", PS.plan_price("no_such_plan") == "0.00")


def test_order_price_is_never_stale() -> None:
    """🔴 2026-10-09 客诉「价格对不上」：展示侧（worker）`_CLOUD_PLANS_TTL=15s`，
    支付侧原为 `PLAN_CACHE_TTL=300s` ⇒ 管理员改价后页面 ≤15s 就显示新价，收款却
    仍按旧价，最长 285s「展示价 ≠ 实收价」。修法：**下单路径 force 取新鲜价**。

    本用例同时钉住"缓存本身仍在"（非 force 吃 TTL 是设计内），避免有人为了修这个
    问题把缓存整个删掉（那会让每次轮询都打授权中心）。
    """
    print("[6] 下单取价：force 绕过缓存（防「页面新价 / 收款旧价」）")

    box = {"price": 2.99}

    def _fake(req, timeout=6):   # noqa: ARG001
        return _Resp(json.dumps({"ok": True, "plans": {"download_plans": {
            "download_3day": {"price_cny": box["price"]}}}}).encode("utf-8"))

    _real = urllib.request.urlopen
    urllib.request.urlopen = _fake
    PS._PLAN_CACHE["at"] = 0.0
    PS._PLAN_CACHE["map"] = {}
    try:
        # ① 首次取价（管理员当时的价 2.99），缓存随之建立
        first = PS.plan_quote("download_3day")
        check("首次取价 = 云端 2.99（source=cloud）",
              first["price"] == "2.99" and first["source"] == "cloud", str(first))

        # ② 管理员改价 → 云端变 1.50（页面 ≤15s 就会显示 1.50）
        box["price"] = 1.50

        # ③ 非 force：TTL 内仍吃缓存（设计内行为，此处显式记录）
        cached = PS.plan_quote("download_3day")
        check("非 force 在 TTL 内吃缓存 → 仍 2.99", cached["price"] == "2.99", str(cached))

        # ④ force（下单路径）：必须拿到新鲜价 1.50
        fresh = PS.plan_quote("download_3day", force=True)
        check("force=True（下单路径）→ 取新鲜价 1.50", fresh["price"] == "1.50", str(fresh))

        # ⑤ 源码口径：下单 handler 必须带 force=True（防回归改回去）
        src = (REPO / "deploy" / "pay_server.py").read_text(encoding="utf-8")
        check("下单 handler 调用 plan_quote(..., force=True)",
              "quote = plan_quote(plan_code, force=True)" in src)

        # ⑥ 拉取失败只退避 RETRY_TTL（不是整个 TTL）——否则一次抖动把旧价锁死 5 分钟
        def _boom(req, timeout=6):   # noqa: ARG001
            raise OSError("network down")

        urllib.request.urlopen = _boom
        PS._PLAN_CACHE["at"] = 0.0
        PS._PLAN_CACHE["map"] = {"download_3day": {"price_cny": 2.99}}
        t0 = time.time()
        PS._cloud_plans()
        age = t0 - float(PS._PLAN_CACHE.get("at") or 0.0)
        check("拉取失败后按 RETRY_TTL(15s) 重试，不锁死整个 TTL",
              age >= PS.PLAN_CACHE_TTL - PS.PLAN_CACHE_RETRY_TTL - 2, "age=%.1fs" % age)
    finally:
        urllib.request.urlopen = _real
        PS._PLAN_CACHE["at"] = 0.0
        PS._PLAN_CACHE["map"] = {}


def main() -> int:
    print("=" * 46)
    print("支付服务价格同源守卫（deploy/pay_server.py ↔ server/membership.py）")
    print("=" * 46)
    test_rules_agree()
    test_flash_price_used()
    test_activity_scope_agrees()
    test_limit_reject_is_refund_not_regrant()
    test_plan_quote_cloud_and_fallback()
    test_unknown_code_not_sellable()
    test_order_price_is_never_stale()
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
