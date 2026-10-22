"""解析需求登记（resolve_demand）离线守卫 —— Downie 机制本土化第一环。

覆盖：
  1. record_demand 落库字段与路径（VDL_DATA_DIR 隔离，不碰真实家目录）；
  2. 同 host 60s 节流（重试 10 次只落 1 条）；
  3. demand_summary「站点×周」口径（2026-10-23 起）：同一自然周多条 → weeks_count=1；
     不同周 → 照计；按 weeks_count 降序；days_count 作为兼容别名同值；
  4. 分类 breakdown、last_url 取最近一条、坏行跳过、空文件返回 []。
运行：python tests/test_resolve_demand.py   （无需网络）
"""

import json
import sys
import tempfile
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

import resolve_demand as RD  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="vdl_demand_"))

    # ① 落库：字段齐全、文件落在隔离目录
    ok1 = RD.record_demand("Example.com", "https://example.com/vod/play/1/sid/2", "unsupported_platform", base_dir=tmp)
    path = tmp / RD.LOG_NAME
    check("record_demand 返回 True 且文件已建", ok1 and path.exists())
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    check("字段：host 小写化 / url 截断 / category / ts",
          rec["host"] == "example.com" and rec["url"].endswith("sid/2")
          and rec["category"] == "unsupported_platform" and rec["ts"] > 0)

    # ② 节流：同 host 60s 内第二次不落
    ok2 = RD.record_demand("example.com", "https://example.com/other", "unsupported_platform", base_dir=tmp)
    lines = path.read_text(encoding="utf-8").splitlines()
    check("同 host 60s 内节流（第二条不落库）", ok2 is False and len(lines) == 1, f"lines={len(lines)}")
    # 换 host 立即可写（节流键是 host，不是全局）
    ok3 = RD.record_demand("other.com", "https://other.com/x", "pending_extractor", base_dir=tmp)
    check("不同 host 不受节流影响", ok3 is True)
    # 空 host 拒绝
    check("空 host 拒绝写入", RD.record_demand("", "https://x.com/1", base_dir=tmp) is False)

    # ③ 「站点×周」口径：手工写跨周记录（绕过 60s 节流）
    #    间隔整 7 天必落在不同自然周（同星期几），与"今天是周几"无关；
    #    "同周第二条"用**同一个 ts**，天然同周，不会在周一凌晨翻车。
    now = int(time.time())
    rows = [
        {"ts": now, "host": "a.com", "url": "https://a.com/1", "category": "unsupported_platform"},           # 本周
        {"ts": now, "host": "a.com", "url": "https://a.com/2", "category": "unsupported_platform"},           # 同周第二条
        {"ts": now - 86400 * 7, "host": "a.com", "url": "https://a.com/3", "category": "pending_extractor"},   # 上周
        {"ts": now - 86400 * 14, "host": "a.com", "url": "https://a.com/4", "category": "pending_extractor"},  # 上上周
        {"ts": now, "host": "b.com", "url": "https://b.com/1", "category": "unsupported_platform"},
        {"ts": now - 86400 * 100, "host": "old.com", "url": "https://old.com/1", "category": "unsupported_platform"},  # 窗口外
    ]
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write("这不是 JSON 的坏行\n")  # 坏行必须被跳过

    s = RD.demand_summary(days=84, base_dir=tmp)
    by_host = {r["host"]: r for r in s}
    check("窗口外记录被排除（old.com 不出现）", "old.com" not in by_host)
    check("a.com weeks_count=3（三周），total=4（含同周多条）",
          by_host["a.com"]["weeks_count"] == 3 and by_host["a.com"]["total"] == 4,
          f"{by_host.get('a.com')}")
    check("days_count 是 weeks_count 的兼容别名（同值）",
          by_host["a.com"]["days_count"] == by_host["a.com"]["weeks_count"])
    check("weeks 命中周列表长度=3（周一日期串，新→旧）",
          len(by_host["a.com"]["weeks"]) == 3
          and by_host["a.com"]["weeks"] == sorted(by_host["a.com"]["weeks"], reverse=True),
          f"{by_host['a.com'].get('weeks')}")
    check("a.com 分类 breakdown 正确",
          by_host["a.com"]["categories"] == {"unsupported_platform": 2, "pending_extractor": 2})
    check("a.com last_url 取最近一条", by_host["a.com"]["last_url"] == "https://a.com/2")
    check("b.com weeks_count=1", by_host["b.com"]["weeks_count"] == 1)
    check("排序：weeks_count 降序（a.com 在 b.com 前）",
          [r["host"] for r in s].index("a.com") < [r["host"] for r in s].index("b.com"))
    # 同文件里还有 ①② 段落的 example.com / other.com 两条当天记录，合计 4 个 host
    check("坏行被跳过不炸（4 个 host 全部解析）", len(s) == 4, f"hosts={[r['host'] for r in s]}")

    # ④ 空目录 / 无文件 → []
    check("无文件时返回 []", RD.demand_summary(days=84, base_dir=Path(tempfile.mkdtemp())) == [])

    # ⑤ 超限截断：写一个 >2MB 的文件，record 后应保留尾部
    big = tmp / "big"
    big.mkdir()
    with open(big / RD.LOG_NAME, "w", encoding="utf-8") as f:
        for i in range(30000):
            f.write(json.dumps({"ts": int(time.time()), "host": f"h{i % 7}.com",
                                "url": "https://x.com/" + "y" * 60, "category": "unsupported_platform"}) + "\n")
    size_before = (big / RD.LOG_NAME).stat().st_size
    RD.record_demand("fresh.com", "https://fresh.com/1", base_dir=big)
    size_after = (big / RD.LOG_NAME).stat().st_size
    check("超 2MB 触发截断（体积回落）", size_before > RD.MAX_BYTES and size_after < size_before,
          f"{size_before}→{size_after}")
    tail_ok = any(json.loads(l)["host"] == "fresh.com"
                  for l in (big / RD.LOG_NAME).read_text(encoding="utf-8").splitlines() if l.strip())
    check("截断后新记录仍在（尾部保留）", tail_ok)

    print("🎉 解析需求守卫全部通过" if not FAILS else f"❌ 失败 {len(FAILS)} 项: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
