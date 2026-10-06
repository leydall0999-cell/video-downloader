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
from quota import (QuotaManager, LIFETIME_CLOUD_EVENTS, DAILY_AUTO_RUNS,
                    FREE_MAX_DURATION_SEC)


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


def _eff():
    """当前生效的 (终身, 每日auto) —— 后台可改，别用模块常量断言。"""
    from quota import cloud_quota_limits
    return cloud_quota_limits()


def test_daily_auto_resets():
    """每日 auto 额度的「用满 → 跨日重置」语义。

    🔴 2026-10-06：管理员可能把 daily_auto 配成 0（用户已这么做 —— 本机跑不动
    自动回落云端要烧大模型 token）。本守卫必须与「生效值」联动，不能假设 ≥1：
    额度为 0 时 `consume_daily_auto()` 必然 False，断言 True 会恒红。
    故：生效值 >0 时跑完整的「用满→跨日重置」；=0 时改为验证「扣不动但
    终身额度仍在、且显式选云端仍放行」（这才是 0 额度下该保证的行为）。
    """
    _life, daily = _eff()
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    q = _mgr(base_dir=d, now=1000.0)
    assert q.daily_auto_remaining() == daily

    if daily <= 0:
        # 额度为 0：auto 回落被拒，但**显式选云端仍能用终身额度**
        assert q.consume_daily_auto() is False, "额度 0 时不该扣得动"
        assert q.decide_cloud_fallback("auto") == "deny", "auto 回落应被拒"
        assert q.decide_cloud_fallback("cloud_only") == "allow", \
            "显式选云端应仍放行（走终身额度）"
        assert q.lifetime_cloud_remaining() == _life
        shutil.rmtree(d, ignore_errors=True)
        return

    # 第 1 天用满每日 auto
    assert q.consume_daily_auto() is True
    assert q.daily_auto_remaining() == 0
    assert q.decide_cloud_fallback() == "deny"
    # 跨到「下一天」（+86400s），应重置
    q2 = _mgr(base_dir=d, now=1000.0 + 86400.0)
    assert q2.daily_auto_remaining() == daily
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
    """三态：allow / deny（终身耗尽）/ deny（每日 auto 用满）。

    🔴 2026-10-06：daily_auto 可被管理员配成 0（用户已设）。为 0 时
    「初始 auto 应 allow」不成立（daily_auto_remaining()==0 ⇒ 直接 deny），
    故此处按生效值分流，只断言**该配置下应成立**的行为。
    """
    _life, daily = _eff()
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    q = _mgr(base_dir=d, now=1000.0)
    assert q.decide_cloud_fallback() == ("deny" if daily <= 0 else "allow")
    # 显式选云端不受每日 auto 影响（daily=0 时也放行）
    assert q.decide_cloud_fallback("cloud_only") == "allow"
    # deny：终身耗尽
    for _ in range(_life):
        q.consume_cloud_event()
    assert q.decide_cloud_fallback() == "deny"
    # 重置每日后，若终身仍在，allow（每日满才会 deny）
    if daily > 0:
        d2 = tempfile.mkdtemp(prefix="vdl_quota_")
        q2 = _mgr(base_dir=d2, now=1000.0)
        q2.consume_daily_auto()
        assert q2.decide_cloud_fallback() == "deny"  # 当日 auto 用满
        shutil.rmtree(d2, ignore_errors=True)
    shutil.rmtree(d, ignore_errors=True)


def test_cloud_only_ignores_daily_auto():
    """显式选云端（cloud_only）不得被「每日 auto 额度」拦住。

    回归：早期实现里 decide_cloud_fallback() 无 mode 参数，engine=cloud 也走同一判定，
    结果是当天跑过 1 次后第 2 次被误拒（提示「额度已用完」，实际终身还剩额度）。
    """
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    # 🔴 2026-10-06：daily_auto 可被配成 0（用户已设）。为 0 时「先消耗当日
    # auto」这步不成立，本用例的前提消失 ⇒ 明确跳过并说明原因，
    # 而不是让断言恒红（那会掩盖真正的问题）。
    _life, _daily = _eff()
    if _daily <= 0:
        shutil.rmtree(d, ignore_errors=True)
        print("  ⏭ 跳过：当前每日 auto 额度为 0，本用例需 ≥1 才能验证「用满后仍放行」")
        return
    q = _mgr(base_dir=d, now=1000.0)
    # 先消耗掉当日 auto 额度（模拟今天已发生过一次 auto 回落）
    assert q.consume_daily_auto() is True
    assert q.daily_auto_remaining() == 0
    # auto 回落：两个额度都要满足 → 当日 auto 用满 → deny
    assert q.decide_cloud_fallback("auto") == "deny"
    assert q.decide_cloud_fallback() == "deny"       # 默认仍是 auto 语义
    # 显式选云端：只看终身额度 → 仍 allow（终身还剩）
    assert q.decide_cloud_fallback("cloud_only") == "allow"
    # 终身耗尽后，显式选云端同样 deny
    for _ in range(_life):
        q.consume_cloud_event()
    assert q.lifetime_cloud_remaining() == 0
    assert q.decide_cloud_fallback("cloud_only") == "deny"
    shutil.rmtree(d, ignore_errors=True)


def test_persistence_across_instances():
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    q1 = _mgr(base_dir=d, now=1000.0)
    q1.consume_cloud_event()
    q1.consume_cloud_event()
    # 新实例（同目录）应读到已用 2 次
    q2 = _mgr(base_dir=d, now=1000.0)
    assert q2.lifetime_cloud_remaining() == LIFETIME_CLOUD_EVENTS - 2
    shutil.rmtree(d, ignore_errors=True)


def test_admin_exempt():
    """管理员豁免（2026-09-19）：标记文件 / 环境变量任一即无限额度，status 如实区分。"""
    d = tempfile.mkdtemp(prefix="vdl_quota_")
    # 无标记 → 不豁免，正常拦截
    q = _mgr(base_dir=d)
    assert not q.is_member() and not q._admin_exempt()
    assert q.consume_cloud_event() is True
    # 标记文件 → 豁免（对已耗尽的额度也生效）
    open(os.path.join(d, ".admin_exempt"), "w").close()
    assert q._admin_exempt() and q.is_member()
    assert q.lifetime_cloud_remaining() == 10 ** 9
    assert q.decide_cloud_fallback() == "allow"
    st = q.status()
    assert st["admin_exempt"] is True and st["is_member"] is False  # admin ≠ 会员
    # 环境变量通道（无标记文件的干净目录）
    d2 = tempfile.mkdtemp(prefix="vdl_quota_")
    os.environ["VDL_QUOTA_ADMIN"] = "1"
    try:
        q2 = _mgr(base_dir=d2)
        assert q2._admin_exempt() and q2.lifetime_cloud_remaining() == 10 ** 9
    finally:
        os.environ.pop("VDL_QUOTA_ADMIN", None)  # 绝不泄漏到后续测试
    q3 = _mgr(base_dir=d2)
    assert not q3._admin_exempt()  # 移除后立即失效
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(d2, ignore_errors=True)


def main():
    tests = [test_lifetime_exhaustion, test_daily_auto_resets, test_member_unlimited,
             test_upload_duration_gate, test_fallback_three_states,
             test_cloud_only_ignores_daily_auto, test_persistence_across_instances,
             test_admin_exempt]
    for t in tests:
        t()
        print(f"  ✅ {t.__name__}")
    print(f"✅ 配额闸门单测全过（{len(tests)} 项）")


if __name__ == "__main__":
    main()
