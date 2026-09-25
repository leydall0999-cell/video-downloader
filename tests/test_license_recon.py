"""每日入账/充值对账（recon_impl）单测 —— 纯函数级，不起 HTTP、不碰真实数据文件。

覆盖 2026-09-26 资金核对：
  paid_no_grant        已收款但没发货（critical）
  grant_no_pay         自动发货找不到已收款订单（critical）
  plan_amount_mismatch 发货套餐与订单实付不符（warn）
  卡密核销单列（线下入账，不算资金差异）
  recon_seen 去重（同一差异只告警一次）
  老数据兜底匹配（note 无 order_id 时按 账号+套餐 就近配对）
"""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "license_server", _here.parent / "deploy" / "license_server.py")
license_server = importlib.util.module_from_spec(_spec)
sys.modules["license_server"] = license_server
_spec.loader.exec_module(license_server)

NOW = time.time()
SECRET = "unit-test-secret"


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    """把 pay_orders.json / events.jsonl 指到临时目录，避免碰真实数据。"""
    pay_orders = tmp_path / "pay_orders.json"
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(license_server, "PAY_ORDERS_PATH", pay_orders)
    monkeypatch.setattr(license_server, "EVENT_LOG_PATH", events)
    return pay_orders, events


def _write_orders(path, orders: dict):
    path.write_text(json.dumps(orders, ensure_ascii=False), encoding="utf-8")


def _write_events(path, events: list):
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                    encoding="utf-8")


def _paid_order(oid, email="u@x.com", plan="download_year",
                amount="179.00", at=NOW, status="PAID"):
    return oid, {"email": email, "plan_code": plan, "amount": amount,
                 "status": status, "created_at": at - 60, "paid_at": at}


def _auto_grant(email="u@x.com", plan="download_year", at=NOW,
                note="alipay-auto:OID1"):
    return {"kind": "grant", "at": at, "email": email, "plan_code": plan,
            "note": note}


def _kinds(state):
    return sorted({a["kind"] for a in (state.get("alerts") or [])})


def test_matched_order_no_mismatch(isolated_paths):
    pay_orders, events = isolated_paths
    _write_orders(pay_orders, dict([_paid_order("OID1")]))
    _write_events(events, [_auto_grant(note="alipay-auto:OID1")])
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW + 60)
    assert r["ok"] and r["mismatches"] == [] and r["new_alerts"] == 0
    assert r["income_yuan_total"] == 179.00
    row = r["day_rows"][0]
    assert row["paid_orders"] == 1 and row["auto_grants"] == 1 and row["mismatch"] == 0
    assert state.get("alerts") in (None, [])


def test_paid_without_grant_raises_critical(isolated_paths):
    pay_orders, events = isolated_paths
    _write_orders(pay_orders, dict([_paid_order("OID1")]))
    _write_events(events, [])                     # 收了钱没发货
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW + 60)
    kinds = [m["kind"] for m in r["mismatches"]]
    assert kinds == ["paid_no_grant"]
    assert r["new_alerts"] == 1
    assert _kinds(state) == ["recon_mismatch"]
    a = state["alerts"][0]
    assert a["level"] == "critical" and "OID1" in a["detail"]


def test_grant_without_pay_raises_critical(isolated_paths):
    pay_orders, events = isolated_paths
    _write_orders(pay_orders, {})                 # 没有任何收款订单
    _write_events(events, [_auto_grant(note="alipay-auto:GHOST")])
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW + 60)
    assert [m["kind"] for m in r["mismatches"]] == ["grant_no_pay"]
    assert r["new_alerts"] == 1
    assert state["alerts"][0]["level"] == "critical"


def test_plan_mismatch_is_warn(isolated_paths):
    pay_orders, events = isolated_paths
    # 实付 179 年卡，却发 5500 AI 包
    _write_orders(pay_orders, dict([_paid_order("OID1", plan="download_year",
                                                amount="179.00")]))
    _write_events(events, [_auto_grant(plan="ai_5500", note="alipay-auto:OID1")])
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW + 60)
    m = r["mismatches"][0]
    assert m["kind"] == "plan_amount_mismatch"
    assert r["new_alerts"] == 1
    assert state["alerts"][0]["level"] == "warn"


def test_grant_failed_counts_as_income(isolated_paths):
    """GRANT_FAILED = 用户已付款但发货失败 —— 必须按已收款处理并报 paid_no_grant。"""
    pay_orders, events = isolated_paths
    _write_orders(pay_orders, dict([_paid_order("OID1", status="GRANT_FAILED")]))
    _write_events(events, [])
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW + 60)
    assert [m["kind"] for m in r["mismatches"]] == ["paid_no_grant"]
    assert r["day_rows"][0]["grant_failed"] == 1


def test_pending_order_ignored(isolated_paths):
    """PENDING（还没付款）不算入账，不产生差异。"""
    pay_orders, events = isolated_paths
    _write_orders(pay_orders, dict([_paid_order("OID1", status="PENDING")]))
    _write_events(events, [])
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW + 60)
    assert r["mismatches"] == [] and r["income_yuan_total"] == 0


def test_same_mismatch_alerts_only_once(isolated_paths):
    pay_orders, events = isolated_paths
    _write_orders(pay_orders, dict([_paid_order("OID1")]))
    _write_events(events, [])
    state = {}
    license_server.recon_impl(state, days=7, now=NOW + 60)
    r2 = license_server.recon_impl(state, days=7, now=NOW + 1200)   # 20 分钟后再扫
    assert r2["mismatches"], "差异仍应出现在报告里"
    assert r2["new_alerts"] == 0, "但不应重复告警"
    assert len(state["alerts"]) == 1


def test_fixed_mismatch_stops_appearing(isolated_paths):
    pay_orders, events = isolated_paths
    _write_orders(pay_orders, dict([_paid_order("OID1")]))
    _write_events(events, [])
    state = {}
    license_server.recon_impl(state, days=7, now=NOW + 60)
    # 补发后：order_id 对上了 → 差异消失
    _write_events(events, [_auto_grant(note="alipay-auto:OID1")])
    r2 = license_server.recon_impl(state, days=7, now=NOW + 1200)
    assert r2["mismatches"] == []


def test_redeem_listed_not_mismatch(isolated_paths):
    """卡密核销 = 线下收款发卡，单列展示，不算资金差异。"""
    pay_orders, events = isolated_paths
    _write_orders(pay_orders, {})
    _write_events(events, [{"kind": "recharge", "at": NOW, "email": "u@x.com",
                            "plan_code": "download_year", "code": "VDL-xxx"}])
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW + 60)
    assert r["mismatches"] == [] and r["new_alerts"] == 0
    row = r["day_rows"][0]
    assert row["redeems"] == 1
    assert abs(row["redeem_income_est"] - 179.00) < 0.01


def test_manual_grant_excluded(isolated_paths):
    """管理员手动 grant（note 不是 alipay-auto）不计资金口径。"""
    pay_orders, events = isolated_paths
    _write_orders(pay_orders, {})
    _write_events(events, [_auto_grant(note="补发赠送给老用户")])
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW + 60)
    assert r["mismatches"] == [] and r["new_alerts"] == 0
    assert r["day_rows"] == [], "手动 grant 被排除后不应产生任何账目行"


def test_fallback_match_legacy_note(isolated_paths):
    """升级前的老 note（无 order_id）按 账号+套餐 时间就近配对，不误报。"""
    pay_orders, events = isolated_paths
    _write_orders(pay_orders, dict([_paid_order("OID1")]))
    _write_events(events, [_auto_grant(note="alipay-auto")])
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW + 60)
    assert r["mismatches"] == []


def test_out_of_horizon_ignored(isolated_paths):
    pay_orders, events = isolated_paths
    old = NOW - 86400 * 30
    _write_orders(pay_orders, dict([_paid_order("OID1", at=old)]))
    _write_events(events, [])
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW)
    assert r["mismatches"] == [] and r["day_rows"] == []


def test_missing_pay_file_ok(isolated_paths):
    """支付订单文件不存在（还没产生过线上订单）不报错。"""
    pay_orders, events = isolated_paths
    _write_events(events, [])
    state = {}
    r = license_server.recon_impl(state, days=7, now=NOW + 60)
    assert r["ok"] and r["mismatches"] == []
