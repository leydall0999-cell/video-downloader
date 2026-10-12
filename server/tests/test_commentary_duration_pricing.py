# -*- coding: utf-8 -*-
"""视频解说「按时间计费」守卫（2026-10-12）

用户定档：视频解说**按时间收费，50 积分/半小时**（此前一律按次收 40）。
为什么必须钉住 —— 三处会各自漂移，且都不是 py_compile 能抓的：

  · 扣费：routers/quota.py::charge_commentary_credits 以前直接
    `gate_message(store, op)` —— 扣的是**表内单价** 40，与时长无关。
    改完仍可能有人把 `cost=total` 写漏 → 又变回按次收（静默，看不出来）。
  · 单价：AI_CREDIT_COSTS 的 cost 字段被重新解释为**每半小时单价**，
    后台 plans.json 覆盖层照旧生效（改价不用重新构建）。
  · 显示：个人中心单价列显示「50 积分/半小时」而非「50 积分/次」——
    后端带 credit_unit，前端优先用它；沿用 unit（次）就是又一次口径分叉。

口径：**向上取整**到半小时（不足半小时按半小时），时长未知按 1 个单位 ——
绝不许算出 0 分白跑（与「长片自动转云端，宁多收不少收」同口径）。

运行：cd server && ../.build_venv/bin/python tests/test_commentary_duration_pricing.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
_SERVER = _HERE.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

os.environ.setdefault("VDL_PLANS_CLOUD", "0")
os.environ.setdefault("VDL_CLOUD_LINK", "0")
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_comduration."))

import membership as M  # noqa: E402
from routers import quota as QR  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  —— " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


UNIT = QR.COMMENTARY_BILLING_UNIT_SEC          # 1800 秒 = 半小时
HALF_HOUR_PRICE = 50


def test_units_round_up():
    """计费单位折算：不足半小时按半小时，时长未知按 1 个。"""
    cases = [
        (0.0, 1, "时长未知（0）按 1 个单位收，绝不算出 0 分"),
        (1.0, 1, "1 秒"),
        (1799.0, 1, "29 分 59 秒"),
        (1800.0, 1, "整半小时"),
        (1801.0, 2, "刚过 1 秒 → 进第二个半小时"),
        (3599.0, 2, "59 分 59 秒"),
        (3600.0, 2, "整 1 小时"),
        (5400.0, 3, "1.5 小时"),
    ]
    for dur, want, why in cases:
        got = QR.commentary_billing_units(dur)
        check(f"[折算] {dur:g}s → {want} 个半小时单位（{why}）",
              got == want, f"实际 {got}")


def test_total_scales_with_duration():
    """总价 = 每半小时单价 × 单位数，且严格随时长递增（不再是恒定 40）。"""
    unit_price = M.credit_cost("commentary_llm")
    check("[单价] commentary_llm 每半小时单价 = 50", unit_price == HALF_HOUR_PRICE,
          f"实际 {unit_price}")
    mlx_price = M.credit_cost("commentary_local_mlx")
    check("[单价] commentary_local_mlx 与云端同价（长片转云端不额外加价）",
          mlx_price == HALF_HOUR_PRICE, f"实际 {mlx_price}")

    expect = [
        (0.0, HALF_HOUR_PRICE),
        (600.0, HALF_HOUR_PRICE),        # 10 分钟
        (1800.0, HALF_HOUR_PRICE),       # 30 分钟
        (2700.0, HALF_HOUR_PRICE * 2),   # 45 分钟
        (3600.0, HALF_HOUR_PRICE * 2),   # 60 分钟
        (7200.0, HALF_HOUR_PRICE * 4),   # 2 小时
    ]
    for dur, want in expect:
        got = QR.commentary_credit_total("commentary_llm", dur)
        check(f"[总价] {dur:g}s（{dur/60:g} 分钟）→ {want} 积分",
              got == want, f"实际 {got}")

    # 单调递增：长片绝不能比短片便宜（旧口径 40 恒定，长片白嫖）
    prev = 0
    for dur in (0, 300, 900, 1800, 2700, 3600, 5400, 7200):
        cur = QR.commentary_credit_total("commentary_llm", float(dur))
        check(f"[单调] {dur}s 计费不低于更短片长", cur >= prev, f"{cur} < {prev}")
        prev = cur


def test_charge_uses_total_not_unit_price():
    """真实扣费必须按「总价」扣，而不是表内单价（变异：删掉 cost=total 必红）。"""
    src = pathlib.Path(_SERVER, "routers", "quota.py").read_text(encoding="utf-8")
    body = src.split("def charge_commentary_credits(")[-1].split("\ndef ")[0]
    check("[接线] charge_commentary_credits 用 commentary_credit_total 算总价",
          "commentary_credit_total(" in body)
    check("[接线] 扣费把 total 传给 gate_message（cost=）",
          "gate_message(" in body and "cost=total" in body,
          "漏了 cost=total 就会退回「按表内单价扣」")
    check("[接线] gate_message 支持显式金额（cost 形参）",
          "cost: int | None = None" in pathlib.Path(
              _SERVER, "membership.py").read_text(encoding="utf-8"))

    # 行为验证：造一个假 store，确认 45 分钟片扣 2 个单位的钱
    class _FakeStore:
        def __init__(self):
            self.spent: list[int] = []

        def spend_credits(self, cost, reason=""):
            self.spent.append(int(cost))
            return {"ok": True, "spent": int(cost), "credits_left": 999}

        def trial_available(self, op, cost):
            return False

        def trial_consume(self, op, reason=""):
            return False

    st = _FakeStore()
    msg = M.gate_message(st, "commentary_llm", reason="t",
                         cost=QR.commentary_credit_total("commentary_llm", 2700.0))
    check("[行为] 45 分钟片实扣 100 积分（gate_message 按 total 扣）",
          msg is None and st.spent == [HALF_HOUR_PRICE * 2], f"实际 {st.spent}")

    st2 = _FakeStore()
    M.gate_message(st2, "commentary_llm", reason="t",
                   cost=QR.commentary_credit_total("commentary_llm", 600.0))
    check("[行为] 10 分钟片实扣 50 积分", st2.spent == [HALF_HOUR_PRICE], f"实际 {st2.spent}")

    # 不传 cost 时行为不变（其余按次计费的功能不受影响）
    st3 = _FakeStore()
    M.gate_message(st3, "commentary_llm", reason="t")
    check("[兼容] 不传 cost 时仍按表内单价扣（其余功能不受影响）",
          st3.spent == [HALF_HOUR_PRICE], f"实际 {st3.spent}")


def test_display_unit_is_half_hour():
    """个人中心单价列必须显示「50 积分/半小时」，且前端优先用 credit_unit。"""
    store = M.MembershipStore()
    rows = {r["key"]: r for r in M.feature_usage_status(store)}
    r = rows.get("commentary")
    check("[显示] 视频解说行存在", r is not None)
    if r:
        check("[显示] 单价 = 50（每半小时）", int(r.get("credit_cost") or 0) == HALF_HOUR_PRICE,
              f"实际 {r.get('credit_cost')}")
        check("[显示] 计价单位为「半小时」而非「次」",
              str(r.get("credit_unit") or "") == "半小时",
              f"实际 credit_unit={r.get('credit_unit')!r}")
        check("[显示]「每日次数」列仍以「次」为单位（两列不混用）",
              str(r.get("unit") or "") == "次", f"实际 unit={r.get('unit')!r}")

    js = pathlib.Path(_SERVER.parent, "web", "app.js").read_text(encoding="utf-8")
    check("[前端] 单价列优先用 credit_unit（否则显示成「50 积分/次」）",
          "f.credit_unit || unit" in js)


def test_precheck_reports_cost():
    """预检响应必须带「本次将消耗 N 积分」（与扣费点同源，选完视频就能看到价钱）。"""
    cases = [
        (600.0, 50, 1, "10 分钟片 → 50 积分"),
        (2700.0, 100, 2, "45 分钟片 → 100 积分"),
        (7200.0, 200, 4, "2 小时片 → 200 积分"),
    ]
    for dur, want_cost, want_units, why in cases:
        res = QR.precheck_commentary(None, dur, engine="cloud")
        check(f"[预检] {why}（credit_cost）",
              int(res.get("credit_cost") or 0) == want_cost,
              f"实际 {res.get('credit_cost')}")
        check(f"[预检] {why}（credit_units）",
              int(res.get("credit_units") or 0) == want_units,
              f"实际 {res.get('credit_units')}")
        check(f"[预检] {why}（credit_op 同扣费口径）",
              str(res.get("credit_op") or "") == "commentary_llm",
              f"实际 {res.get('credit_op')!r}")

    # 前端必须真的展示这个字段（不然白算）
    js = pathlib.Path(_SERVER.parent, "web", "app.js").read_text(encoding="utf-8")
    check("[前端] comApplyPrecheck 展示「本次将消耗 N 积分」",
          "res.credit_cost > 0" in js and "本次将消耗" in js,
          "漏了展示 = 用户仍要等扣费失败才知道价钱")


def main():
    print("=== 视频解说按时间计费守卫 ===")
    print("  ── 计费单位折算 ──")
    test_units_round_up()
    print("  ── 总价随时长缩放 ──")
    test_total_scales_with_duration()
    print("  ── 真实扣费接线 ──")
    test_charge_uses_total_not_unit_price()
    print("  ── 显示口径 ──")
    test_display_unit_is_half_hour()
    print("  ── 预检费用提示 ──")
    test_precheck_reports_cost()

    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print("   · " + f)
        return 1
    print("✅ 解说按时间计费守卫全部通过（50 积分/半小时，不足半小时按半小时）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
