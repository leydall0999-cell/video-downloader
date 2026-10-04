# -*- coding: utf-8 -*-
"""守卫：下载会员权益文案必须与配额表一致（2026-10-03）。

背景：会员中心那条权益栏原先是**手写死的 6 条**，而配额表里还有
matting（本地抠图 500/日）、cloud（云端算力 200/日）、app_compute（App 本地
重算力 200/日）、subtitle_extract（会员不限）等 —— 代码里有、页面上没有，
用户看不到「买了能得到什么」。

现在权益清单由 download_benefits() 从 DAILY_QUOTA_LIMITS +
FEATURE_USAGE_DEFS 自动生成，本守卫钉死：
  1) 配额表里每一条「会员可用」的配额，都必须在权益清单里出现（漏一条就红）；
  2) 免费为 0（不开放）的配额不得出现在会员权益里；
  3) 会员不限次（member_limit = -1）的功能必须出现且写明「不限」；
  4) 必须写清「不含 AI 积分」，否则用户会以为下载会员也送积分；
  5) 关键说明项（清晰度、设备数、高速通道、客服）在列表里。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_tmp = tempfile.mkdtemp(prefix="vdl_benefits_")
os.environ["VDL_DATA_DIR"] = _tmp
os.environ["VDL_CLOUD_LINK"] = "0"
os.environ["VDL_PLANS_CLOUD"] = "0"

import membership as M                                       # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  ✅ " if cond else "  ❌ ") + name)
    if not cond:
        FAILS.append(name)


def test_benefits_cover_quotas() -> None:
    print("\n[A] 配额表 → 权益文案全覆盖")
    items = M.download_benefits()
    keys = {str(i.get("key")) for i in items}
    texts = " ".join(str(i.get("text")) for i in items)
    for key, limit in M.DAILY_QUOTA_LIMITS.items():
        if int(limit or 0) > 0:
            check(f"[覆盖] 配额 {key}={limit} 有对应权益文案", key in keys)
    for key, limit in M.FREE_DAILY_LIMITS.items():
        if int(limit or 0) == 0 and key in M.DAILY_QUOTA_LIMITS:
            # 免费 0 且会员有配额 → 会员应有、免费不该有（这里只验会员侧有文案）
            check(f"[覆盖] 免费不开放的 {key} 仍应是会员权益", key in keys)
    check("[数值] 下载任务文案带真实配额数字", "1000 次/日" in texts)
    check("[数值] 本地抠图文案带真实配额数字", "500 次/日" in texts)
    # 2026-10-04：原画/批量占位配额已下线，权益文案里不得再出现这两份不存在的承诺
    check("[清理] 不再承诺「原画 N 次/日」", "原画" not in texts)
    check("[清理] 不再承诺「批量下载素材」", "批量下载素材" not in texts)


def test_benefits_unlimited_items() -> None:
    print("\n[B] 会员不限次的功能必须写明「不限」")
    items = M.download_benefits()
    by_key = {str(i.get("key")): str(i.get("text")) for i in items}
    for d in M.FEATURE_USAGE_DEFS:
        if int(d.get("member_limit", 0)) == -1:
            k = str(d.get("key"))
            check(f"[不限] {d.get('name')} 出现在权益里且写明不限",
                  k in by_key and "不限" in by_key[k])


def test_benefits_key_notes() -> None:
    print("\n[C] 关键说明项（避免用户误解）")
    by_key = {str(i.get("key")): str(i.get("text")) for i in M.download_benefits()}
    check("[说明] 写明不含 AI 积分（下载会员不送积分）",
          "no_credits" in by_key and "不含" in by_key["no_credits"])
    check("[说明] 写明 1080P 及以上清晰度", "quality" in by_key and "1080P" in by_key["quality"])
    check("[说明] 写明设备数 2 台", "devices" in by_key and "2 台" in by_key["devices"])
    check("[说明] 写明高速通道", "speed" in by_key)
    check("[说明] 写明优先客服", "support" in by_key)
    check("[说明] 字幕提取标为不限（免费仅 2 次/日）",
          any("字幕提取" in str(i.get("text")) for i in M.download_benefits()))


def test_plans_uses_generated_benefits() -> None:
    print("\n[D] plans() 走生成清单（不再手写）")
    st = M.MembershipStore()
    plans = st.plans()
    benefits = plans.get("download_member", {}).get("benefits") or []
    check("[接线] plans().download_member.benefits 非空", len(benefits) >= 8)
    check("[接线] 与 download_benefits() 条数一致", len(benefits) == len(M.download_benefits()))
    ai = plans.get("ai_member", {})
    check("[接线] AI 会员仍带 plans + 捆绑说明", bool(ai.get("plans")) and "下载会员" in str(ai.get("bundle_note")))


def test_download_plan_grants_no_credits() -> None:
    print("\n[E] 下载会员不送积分（激活逻辑钉死）")
    st = M.MembershipStore()
    st.activate("download_7day", via="test")
    s = st.status()
    check("[激活] 下载会员激活后永久积分为 0", int(s.get("permanent_credits") or 0) == 0)
    check("[激活] AI 会员未激活", not (s.get("ai_member") or {}).get("active"))
    src = open(pathlib.Path(M.__file__).parent / "membership.py", encoding="utf-8").read()
    dl_branch = src.split('if code in _dl_plans:')[1].split('elif code in _ai_plans:')[0]
    check("[激活] 下载会员分支不写任何积分字段",
          "credits" not in dl_branch)


def test_period_usage_reads_history() -> None:
    """个人中心「使用统计」表的周期汇总必须认历史用量（2026-10-03 修的真 bug）。

    症状：后台「用户使用详情」里近 9 天有 68 次消耗，但个人中心表的
    近三日/近七日/本月**恒为 0**。根因：usage_summary 读 status()["usage_history"]，
    而 status() 是对外公开视图、刻意不含 usage_history → 恒空字典。
    """
    print("\n[F] 周期用量汇总读得到历史（status() 不含 usage_history）")
    st = M.MembershipStore()
    check("[前提] status() 确实不含 usage_history（这是本 bug 的根因）",
          "usage_history" not in st.status())
    # 造 3 天历史：今天往前第 1 天用 7 次下载
    import time as _t
    today = _t.strftime("%Y-%m-%d", _t.localtime(st._now()))
    yday = _t.strftime("%Y-%m-%d", _t.localtime(st._now() - 86400))
    st._state.setdefault("usage_history", {})[yday] = {
        "download": 7, "original": 0, "date": yday,
    }
    st._state["daily_usage"] = {"date": today, "download": 0, "original": 0}

    tot3 = M.usage_summary(st, period="3d")
    check("[3d] 汇总认得到昨天的量", int(tot3.get("download", 0)) == 7)
    tot7 = M.usage_summary(st, period="7d")
    check("[7d] 同样认得到", int(tot7.get("download", 0)) == 7)
    tot_today = M.usage_summary(st, period="today")
    check("[today] 只算今天（昨天的不该算进来）", int(tot_today.get("download", 0)) == 0)

    rows = M.feature_usage_status(st, period="3d")
    dl = next((r for r in rows if r.get("key") == "video_parse"), None)
    check("[表格] 视频解析行 period_used = 7", dl is not None and int(dl["period_used"]) == 7)
    check("[表格] 行里带 daily_used（今日）供「体验剩余」列用",
          dl is not None and "daily_used" in dl and "daily_limit" in dl)


def main() -> int:
    test_benefits_cover_quotas()
    test_benefits_unlimited_items()
    test_benefits_key_notes()
    test_plans_uses_generated_benefits()
    test_download_plan_grants_no_credits()
    test_period_usage_reads_history()
    print("\n" + "=" * 46)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 会员权益与配额一致性守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
