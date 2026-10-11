"""积分口径一致性 + 停滞看门狗终态 回归守卫（2026-10-11 全功能实测发现）。

覆盖三处实测缺陷（详见 VDL_全功能实测报告_2026-10-11）：

- F1 `add_credits(pool="ai")` 给非活跃账号充 AI 池 → 账面显示 N、扣费报「当前 0」。
  修复：非活跃账号的 AI 池正授予直接拒绝；被拒时 admin_store 不得再推云端。
- F2 看门狗停滞只写步骤、不落任务终态 → 任务僵在 downloading 直到硬超时（最长 2h），
  DELETE 清不掉、白占并发槽。修复：抽 `_finalize_terminated`，主等待循环短切片 join
  并在看到 cancel_requested 时立即落终态。
- F3 设备锁定时 `credits_total` 未随 AI 池置零重算 → 账面残留已不可用的旧值。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp())

import downloader  # noqa: E402
from membership import MembershipStore  # noqa: E402
from tasks import TaskStore  # noqa: E402


def _new_store():
    st = MembershipStore(path=Path(tempfile.mkdtemp()) / "m.json")
    st._ensure_loaded()
    st._device_lock = lambda: None   # 测试不依赖真实设备指纹
    return st


def test_f1_ai_pool_grant_rejected_when_inactive():
    st = _new_store()
    assert st.status()["ai_member"]["active"] is False
    r = st.add_credits(1000, pool="ai")
    assert r.get("ok") is False, "非活跃账号充 AI 池必须拒绝，不得造出「看得到扣不动」的假余额"
    err = r.get("error", "")
    assert "AI 会员" in err and "永久积分" in err, f"拒绝理由要能指路，实际：{err}"
    # 账面不得虚增（原缺陷：credits_total 会显示 1000）
    assert st.credits_balance()["total"] == 0
    assert st.status()["credits_total"] == 0


def test_f1_ai_pool_grant_ok_when_active():
    st = _new_store()
    st._state["ai_member"].update(
        {"active": True, "credits_left": 0, "expire_at": time.time() + 86400})
    st._persist()
    r = st.add_credits(1000, pool="ai")
    assert r.get("ok") is True, "活跃 AI 会员充 AI 池必须放行"
    assert st.credits_balance()["ai_subscription"] == 1000
    # 且真能扣 —— 账面与可用额一致
    sp = st.spend_credits(10, reason="guard")
    assert sp.get("ok") is True and sp.get("ai_taken") == 10


def test_f1_admin_adjust_skips_cloud_on_local_reject():
    """本地被拒时 admin_store 不得把该笔推云端（否则两端账目分叉）。"""
    src = (Path(downloader.__file__).parent / "admin_store.py").read_text(encoding="utf-8")
    i = src.index("res = get_user_store(user_id).add_credits(delta, pool=pool)")
    chunk = src[i:i + 900]
    assert 'if not res.get("ok"):' in chunk, "被拒分支缺失"
    assert "return res" in chunk, "被拒必须提前返回，不能走到 _sync_credits_to_cloud"
    assert chunk.index('if not res.get("ok"):') < chunk.index("_sync_credits_to_cloud("), \
        "拒绝判定必须早于云端同步"


def test_f2_finalize_terminated_sets_terminal_state():
    store = TaskStore(Path(tempfile.mkdtemp()))
    # 停滞 → failed + 可续传类别
    t = store.create(url="https://example.com/a", title="a", platform="test", quality="360")
    tid = t.task_id if hasattr(t, "task_id") else t.id
    t.cancel_reason = "stall"
    t.cancel_requested = True
    downloader._finalize_terminated(t, store)
    got = store.get(tid)
    assert got.status == "failed", "停滞必须落 failed 终态（可行动错误），不能停在 downloading"
    assert "停滞" in got.error

    # 用户取消（无 cancel_reason）→ canceled
    t2 = store.create(url="https://example.com/b", title="b", platform="test", quality="360")
    tid2 = t2.task_id if hasattr(t2, "task_id") else t2.id
    t2.cancel_reason = ""
    downloader._finalize_terminated(t2, store)
    assert store.get(tid2).status == "canceled"

    # 幂等：再调一次仍是同一终态
    downloader._finalize_terminated(t, store)
    assert store.get(tid).status == "failed"


def test_f2_wait_loop_short_slice_source_pins():
    src = Path(downloader.__file__).read_text(encoding="utf-8")
    assert "def _finalize_terminated(" in src
    # 子线程 except 分支必须复用同一份逻辑
    assert "_finalize_terminated(task, store)" in src
    # 主等待循环不得再「一次性 join 整个硬上限」——那是 2h 僵尸的根因
    assert "th.join(timeout=DOWNLOAD_HARD_TIMEOUT)" not in src, \
        "主等待循环不得再一次性干等硬上限，必须短切片 join"
    # 短切片 + 取消即中断
    assert "task.cancel_requested and not task.is_finished" in src
    assert "min(WATCHDOG_POLL, _wait_left)" in src


def test_f3_device_lock_recomputes_credits_total():
    st = _new_store()
    st._state["ai_member"].update(
        {"active": True, "credits_left": 500, "expire_at": time.time() + 86400})
    st._state["permanent_credits"]["total"] = 300
    st._persist()
    assert st.status()["credits_total"] == 800
    # 模拟设备锁定：AI 池清零，合计必须同口径重算
    st._device_lock = lambda: "fingerprint-mismatch"
    out = st.status()
    assert out["ai_member"]["credits_left"] == 0
    assert out["credits_total"] == 300, "锁定态合计必须按置零后的口径重算，不得残留旧值"


if __name__ == "__main__":
    print("▶ 积分口径 + 停滞终态 一致性回归")
    test_f1_ai_pool_grant_rejected_when_inactive()
    test_f1_ai_pool_grant_ok_when_active()
    test_f1_admin_adjust_skips_cloud_on_local_reject()
    test_f2_finalize_terminated_sets_terminal_state()
    test_f2_wait_loop_short_slice_source_pins()
    test_f3_device_lock_recomputes_credits_total()
    print("🎉 积分口径 + 停滞终态 一致性回归全部通过")
