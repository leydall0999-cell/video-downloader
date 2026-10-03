# -*- coding: utf-8 -*-
"""守卫：档位「模式 / 秒杀 / 限量 / 活动时间」售卖状态（2026-10-03）。

钉住：
  1) plan_sales_state 的四种不可买原因（下架 / 未开始 / 已结束 / 售罄）与现价；
  2) 秒杀窗口内用秒杀价，窗口外回原价；
  3) ensure_plan_buyable 未知 code 也能拒；
  4) 激活入口真的做了校验（售罄档发不了）且成功后已售 +1；
  5) plans() 每档都带 state，前端可直接置灰。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_tmp = tempfile.mkdtemp(prefix="vdl_sales_")
os.environ["VDL_DATA_DIR"] = _tmp
os.environ["VDL_CLOUD_LINK"] = "0"
os.environ["VDL_PLANS_CLOUD"] = "0"

import membership as M                                       # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  ✅ " if cond else "  ❌ ") + name)
    if not cond:
        FAILS.append(name)


NOW = 1_700_000_000.0


def test_state_basics() -> None:
    print("\n[A] 基础状态：默认可买、现价=原价")
    st = M.plan_sales_state({"price_cny": 29.8, "days": 30}, now=NOW)
    check("[默认] 可买", st["buyable"] is True and st["reason"] == "")
    check("[默认] 现价=原价", st["price"] == 29.8 and st["is_flash"] is False)
    check("[默认] 不限量时 remaining 为 None", st["remaining"] is None)
    check("[默认] 模式 normal", st["mode"] == "normal")


def test_state_reasons() -> None:
    print("\n[B] 四种不可买原因")
    off = M.plan_sales_state({"price_cny": 9.9, "on_sale": False}, now=NOW)
    check("[下架] on_sale=false → 已下架", off["buyable"] is False and off["reason"] == "已下架")
    pre = M.plan_sales_state({"price_cny": 9.9, "start_at": NOW + 3600}, now=NOW)
    check("[时间] 未到开始 → 活动未开始", pre["buyable"] is False and pre["reason"] == "活动未开始")
    post = M.plan_sales_state({"price_cny": 9.9, "end_at": NOW - 10}, now=NOW)
    check("[时间] 已过结束 → 活动已结束", post["buyable"] is False and post["reason"] == "活动已结束")
    sold_out = M.plan_sales_state({"price_cny": 9.9, "stock": 10, "sold": 10}, now=NOW)
    check("[限量] 售罄 → 已售罄", sold_out["buyable"] is False and sold_out["reason"] == "已售罄")
    half = M.plan_sales_state({"price_cny": 9.9, "stock": 10, "sold": 3}, now=NOW)
    check("[限量] 还有余量 → 可买且 remaining=7",
          half["buyable"] is True and half["remaining"] == 7)
    in_win = M.plan_sales_state({"price_cny": 9.9, "start_at": NOW - 60, "end_at": NOW + 60}, now=NOW)
    check("[时间] 窗口内可买", in_win["buyable"] is True)


def test_flash_sale() -> None:
    print("\n[C] 秒杀：窗口内秒杀价、窗口外原价")
    plan = {"price_cny": 99.0, "mode": "flash_sale", "flash_price": 49.9,
            "flash_start": NOW - 60, "flash_end": NOW + 600}
    hot = M.plan_sales_state(plan, now=NOW)
    check("[秒杀] 窗口内现价=秒杀价", hot["price"] == 49.9 and hot["is_flash"] is True)
    check("[秒杀] 原价保留用于划线", hot["original_price"] == 99.0)
    cold = M.plan_sales_state(plan, now=NOW + 99999)
    check("[秒杀] 窗口外回原价", cold["price"] == 99.0 and cold["is_flash"] is False)
    no_price = M.plan_sales_state({**plan, "flash_price": 0}, now=NOW)
    check("[秒杀] 没填秒杀价 → 不用秒杀", no_price["price"] == 99.0)
    bad_mode = M.plan_sales_state({**plan, "mode": "乱写"}, now=NOW)
    check("[模式] 非法模式回落 normal", bad_mode["mode"] == "normal")


def test_ensure_buyable() -> None:
    print("\n[D] ensure_plan_buyable")
    M.save_plan_overrides({"download_plans": {"download_7day": {
        "price_cny": 9.9, "stock": 1, "sold": 1,
    }}})
    r = M.ensure_plan_buyable("download_7day")
    check("[校验] 售罄档不可买", r["ok"] is False and r["error"] == "已售罄")
    ok = M.ensure_plan_buyable("download_1day")
    check("[校验] 正常档可买", ok["ok"] is True)
    check("[校验] 未知 code 被拒", M.ensure_plan_buyable("no_such_code")["ok"] is False)


def test_activate_enforces_and_counts() -> None:
    print("\n[E] 激活：售罄拒发 + 成功后已售 +1")
    st = M.MembershipStore()
    M.save_plan_overrides({"download_plans": {"download_3day": {
        "price_cny": 4.9, "days": 3, "stock": 5, "sold": 0,
    }}})
    # 售罄 → 拒发
    M.save_plan_overrides({"download_plans": {"download_3day": {"sold": 5}}})
    r = st.activate("download_3day", via="test")
    check("[激活] 售罄档激活被拒", r.get("ok") is False and "售罄" in str(r.get("error")))
    # 恢复库存 → 成功且已售 +1
    M.save_plan_overrides({"download_plans": {"download_3day": {"sold": 2}}})
    r2 = st.activate("download_3day", via="test")
    check("[激活] 有货时激活成功", r2.get("ok") is True)
    ov = M.load_plan_overrides().get("download_plans", {}).get("download_3day", {})
    check("[计数] 激活后 sold 自动 +1", int(ov.get("sold") or 0) == 3)


def test_plans_carry_state() -> None:
    print("\n[F] plans() 每档带 state")
    plans = M.MembershipStore().plans()
    dl = plans.get("download_member", {}).get("plans", {})
    check("[接线] 下载会员每档都有 state", all("state" in p for p in dl.values()))
    check("[接线] state 含 buyable/price/reason",
          all({"buyable", "price", "reason"} <= set(p["state"]) for p in dl.values()))
    ai = plans.get("ai_member", {}).get("plans", {})
    check("[接线] AI 会员每档都有 state", all("state" in p for p in ai.values()))
    pay = M.effective_pay_plans()
    check("[下单] 下单表带 buyable 标记", "buyable" in pay.get("download_1day", {}))


def main() -> int:
    test_state_basics()
    test_state_reasons()
    test_flash_sale()
    test_ensure_buyable()
    test_activate_enforces_and_counts()
    test_plans_carry_state()
    print("\n" + "=" * 46)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 档位售卖状态（秒杀/限量/活动时间）守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
