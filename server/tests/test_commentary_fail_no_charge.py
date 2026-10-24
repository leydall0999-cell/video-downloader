"""server/tests/test_commentary_fail_no_charge.py — 解说「失败不扣积分」守卫（2026-10-23）。

事故经过（用户实测）：
  视频解说在**转写阶段 1 秒崩溃**（安装包内 av 19.0.1 与 faster-whisper 1.2.1 不兼容，
  `av.open()` 不再接受 `metadata_errors` 参数），成片/解说词一个都没产出。
  但 AI 积分早在**开跑前**的 `precheck_or_raise` 里就真扣掉了，而失败路径
  （`_refund_quota_if`）只退云端免费次数与每日次数，**没有任何退积分的通道**
  ⇒ 用户连试两次被白白扣掉 **320 积分**，一个字都没拿到。

修复契约（本文件钉死）：
  1. 预检 `precheck_or_raise` **只读**：余额够就放行、不够就 402，全程零扣减、
     不烧「首次体验」名额（绝不能用会真扣的 `gate_message` 当探针）。
  2. 预检把「账单」(`billing`) 回传给调用方，调用方存进 `commentary_jobs[job_id]`。
  3. **只有产出成功**（`status="completed"` 或 `status="script_ready"`）才调
     `app._commentary_charge_on_success(job_id)` 落账；失败路径根本走不到那里。
  4. 落账幂等：`billing["charged"]` 置位后重复调用不再扣（脚本审核后渲染复用账
     单、或分支重入都靠它兜住）。

设计约束：全部用临时目录里的 `MembershipStore`，并把 `user_membership.store_for_owner`
换成替身 ⇒ 绝不碰用户真实 `~/.video-downloader` 的积分账本。

运行：
    cd server && python tests/test_commentary_fail_no_charge.py
    cd server && python -m pytest tests/test_commentary_fail_no_charge.py -v
"""
import os
import pathlib
import shutil
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402  先完成 app 初始化，避免 routers 循环导入
import membership as mem  # noqa: E402
import user_membership as umem  # noqa: E402
from routers import quota as quota_router  # noqa: E402

_TMPS: list[str] = []
_PASS = 0
_FAILS: list[str] = []


def check(label: str, ok: bool, hint: str = "") -> None:
    global _PASS
    if ok:
        _PASS += 1
        print(f"  ✅ {label}")
    else:
        _FAILS.append(label)
        print(f"  ❌ {label}" + (f" — {hint}" if hint else ""))


def _new_store(credits: int = 0) -> "mem.MembershipStore":
    """一个全新、与真实家目录完全隔离的会员 store。"""
    d = tempfile.mkdtemp(prefix="vdl_nofee_")
    _TMPS.append(d)
    st = mem.MembershipStore(path=pathlib.Path(d) / "member.json")
    if credits > 0:
        st.add_credits(credits, reason="test_seed")
    return st


def _balance(st: "mem.MembershipStore") -> int:
    bal = st.credits_balance() or {}
    # 永久池 + 赠送池合计，避免只盯一个池而误判
    return int(bal.get("perm", 0)) + int(bal.get("gift", 0)) + int(bal.get("total", 0))


class _StoreProxy:
    """包一层 store：只把「每日免费次数」闸门强制放行，其余全部透传。

    🔴 为什么必须包：真实 `~/.video-downloader/plans.json` 里解说（`cloud_commentary`）
    的**免费档日配额被配成 0**（解说本就是会员/积分功能，免费档不开放）。本文件验的是
    「AI 积分扣不扣」，若不摘掉这道闸门，每个「应放行」的用例都会先撞上日配额 402
    （`test_commentary_precheck.py` 里那条既有失败就是同一个原因）。
    """

    def __init__(self, inner: "mem.MembershipStore"):
        self._inner = inner

    def quota_state(self, resource: str, *a, **k) -> dict:
        d = dict(self._inner.quota_state(resource, *a, **k) or {})
        d["allowed"] = True
        return d

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _Patch:
    """把预检的外部依赖换成替身：隔离真实家目录、真实引擎探测、真实 store 解析。"""

    def __init__(self, store: "mem.MembershipStore", daily_ok: bool = True):
        self.store = _StoreProxy(store)   # 摘掉日配额闸门，其余透传
        self.raw = store
        self.daily_ok = daily_ok

    def __enter__(self):
        self._qm = quota_router.get_quota_manager
        self._ready = quota_router._local_engine_ready
        self._appmod = quota_router.app_module
        self._sfo = umem.store_for_owner

        class _FakeQM:
            def __init__(self, ok):
                self._ok = ok

            def precheck_commentary(self, duration_sec, local_engine_ready=False,
                                    engine="", resource="cloud_commentary"):
                return {"allowed": True, "reason": "", "hint": "", "code": "",
                        "engine": engine, "daily_ok": self._ok}

            def status(self):
                return {"is_member": False, "free_max_duration_sec": 1800.0,
                        "cloud_lifetime_remaining": {"cloud_commentary": 99}}

        class _FakeApp:
            store = self.store

            @staticmethod
            def current_member_store(_request=None):
                return _FakeApp.store

            @staticmethod
            def _task_owner(_request=None):
                return "test-owner"

        quota_router.get_quota_manager = lambda request=None: _FakeQM(self.daily_ok)
        quota_router._local_engine_ready = lambda engine="": False   # 强制按云端价
        quota_router.app_module = lambda: _FakeApp()
        umem.store_for_owner = lambda owner="": self.store
        return self

    def __exit__(self, *exc):
        quota_router.get_quota_manager = self._qm
        quota_router._local_engine_ready = self._ready
        quota_router.app_module = self._appmod
        umem.store_for_owner = self._sfo
        return False


# ── 1. 预检只读：跑完一分不扣 ─────────────────────────────────────────── #
def test_precheck_is_read_only() -> None:
    print("\n[1] 预检只查不扣（这是「失败不扣积分」的根本）")
    st = _new_store(credits=500)
    before = _balance(st)
    with _Patch(st):
        res = quota_router.precheck_or_raise(None, 600.0, vision=True)
    after = _balance(st)
    check("预检放行并返回账单 billing", isinstance(res, dict) and bool(res.get("billing")))
    check("预检后积分**一分未动**", after == before, f"{before} → {after}")
    bill = res.get("billing") or {}
    check("账单带 op / cost（落账时才知道扣多少）",
          bool(bill.get("op")) and int(bill.get("cost", 0)) > 0, str(bill))
    check("勾了画面理解 → 账单带 vision_cost",
          int(bill.get("vision_cost", 0)) > 0, str(bill))
    check("账单未标记已扣费", not bill.get("charged"))


# ── 2. 余额不足：开跑前就 402，且仍一分不扣 ────────────────────────────── #
def test_precheck_blocks_without_charging() -> None:
    print("\n[2] 余额不足 → 开跑前 402（不让用户白等十几分钟），且零扣减")
    st = _new_store(credits=1)
    # 🔴 先烧掉「首次体验」名额：免费用户付不起时系统会给一次免费体验（正确行为），
    # 那不是本用例要验的东西。本用例要验的是「连试用都没有时，开跑前就该被拦」。
    st.trial_consume("commentary_llm", reason="test_burn")
    st.trial_consume("commentary_vision", reason="test_burn")
    before = _balance(st)
    from fastapi import HTTPException
    raised = None
    with _Patch(st):
        try:
            quota_router.precheck_or_raise(None, 7200.0, vision=True)   # 2 小时长片
        except HTTPException as e:
            raised = e
    check("余额不足抛 402", raised is not None and raised.status_code == 402,
          str(raised.status_code) if raised else "没抛")
    check("拒绝时 detail 带 MEMBER_QUOTA 前缀（前端弹会员中心）",
          raised is not None and "MEMBER_QUOTA" in str(raised.detail))
    check("被拒也**不扣任何积分**", _balance(st) == before, f"{before} → {_balance(st)}")


# ── 3. 产出成功才落账 ──────────────────────────────────────────────────── #
def test_charge_only_on_success() -> None:
    print("\n[3] 产出成功 → charge_commentary_on_success 真扣费")
    st = _new_store(credits=500)
    before = _balance(st)
    with _Patch(st):
        bill = quota_router.commentary_billing(None, "cloud", 1800.0, vision=True)
        res = quota_router.charge_commentary_on_success(bill)
    spent = before - _balance(st)
    expect = int(bill.get("cost", 0)) + int(bill.get("vision_cost", 0))
    check("落账返回已扣项", bool((res or {}).get("charged")), str(res))
    check(f"扣减额 = 解说 {bill.get('cost')} + 画面理解 {bill.get('vision_cost')} = {expect}",
          spent == expect, f"实扣 {spent}")


# ── 4. 任务级幂等：同一个 job 重复落账不重复扣 ──────────────────────────── #
def test_job_charge_is_idempotent() -> None:
    print("\n[4] _commentary_charge_on_success 幂等（脚本审核后渲染不会二次扣）")
    st = _new_store(credits=500)
    before = _balance(st)
    job_id = "testjob0001"
    with _Patch(st):
        bill = quota_router.commentary_billing(None, "cloud", 1800.0, vision=False)
        server_app.commentary_jobs[job_id] = {"status": "completed", "billing": dict(bill)}
        server_app._commentary_charge_on_success(job_id)
        mid = _balance(st)
        server_app._commentary_charge_on_success(job_id)
        server_app._commentary_charge_on_success(job_id)
    after = _balance(st)
    check("首次落账扣了", mid < before, f"{before} → {mid}")
    check("重复调用**不再扣**", after == mid, f"{mid} → {after}")
    check("job 账单标记 charged=True",
          bool((server_app.commentary_jobs[job_id]["billing"] or {}).get("charged")))
    server_app.commentary_jobs.pop(job_id, None)


# ── 5. 失败任务：账单在手里但从未落账 ──────────────────────────────────── #
def test_failed_job_never_charges() -> None:
    print("\n[5] 失败任务不落账（事故场景复现：转写 1 秒崩溃）")
    st = _new_store(credits=500)
    before = _balance(st)
    job_id = "testjob0002"
    with _Patch(st):
        bill = quota_router.commentary_billing(None, "cloud", 2700.0, vision=True)
        server_app.commentary_jobs[job_id] = {"status": "failed", "billing": dict(bill),
                                              "error": "解说管线退出码 1"}
        # 失败路径只会标 failed，**不会**调用落账口；这里显式证明它没被调用过
        check("失败任务账单未标记 charged",
              not (server_app.commentary_jobs[job_id]["billing"] or {}).get("charged"))
    check("失败任务积分**一分未动**", _balance(st) == before, f"{before} → {_balance(st)}")
    server_app.commentary_jobs.pop(job_id, None)


# ── 6. 空账单（老任务 / 预检异常 fail-open）不误扣 ──────────────────────── #
def test_empty_billing_no_charge() -> None:
    print("\n[6] 无账单不扣（fail-open 绝不反向变成乱扣）")
    st = _new_store(credits=500)
    before = _balance(st)
    job_id = "testjob0003"
    with _Patch(st):
        server_app.commentary_jobs[job_id] = {"status": "completed", "billing": {}}
        server_app._commentary_charge_on_success(job_id)
    check("空账单不扣分", _balance(st) == before, f"{before} → {_balance(st)}")
    server_app.commentary_jobs.pop(job_id, None)


# ── 7. 只读校验口径必须与真扣费一致 ────────────────────────────────────── #
def test_can_afford_matches_gate_message() -> None:
    print("\n[7] can_afford(total=...) 与 gate_message(cost=...) 同口径")
    st = _new_store(credits=100)
    st.trial_consume("commentary_llm", reason="test_burn")   # 同上：排除试用干扰
    # 余额够：只读校验说行，真扣也确实成功
    check("余额够 → can_afford 放行", mem.can_afford(st, "commentary_llm", total=100) == "")
    # 余额不够：只读校验说不行，且**没有**把余额扣掉
    check("余额不够 → can_afford 拒绝", mem.can_afford(st, "commentary_llm", total=101) != "")
    check("只读校验后余额不变", _balance(st) == 100, str(_balance(st)))
    # 反例：不传 total 会按表内单价（1 个单位）误判「付得起」
    check("不传 total 时按单价判（长片会误放行 ⇒ 必须传总额）",
          mem.can_afford(st, "commentary_llm") == "")


def main() -> int:
    print("=" * 62)
    print("解说「失败不扣积分」守卫")
    print("=" * 62)
    try:
        test_precheck_is_read_only()
        test_precheck_blocks_without_charging()
        test_charge_only_on_success()
        test_job_charge_is_idempotent()
        test_failed_job_never_charges()
        test_empty_billing_no_charge()
        test_can_afford_matches_gate_message()
    finally:
        for d in _TMPS:
            shutil.rmtree(d, ignore_errors=True)
    print()
    if _FAILS:
        print(f"❌ 失败 {len(_FAILS)} 项：")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print(f"🎉 {_PASS} 项全部通过 —— 预检只读、成功才扣、失败不扣")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
