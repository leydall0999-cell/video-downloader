"""配额退还：任务失败不该消耗用户的免费额度。

背景（2026-09-15 实测）：免费用户跑一条 3 分钟素材，管线的 Stage2 因推理 token
吃满输出预算返回空内容而整条任务失败，但**终身云端额度仍然被扣掉 1 次**——
用户既没拿到成片、又少了 1 次机会，体感等同「花钱买失败」。

修复方式：任务失败（超时 / 非 0 退出 / 产物缺失）时，由父进程按实际增量退还。
本文件校验 QuotaManager 的退还语义正确，防止「少扣/超退/会员被误退」。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quota import QuotaManager  # noqa: E402


def _mk(is_member: bool = False):
    home = Path(tempfile.mkdtemp(prefix="vdl_quota_refund_"))
    return QuotaManager(base_dir=home, is_member_fn=lambda: is_member)


# ── 用例 ───────────────────────────────────────────────────────────────── #
def test_refund_restores_consumed_quota():
    """扣多少退多少：用完再退，应回到初始状态。"""
    q = _mk()
    q.consume_cloud_event()
    q.consume_daily_auto()
    assert q.snapshot() == {"lifetime_cloud_used": 1, "daily_auto_used": 1}
    q.refund(lifetime=1, daily=1)
    assert q.snapshot() == {"lifetime_cloud_used": 0, "daily_auto_used": 0}


def test_refund_never_goes_negative():
    """异常路径下重复退还不能把计数退成负数（否则用户白赚额度 / 状态失真）。"""
    q = _mk()
    q.consume_cloud_event()
    for _ in range(5):
        q.refund(lifetime=1)
    assert q.snapshot()["lifetime_cloud_used"] == 0


def test_refund_zero_delta_is_noop():
    """没扣过就退 = 空操作，不许写入文件制造脏数据。"""
    q = _mk()
    before = q.path.read_text(encoding="utf-8") if q.path.exists() else ""
    after_dims = q.refund(lifetime=1)
    assert after_dims == {"lifetime_cloud_used": 0, "daily_auto_used": 0}
    assert (q.path.read_text(encoding="utf-8") if q.path.exists() else "") == before


def test_refund_skipped_for_member():
    """会员本就不扣额度，退还也必须绕过，避免负数曲目额度被误写。"""
    q = _mk(is_member=True)
    q.consume_cloud_event()          # 会员分支直接 True，不落账
    snap = q.refund(lifetime=1, daily=1)
    assert snap == {"lifetime_cloud_used": 0, "daily_auto_used": 0}


def test_snapshot_reflects_real_consumption():
    """退还必须基于真实快照，snapshot 读错会让退款变成随机加减。"""
    q = _mk()
    q.consume_cloud_event()
    q.consume_cloud_event()
    snap = q.snapshot()
    assert snap["lifetime_cloud_used"] == 2
    q.refund(lifetime=2)
    assert q.lifetime_cloud_remaining() == 3


def test_daily_auto_refund_independent_from_lifetime():
    """两条额度各自退，互不串台。"""
    q = _mk()
    q.consume_cloud_event()
    q.consume_daily_auto()
    q.refund(daily=1)                       # 只退每日
    snap = q.snapshot()
    assert snap == {"lifetime_cloud_used": 1, "daily_auto_used": 0}


_TESTS = [
    test_refund_restores_consumed_quota,
    test_refund_never_goes_negative,
    test_refund_zero_delta_is_noop,
    test_refund_skipped_for_member,
    test_snapshot_reflects_real_consumption,
    test_daily_auto_refund_independent_from_lifetime,
]


if __name__ == "__main__":
    failed = 0
    for fn in _TESTS:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  💥 {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n配额退还测试：{len(_TESTS) - failed}/{len(_TESTS)} 通过")
    sys.exit(1 if failed else 0)
