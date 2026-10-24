"""VIP 会员赠 300 积分（2026-10-24 用户定档）回归测试。

背景
----
用户报「VIP 会员现在是 0 积分，改为 300 积分」。查下来**两处**都得改：
  1. 会员引擎 activate()：VIP 档此前只加天数，一分积分都不发（`permanent_credits` 恒 0）；
  2. 云端授权中心 license_server.py 的 PLAN_EFFECT：dl 档同样没有 credits 字段。
     ⚠️ 授权中心是唯一真源 —— 只改本地引擎，下次apply_cloud_authoritative 会把
     perm_credits 覆盖回云端值（0），本地白改。

本测试钉住「本仓库这一半」的契约（授权中心那半边只能在 ECS 上验）：
  ① VIP 档开通即赠 300 积分，进**永久池**（AI 池到期会清零，不能放那里）；
  ② 每买一次送一次（续费累加），不是「一辈子只送一次」；
  ③ 积分包 / AI 档行为不变（不重复送、不串池）；
  ④ 会员到期后永久积分仍在（不该被会员过期清掉）；
  ⑤ 权益文案与数值同源：文案里的数字来自 VIP_MEMBER_CREDITS，且不再出现
     「不含积分额度」这种与事实相反的旧话术。

全程离线：store 用临时目录，VDL_PLANS_CLOUD=0 断掉云端取价。

运行：cd server && python3 tests/test_vip_bonus_credits.py
"""
import os
import sys
import tempfile
import time
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

os.environ.setdefault("VDL_PLANS_CLOUD", "0")
os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_vip_bonus")

from membership import (  # noqa: E402
    MembershipStore, download_benefits, VIP_MEMBER_CREDITS,
)


def _store(td: str) -> MembershipStore:
    return MembershipStore(path=Path(td) / "member.json")


def test_vip_grants_300():
    assert int(VIP_MEMBER_CREDITS) == 300, f"VIP 赠分常量应为 300，实际 {VIP_MEMBER_CREDITS}"
    with tempfile.TemporaryDirectory() as d:
        st = _store(d)
        assert int(st.status().get("permanent_credits") or 0) == 0, "初始永久积分应为 0"
        r = st.activate("download_month")
        assert r.get("ok"), f"开通失败: {r}"
        assert int(r.get("credits_added") or 0) == 300, f"activate 应回 credits_added=300: {r}"
        s = st.status()
        assert int(s.get("permanent_credits") or 0) == 300, f"永久积分应为 300: {s.get('permanent_credits')}"
        assert int(s.get("credits_total") or 0) == 300, f"可用积分合计应为 300: {s.get('credits_total')}"
    print("✅ VIP 开通赠 300 积分，落在永久池（credits_total 同步为 300）")


def test_renew_accumulates():
    with tempfile.TemporaryDirectory() as d:
        st = _store(d)
        st.activate("download_month")
        st.activate("download_1day")
        got = int(st.status().get("permanent_credits") or 0)
        assert got == 600, f"每买一次送一次（30天+1天 → 600），实际 {got}"
    print("✅ 续费/再次购买累加（30天 + 1天 → 600），不是「只送一次」")


def test_other_tracks_unchanged():
    with tempfile.TemporaryDirectory() as d:
        st = _store(d)
        st.activate("credits_5000")
        assert int(st.status().get("permanent_credits") or 0) == 5000, "积分包只加 5000，不应被 VIP 赠分污染"
        st.activate("ai_5500")
        s = st.status()
        assert int(s.get("permanent_credits") or 0) == 5000, "AI 档不该往永久池加钱"
        assert int(s["ai_member"]["credits_left"] or 0) == 5500, f"AI 档积分池应为 5500: {s['ai_member']}"
    print("✅ 积分包 / AI 档行为不变（各进自己的池子，不串账）")


def test_perm_survives_expiry():
    with tempfile.TemporaryDirectory() as d:
        st = _store(d)
        st.activate("download_month")
        # 把会员改成「已过期」，模拟到期后的一次 status 判定
        st._state["download_member"]["expire_at"] = time.time() - 10   # noqa: SLF001
        s = st.status()
        assert not s.get("download_active"), "构造的过期态应判为未激活"
        assert int(s.get("permanent_credits") or 0) == 300, "永久积分不该随 VIP 到期被清空"
    print("✅ 会员到期后永久积分仍在（不会被过期清零）")


def test_benefit_copy_matches():
    texts = " ".join(b["text"] for b in download_benefits())
    assert f"{VIP_MEMBER_CREDITS} 积分" in texts, f"权益文案必须写明 {VIP_MEMBER_CREDITS} 积分: {texts[:200]}"
    assert "不含积分额度" not in texts, "旧话术「不含积分额度」与实际发放矛盾，必须删掉"
    print("✅ 权益文案与数值同源（写明 300 积分，旧的「不含积分额度」已移除）")


if __name__ == "__main__":
    test_vip_grants_300()
    test_renew_accumulates()
    test_other_tracks_unchanged()
    test_perm_survives_expiry()
    test_benefit_copy_matches()
    print("\n全部通过 ✅（VIP 赠 300 积分：发放/累加/不串池/到期不清/文案同源 共 5 例）")
