# -*- coding: utf-8 -*-
"""守卫：授权中心「每日对账」grant_no_pay（自动发货找不到收款订单）的归并口径。

背景（2026-10-09 用户截图）：后台「每日对账」里出现**两行内容完全相同**的红字
「2026-10-09 09:22 给 limitprobe@vdl.local 自动发货「download_1day」但找不到已收款订单
—— 疑似绕过支付/伪造发货调用」。查下来是两笔独立事件（09:22:10 / 09:22:23，相隔 13 秒），
但 detail 只到分 ⇒ 两行渲染成同一个 HH:MM，既看不出是两笔、也看不出时间跨度，
用户只能看到「两条一模一样的告警」。

修法：pass 3 由「一笔事件一行」改为按 (账号, 档位, 账期日) 归并成一行，明细带**总笔数**；
多笔时改用**到秒的时刻区间**（分钟级无从区分同分钟的多笔），单笔时保持分钟级文案。
day_rows 的差异计数随之由「事件数」变为「异常次数」，与 paid_no_grant 口径一致。

本测试钉住：
  [A] N 笔未配对自动发货（同账号/同档/同日）→ **1 行**，含「共 N 笔」+ 到秒区间
  [B] 单笔 → 1 行，分钟级（不得出现秒，不得出现「共」）
  [C] 不同账号 / 不同档 / 不同账期日 → 各自成行（归并维度不能过宽）
  [D] 已配对订单不误报 paid_no_grant（含渠道前缀泛化：xunhupay / alipay 都要认）
  [E] 非渠道 note（admin manual regrant / probe）不算自动发货，不入对账
  [F] 口径一致：day_rows 的 mismatch 计数 == len(mismatches)
  [G] 归并不得影响 paid_no_grant（真·未发货仍要按订单逐条报）
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  ✅ " + name)
    else:
        print("  ❌ " + name + (("  | " + detail) if detail else ""))
        FAILS.append(name)


_TMP = tempfile.mkdtemp(prefix="vdl_reconagg_")
_DATA = pathlib.Path(_TMP)
os.environ["VDL_LICENSE_DATA"] = str(_DATA / "cards.json")
os.environ["VDL_PAY_ORDERS"] = str(_DATA / "pay_orders.json")
os.environ["VDL_LICENSE_ADMIN_TOKEN"] = "test-admin"
os.environ["VDL_LICENSE_SECRET"] = "test-secret"

# deploy/license_server.py 模块级只有常量赋值（服务在 __main__ 里才启动），加载无副作用。
_spec = importlib.util.spec_from_file_location(
    "vdl_license_reconagg", str(REPO / "deploy" / "license_server.py"))
LIC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(LIC)

NOW = 1_700_000_000.0
MIN = int(NOW // 60) * 60          # 同一分钟的对齐基准（保证 MIN+5 / MIN+18 同分钟）
DAY = 86400.0

_state: dict = {}


def _write(events: list, orders: dict) -> None:
    _DATA.mkdir(parents=True, exist_ok=True)
    (_DATA / "cards.json").write_text(
        json.dumps(_state, ensure_ascii=False), encoding="utf-8")
    (_DATA / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events), encoding="utf-8")
    (_DATA / "pay_orders.json").write_text(
        json.dumps(orders, ensure_ascii=False), encoding="utf-8")


def _grant(at: float, email: str, plan: str, note: str) -> dict:
    return {"kind": "grant", "at": at, "email": email, "plan_code": plan, "note": note}


def _order(oid: str, email: str, plan: str, status: str, at: float,
           amount: str = "1.90", mode: str = "xunhupay") -> tuple:
    o = {"email": email, "plan_code": plan, "status": status,
         "amount": amount, "paid_at": at, "created_at": at}
    if mode:
        o["mode"] = mode
    return oid, o


def _run(events: list, orders: dict) -> dict:
    global _state
    _state = {"alerts": [], "recon_seen": []}
    _write(events, orders)
    return LIC.recon_impl(_state, days=7, now=NOW)


def _gno(r: dict) -> list:
    return [m for m in r["mismatches"] if m["kind"] == "grant_no_pay"]


# ── [A] 多笔未配对（同账号/同档/同日）→ 一行 ──────────────────────────────── #
def test_a_multi_aggregates() -> None:
    print("[A] 2 笔未配对自动发货（同分钟、相隔 13 秒）→ 归并成 1 行")
    evs = [_grant(MIN + 5, "probe@vdl.local", "download_1day", "xunhupay-auto:PROBE-FIRST"),
           _grant(MIN + 18, "probe@vdl.local", "download_1day", "xunhupay-auto:PROBE-FIRST")]
    r = _run(evs, {})
    g = _gno(r)
    check("归并后只有 1 行", len(g) == 1, f"实际 {len(g)} 行")
    if not g:
        return
    d = g[0]["detail"]
    print("      → " + d)
    check("明细节数「共 2 笔」", "共 2 笔" in d, d)
    check("明细带账号与档位", "probe@vdl.local" in d and "download_1day" in d, d)
    # 到秒区间：13 秒差在分钟级下不可分，必须给到秒
    check("多笔时给到秒（HH:MM:SS ~ HH:MM:SS）",
          d.count(":") >= 4 and "~" in d, d)
    check("区间两端确实是两笔的真实时刻",
          time.strftime("%H:%M:%S", time.gmtime(MIN + 5 + 8 * 3600)) in d
          and time.strftime("%H:%M:%S", time.gmtime(MIN + 18 + 8 * 3600)) in d, d)


# ── [A2] 同一天但不同分钟 → 仍归并成一行（归并是按「账期日」不是按「分钟」）── #
def test_a2_same_day_diff_minutes() -> None:
    print("[A2] 同一账期日、相隔数小时的两笔 → 仍归并成 1 行（按天归并）")
    evs = [_grant(MIN + 5, "hang@vdl.local", "download_1day", "xunhupay-auto:H1"),
           _grant(MIN + 70, "hang@vdl.local", "download_1day", "xunhupay-auto:H1"),   # 次一分钟
           _grant(MIN + 3600 * 3, "hang@vdl.local", "download_1day", "xunhupay-auto:H1")]  # 3 小时后
    r = _run(evs, {})
    g = _gno(r)
    check("跨分钟仍只有 1 行", len(g) == 1, f"实际 {len(g)} 行：{[x['detail'][:40] for x in g]}")
    if not g:
        return
    d = g[0]["detail"]
    print("      → " + d)
    check("笔数为 3（按天累计）", "共 3 笔" in d, d)
    check("区间跨到 3 小时后",
          time.strftime("%H:%M:%S", time.gmtime(MIN + 3600 * 3 + 8 * 3600)) in d, d)
    check("日期段只出现一次（不是每分钟一段）", d.count(LIC._bj_day(MIN + 5)) == 1, d)


# ── [B] 单笔 → 分钟级、无「共」 ───────────────────────────────────────────── #
def test_b_single_keeps_minute() -> None:
    print("[B] 单笔未配对 → 1 行，保持分钟级（不引入秒）")
    evs = [_grant(MIN + 5, "solo@vdl.local", "download_1day", "xunhupay-auto:ONLY-ONE")]
    r = _run(evs, {})
    g = _gno(r)
    check("只有 1 行", len(g) == 1, f"实际 {len(g)}")
    if not g:
        return
    d = g[0]["detail"]
    print("      → " + d)
    check("不带「共 N 笔」", "共" not in d, d)
    check("不含秒（HH:MM:SS 不出现）", ":" not in d.split("给")[0].split(" ")[-1].replace(":", "", 1), d)
    import re
    check("时刻为分钟级 YYYY-MM-DD HH:MM",
          bool(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} ", d)), d)


# ── [C] 归并维度：账号 / 档位 / 账期日 任一不同都要分开 ──────────────────── #
def test_c_group_key_is_tight() -> None:
    print("[C] 归并维度不能过宽：账号/档位/账期日 任一不同 → 各自成行")
    evs = [_grant(MIN + 5, "a@vdl.local", "download_1day", "xunhupay-auto:X"),
           _grant(MIN + 6, "b@vdl.local", "download_1day", "xunhupay-auto:X"),   # 账号不同
           _grant(MIN + 7, "a@vdl.local", "download_3day", "xunhupay-auto:X"),   # 档位不同
           _grant(MIN - DAY + 5, "a@vdl.local", "download_1day", "xunhupay-auto:X")]  # 日不同
    r = _run(evs, {})
    g = _gno(r)
    check("4 种分组 → 4 行", len(g) == 4, f"实际 {len(g)} 行：{[x['detail'][:34] for x in g]}")
    check("无任何一行被误标「共 N 笔」", all("共" not in x["detail"] for x in g))


# ── [D] 已配对订单不误报（含渠道前缀泛化）────────────────────────────────── #
def test_d_paid_with_grant_not_flagged() -> None:
    print("[D] 已收款且已发货的订单不得误报（渠道前缀泛化：alipay / xunhupay）")
    o1, o2 = ("VDLP-XUNHU-1", {"email": "u1@x.com", "plan_code": "download_1day",
                               "status": "PAID", "amount": "0.10", "paid_at": MIN + 5,
                               "created_at": MIN + 5}), \
             ("VDLP-ALI-1", {"email": "u2@x.com", "plan_code": "download_1day",
                             "status": "PAID", "amount": "1.90", "paid_at": MIN + 5,
                             "created_at": MIN + 5})
    evs = [_grant(MIN + 12, "u1@x.com", "download_1day", "xunhupay-auto:VDLP-XUNHU-1"),
           _grant(MIN + 12, "u2@x.com", "download_1day", "alipay-auto:VDLP-ALI-1")]
    r = _run(evs, {o1[0]: o1[1], o2[0]: o2[1]})
    check("无 grant_no_pay", not _gno(r), str(_gno(r)))
    check("无 paid_no_grant", not [m for m in r["mismatches"] if m["kind"] == "paid_no_grant"],
          str(r["mismatches"]))
    check("两笔都计入 auto_grants",
          sum(x.get("auto_grants", 0) for x in r["day_rows"]) == 2,
          str(r["day_rows"]))


# ── [E] 非渠道 note 不算自动发货 ─────────────────────────────────────────── #
def test_e_manual_notes_ignored() -> None:
    print("[E] 非「<渠道>-auto:」的注记不算自动发货（管理员手动补发不该进资金对账）")
    evs = [_grant(MIN + 5, "m@vdl.local", "download_1day", "admin manual regrant"),
           _grant(MIN + 6, "m@vdl.local", "download_1day", "probe"),
           _grant(MIN + 7, "m@vdl.local", "download_1day", "")]
    r = _run(evs, {})
    check("不产生任何差异", not r["mismatches"], str(r["mismatches"]))
    check("不计入 auto_grants",
          sum(x.get("auto_grants", 0) for x in r["day_rows"]) == 0, str(r["day_rows"]))


# ── [F] 口径一致 ─────────────────────────────────────────────────────────── #
def test_f_counts_agree() -> None:
    print("[F] day_rows 的 mismatch 计数 == len(mismatches)（归并后仍一致）")
    evs = [_grant(MIN + 5, "p@vdl.local", "download_1day", "xunhupay-auto:A"),
           _grant(MIN + 18, "p@vdl.local", "download_1day", "xunhupay-auto:A"),
           _grant(MIN + 6, "q@vdl.local", "download_1day", "xunhupay-auto:B")]
    o = _order("VDLP-NOG", "z@x.com", "download_1day", "PAID", MIN + 7)
    r = _run(evs, {o[0]: o[1]})
    total = sum(x.get("mismatch", 0) for x in r["day_rows"])
    check("汇总计数 == 明细条数", total == len(r["mismatches"]),
          f"汇总 {total} vs 明细 {len(r['mismatches'])}")
    check("归并后为 2 行（1 行 2 笔 + 1 行 1 笔）+ 1 条 paid_no_grant = 3",
          len(r["mismatches"]) == 3, str([m["kind"] for m in r["mismatches"]]))


# ── [G] 归并不影响 paid_no_grant ─────────────────────────────────────────── #
def test_g_paid_no_grant_intact() -> None:
    print("[G] 真·已收款未发货：仍按订单逐条报，不受归并影响")
    a = _order("VDLP-NG-1", "n1@x.com", "download_1day", "PAID", MIN + 5, mode="xunhupay")
    b = _order("VDLP-NG-2", "n2@x.com", "download_1day", "PAID", MIN + 6, mode="xunhupay")
    no_mode = _order("VDLP-NG-3", "n3@x.com", "download_1day", "PAID", MIN + 7, mode="")
    r = _run([], {a[0]: a[1], b[0]: b[1], no_mode[0]: no_mode[1]})
    png = [m for m in r["mismatches"] if m["kind"] == "paid_no_grant"]
    check("3 笔各成一条", len(png) == 3, str([m["order_id"] for m in png]))
    check("文案仍是「请立即补发」", all("请立即补发" in m["detail"] for m in png),
          str(png[:1]))
    byid = {m["order_id"]: m["detail"] for m in png}
    # 补发指引按订单的支付渠道给前缀（线上订单 mode 均为 xunhupay）
    check("带 xunhupay 渠道的订单 → 指引 xunhupay-auto:<order_id>",
          "xunhupay-auto:VDLP-NG-1" in byid.get("VDLP-NG-1", ""), byid.get("VDLP-NG-1", ""))
    # 老订单没有 mode → 回落到 alipay-auto；对账侧认「任意 <渠道>-auto:」故照样能销号
    check("无 mode 的老订单 → 回落 alipay-auto 前缀",
          "alipay-auto:VDLP-NG-3" in byid.get("VDLP-NG-3", ""), byid.get("VDLP-NG-3", ""))

    print("      [G2] 按指引补发后必须能自动销号（前缀泛化：渠道前缀不匹配也要认）")
    evs = [_grant(MIN + 20, "n3@x.com", "download_1day", "alipay-auto:VDLP-NG-3")]
    r2 = _run(evs, {no_mode[0]: no_mode[1]})
    check("补发后 paid_no_grant 消失", not [m for m in r2["mismatches"]
                                            if m["kind"] == "paid_no_grant"],
          str(r2["mismatches"]))
    check("补发的发货也不再算 grant_no_pay", not _gno(r2), str(_gno(r2)))


# ── [H] 告警自证性质：订单号不存在（探针/伪造）vs 存在但未收款（真该查）────── #
def test_h_hint_self_describes() -> None:
    """原文案一律写「疑似绕过支付/伪造发货调用」⇒ 探针噪音与真事故长得一样。

    修法：detail 追加 `_grant_no_pay_hint()` 的判定，逐订单号分三类写清：
      不存在（订单号是编的）/ 存在但非收款状态（按状态给建议）/ 已是收款状态但窗口外。
    钉住判据必须用**全量订单表**（all_by_oid），只看 in_money 会把 PENDING 误判成「不存在」。
    """
    print("[H] grant_no_pay 必须自证差异性质，不能一概喊「疑似绕过支付」")

    print("      [H1] 订单号在全量订单表里根本不存在（探针/伪造调用）")
    r = _run([_grant(MIN + 5, "probe@vdl.local", "download_1day",
                     "xunhupay-auto:PROBE-FIRST")], {})
    g = _gno(r)
    check("仍报 1 条差异（自证≠免报）", len(g) == 1, str(g))
    if not g:
        return
    d = g[0]["detail"]
    print("        → " + d)
    check("标注「不存在」", "不存在" in d, d)
    check("点名「该订单号是编的」并回显订单号", "编的" in d and "PROBE-FIRST" in d, d)
    check("不再一律甩「疑似绕过支付」", "疑似绕过支付" not in d, d)
    check("要求核查调用来源", "核查调用来源" in d, d)

    print("      [H2] 订单真实存在但状态 PENDING（未收款即发货，真该查）")
    o = _order("VDLP-PEND-1", "p@x.com", "download_1day", "PENDING", MIN + 3)
    r = _run([_grant(MIN + 5, "p@x.com", "download_1day", "xunhupay-auto:VDLP-PEND-1")],
             {o[0]: o[1]})
    g = _gno(r)
    check("报 1 条差异", len(g) == 1, str(g))
    if not g:
        return
    d = g[0]["detail"]
    print("        → " + d)
    check("标注「真实存在但未进入已收款口径」",
          "真实存在" in d and "未进入已收款口径" in d, d)
    check("带状态原文与白话处置", "PENDING" in d and "付款未完成" in d, d)
    # 🔴 判据若只看 in_money（by_oid），PENDING 单会被误判成「订单号不存在」⇒ 冤枉成伪造调用
    check("不得误标「不存在」（判据必须用全量订单表）", "不存在" not in d, d)

    print("      [H3] 订单存在但仍在发货处理中（GRANTING 未满卡死阈值）")
    o = _order("VDLP-GR-1", "g@x.com", "download_1day", "GRANTING", MIN + 3)
    r = _run([_grant(MIN + 5, "g@x.com", "download_1day", "xunhupay-auto:VDLP-GR-1")],
             {o[0]: o[1]})
    d = _gno(r)[0]["detail"] if _gno(r) else ""
    print("        → " + d)
    check("说明会自动销号", "发货处理中" in d and "自动销号" in d, d)
    # 自相矛盾检查：GRANTING 是发货还在跑，不能再喊「核查付款是否未完成即发货」
    check("不得同时喊「付款未完成即发货」（自相矛盾）", "付款是否未完成" not in d, d)

    print("      [H4] 一行内混合两种性质：编造的号 + 真实未收款号（同号重复出现要合并）")
    o = _order("VDLP-MIX-REAL", "mix@x.com", "download_1day", "EXPIRED", MIN + 3)
    r = _run([_grant(MIN + 5, "mix@x.com", "download_1day", "xunhupay-auto:VDLP-MIX-FAKE"),
              _grant(MIN + 8, "mix@x.com", "download_1day", "xunhupay-auto:VDLP-MIX-REAL"),
              _grant(MIN + 9, "mix@x.com", "download_1day", "xunhupay-auto:VDLP-MIX-REAL")],
             {o[0]: o[1]})
    g = _gno(r)
    check("同日同账号同档仍归并 1 行", len(g) == 1, str([x["detail"][:40] for x in g]))
    if not g:
        return
    d = g[0]["detail"]
    print("        → " + d)
    check("笔数为 3", "共 3 笔" in d, d)
    check("两种性质都在同一行写明", "不存在" in d and "未进入已收款口径" in d, d)
    check("同一订单号只报一次（去重）", d.count("VDLP-MIX-REAL") == 1, d)
    check("伪编号也只报一次", d.count("VDLP-MIX-FAKE") == 1, d)
    check("EXPIRED 带白话「已过期」", "已过期" in d, d)

    print("      [H5] note 未携带订单号 → 明说无法核对，不猜")
    r = _run([_grant(MIN + 5, "n@vdl.local", "download_1day", "xunhupay-auto:")], {})
    d = _gno(r)[0]["detail"] if _gno(r) else ""
    print("        → " + d)
    check("明说「未携带订单号」", "未携带订单号" in d, d)
    check("不得凭空断言「编的」", "编的" not in d, d)

    print("      [H6] 订单已收款但落在账期窗口外 → 属跨窗对账，不是漏发")
    o = _order("VDLP-OLD-1", "old@x.com", "download_1day", "PAID", MIN - 10 * DAY)
    r = _run([_grant(MIN + 5, "old@x.com", "download_1day", "xunhupay-auto:VDLP-OLD-1")],
             {o[0]: o[1]})
    d = _gno(r)[0]["detail"] if _gno(r) else ""
    print("        → " + d)
    check("说明落在账期窗口外", "账期窗口外" in d, d)
    check("不误判为「订单号不存在」", "不存在" not in d, d)


def main() -> int:
    print("=" * 58)
    print("对账 grant_no_pay 归并守卫（deploy/license_server.py）")
    print("=" * 58)
    try:
        test_a_multi_aggregates()
        test_a2_same_day_diff_minutes()
        test_b_single_keeps_minute()
        test_c_group_key_is_tight()
        test_d_paid_with_grant_not_flagged()
        test_e_manual_notes_ignored()
        test_f_counts_agree()
        test_g_paid_no_grant_intact()
        test_h_hint_self_describes()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("=" * 58)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 对账归并守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
