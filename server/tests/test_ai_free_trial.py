"""免费用户「首次体验」守卫（2026-10-05 用户定档）。

背景：把 AI 积分墙补齐（test_ai_credit_costs.py 那一轮）之后，**积分池为 0 的免费用户
任何 AI 功能第一次点就被 402 拦住** —— 连"这东西到底好不好用"都没机会知道，漏斗在第一
步就断了。用户定档：**每个功能各免一次**，口径可在后台改（per_op / once / off）。

本守卫钉住这五件事（都是「看起来实现了、实际不生效」的高发区）：

[A] 默认口径 = 每个功能各免一次，且**只对免费用户**（会员默认不给，避免松开付费纪律）
[B] 真的能免：首次 gate_message 放行且不扣积分；**第二次必须拦**，且文案要说清
    「该功能的免费体验已用过」（不能只丢「积分不足」——用户上次明明跑通过）
[C] 名额必须具备**跨进程持久性**：重建 Store 仍能読回「已用过」（只存在内存 = 重启白薅）
[D] 有积分时不消耗名额；once 口径下一个 op 用掉即全站用完；exclude / max_cost 生效
[E] 后台接线完整：GET/POST 都带策略、非法口径与拼错的键当场 400（写进去会静默失效）

跑法：`../.build_venv/bin/python tests/test_ai_free_trial.py`
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # server/
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="vdl_trial_")
os.environ["VDL_DATA_DIR"] = TMP
os.environ["VDL_PLANS_CLOUD"] = "0"   # 离线必须关云端，否则会连真授权中心

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + extra) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def _read(name: str) -> str:
    p = REPO / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _write_plans(patch: dict) -> None:
    """直接写 plans.json 覆盖层，模拟后台改了配置。"""
    p = Path(TMP) / "plans.json"
    try:
        cur = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        cur = {}
    cur.update(patch)
    p.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")


def _reset_plans() -> None:
    p = Path(TMP) / "plans.json"
    if p.exists():
        p.unlink()
    import membership as M
    M._PLAN_OVERRIDE_CACHE = None


def fresh(tag: str = "u"):
    """每个用例一套干净状态文件，互不干扰。"""
    import membership as M
    st = M.MembershipStore(path=Path(TMP) / f"membership_{tag}.json")
    return M, st


# ─────────────────────────────────────────────────────────────────────────── #
def test_a_default_policy() -> None:
    print("\n[A] 默认口径：账号首次使用免费一次，且只对免费用户")
    _reset_plans()
    import membership as M
    pol = M.free_trial_policy()
    check("默认开启", pol["enabled"] is True)
    check("默认口径是「账号只免一次」（2026-10-05 晚定档）",
          pol["mode"] == "once", f"实际 {pol['mode']}")
    check("默认不给会员（保住付费纪律）", pol["members_too"] is False)
    check("默认不排除任何功能", list(pol["exclude"]) == [])
    check("策略键集与代码默认一致",
          set(pol) == set(M.DEFAULT_FREE_TRIAL_POLICY),
          f"{sorted(pol)} vs {sorted(M.DEFAULT_FREE_TRIAL_POLICY)}")
    # 脏值收敛：mode 拼错不能导致全站免单，也不能崩
    _write_plans({"free_trial": {"mode": "per-op"}})
    check("拼错的 mode 收敛回默认口径", M.free_trial_policy()["mode"] == "once")
    _write_plans({"free_trial": {"enabled": "yes"}})
    check("enabled 非布尔也被收敛为布尔", isinstance(M.free_trial_policy()["enabled"], bool))
    _reset_plans()


def test_b_first_run_free_second_blocked() -> None:
    print("\n[B] 首次放行（不扣分）、第二次必须拦且文案说明「体验已用过」")
    _reset_plans()
    M, st = fresh("b")
    op = "matting_cloud"
    cost = M.credit_cost(op)
    check("先确认该功能确实要钱（否则本用例没意义）", cost > 0, f"cost={cost}")

    before = st.status()["credits_total"]
    first = M.gate_message(st, op, reason="t")
    check("首次放行", first is None, f"实际返回 {first!r}")
    check("首次不扣积分", st.status()["credits_total"] == before == 0)

    second = M.gate_message(st, op, reason="t")
    check("第二次被拦", second is not None, "第二次竟还放行 ⇒ 可无限白嫖")
    check("拦的不是冷冰冰的「积分不足」", bool(second) and "免费体验" in second,
          f"实际文案：{second!r}")
    third = M.gate_message(st, op, reason="t")
    check("第三次仍被拦（不会复活）", third is not None)

    # 🔴 once 口径的要害：**换任何功能都应当被拦**。这条断言的方向与旧 per_op 口径
    #    正好相反 —— 默认口径一改，这里若不跟着反，守卫就成了旧口径的守墓人。
    other = M.gate_message(st, "local_matting_ai", reason="t")
    check("once 口径下换另一个功能也被拦", other is not None,
          f"换功能竟还放行（返回 {other!r}）⇒ 实际仍是 per_op，默认口径改动没生效")
    anytime = M.gate_message(st, "subtitle_asr", reason="t")
    check("第三个功能同样被拦", anytime is not None)


def test_c_persists_across_reload() -> None:
    print("\n[C] 名额必须落盘：重建 Store 后仍记得已用过（否则重启即可白薅）")
    _reset_plans()
    M, st = fresh("c")
    check("首次放行", M.gate_message(st, "dewatermark_ai", reason="t") is None)
    st2 = M.MembershipStore(path=st.path)
    again = M.gate_message(st2, "dewatermark_ai", reason="t")
    check("换一个 Store 实例读同一状态文件，仍被拦", again is not None,
          "没拦住 ⇒ 名额只在内存里，重启 App 就能反复薅")
    disk = json.loads(Path(st.path).read_text(encoding="utf-8") or "{}")
    check("free_trials 真的写进了状态文件", bool((disk.get("free_trials") or {})))


def test_d_policy_variants() -> None:
    print("\n[D] 有积分不烧名额 / once 口径 / exclude / max_cost / 会员豁免")
    _reset_plans()
    M, st = fresh("d")
    st.add_credits(500, reason="case")
    check("先确认账户有钱", st.status()["credits_total"] >= 50)
    check("有积分时正常扣费", M.gate_message(st, "matting_cloud", reason="t") is None)
    check("有积分时不烧名额（名额留给更贵的功能）", not st.trial_used("matting_cloud"),
          "有钱却先把免费名额用掉了 ⇒ 名额被浪费")
    check("积分确实被扣掉", st.status()["credits_total"] == 450,
          f"实际余额 {st.status()['credits_total']}")

    # once：用掉一个 ⇒ 全站都没了
    _write_plans({"free_trial": {"mode": "once"}})
    M, st = fresh("once")
    check("once 模式下首个功能放行", M.gate_message(st, "subtitle_asr", reason="t") is None)
    check("once 模式下第二个功能被拦",
          M.gate_message(st, "voice_clone", reason="t") is not None,
          "once 失效 ⇒ 实际仍是 per_op")
    _reset_plans()

    # exclude = 把最贵的解说/画面理解排除掉
    _write_plans({"free_trial": {"exclude": ["commentary_vision"]}})
    M, st = fresh("ex")
    blocked = M.gate_message(st, "commentary_vision", reason="t")
    check("exclude 里的功能不免", blocked is not None, "排除项竟然还免费")
    check("exclude 之外的功能照常免", M.gate_message(st, "matting_vision", reason="t") is None)
    _reset_plans()

    # max_cost：只给便宜功能免一次
    _write_plans({"free_trial": {"max_cost": 20}})
    M, st = fresh("mc")
    check("单价 > max_cost 的功能不免（50 积分的云端抠图）",
          M.gate_message(st, "matting_cloud", reason="t") is not None)
    check("单价 ≤ max_cost 的功能免（10 积分的本地抠图）",
          M.gate_message(st, "local_matting_ai", reason="t") is None)
    _reset_plans()

    # 会员：默认不给，members_too=True 才给
    M, st = fresh("mem")
    st._ensure_loaded()
    st._state["ai_member"]["active"] = True
    st._state["ai_member"]["expire_at"] = 9e9
    check("会员（默认）不享受免费体验",
          M.gate_message(st, "matting_cloud", reason="t") is not None,
          "会员积分耗尽仍能白用 ⇒ 付费纪律被自己松开")
    _write_plans({"free_trial": {"members_too": True}})
    check("members_too=True 时会员也能用一次",
          M.gate_message(st, "matting_cloud", reason="t") is None)
    _reset_plans()

    # off = 恢复「第一次就撞墙」的旧行为
    _write_plans({"free_trial": {"mode": "off"}})
    M, st = fresh("off")
    check("口径关闭后立刻回到付费墙", M.gate_message(st, "matting_cloud", reason="t") is not None)
    check("关闭后 enabled 也应为假", M.free_trial_policy()["enabled"] is False)
    _reset_plans()


def test_e_status_and_unknown_op() -> None:
    print("\n[E] status() 暴露余量 / 表外 op 不占名额")
    _reset_plans()
    M, st = fresh("e")
    ft0 = st.status()["free_trials"]
    n0 = ft0["remaining_count"]
    # 🔴 once 口径下 `remaining` 逐 op 算出来每项都是 1（共用一个名额），若直接
    #    sum 就会得出「还剩 11 次」——这是口径改动最容易被带出去的错，必须钉住。
    check("初始余量是 1 而不是计费项总数（once 口径）", n0 == 1,
          f"{n0} vs op 总数 {len(M.AI_CREDIT_COSTS)}")
    check("status 里带 mode 供前端展示", ft0["mode"] == "once", f"实际 {ft0['mode']}")
    M.gate_message(st, "voice_clone", reason="t")
    ft = st.status()["free_trials"]
    check("用掉那一项后余量归零", ft["remaining"]["voice_clone"] == 0)
    check("共用名额：其余项也一并归零", ft["remaining"]["dewatermark_ai"] == 0)
    check("remaining_count 归零（不是减一）", ft["remaining_count"] == 0,
          f"实际 {ft['remaining_count']} —— 说明仍在按 per_op 求和")
    check("used_any 标记已用", ft["used_any"] is True)
    # 表外 op：按 0 分免费放行，但**不该**占用任何名额
    M.gate_message(st, "不存在的计费项", reason="t")
    check("表外 op 不写 free_trials", st.status()["free_trials"]["remaining_count"] == 0)


def test_f_admin_wiring() -> None:
    print("\n[F] 后台接线：GET/POST 带策略、脏值当场 400、前端控件在")
    admin_src = _read("server/routers/admin.py")
    mem_src = _read("server/membership.py")
    appjs = _read("web/app.js")
    index = _read("web/index.html")

    check("GET 返回 free_trial 策略", '"free_trial": pol' in admin_src)
    check("POST 返回更新后的策略", '"free_trial": free_trial_policy()' in admin_src)
    check("POST 校验入口存在", "_validate_free_trial(trial_in, AI_CREDIT_COSTS)" in admin_src)
    check("overrides 可持久化 free_trial 键", '"free_trial"' in mem_src and
          '"free_trial"' in mem_src.split("_SAVE_TABLE_KEYS")[1][:300])
    _reset_plans()
    from routers.admin import _validate_free_trial
    from membership import AI_CREDIT_COSTS
    ok = _validate_free_trial({"enabled": True, "mode": "once"}, AI_CREDIT_COSTS)
    check("合法载荷放行", ok == {"enabled": True, "mode": "once"}, str(ok))
    for bad, label in (({"mode": "per-op"}, "拼错的口径"),
                       ({"exclude": ["不存在的功能"]}, "exclude 里的未登记项"),
                       ({"打错的键": 1}, "拼错的策略字段"),
                       ({"max_cost": -1}, "负数 max_cost")):
        raised = False
        try:
            _validate_free_trial(bad, AI_CREDIT_COSTS)
        except Exception:  # noqa: BLE001
            raised = True
        check(f"非法载荷当场拒绝：{label}", raised,
              "写进去后会在读取时被静默收敛 ⇒ 管理员以为配了其实没生效")
    check("恢复默认支持 null 删除", _validate_free_trial({"mode": None}, AI_CREDIT_COSTS) == {"mode": None})

    check("前端渲染试用策略控件", "adminTrialMode" in index)
    check("前端有保存策略的动作", "free_trial" in appjs)
    # 用户侧：要在**撞到付费墙之前**就看见自己还剩几次免费，不然这功能等于没有
    check("会员面板展示剩余免费体验次数",
          "free_trials" in appjs and "remaining_count" in appjs,
          "后端已经在 status() 里下发余量了，前端不展示等于用户永远不知道有这回事")
    css = _read("web/styles.css")
    check("试用 chip 有对应样式", ".member-chip-trial" in css)


def test_g_gate_is_the_single_choke_point() -> None:
    print("\n[G] 所有扣费入口都经过 gate_message ⇒ 试用逻辑一处生效全线覆盖")
    _reset_plans()
    mem_src = _read("server/membership.py")
    # 反向钉：如果有人绕过收口直接 spend_credits，新功能就不会有免费体验
    for op_entry in ("trial_available", "trial_consume"):
        check(f"gate_message 内调用了 {op_entry}",
              op_entry in mem_src.split("def gate_message")[1].split("\ndef ")[0])
    srcs = {
        "server/routers/matting.py": "gate_message",
        "server/routers/dewatermark.py": "gate_message",
        "server/routers/subtitle.py": "gate_message",
        "server/routers/subtitles.py": "gate_message",
        "server/routers/quota.py": "charge_commentary_credits",
    }
    for f, sym in srcs.items():
        check(f"{Path(f).name} 走统一扣费收口", sym in _read(f))


def main() -> int:
    print("=" * 60)
    print("免费用户「首次体验」守卫")
    print("=" * 60)
    test_a_default_policy()
    test_b_first_run_free_second_blocked()
    test_c_persists_across_reload()
    test_d_policy_variants()
    test_e_status_and_unknown_op()
    test_f_admin_wiring()
    test_g_gate_is_the_single_choke_point()
    print("")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过：")
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
