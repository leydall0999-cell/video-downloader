# -*- coding: utf-8 -*-
"""云端抠图「积分不足」必须**不升级 + 有提示**（2026-10-06 A+B 段回归）

## 背景（用户提问触发）
用户看到后台「一键抠图 / 选云端抠图时按 AI 积分计费（云端抠图 50 积分）」这一行，
问「没积分会提示吗」。查证结果：三条路径里**两条有 402 拦截**（显式选云端、本机
抠图），但「本地效果差 → 自动升级云端」是**事后扣费**：扣不到只记后端 warning，
图照出、界面零提示 ⇒ 积分用完的用户白嫖一次且不知情。

## 用户定档：A + B 一起做
- **B（不白嫖）**：自动升级**之前**先探积分（只查不扣）。不够就**不升级**，
  继续走本机兜底 —— 不存在「出了云端图却没扣分」。
- **A（有提示）**：把「因积分不足没升级 / 云端跑了没扣分」回传前端，明确告知
  并给购买入口（`openMemberCenter`）。

## 本守卫钉住
① `routers/matting.py` 记账钩子带 `can_charge`（只查不扣）探针，且探针挂在
   **函数属性**上（注入进 job 的是 hook 函数本身，不是 box）；
② `matting_ai.py` 升级前调用探针；积分不足时置 `_force_cloud=False` +
   `meta["cloud_escalate_skipped"]` + `meta["_no_cloud_credit"]`；
③ **所有**云端分支都要看 `_no_cloud_credit`（人像 MediaKit / 通用 MediaKit /
   cv 兜底共 4 处），否则「不升级」只挡住第一处、本地再失败仍会滑到云端；
④ 云端真跑但没扣过 → 兜底闸门先扣（幂等），失败写 `cloud_charge_error`；
⑤ 状态端点回传 `cloud_escalate_skipped` / `cloud_charge_error`；
⑥ 前端两条提示分支 + 购买入口。

运行：cd server && python tests/test_matting_credit_gate.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
_SERVER = _HERE.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  —— " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _src(name: str) -> str:
    return (_SERVER / name).read_text(encoding="utf-8")


def test_hook_exposes_probe() -> None:
    print("\n[A] 记账钩子带只查不扣的探针")
    src = _src("routers/matting.py")
    check("定义了 can_charge 探针", "def can_charge(op: str)" in src)
    # 🔴 反向断言：探针**禁止**用 gate_message —— 它会真扣积分、还会烧掉
    #   「首次体验」名额（实测 0 积分用户探测后日志出现 free trial consumed）。
    check("探针用零副作用的 can_afford（不是 gate_message）",
          "_mem.can_afford(st, op)" in src)
    check("探针没有误用 gate_message（会烧免费名额）",
          "matting_escalate_probe" not in src)
    # 🔴 注入进 job 的是 hook **函数**，探针必须挂函数属性，否则工作线程拿不到
    check("探针挂在 hook 函数属性上（_hook.can_charge）",
          "_hook.can_charge = can_charge" in src)
    check("幂等状态挂在函数属性上（_hook.state）", "_hook.state = box" in src)


def test_escalation_probes_before_charge() -> None:
    print("\n[B] 自动升级前先探积分：不够就不升级（B 段）")
    src = _src("matting_ai.py")
    check("升级分支里调用了 can_charge 探针",
          '_probe("matting_cloud")' in src)
    check("积分不足时把 _force_cloud 置回 False（不升级）",
          "_force_cloud = False" in src)
    check("置 _no_cloud_credit 标志（守住后续所有云端分支）",
          'meta["_no_cloud_credit"] = True' in src)
    check("记录 cloud_escalate_skipped 供前端提示",
          'meta["cloud_escalate_skipped"]' in src)
    check("探针异常按「够用」处理（不误伤正常用户）",
          src.count("except Exception:  # noqa: BLE001") >= 1)


def test_all_cloud_branches_guarded() -> None:
    print("\n[C] 所有云端入口都必须看 _no_cloud_credit（否则不升级形同虚设）")
    src = _src("matting_ai.py")
    guards = src.count('_no_cloud_credit')
    # 1 处赋值 + 4 处分支守卫（人像优先 / 人像 human / 通用 / cv 兜底）+ 兜底闸门 2 处
    check(f"守卫覆盖充分（当前出现 {guards} 次，至少 6）", guards >= 6,
          "少一处就可能仍滑到云端")
    for name, needle in (
        ("人像优先 MediaKit", 'if not (meta or {}).get("_no_cloud_credit") and is_cloud_matting_mediakit_ready():'),
        ("通用 MediaKit", 'elif (not (meta or {}).get("_no_cloud_credit")) and is_cloud_matting_mediakit_ready():'),
        ("cv 视觉智能兜底", 'if not (meta or {}).get("_no_cloud_credit") and is_cloud_matting_ready():'),
    ):
        check(f"{name} 有守卫", needle in src)


def test_fallback_gate_charges() -> None:
    print("\n[D] 云端真跑但没扣过 → 兜底闸门先扣（覆盖直落云端的兄弟漏洞）")
    src = _src("matting_ai.py")
    check("有兜底闸门（探针 + 幂等扣费）",
          '_cb0 = (meta or {}).get("on_cloud_charge")' in src)
    check("扣费前判幂等（已扣不重复）",
          'not (_st.get("already_charged") or _st.get("charged"))' in src)
    check("记账失败写 cloud_charge_error（A 段回传）",
          'meta["cloud_charge_error"] = str(_e)' in src)


def test_status_endpoint_exposes_fields() -> None:
    print("\n[E] 状态端点把两个诊断字段回传前端")
    src = _src("routers/matting.py")
    check("回传 cloud_escalate_skipped", '"cloud_escalate_skipped"' in src)
    check("回传 cloud_charge_error", '"cloud_charge_error"' in src)


def test_frontend_shows_notice() -> None:
    print("\n[F] 前端有提示且能一键购买（A 段的落点）")
    js = (_SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")
    check("有 cloud_escalate_skipped 提示分支", "d.cloud_escalate_skipped" in js)
    check("提示文案说明「未使用云端精修」", "未使用云端精修" in js)
    check("有 cloud_charge_error 提示分支", "d.cloud_charge_error" in js)
    check("两条提示都带购买入口（openMemberCenter）",
          js.count("openMemberCenter") >= 2)


# ── 功能级：探针语义（静态守卫挡不住「探针有没有副作用」）──────────────────
def test_probe_semantics_functional() -> None:
    print("\n[G] 探针语义：够用才放行 / 名额不被烧 / 零副作用（功能级）")
    import time
    import app  # noqa: F401 — 先完成 app 初始化，避免 routers 循环导入
    import routers.matting as M
    import membership as mem

    base = pathlib.Path(tempfile.mkdtemp(prefix="vdl_mcg_"))

    def mk(name, credits, trial_used=False):
        st = mem.MembershipStore(path=base / name)
        st._ensure_loaded()
        st._state["ai_member"] = {"active": False, "expire_at": 0.0,
                                  "credits_left": credits, "grant_credits": 0,
                                  "feature_credits": {}}
        st._state["download_member"] = {"active": False, "plan": None, "expire_at": 0.0}
        if trial_used:
            st._state["free_trials"] = {"*": time.time()}
        st._persist()
        return st

    # ① 有积分 → 可升级
    h1 = M._make_cloud_charge_hook(None, store=mk("a.json", 500))["hook"]
    check("有积分时探针放行", h1.can_charge("matting_cloud") == "")
    # ② 0 积分但有首次体验名额 → 放行，**且名额不能被探针烧掉**
    st2 = mk("b.json", 0)
    h2 = M._make_cloud_charge_hook(None, store=st2)["hook"]
    check("有免费名额时探针放行", h2.can_charge("matting_cloud") == "")
    check("探针没有烧掉免费名额（gate_message 才会）",
          st2.trial_available("matting_cloud", 50) is True)
    # ③ 0 积分 + 名额已用 → 必须拒绝（B 段拦的就是这一档）
    h3 = M._make_cloud_charge_hook(None, store=mk("c.json", 0, True))["hook"]
    check("积分用尽时探针给出原因（→ B 段不升级）", bool(h3.can_charge("matting_cloud")))
    # ④ 零副作用：连探 3 次状态不变
    st4 = mk("d.json", 0, True)
    snap = dict(st4._state)
    for _ in range(3):
        mem.can_afford(st4, "matting_cloud")
    check("can_afford 零副作用", dict(st4._state) == snap)


def main() -> int:
    print("=== 云端抠图积分闸门守卫（A+B 段）===")
    test_hook_exposes_probe()
    test_escalation_probes_before_charge()
    test_all_cloud_branches_guarded()
    test_fallback_gate_charges()
    test_status_endpoint_exposes_fields()
    test_frontend_shows_notice()
    test_probe_semantics_functional()
    print("\n" + "=" * 46)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 积分不足时：不升级云端（B）+ 明确提示并引导购买（A）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
