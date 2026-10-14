# -*- coding: utf-8 -*-
"""守卫：授权中心「反破解风控」风险评分（第 1 批「纯观测」，deploy/license_server.py）。

背景：完整方案见 WorkBuddy/反破解风控方案_异常检测_自动降级_人工复核_2026-10-14.md。
第 1 批只算分、记事件、交给看板复核，**不挂任何自动响应**。本测试钉住的是「评分这件事
本身按设计工作」以及几条**一旦被改坏就会引入重大风险**的红线。

本测试钉住：
  [A] 六信号各自独立命中、权重正确（device_churn/fp_reuse/hb_anomaly/spend_burst/
      redeem_brute/integrity）
  [B] 分数 = 命中权重之和，**上限 100**；档位阈值单调（T1<T2<T3）
  [C] 档位边界：0/28→observe、30/45/48→throttle、55/75→challenge、80/满贯→review
  [D] 🔴 自清性：命中项离开信号窗口后分数**立刻回落**，不累加、不沉淀（等效衰减）
  [E] 🔴 弱信号（客户端上报 integrity）权重 < T1 ⇒ **不得单独跨档**（可伪造，必须叠加）
  [F] 豁免：账号 risk.exempt 与 env 白名单 fp 两路都恒 0
  [G] risk_top 按分降序 + min_score floor + 排除豁免账号
  [H] risk_clear：清分、保留 exempt、写 cleared_at 与事件
  [I] 🔴 红线一：**IP 永不进风险路径**（六信号签名不含 ip ⇒ 杜绝 CGNAT 一锁一片）
  [J] 🔴 红线二：档位集合不含 ban（**自动永久封禁永远不做**）
  [K] 🔴 红线三：纯观测——risk_eval 返回值不含任何自动动作字段
  [L] _hb_overdense 连续段算法 / _ring_match 窗口裁剪 / fp_index 容量淘汰（3 项基建）
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  ✅ " + name)
    else:
        print("  ❌ " + name + (("  | " + detail) if detail else ""))
        FAILS.append(name)


_TMP = tempfile.mkdtemp(prefix="vdl_risk_")
_DATA = pathlib.Path(_TMP)
os.environ["VDL_LICENSE_DATA"] = str(_DATA / "cards.json")
os.environ["VDL_PAY_ORDERS"] = str(_DATA / "pay_orders.json")
os.environ["VDL_LICENSE_ADMIN_TOKEN"] = "test-admin"
os.environ["VDL_LICENSE_SECRET"] = "test-secret"

# 变异测试用 VDL_RISK_SERVER 指向临时副本；缺省用真实源码。
_SRC = pathlib.Path(os.environ.get("VDL_RISK_SERVER") or (REPO / "deploy" / "license_server.py"))

# deploy/license_server.py 模块级只有常量赋值（服务在 __main__ 里才启动），加载无副作用。
_spec = importlib.util.spec_from_file_location("vdl_license_risk", str(_SRC))
LIC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(LIC)

NOW = 1_800_000_000.0
DAY = 86400.0


def _state() -> dict:
    return {"users": {}, "ev_ring": [], "fp_index": {}, "alerts": []}


def _user(st: dict, uid: str, **kw) -> dict:
    u = {"user_id": uid, "email": uid}
    u.update(kw)
    st["users"][uid] = u
    return u


def _ev(st: dict, kind: str, at: float, **fields) -> dict:
    return LIC._log_event(st, kind, at, **fields)


def _score(st: dict, uid: str) -> int:
    return LIC.risk_eval(st, uid, NOW)["score"]


def _level(st: dict, uid: str) -> str:
    return LIC.risk_eval(st, uid, NOW)["level"]


def _reasons(st: dict, uid: str) -> list:
    return LIC.risk_eval(st, uid, NOW)["reasons"]


# ── [A] 六信号独立命中 ───────────────────────────────────────────────────── #
def test_a_signals() -> None:
    print("[A] 六信号各自独立命中、权重正确")

    print("      [A1] device_churn：7 天内新增 ≥4 台设备 → 20")
    st = _state()
    _user(st, "u@x.com")
    for i in range(4):
        _ev(st, "device_add", NOW - i * 60, email="u@x.com", fp=f"dc{i}")
    check("device_churn 命中", _score(st, "u@x.com") == 20, str(_score(st, "u@x.com")))
    check("命中项为 device_churn",
          [r["k"] for r in _reasons(st, "u@x.com")] == ["device_churn"],
          str(_reasons(st, "u@x.com")))

    print("      [A2] fp_reuse：同一设备指纹被 ≥3 个账号使用 → 25")
    st = _state()
    _user(st, "v@x.com", devices=[{"fp": "shared", "last_seen": NOW}])
    st["fp_index"] = {"shared": {"uids": ["v@x.com", "a2@x", "b2@x"], "first": NOW, "last": NOW}}
    check("fp_reuse 命中", _score(st, "v@x.com") == 25, str(_score(st, "v@x.com")))

    print("      [A3] hb_anomaly：连续 ≥10 次心跳间隔 <30s → 15")
    st = _state()
    _user(st, "w@x.com", hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])
    check("hb_anomaly 命中", _score(st, "w@x.com") == 15, str(_score(st, "w@x.com")))

    print("      [A4] spend_burst：10 分钟内 ≥30 次扣费/用量上报 → 20")
    st = _state()
    _user(st, "s@x.com")
    for i in range(30):
        _ev(st, "spend", NOW - i, email="s@x.com", applied=1)
    check("spend_burst 命中", _score(st, "s@x.com") == 20, str(_score(st, "s@x.com")))

    print("      [A5] redeem_brute：10 分钟内 ≥5 次核销失败 → 15")
    st = _state()
    _user(st, "r@x.com")
    for i in range(5):
        _ev(st, "redeem_fail", NOW - i, email="r@x.com", err="USED")
    check("redeem_brute 命中", _score(st, "r@x.com") == 15, str(_score(st, "r@x.com")))

    print("      [A6] integrity：客户端上报自检失败 → 8（弱信号，第 3 批接入）")
    st = _state()
    _user(st, "i@x.com", devices=[{"fp": "zz", "last_seen": NOW, "integrity": "fail"}])
    check("integrity 命中", _score(st, "i@x.com") == 8, str(_score(st, "i@x.com")))
    check("权重表 integrity==8", LIC.RISK_WEIGHTS.get("integrity") == 8,
          str(LIC.RISK_WEIGHTS.get("integrity")))

    print("      [A7] 干净账号 → 0")
    st = _state()
    _user(st, "clean@x.com")
    check("无信号 0 分", _score(st, "clean@x.com") == 0, str(_score(st, "clean@x.com")))
    check("不存在账号返回 0/observe",
          LIC.risk_eval(st, "ghost@x.com", NOW)["score"] == 0
          and LIC.risk_eval(st, "ghost@x.com", NOW)["level"] == "observe")


# ── [B][C] 分数构成、上限与档位边界 ─────────────────────────────────────── #
def test_bc_score_and_levels() -> None:
    print("[B] 分数 = 权重之和，上限 100；阈值单调")
    check("阈值单调 T1<T2<T3",
          LIC.RISK_T1 < LIC.RISK_T2 < LIC.RISK_T3,
          f"{LIC.RISK_T1}/{LIC.RISK_T2}/{LIC.RISK_T3}")
    check("弱信号权重 < T1（不可单独跨档）",
          LIC.RISK_WEIGHTS["integrity"] < LIC.RISK_T1,
          f"{LIC.RISK_WEIGHTS['integrity']} vs {LIC.RISK_T1}")

    print("      [B1] 叠加 = 求和：device_churn(20)+hb(15)=35")
    st = _state()
    _user(st, "m@x.com", hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])
    for i in range(4):
        _ev(st, "device_add", NOW - i * 60, email="m@x.com", fp=f"m{i}")
    check("两信号求和 35", _score(st, "m@x.com") == 35, str(_score(st, "m@x.com")))

    print("      [B2] 满贯六信号 = 103 → 截到 100")
    st = _state()
    _user(st, "full@x.com",
          devices=[{"fp": "shared", "last_seen": NOW, "integrity": "fail"}],
          hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])
    st["fp_index"] = {"shared": {"uids": ["full@x.com", "p@x", "q@x"], "first": NOW, "last": NOW}}
    for i in range(4):
        _ev(st, "device_add", NOW - i * 60, email="full@x.com", fp=f"f{i}")
    for i in range(30):
        _ev(st, "spend", NOW - i, email="full@x.com", applied=1)
    for i in range(5):
        _ev(st, "redeem_fail", NOW - i, email="full@x.com", err="USED")
    s = _score(st, "full@x.com")
    check("满贯被截到 100", s == 100, str(s))
    check("六信号全在 reasons", {r["k"] for r in _reasons(st, "full@x.com")} ==
          {"device_churn", "fp_reuse", "hb_anomaly", "spend_burst", "redeem_brute", "integrity"},
          str([r["k"] for r in _reasons(st, "full@x.com")]))

    print("[C] 档位边界：0/28→observe、30/45/48→throttle、55/75→challenge、80→review")
    cases = [
        ("0 分 → observe", 0, "observe"),
        ("28 分（<T1）→ observe", 28, "observe"),
        ("30 分（=T1）→ throttle", 30, "throttle"),
        ("45 分 → throttle", 45, "throttle"),
        ("48 分 → throttle", 48, "throttle"),
        ("55 分（=T2）→ challenge", 55, "challenge"),
        ("60 分 → challenge", 60, "challenge"),
        ("80 分（=T3）→ review", 80, "review"),
    ]
    for label, target, want in cases:
        st, uid = _mk_score(target)
        got = _level(st, uid)
        check(f"{label}（实得 {_score(st, uid)}）", got == want, f"got {got}")

    print("      [C2] _level_of 全覆盖 0..100：档位集合合法、单调不回退")
    lv = [LIC._level_of(s) for s in range(0, 101)]
    check("[J] 档位集合不含 ban（不自动永久封禁）",
          set(lv) <= {"observe", "throttle", "challenge", "review"}
          and "ban" not in set(lv), str(set(lv)))
    # 未知档位按最高危处理（防御取值），避免出现非法档位时这里先崩、掩盖真正的红字
    order = {"observe": 0, "throttle": 1, "challenge": 2, "review": 3}
    check("随分数单调不降",
          all(order.get(lv[i], 99) <= order.get(lv[i + 1], 99) for i in range(100)),
          str(sorted(set(lv))))


def _mk_score(target: int) -> tuple[dict, str]:
    """构造一个恰好得 target 分的账号（六信号权重子集求和，精确命中再构造）。"""
    import itertools

    st = _state()
    uid = "c@x.com"
    _user(st, uid, hb_ring=[], devices=[])
    w = LIC.RISK_WEIGHTS
    kinds = ["device_churn", "fp_reuse", "hb_anomaly",
             "spend_burst", "redeem_brute", "integrity"]

    def build(k: str) -> None:
        u = st["users"][uid]
        if k == "device_churn":
            for i in range(4):
                _ev(st, "device_add", NOW - i * 60, email=uid, fp=f"c{i}")
        elif k == "fp_reuse":
            st["fp_index"] = {"cshared": {"uids": [uid, "x", "y"], "first": NOW, "last": NOW}}
            u["devices"] = (u.get("devices") or []) + [{"fp": "cshared", "last_seen": NOW}]
        elif k == "hb_anomaly":
            u["hb_ring"] = [NOW - i * 5 for i in range(12, 0, -1)]
        elif k == "spend_burst":
            for i in range(30):
                _ev(st, "spend", NOW - i, email=uid, applied=1)
        elif k == "redeem_brute":
            for i in range(5):
                _ev(st, "redeem_fail", NOW - i, email=uid, err="USED")
        elif k == "integrity":
            u["devices"] = (u.get("devices") or []) + [{"fp": "zz", "integrity": "fail",
                                                        "last_seen": NOW}]

    for size in range(len(kinds) + 1):
        for combo in itertools.combinations(kinds, size):
            if sum(w[k] for k in combo) == target:
                for k in combo:
                    build(k)
                return st, uid
    return st, uid                          # 拼不出（调用点已挑好可拼目标）


# ── [D] 自清性（无沉淀）──────────────────────────────────────────────────── #
def test_d_self_clearing() -> None:
    print("[D] 🔴 自清性：命中项离开窗口后分数立刻回落，不累加、不沉淀")

    print("      [D1] 摘除触发行（清空 hb_ring / devices / 事件）→ 分数归 0")
    st = _state()
    _user(st, "z@x.com",
          devices=[{"fp": "shared", "last_seen": NOW}],
          hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])
    st["fp_index"] = {"shared": {"uids": ["z@x.com", "a", "b"], "first": NOW, "last": NOW}}
    for i in range(5):
        _ev(st, "device_add", NOW - i * 60, email="z@x.com", fp=f"z{i}")
    hi = _score(st, "z@x.com")
    check("先命中（>0）", hi > 0, str(hi))
    st["ev_ring"] = []                       # 事件环清空
    st["fp_index"] = {}                      # 指纹索引清空
    st["users"]["z@x.com"]["hb_ring"] = []   # 心跳环清空
    st["users"]["z@x.com"]["devices"] = []   # 设备清空
    check("摘除后回落 0（不沉淀）", _score(st, "z@x.com") == 0, str(_score(st, "z@x.com")))

    print("      [D2] 幂等：同 state 连算两次分数一致，且不改写事件环")
    st = _state()
    _user(st, "idem@x.com", hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])
    for i in range(4):
        _ev(st, "device_add", NOW - i * 60, email="idem@x.com", fp=f"i{i}")
    n0 = len(st["ev_ring"])
    s1 = _score(st, "idem@x.com")
    s2 = _score(st, "idem@x.com")
    check("两次分数一致", s1 == s2, f"{s1}/{s2}")
    check("risk_eval 不写事件环", len(st["ev_ring"]) == n0,
          f"{n0}→{len(st['ev_ring'])}")

    print("      [D3] 时间推移：设备事件滑出 7 天窗口 → device_churn 自然失效")
    st = _state()
    _user(st, "slide@x.com")
    for i in range(4):
        _ev(st, "device_add", NOW - i * 60, email="slide@x.com", fp=f"s{i}")
    check("当下命中 device_churn", _score(st, "slide@x.com") == 20, str(_score(st, "slide@x.com")))
    later = NOW + 8 * DAY                    # 8 天后，事件全部超出 7 天窗口
    check("8 天后自动失效",
          LIC.risk_eval(st, "slide@x.com", later)["score"] == 0,
          str(LIC.risk_eval(st, "slide@x.com", later)["score"]))


# ── [E] 弱信号不得单独跨档 ──────────────────────────────────────────────── #
def test_e_weak_signal_cannot_trip() -> None:
    print("[E] 🔴 弱信号（客户端上报）单独命中不得跨档")
    st = _state()
    _user(st, "weak@x.com", devices=[{"fp": "w", "last_seen": NOW, "integrity": "fail"}])
    sc = _score(st, "weak@x.com")
    check("仅 integrity 命中 = 8", sc == 8, str(sc))
    check("仍为 observe（未跨 T1）", _level(st, "weak@x.com") == "observe",
          _level(st, "weak@x.com"))
    print("      [E2] 伪造 integrity=ok 不会清掉其他真实信号")
    st = _state()
    _user(st, "forge@x.com", hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])
    for i in range(4):
        _ev(st, "device_add", NOW - i * 60, email="forge@x.com", fp=f"g{i}")
    st["users"]["forge@x.com"]["devices"] = [{"fp": "g0", "integrity": "ok", "last_seen": NOW}]
    check("真实信号仍在（integrity=ok 不清分）", _score(st, "forge@x.com") == 35,
          str(_score(st, "forge@x.com")))


# ── [F] 豁免 ─────────────────────────────────────────────────────────────── #
def test_f_exempt() -> None:
    print("[F] 豁免：risk.exempt 与 env 白名单 fp 两路恒 0")
    st = _state()
    _user(st, "ex@x.com", hb_ring=[NOW - i * 5 for i in range(12, 0, -1)],
          risk={"exempt": True})
    for i in range(4):
        _ev(st, "device_add", NOW - i * 60, email="ex@x.com", fp=f"e{i}")
    check("risk.exempt → 恒 0", _score(st, "ex@x.com") == 0, str(_score(st, "ex@x.com")))
    check("exempt 标记回传", LIC.risk_eval(st, "ex@x.com", NOW)["exempt"] is True)

    print("      [F2] env 白名单 fp 命中 → 恒 0")
    os.environ["VDL_LICENSE_RISK_EXEMPT_FPS"] = "white_fp, other_fp"
    try:
        st = _state()
        _user(st, "env@x.com", devices=[{"fp": "white_fp", "last_seen": NOW}],
              hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])
        for i in range(4):
            _ev(st, "device_add", NOW - i * 60, email="env@x.com", fp="white_fp")
        check("白名单 fp 账号恒 0", _score(st, "env@x.com") == 0, str(_score(st, "env@x.com")))
    finally:
        os.environ.pop("VDL_LICENSE_RISK_EXEMPT_FPS", None)


# ── [G] risk_top ─────────────────────────────────────────────────────────── #
def test_g_risk_top() -> None:
    print("[G] risk_top：按分降序 + min_score floor + 排除豁免账号")
    st = _state()
    _user(st, "low@x.com")                        # 0 分
    _user(st, "mid@x.com", hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])  # 15
    _user(st, "hig@x.com",
          devices=[{"fp": "top", "last_seen": NOW}],
          hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])                    # 15+25=40
    st["fp_index"] = {"top": {"uids": ["hig@x.com", "a", "b"], "first": NOW, "last": NOW}}
    _user(st, "exm@x.com", risk={"exempt": True},
          hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])                    # 豁免

    r = LIC.risk_top_impl(st, NOW, limit=50, min_score=1)
    ids = [x["email"] for x in r["rows"]]
    check("floor=1 排除 0 分账号", "low@x.com" not in ids, str(ids))
    check("豁免账号不在列表", "exm@x.com" not in ids, str(ids))
    check("按分降序", [x["score"] for x in r["rows"]] == sorted(
        [x["score"] for x in r["rows"]], reverse=True), str([x["score"] for x in r["rows"]]))
    check("最高分排首位", ids and ids[0] == "hig@x.com", str(ids))

    r2 = LIC.risk_top_impl(st, NOW, limit=50, min_score=30)
    check("floor=30 只剩高风险账号", [x["email"] for x in r2["rows"]] == ["hig@x.com"],
          str([x["email"] for x in r2["rows"]]))
    check("count 反映命中总数", r2["count"] == 1, str(r2["count"]))

    r3 = LIC.risk_top_impl(st, NOW, limit=1, min_score=1)
    check("limit 生效", len(r3["rows"]) == 1, str(len(r3["rows"])))


# ── [H] risk_clear ───────────────────────────────────────────────────────── #
def test_h_risk_clear() -> None:
    print("[H] risk_clear：清分、保留 exempt、写 cleared_at 与事件")
    st = _state()
    _user(st, "clr@x.com", hb_ring=[NOW - i * 5 for i in range(12, 0, -1)],
          risk={"exempt": True})
    for i in range(4):
        _ev(st, "device_add", NOW - i * 60, email="clr@x.com", fp=f"c{i}")
    out = LIC.risk_clear_impl(st, "clr@x.com", NOW, "误报：测试机")
    check("返回 score 归 0", out["score"] == 0, str(out))
    check("保留 exempt 标记", out["exempt"] is True, str(out))
    risk = st["users"]["clr@x.com"]["risk"]
    check("cleared_at 已写", float(risk.get("cleared_at") or 0) == NOW, str(risk.get("cleared_at")))
    check("updated 已刷新", float(risk.get("updated") or 0) == NOW, str(risk.get("updated")))
    evs = [e for e in st["ev_ring"] if e.get("kind") == "risk_clear"]
    check("写 risk_clear 事件", len(evs) == 1, str(len(evs)))
    check("事件带账号与备注",
          evs and evs[0].get("email") == "clr@x.com" and "误报" in (evs[0].get("note") or ""),
          str(evs[:1]))
    print("      [H2] 清空不消除信号：下一轮 risk_eval 仍现算命中（本批只记不判）")
    _user(st, "clr2@x.com", hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])
    LIC.risk_clear_impl(st, "clr2@x.com", NOW, "误报")
    check("信号仍在 → 分数会回来（cleared_at 供第 2 批抑制响应）",
          _score(st, "clr2@x.com") == 15, str(_score(st, "clr2@x.com")))
    print("      [H3] 不存在账号 → 404 NO_ACCOUNT")
    try:
        LIC.risk_clear_impl(st, "ghost@x.com", NOW, "")
        check("抛 404", False, "未抛错")
    except Exception as e:                       # ApiError(status, code, msg)
        check("抛 404 NO_ACCOUNT",
              getattr(e, "status", None) == 404 and getattr(e, "code", None) == "NO_ACCOUNT",
              f"status={getattr(e, 'status', None)} code={getattr(e, 'code', None)}")


# ── [I][K] 红线：IP 不进风险路径 / 纯观测无自动动作 ─────────────────────── #
def test_i_k_redlines() -> None:
    print("[I] 🔴 红线：IP 永不进风险路径（六信号签名不含 ip）")
    ok = True
    for fn in LIC.RISK_SIGNALS:
        params = list(inspect.signature(fn).parameters)
        if params != ["state", "uid", "user", "now"]:
            ok = False
            check(f"信号 {fn.__name__} 签名 = (state,uid,user,now)", False, str(params))
    check("六信号均不接收 ip 参数", ok)

    print("      [I2] 同 IP 的两个账号互不影响（IP 不是风险维度）")
    st = _state()
    _user(st, "ipa@x.com")
    _user(st, "ipb@x.com")
    for i in range(4):                            # 同 IP 大量设备事件
        _ev(st, "device_add", NOW - i * 60, email="ipa@x.com", fp=f"a{i}", ip="1.2.3.4")
        _ev(st, "device_add", NOW - i * 60, email="ipb@x.com", fp=f"b{i}", ip="1.2.3.4")
    check("各自按自身信号计分（不因同 IP 相互牵连）",
          _score(st, "ipa@x.com") == 20 and _score(st, "ipb@x.com") == 20,
          f"{_score(st, 'ipa@x.com')}/{_score(st, 'ipb@x.com')}")

    print("[K] 🔴 纯观测：risk_eval 返回值不含任何自动动作字段")
    st = _state()
    _user(st, "act@x.com", hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])
    keys = set(LIC.risk_eval(st, "act@x.com", NOW).keys())
    check("无 action/deny/ban/block",
          not (keys & {"action", "deny", "ban", "block", "reject"}), str(keys))
    check("仅含 score/level/reasons/exempt",
          keys <= {"score", "level", "reasons", "exempt"}, str(keys))


# ── [L] 三项基建 ─────────────────────────────────────────────────────────── #
def test_l_primitives() -> None:
    print("[L] 基建：_hb_overdense / _ring_match 窗口 / fp_index 容量")

    print("      [L1] _hb_overdense 取最长连续过密段")
    check("5s×12 → 11 段", LIC._hb_overdense([NOW - i * 5 for i in range(12, 0, -1)]) == 11,
          str(LIC._hb_overdense([NOW - i * 5 for i in range(12, 0, -1)])))
    check("40s 间隔 → 0 段", LIC._hb_overdense([NOW - i * 40 for i in range(12, 0, -1)]) == 0)
    mix = [NOW, NOW - 5, NOW - 10, NOW - 15, NOW - 20, NOW - 400, NOW - 405, NOW - 410]
    check("密集段断开 → 取最长（4）", LIC._hb_overdense(mix) == 4, str(LIC._hb_overdense(mix)))
    check("空/异常输入不炸", LIC._hb_overdense(None) == 0 and LIC._hb_overdense([]) == 0)

    print("      [L2] _ring_match 窗口裁剪：超窗事件不计")
    st = _state()
    _user(st, "win@x.com")
    for i in range(4):                            # 窗口内（6 天前）
        _ev(st, "device_add", NOW - 6 * DAY, email="win@x.com", fp=f"in{i}")
    check("窗口内命中", _score(st, "win@x.com") == 20, str(_score(st, "win@x.com")))
    st2 = _state()
    _user(st2, "win2@x.com")
    for i in range(4):                            # 窗口外（8 天前）
        _ev(st2, "device_add", NOW - 8 * DAY, email="win2@x.com", fp=f"out{i}")
    check("窗口外不命中", _score(st2, "win2@x.com") == 0, str(_score(st2, "win2@x.com")))

    print("      [L3] fp_index 容量淘汰最久未出现")
    st = {"users": {}, "fp_index": {
        f"fp{i}": {"uids": [], "first": 0.0, "last": float(i)} for i in range(1, 5001)}}
    LIC._touch_fp_index(st, "u1", "newfp", 9999.0)
    check("不超上限", len(st["fp_index"]) == LIC.FP_INDEX_CAP, str(len(st["fp_index"])))
    check("淘汰最久未出现（fp1 移除）", "fp1" not in st["fp_index"])
    check("新 fp 记入且含 uid",
          "newfp" in st["fp_index"] and "u1" in st["fp_index"]["newfp"]["uids"],
          str(st["fp_index"].get("newfp")))
    print("      [L4] 同 fp 同账号重复调用 → uids 去重")
    st2 = {"users": {}, "fp_index": {}}
    LIC._touch_fp_index(st2, "same@x.com", "F", NOW)
    LIC._touch_fp_index(st2, "same@x.com", "F", NOW + 1)
    check("uids 无重复", st2["fp_index"]["F"]["uids"] == ["same@x.com"],
          str(st2["fp_index"]["F"]["uids"]))

    print("      [L5] reasons：k 互不重复 + 按权重降序 + 不超上限")
    st = _state()
    _user(st, "ord@x.com",
          devices=[{"fp": "shared", "last_seen": NOW, "integrity": "fail"}],
          hb_ring=[NOW - i * 5 for i in range(12, 0, -1)])
    st["fp_index"] = {"shared": {"uids": ["ord@x.com", "a", "b"], "first": NOW, "last": NOW}}
    rs = _reasons(st, "ord@x.com")
    ks = [r["k"] for r in rs]
    check("k 无重复", len(ks) == len(set(ks)), str(ks))
    check("按权重降序", [r["w"] for r in rs] == sorted([r["w"] for r in rs], reverse=True),
          str([r["w"] for r in rs]))
    check("不超 RISK_REASON_CAP", len(rs) <= LIC.RISK_REASON_CAP, str(len(rs)))


def main() -> int:
    print("=" * 60)
    print("反破解风控 · 风险评分守卫（deploy/license_server.py）")
    print("=" * 60)
    try:
        test_a_signals()
        test_bc_score_and_levels()
        test_d_self_clearing()
        test_e_weak_signal_cannot_trip()
        test_f_exempt()
        test_g_risk_top()
        test_h_risk_clear()
        test_i_k_redlines()
        test_l_primitives()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("=" * 60)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 风险评分守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
