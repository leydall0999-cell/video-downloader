"""server/tests/test_quota.py — 配额闸门单测（docs/llm-cloud-fallback-design.md §12）。

覆盖：终身云端事件 3 次耗尽拦截、每日 auto 自然日重置、会员无限、
单视频 ≤30 分钟门、decide_cloud_fallback 三态、JSON 持久化。
全部用临时 base_dir + 注入 now_fn/is_member_fn，不碰真实 ~/.video-downloader。
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
from quota import QuotaManager, LIFETIME_CLOUD_EVENTS, DAILY_AUTO_RUNS, FREE_MAX_DURATION_SEC


def _mgr(member=False, base_dir=None, now=1000000000.0):
    if base_dir is None:
        base_dir = tempfile.mkdtemp(prefix="vdl_quota_")
    return QuotaManager(base_dir=base_dir, now_fn=lambda: now, is_member_fn=lambda: member)


def test_lifetime_exhaustion():
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    q = _mgr(base_dir=d, now=1000.0)
    assert q.lifetime_cloud_remaining() == LIFETIME_CLOUD_EVENTS
    for _ in range(LIFETIME_CLOUD_EVENTS):
        assert q.consume_cloud_event() is True
    assert q.lifetime_cloud_remaining() == 0
    # 耗尽后再扣应失败
    assert q.consume_cloud_event() is False
    # 闸门 deny
    assert q.decide_cloud_fallback() == "deny"
    # 持久化已写入
    assert os.path.exists(os.path.join(d, "quota.json"))
    shutil.rmtree(d, ignore_errors=True)


def test_daily_auto_resets():
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    # 第 1 天用满每日 auto
    q = _mgr(base_dir=d, now=1000.0)
    assert q.daily_auto_remaining() == DAILY_AUTO_RUNS
    assert q.consume_daily_auto() is True
    assert q.daily_auto_remaining() == 0
    assert q.decide_cloud_fallback() == "deny"
    # 跨到「下一天」（+86400s），应重置
    q2 = _mgr(base_dir=d, now=1000.0 + 86400.0)
    assert q2.daily_auto_remaining() == DAILY_AUTO_RUNS
    assert q2.decide_cloud_fallback() == "allow"
    shutil.rmtree(d, ignore_errors=True)


def test_member_unlimited():
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    q = _mgr(member=True, base_dir=d, now=1000.0)
    assert q.lifetime_cloud_remaining() == 10 ** 9
    assert q.daily_auto_remaining() == 10 ** 9
    assert q.decide_cloud_fallback() == "allow"
    # 超额消耗也不受限
    for _ in range(10):
        assert q.consume_cloud_event() is True
    assert q.can_upload_video(999999) is True
    shutil.rmtree(d, ignore_errors=True)


def test_upload_duration_gate():
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    q = _mgr(base_dir=d, now=1000.0)
    assert q.can_upload_video(FREE_MAX_DURATION_SEC) is True          # 等于上限放行
    assert q.can_upload_video(FREE_MAX_DURATION_SEC + 1) is False     # 超 1 秒拦截
    assert q.can_upload_video(0) is True                              # 未知时长 fail-open
    assert q.can_upload_video(-5) is True
    shutil.rmtree(d, ignore_errors=True)


def test_fallback_three_states():
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    # allow：初始
    q = _mgr(base_dir=d, now=1000.0)
    assert q.decide_cloud_fallback() == "allow"
    # deny：终身耗尽
    for _ in range(LIFETIME_CLOUD_EVENTS):
        q.consume_cloud_event()
    assert q.decide_cloud_fallback() == "deny"
    # 重置每日后，若终身仍在，allow（每日满才会 deny）
    d2 = tempfile.mkdtemp(prefix="vdl_quota_")
    q2 = _mgr(base_dir=d2, now=1000.0)
    q2.consume_daily_auto()
    assert q2.decide_cloud_fallback() == "deny"  # 当日 auto 用满
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(d2, ignore_errors=True)


def test_persistence_across_instances():
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    q1 = _mgr(base_dir=d, now=1000.0)
    q1.consume_cloud_event()
    q1.consume_cloud_event()
    # 新实例（同目录）应读到已用 2 次
    q2 = _mgr(base_dir=d, now=1000.0)
    assert q2.lifetime_cloud_remaining() == LIFETIME_CLOUD_EVENTS - 2
    shutil.rmtree(d, ignore_errors=True)


def main():
    tests = [test_lifetime_exhaustion, test_daily_auto_resets, test_member_unlimited,
             test_upload_duration_gate, test_fallback_three_states,
             test_persistence_across_instances]
    for t in tests:
        t()
        print(f"  ✅ {t.__name__}")
    print(f"✅ 配额闸门单测全过（{len(tests)} 项）")


if __name__ == "__main__":
    main()
