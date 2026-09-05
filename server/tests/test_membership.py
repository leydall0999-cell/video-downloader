"""VDL 会员引擎单元测试（server/tests/test_membership.py）。

覆盖：套餐表结构 / 激活与续费顺延 / AI 捆绑下载权益 / 积分消耗顺序（先订阅后永久）/
到期清零 / 永久积分包不过期 / 日配额惰性重置与会员门禁 / 状态持久化。

零外部依赖、不碰真实 ~/.video-downloader/membership.json（全程 tmp 路径 + 可控时钟）。
运行：
    cd server && python tests/test_membership.py
或
    .build_venv/bin/python -m pytest server/tests/test_membership.py -v
"""
import os
import sys
import tempfile
import time
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from membership import (  # noqa: E402
    AI_PLANS, CREDIT_PACKS, DAILY_QUOTA_LIMITS, DOWNLOAD_PLANS, MembershipStore,
)

T0 = 1_780_000_000.0  # 固定基准时间（unix 秒）


def _mkstore(tmp: str, cur: list) -> MembershipStore:
    """tmp 为临时目录，cur 为 [当前时间] 列表（测试中手动推进）。"""
    return MembershipStore(path=Path(tmp) / "membership.json", now_fn=lambda: cur[0])


def test_plans_structure():
    st = _mkstore(tempfile.mkdtemp(), [T0])
    p = st.plans()
    assert p["currency"] == "CNY"
    # 下载会员 3 档，年档带 best 标记
    assert set(DOWNLOAD_PLANS) == {"download_month", "download_half_year", "download_year"}
    assert DOWNLOAD_PLANS["download_year"]["days"] == 365
    assert DOWNLOAD_PLANS["download_year"].get("best") is True
    assert DOWNLOAD_PLANS["download_half_year"]["saving"] > 0.4
    # AI 会员 2 档，捆绑提示
    assert set(AI_PLANS) == {"ai_5500", "ai_15000"}
    assert "包含下载会员全部权益" in p["ai_member"]["bundle_note"]
    # 积分包 2 档
    assert set(CREDIT_PACKS) == {"credits_5000", "credits_15000"}
    # 权益文案含日配额
    texts = [b["text"] for b in p["download_member"]["benefits"]]
    assert any("1000" in t and "解析" in t for t in texts)
    print("✅ plans 结构正确")


def test_activate_download_month():
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    r = st.activate("download_month")
    assert r["ok"] and r["kind"] == "download_member"
    s = st.status()
    assert s["download_member"]["active"] is True
    assert abs(s["download_member"]["expire_at"] - (T0 + 30 * 86400)) < 1
    assert s["download_member"]["source"] == "download"
    assert s["ai_member"]["active"] is False
    print("✅ 下载会员月卡激活，到期 = now+30d")


def test_activate_renewal_extends():
    """续费顺延：到期日 = max(now, 当前到期) + 时长，不吞已有天数。"""
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    st.activate("download_month")
    cur[0] = T0 + 5 * 86400  # 5 天后续一个月
    r = st.activate("download_month")
    assert r["ok"]
    # 预期：max(T0+5d, T0+30d) + 30d = T0+60d（原剩余 25d 保留）
    assert abs(st.status()["download_member"]["expire_at"] - (T0 + 60 * 86400)) < 1
    print("✅ 续费顺延，剩余天数不吞")


def test_ai_member_bundles_download():
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    st.activate("ai_5500")
    s = st.status()
    assert s["ai_member"]["active"] is True
    assert s["ai_member"]["credits_left"] == 5500
    # 捆绑：下载权益 active 且到期对齐 AI
    assert s["download_member"]["active"] is True
    assert abs(s["download_member"]["expire_at"] - (T0 + 30 * 86400)) < 1
    assert s["download_member"]["source"] == "ai_bundle"
    print("✅ AI 会员自动捆绑下载会员权益（到期对齐）")


def test_ai_expiry_clears_subscription_credits():
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    st.activate("ai_15000")
    cur[0] = T0 + 31 * 86400  # 过期 1 天
    s = st.status()
    assert s["ai_member"]["active"] is False
    assert s["ai_member"]["credits_left"] == 0      # 订阅积分到期清零
    assert s["download_member"]["active"] is False   # 捆绑权益随 AI 过期失效
    print("✅ AI 到期后订阅积分清零、捆绑下载权益失效")


def test_credit_spend_order_ai_first():
    """消耗顺序：AI 订阅积分优先，永久积分兜底。"""
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    st.activate("ai_5500")       # 订阅积分 5500
    st.activate("credits_5000")  # 永久 5000
    assert st.credits_balance()["total"] == 10500
    # 花 6000：全部 AI 5500 + 永久 500
    r = st.spend_credits(6000, reason="asr")
    assert r["ok"]
    b = st.credits_balance()
    assert b["ai_subscription"] == 0
    assert b["permanent"] == 4500
    assert b["total"] == 4500
    print("✅ 积分先扣 AI 订阅、再扣永久")


def test_credit_spend_insufficient():
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    st.activate("credits_5000")
    r = st.spend_credits(5001)
    assert r["ok"] is False
    assert st.credits_balance()["total"] == 5000  # 余额不变
    print("✅ 积分不足时拒绝且余额不变")


def test_permanent_credits_survive_expiry():
    """永久积分包不受 AI 会员到期影响。"""
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    st.activate("credits_15000")
    st.activate("ai_5500")
    cur[0] = T0 + 40 * 86400  # AI 过期
    b = st.credits_balance()
    assert b["permanent"] == 15000  # 永久积分仍在
    assert b["ai_subscription"] == 0
    print("✅ 永久积分包过期不清零")


def test_free_quota_10_then_blocked():
    """免费档：resolve 10 次/日，第 11 次被拒并带 MEMBER_QUOTA 码。"""
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    for i in range(10):
        assert st.use_daily("resolve")["ok"] is True, f"第 {i+1} 次免费解析应放行"
    r = st.use_daily("resolve")
    assert r["ok"] is False
    assert r.get("code") == "MEMBER_QUOTA"
    assert "免费" in r["error"] and "开通下载会员" in r["error"]
    q = st.quota_state("resolve")
    assert q["tier"] == "free" and q["limit"] == 10 and q["used"] == 10
    # 免费不开放原画/批量
    assert st.quota_state("original")["allowed"] is False
    assert st.quota_state("batch_material")["allowed"] is False
    print("✅ 免费档 resolve 10/日，超限带 MEMBER_QUOTA；原画/批量免费不开放")


def test_member_quota_upgrade_after_activation():
    """开通下载会员后 resolve 额度升到 1000/日，当日已用计数延续。"""
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    for _ in range(10):
        assert st.use_daily("resolve")["ok"] is True
    st.activate("download_month")
    q = st.quota_state("resolve")
    assert q["tier"] == "member"
    assert q["limit"] == DAILY_QUOTA_LIMITS["resolve"]
    assert q["used"] == 10  # 已用计数保留
    assert q["remaining"] == DAILY_QUOTA_LIMITS["resolve"] - 10
    assert st.use_daily("resolve")["ok"] is True
    # 原画/批量随会员解锁
    assert st.quota_state("original")["allowed"] is True
    assert st.quota_state("batch_material")["allowed"] is True
    print("✅ 会员激活后 resolve 升 1000/日、原画/批量解锁、计数延续")


def test_daily_quota_unlimited_and_unknown():
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    # unlimited 资源恒放行
    assert st.quota_state("comment")["unlimited"] is True
    assert st.use_daily("comment")["unlimited"] is True
    # unknown 资源保守放行
    assert st.quota_state("whatever_future")["unknown"] is True
    print("✅ 日配额：unlimited / unknown 保守放行")


def test_daily_quota_reset_on_new_day():
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    st.activate("download_month")
    st.use_daily("resolve", n=7)
    assert st.quota_state("resolve")["used"] == 7
    cur[0] = T0 + 86400  # 第二天
    q = st.quota_state("resolve")
    assert q["used"] == 0
    assert q["remaining"] == DAILY_QUOTA_LIMITS["resolve"]
    print("✅ 日配额跨日惰性重置")


def test_state_persists_across_instances():
    """激活状态落盘，新实例可读到（模拟重启 app）。"""
    d = tempfile.mkdtemp()
    p = Path(d) / "membership.json"
    cur = [T0]
    st1 = MembershipStore(path=p, now_fn=lambda: cur[0])
    st1.activate("download_year")
    st2 = MembershipStore(path=p, now_fn=lambda: cur[0])
    s = st2.status()
    assert s["download_member"]["active"] is True
    assert abs(s["download_member"]["expire_at"] - (T0 + 365 * 86400)) < 1
    print("✅ 状态持久化，跨实例重启可恢复")


def test_history_capped():
    cur = [T0]
    st = _mkstore(tempfile.mkdtemp(), cur)
    for _ in range(250):
        st.activate("credits_5000")
    assert len(st._state["meta"]["history"]) <= 200
    print("✅ 激活历史最多保留 200 条")


if __name__ == "__main__":
    tests = [
        test_plans_structure,
        test_activate_download_month,
        test_activate_renewal_extends,
        test_ai_member_bundles_download,
        test_ai_expiry_clears_subscription_credits,
        test_credit_spend_order_ai_first,
        test_credit_spend_insufficient,
        test_permanent_credits_survive_expiry,
        test_free_quota_10_then_blocked,
        test_member_quota_upgrade_after_activation,
        test_daily_quota_unlimited_and_unknown,
        test_daily_quota_reset_on_new_day,
        test_state_persists_across_instances,
        test_history_capped,
    ]
    for t in tests:
        t()
    print(f"\n🎉 会员引擎单测全部通过（{len(tests)} 项）")
