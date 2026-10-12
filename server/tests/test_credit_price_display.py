# -*- coding: utf-8 -*-
"""积分单价显示 + 匿名归属严格化 守卫（2026-10-12）

背景（用户 2026-10-12 截图反馈）：个人中心「使用统计」的「积分单价」列，
消耗 AI 积分的功能（视频解说 / 字幕提取 / 图片去水印 / 字幕翻译 / 一键抠图 /
视频去水印 / 声音克隆）全部错误显示「免费」。根因：
  · FEATURE_USAGE_DEFS 行内烘焙 credit_cost=0，显示层直读烘焙值；
  · 真实扣费走 AI_CREDIT_COSTS（credit_cost(op)），两套数据不同源；
  · 纯积分功能（无日配额墙）根本不在表里，价格无处可显示。

同轮安全修复：tasks/subscriptions 的 list_for_owner「无归属任务对所有人可见」
（web-dev 同源漏洞），桌面端保留「非匿名可见历史遗留」的兼容，但匿名一律不可见。

运行：cd server && ../.build_venv/bin/python tests/test_credit_price_display.py
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
_SERVER = _HERE.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

os.environ.setdefault("VDL_PLANS_CLOUD", "0")
os.environ.setdefault("VDL_CLOUD_LINK", "0")
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_credprice."))

import membership as M  # noqa: E402
import subscriptions as SUBS  # noqa: E402
import tasks as TASKS  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  —— " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def test_credit_price_effective():
    store = M.MembershipStore()
    rows = {r["key"]: r for r in M.feature_usage_status(store)}

    # ① 表内四行：显示价 = credit_cost(op) 生效价（>0，不得再显示「免费」）
    for key, op in (("commentary", "commentary_llm"),
                    ("subtitle_extract", "subtitle_asr"),
                    ("cloud_dewatermark", "dewatermark_ai"),
                    ("cloud_subtitle_translate", "subtitle_translate")):
        r = rows.get(key)
        want = M.credit_cost(op)
        check(f"[单价] {key} 显示生效价（{op} = {want}）",
              r is not None and int(r.get("credit_cost") or 0) == want > 0,
              f"实际 {r and r.get('credit_cost')}")

    # ② 覆盖层跟随：后台改 commentary_llm 价 → 显示层实时跟随（与扣费同源）
    base = M.credit_cost("commentary_llm")
    try:
        M.save_plan_overrides({"credit_costs": {"commentary_llm": base + 9}})
        rows2 = {r["key"]: r for r in M.feature_usage_status(store)}
        check("[单价] 后台覆盖 commentary_llm → 显示层跟随",
              int(rows2["commentary"]["credit_cost"]) == base + 9,
              f"实际 {rows2['commentary']['credit_cost']}")
    finally:
        M.save_plan_overrides({"credit_costs": {"commentary_llm": None}})

    # ③ 纯积分功能行：在表里、价 > 0、无日配额（daily_limit None → 前端显示「—」）
    for key, name in (("credit_matting", "一键抠图"),
                      ("credit_dw_video", "视频去水印"),
                      ("credit_voice", "声音克隆")):
        r = rows.get(key)
        check(f"[纯积分] {name} 行存在且单价 > 0",
              r is not None and int(r.get("credit_cost") or 0) > 0,
              f"实际 {r}")
        if r:
            check(f"[纯积分] {name} 无日配额（体验剩余列显示「—」）",
                  r.get("daily_limit") is None)

    # ④ 一键抠图多档价 → label「N 起」（本机 10 / 云端 50）
    m = rows.get("credit_matting")
    check("[纯积分] 一键抠图单价 label 为「N 起」",
          m is not None and str(m.get("credit_cost_label") or "").endswith("起"),
          f"实际 {m and m.get('credit_cost_label')}")


def test_owner_strict_anonymous(tmp_root: str):
    d = pathlib.Path(tmp_root)
    tstore = TASKS.TaskStore(root=d / "work", state_dir=d)
    own = tstore.create(url="https://example.com/a.mp4", title="a", platform="x",
                        quality="best", owner_id="d:device-a")
    legacy = tstore.create(url="https://example.com/b.mp4", title="b", platform="x",
                           quality="best")

    # ① 匿名请求（owner_id=""）→ 一律空，含历史遗留
    check("[归属] 匿名 list_for_owner 返回空", tstore.list_for_owner("") == [])
    # ② 非匿名 → 本人任务 + 历史遗留
    got = tstore.list_for_owner("d:device-a")
    ids = {t.id for t in got}
    check("[归属] 本人任务可见", own.id in ids)
    check("[归属] 历史遗留任务对非匿名可见（桌面兼容）", legacy.id in ids)
    # ③ 他人 → 只看到历史遗留，看不到本人任务
    got2 = tstore.list_for_owner("d:device-b")
    ids2 = {t.id for t in got2}
    check("[归属] 他人看不到本人任务", own.id not in ids2)

    # ④ 订阅同口径（SubscriptionStore(path)，add(Subscription)）
    sub = SUBS.Subscription(id="t1", url="https://example.com/rss", name="t",
                            platform="bilibili", quality_key="best",
                            quality_label="best", owner_id="d:device-a") \
        if "owner_id" in SUBS.Subscription.__dataclass_fields__ else None
    if sub is not None:
        sstore = SUBS.SubscriptionStore(d / "subs.json")
        sstore.add(sub)
        check("[归属] 订阅匿名 list_for_owner 返回空", sstore.list_for_owner("") == [])
        check("[归属] 订阅本人可见", len(sstore.list_for_owner("d:device-a")) == 1)
    else:
        print("  ⚠️ Subscription 无 owner_id 字段，跳过订阅用例")


def main() -> int:
    test_credit_price_effective()
    test_owner_strict_anonymous(tempfile.mkdtemp(prefix="vdl_owner."))
    print("\n" + "=" * 46)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 积分单价显示 + 匿名归属守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
