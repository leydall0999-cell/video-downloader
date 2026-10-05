# -*- coding: utf-8 -*-
"""守卫：网页版 AI 积分成本表 + 免费用户「首次体验」策略。

背景（2026-10-05）：
  · 网页版原本**完全没有 AI 计费** —— `membership.gate_message` 定义了却零调用，
    只有去水印/字幕走「云端算力免费 3 次/日」的每日配额。用户拍板：
    **接桌面端同一套 AI 积分 + 账号首次使用免费一次**，并同步后台改价面板。
  · 接上积分墙后，积分池为 0 的新账号第一次点 AI 就撞 402，连"好不好用"都
    没法判断。故加一层首次免费：判定顺序 **先扣积分 → 扣不动才动用名额 → 都不行才拦**。

本守卫钉住七组：
  [A] 默认口径是 once（账号只免一次），只对免费用户
  [B] 首次放行且不扣分；第二次被拦，且**换任何功能也被拦**（once 的要害）
  [C] 名额必须落盘 —— 重建 Store 后仍记得（否则重启即可反复白嫖，最隐蔽的一条）
  [D] 有积分时不消耗名额；exclude / max_cost / 会员豁免
  [E] status() 暴露余量，且 once 下 remaining_count 是 0/1（不是各项求和）
  [F] 后台接线：GET/POST 带策略、脏值当场 400、后台页面与前端 chip 都在
  [G] 所有 AI 扣费入口都经过 gate_message ⇒ 试用逻辑一处生效、全线覆盖

🔴 网页版的成本性质与桌面端**不同**：去水印 LaMa / 字幕 Whisper 在网页端跑在
   服务端 ECS 上，占的是服务器 CPU，不是用户机器 —— 所以这几项在网页版
   更应该计费。守卫里对注释所言与实际落点的对应关系也做了断言。

所有用例都在 VDL_DATA_DIR 临时目录内进行，绝不写真实家目录。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))

_tmp = tempfile.mkdtemp(prefix="vdl_trial_web_")
os.environ["VDL_DATA_DIR"] = _tmp
os.environ["VDL_PLANS_CLOUD"] = "0"
os.environ["VDL_CLOUD_LINK"] = "0"

import membership as M                                    # noqa: E402
from membership import MembershipStore, AI_CREDIT_COSTS   # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  ✅ {name}")
    else:
        FAILS.append(name)
        print(f"  ❌ {name}{(' — ' + extra) if extra else ''}")


def _d() -> pathlib.Path:
    return pathlib.Path(os.environ["VDL_DATA_DIR"])


def _plans() -> pathlib.Path:
    return _d() / "plans.json"


def _write_plans(obj: dict) -> None:
    """写 plans.json 并让覆盖层缓存失效。

    🔴 不能用 `os.utime` 把 mtime 推到未来来逼重读：多次写入时 mtime 会被文件系统
    clamp 到同一个上限值（实测 9223372036854775807），第二次写入 mtime 没变
    ⇒ 缓存命中旧数据，后面的 exclude/max_cost/off 用例会集体读到第一份配置。
    """
    _plans().write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    M._PLAN_OVERRIDE_CACHE = None


def _reset_plans() -> None:
    try:
        _plans().unlink()
    except FileNotFoundError:
        pass
    M._PLAN_OVERRIDE_CACHE = None


def fresh(tag: str):
    _reset_plans()
    p = _d() / f"member_{tag}.json"
    return M, MembershipStore(path=p)


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")


# --------------------------------------------------------------------------- #
def test_a_default_policy() -> None:
    print("\n[A] 默认口径：账号首次使用免费一次，且只对免费用户")
    _reset_plans()
    pol = M.free_trial_policy()
    check("默认开启", pol["enabled"] is True)
    check("默认口径是 once（账号只免一次）", pol["mode"] == "once", f"实际 {pol['mode']}")
    check("默认不给会员（保住付费纪律）", pol["members_too"] is False)
    check("策略键集与代码默认一致",
          set(pol) == set(M.DEFAULT_FREE_TRIAL_POLICY),
          f"{sorted(pol)} vs {sorted(M.DEFAULT_FREE_TRIAL_POLICY)}")
    # 脏值收敛：拼错不能导致全站免单，也不能崩
    _write_plans({"free_trial": {"mode": "per-op"}})
    check("拼错的 mode 收敛回默认口径", M.free_trial_policy()["mode"] == "once")
    _write_plans({"free_trial": {"enabled": "yes"}})
    check("enabled 非布尔被收敛为布尔", isinstance(M.free_trial_policy()["enabled"], bool))
    _reset_plans()


def test_b_first_free_second_blocked() -> None:
    print("\n[B] 首次放行（不扣分）、第二次被拦、且换功能也被拦")
    _reset_plans()
    M, st = fresh("b")
    op = "commentary_llm"
    check("先确认解说确实要钱（否则本用例没意义）", M.credit_cost(op) > 0)

    before = st.status()["credits_total"]
    check("首次放行", M.gate_message(st, op, reason="t") is None)
    check("首次不扣积分", st.status()["credits_total"] == before == 0)

    second = M.gate_message(st, op, reason="t")
    check("第二次被拦", second is not None, "第二次竟还放行 ⇒ 可无限白嫖")
    check("拦的不是冷冰冰的「积分不足」", bool(second) and "免费体验" in second, f"实际：{second!r}")
    check("第三次仍被拦（不会复活）", M.gate_message(st, op, reason="t") is not None)

    # 🔴 once 的要害：换任何功能都应当被拦。这条方向与旧 per_op 口径**相反**。
    other = M.gate_message(st, "dewatermark_ai", reason="t")
    check("once 口径下换功能也被拦", other is not None,
          f"换功能竟还放行（{other!r}）⇒ 实际仍是 per_op")
    check("第三个功能同样被拦", M.gate_message(st, "subtitle_asr", reason="t") is not None)


def test_c_persists_across_reload() -> None:
    print("\n[C] 名额必须落盘：重建 Store 后仍记得已用过")
    _reset_plans()
    M, st = fresh("c")
    check("首次放行", M.gate_message(st, "dewatermark_ai", reason="t") is None)
    st2 = MembershipStore(path=st.path)
    again = M.gate_message(st2, "dewatermark_ai", reason="t")
    check("换 Store 实例读同一文件，仍被拦", again is not None,
          "没拦住 ⇒ 名额只在内存里，重启就能反复薅")
    disk = json.loads(st.path.read_text(encoding="utf-8") or "{}")
    check("free_trials 真的写进了状态文件", bool(disk.get("free_trials")))


def test_d_credits_first_and_filters() -> None:
    print("\n[D] 有积分不烧名额 / exclude / max_cost / 会员豁免")
    M, st = fresh("d1")
    st.add_credits(500, reason="seed")
    check("账户有积分", st.status()["credits_total"] >= 500)
    before = st.status()["credits_total"]
    check("有积分时正常放行", M.gate_message(st, "commentary_llm", reason="t") is None)
    after = st.status()["credits_total"]
    check("有积分时扣的是自己的积分", after < before, f"{before} → {after}")
    check("🔴 有积分时不消耗免费名额", not st.trial_used("commentary_llm"),
          "还有余额却先把名额烧了 ⇒ 一次性资源白送给付费用户")

    # 先 fresh 再写配置：`fresh()` 会 unlink plans.json，反着写等于刚配好就被擦掉
    M, st = fresh("d2")
    _write_plans({"free_trial": {"exclude": ["commentary_llm"]}})
    check("exclude 里的功能不免", M.gate_message(st, "commentary_llm", reason="t") is not None)
    check("exclude 之外的功能照常免", M.gate_message(st, "subtitle_translate", reason="t") is None)

    M, st = fresh("d3")
    _write_plans({"free_trial": {"max_cost": 8}})
    check("单价 > max_cost 的功能不免（解说 40）",
          M.gate_message(st, "commentary_llm", reason="t") is not None)
    check("10 积分的去水印也超上限 ⇒ 同样不免",
          M.gate_message(st, "dewatermark_ai", reason="t") is not None)
    M, st = fresh("d4")
    _write_plans({"free_trial": {"max_cost": 45}})
    check("放宽上限后解说可免", M.gate_message(st, "commentary_llm", reason="t") is None)

    _reset_plans()
    M, st = fresh("d5")
    st.redeem_code("ai_year") if hasattr(st, "redeem_code") else None
    st._ensure_loaded()
    st._state["download_member"]["active"] = True
    st._state["download_member"]["expire_at"] = M.time.time() + 86400
    st._persist()
    check("会员（默认）不享受免费体验",
          M.gate_message(st, "commentary_llm", reason="t") is not None)
    M, stm = fresh("d6")
    stm._ensure_loaded()
    stm._state["download_member"]["active"] = True
    stm._state["download_member"]["expire_at"] = M.time.time() + 86400
    stm._persist()
    _write_plans({"free_trial": {"members_too": True}})
    check("members_too=True 时会员也能用一次",
          M.gate_message(stm, "commentary_llm", reason="t") is None)
    M, st = fresh("off")
    _write_plans({"free_trial": {"mode": "off"}})
    check("口径关闭后立刻回到付费墙",
          M.gate_message(st, "commentary_llm", reason="t") is not None)
    check("关闭后 enabled 也应为假", M.free_trial_policy()["enabled"] is False)
    _reset_plans()


def test_e_status_and_unknown_op() -> None:
    print("\n[E] status() 暴露余量 / once 下 remaining_count 是 0 或 1 / 表外 op 不占名额")
    _reset_plans()
    M, st = fresh("e")
    ft0 = st.status()["free_trials"]
    check("初始余量是 1 而不是计费项总数（once 口径）", ft0["remaining_count"] == 1,
          f"{ft0['remaining_count']} vs op 总数 {len(AI_CREDIT_COSTS)}")
    check("status 里带 mode 供前端展示", ft0["mode"] == "once", f"实际 {ft0['mode']}")
    M.gate_message(st, "commentary_llm", reason="t")
    ft = st.status()["free_trials"]
    check("用掉后余量归零", ft["remaining_count"] == 0, "仍显示有余量 ⇒ 前端会误报")
    check("共用名额：其余项也一并归零", ft["remaining"]["dewatermark_ai"] == 0)
    check("used_any 标记已用", ft["used_any"] is True)
    M.gate_message(st, "不在表里的功能", reason="t")
    check("表外 op 不写 free_trials", st.status()["free_trials"]["remaining_count"] == 0)


def test_f_admin_and_wiring() -> None:
    print("\n[F] 后台接线：GET/POST 带策略、脏值当场 400、前端页面在")
    admin_src = _read("server/routers/admin.py")
    mem_src = _read("server/membership.py")
    index_src = _read("web/admin/index.html")
    appjs_src = _read("web/app.js")

    check("GET 返回 free_trial 策略", "free_trial" in admin_src and "trial_modes" in admin_src)
    check("POST 返回更新后的策略", "credit_cost_table()" in admin_src)
    check("存在载荷校验入口", "_validate_free_trial" in admin_src)
    check("免费用户「首次体验」策略（2026-10-05 晚定档：账号首次使用免费一次）",
          "free_trial" in M._SAVE_TABLE_KEYS,
          "漏了这条 ⇒ 后台「只提交 enabled」会整表替换，其余字段凭空消失")
    check("后台页面存在", "AI 计费后台" in index_src)
    check("后台页面有口径下拉", 'id="trialMode"' in index_src)
    check("后台页面有保存动作", "/api/admin/ai/credit-costs" in index_src)
    check("会员面板展示剩余免费体验", "free_trials" in appjs_src and "首次使用" in appjs_src)

    from routers.admin import _validate_free_trial
    check("合法载荷放行", _validate_free_trial({"enabled": True, "mode": "once"}, AI_CREDIT_COSTS)
          == {"enabled": True, "mode": "once"})
    for bad, label in (({"mode": "per-op"}, "拼错的口径"),
                       ({"exclude": ["不在表里"]}, "exclude 里的未登记项"),
                       ({"typo": 1}, "拼错的策略字段"),
                       ({"max_cost": -1}, "负数 max_cost"),
                       ({"enabled": "yes"}, "enabled 非布尔")):
        try:
            _validate_free_trial(bad, AI_CREDIT_COSTS)
            check(f"非法载荷当场拒绝：{label}", False, "竟然放行了")
        except Exception:
            check(f"非法载荷当场拒绝：{label}", True)
    check("恢复默认支持 null 删除",
          _validate_free_trial({"mode": None}, AI_CREDIT_COSTS) == {"mode": None})


def test_g_all_ai_entries_gated() -> None:
    print("\n[G] 所有 AI 扣费入口都经过 gate_message ⇒ 一处生效全线覆盖")
    mem_src = _read("server/membership.py")
    check("gate_message 内调用了 trial_available", "store.trial_available(op, cost)" in mem_src)
    check("gate_message 内调用了 trial_consume", "store.trial_consume(" in mem_src)
    check("spend_for 也有同口径分支（覆盖不走 gate_message 的调用点）",
          'trial": True' in mem_src)

    expect = {
        "server/routers/commentary.py": ("commentary_llm", 4),
        "server/routers/dewatermark.py": ("dewatermark_ai", 1),
        "server/routers/subtitle.py": ("subtitle_asr", 2),
        "server/routers/subtitles.py": ("subtitle_translate", 1),
    }
    for rel, (op, want) in expect.items():
        src = _read(rel)
        n = src.count(f'"{op}"')
        check(f"{rel} 接了 {op}（{want} 处）", n >= want, f"实际出现 {n} 次")
        check(f"{rel} 走统一扣费收口", "membership.gate_message(" in src)


def test_h_cost_table_integrity() -> None:
    print("\n[H] 成本表自身完整：每项都有 real_cost / where / name，且评论区与多久了对照一致")
    _reset_plans()
    rows = M.credit_cost_table()
    check("表格非空", len(rows) > 0)
    for r in rows:
        ok = bool(r.get("name")) and bool(r.get("where")) and bool(r.get("real_cost"))
        check(f"{r['op']} 字段齐全（含平台真实成本）", ok,
              f"name={r.get('name')!r} where={r.get('where')!r}")
    ops = {r["op"] for r in rows}
    for must in ("commentary_llm", "dewatermark_ai", "subtitle_asr", "subtitle_translate"):
        check(f"网页版实际有的功能已登记：{must}", must in ops)
    check("🔴 网页版不登记桌面端专属的画面理解（本实例无此路由）",
          "commentary_vision" not in ops)


def test_i_unknown_op_warns() -> None:
    print("\n[I] 表外 op 必须告警 —— 静默免费是财务漏洞的温床")
    import logging
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    lg = logging.getLogger("membership")
    h = _Capture(level=logging.WARNING)
    lg.addHandler(h)
    try:
        val = M.credit_cost("一个完全没登记的功能")
    finally:
        lg.removeHandler(h)
    check("表外 op 按 0 处理", val == 0, f"实际 {val}")
    # 🔴 这条是「新功能忘了登记价 ⇒ 静默全站免单」的唯一防线。
    #    0 本身也是合法单价（想把某项改免费就填 0），区分不了，只能靠日志留痕。
    check("表外 op 发出 WARNING 日志",
          any(r.levelno >= logging.WARNING and "未知 op" in r.getMessage() for r in records),
          "没告警 ⇒ 漏登记的功能会静默免费，只能等账单异常才发现")


def main() -> None:
    print("网页版 AI 免费体验守卫")
    print("=" * 62)
    for fn in (test_a_default_policy, test_b_first_free_second_blocked,
               test_c_persists_across_reload, test_d_credits_first_and_filters,
               test_e_status_and_unknown_op, test_f_admin_and_wiring,
               test_g_all_ai_entries_gated, test_h_cost_table_integrity,
               test_i_unknown_op_warns):
        fn()
    print("\n" + "=" * 62)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"   · {f}")
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()
