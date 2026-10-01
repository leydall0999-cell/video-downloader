#!/usr/bin/env python3
"""网页版访客统计 server/web_stats.py 回归测试（2026-10-01）。

背景：网页版此前零统计（无 PV/UV/来源/转化），任何变现决策（广告 / CPS / 会员）
都没有数据依据。web_stats 补上这块，但必须守住隐私硬约束——
**不落 IP 明文、UV 哈希随日期派生（跨天不可关联同一访客）**，
否则「为了看流量」反而变成「建了一个可追踪用户的库」。

与 server/stats.py 的分工：stats = 业务功能成功计数；web_stats = 网页访客统计。

运行：
    cd server && python tests/test_web_stats.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试隔离：绝不写用户家目录
_TMP = tempfile.mkdtemp(prefix="vdl_test_webstats_")
os.environ["VDL_DATA_DIR"] = _TMP

import web_stats  # noqa: E402


def _ts(day: str) -> float:
    return time.mktime(time.strptime(day + " 12:00:00", "%Y-%m-%d %H:%M:%S"))


def test_pv_uv_same_day():
    p = web_stats.default_path()
    web_stats.reset(p)
    for _ in range(3):
        assert web_stats.record("page_view", ip="1.2.3.4",
                                ua="Mozilla/5.0 (Macintosh)",
                                now=_ts("2026-10-01"), path=p) is True
    web_stats.record("page_view", ip="5.6.7.8", ua="Mozilla/5.0 (iPhone)",
                     now=_ts("2026-10-01"), path=p)
    d = web_stats.summary(1, now=_ts("2026-10-01"), path=p)["days"][0]
    assert d["pv"] == 4, f"PV 应为 4，实际 {d['pv']}"
    assert d["uv"] == 2, f"UV 应为 2（同 IP 去重），实际 {d['uv']}"
    print("✅ PV/UV：同天同 IP 重复访问只算 1 个 UV")


def test_no_ip_plaintext():
    """隐私红线：库里绝不能出现 IP 明文 / 完整来源 URL / UA 原文。"""
    p = web_stats.default_path()
    web_stats.reset(p)
    web_stats.record("page_view", ip="203.0.113.42", ua="curl/8.1.2",
                     ref="https://www.google.com/search?q=secret",
                     now=_ts("2026-10-01"), path=p)
    raw = p.read_text(encoding="utf-8")
    assert "203.0.113.42" not in raw, "统计文件里绝不能出现 IP 明文"
    assert "google.com/search" not in raw, "来源只存 host，不存完整 URL"
    assert "curl/8.1.2" not in raw, "UA 只存归并后的设备类，不存原文"
    assert "google.com" in raw, "来源 host 应保留"
    data = json.loads(raw)
    assert data["days"]["2026-10-01"]["devices"].get("bot") == 1, "curl 应归并为 bot"
    print("✅ 隐私：无 IP 明文 / 来源只留 host / UA 只留设备类")


def test_uv_cross_day_not_linkable():
    p = web_stats.default_path()
    web_stats.reset(p)
    for day in ("2026-10-01", "2026-10-02"):
        web_stats.record("page_view", ip="1.2.3.4", ua="Mozilla/5.0 (Macintosh)",
                         now=_ts(day), path=p)
    s = web_stats.summary(2, now=_ts("2026-10-02"), path=p)
    assert s["totals"]["uv"] == 2, "跨天 UV 按人次计（哈希随日期派生，不可去重）"
    data = json.loads(p.read_text(encoding="utf-8"))
    h1 = list(data["days"]["2026-10-01"]["uv"].keys())[0]
    h2 = list(data["days"]["2026-10-02"]["uv"].keys())[0]
    assert h1 != h2, "同一 IP 跨天的 UV 哈希必须不同（否则可跨天追踪个人）"
    print("✅ 隐私：同一 IP 跨天哈希不同，无法跨天追踪")


def test_event_whitelist():
    p = web_stats.default_path()
    web_stats.reset(p)
    assert web_stats.record("__evil_key__", now=_ts("2026-10-01"), path=p) is False
    assert web_stats.record("", now=_ts("2026-10-01"), path=p) is False
    assert web_stats.record("download_done", now=_ts("2026-10-01"), path=p) is True
    d = web_stats.summary(1, now=_ts("2026-10-01"), path=p)["days"][0]
    assert "__evil_key__" not in d["events"], "白名单外事件不得落库"
    assert d["events"]["download_done"] == 1
    assert d["pv"] == 0, "只有 page_view 计 PV"
    print("✅ 事件白名单：非法 kind 丢弃，非 page_view 不计 PV")


def test_ref_and_device():
    p = web_stats.default_path()
    web_stats.reset(p)
    web_stats.record("page_view", ip="1.1.1.1", ua="Mozilla/5.0 (iPhone; CPU iPhone OS)",
                     ref="https://www.google.com/", own_host="hanyuxz.top",
                     now=_ts("2026-10-01"), path=p)
    web_stats.record("page_view", ip="2.2.2.2", ua="Mozilla/5.0 (Windows NT 10.0)",
                     ref="https://hanyuxz.top/download", own_host="hanyuxz.top",
                     now=_ts("2026-10-01"), path=p)
    d = web_stats.summary(1, now=_ts("2026-10-01"), path=p)["days"][0]
    refs = dict(d["top_refs"])
    assert refs.get("google.com") == 1, f"外链应归 google.com，实际 {d['top_refs']}"
    assert refs.get("direct") == 1, f"同源跳转应归 direct，实际 {d['top_refs']}"
    assert d["devices"].get("mobile") == 1, f"设备归并错误 {d['devices']}"
    assert d["devices"].get("desktop") == 1, f"设备归并错误 {d['devices']}"
    print("✅ 来源归一：外链留 host、同源归 direct；UA 只归设备类")


def test_summary_totals():
    p = web_stats.default_path()
    web_stats.reset(p)
    web_stats.record("page_view", ip="1.1.1.1", now=_ts("2026-10-01"), path=p)
    web_stats.record("resolve_ok", now=_ts("2026-10-01"), path=p)
    web_stats.record("resolve_fail", now=_ts("2026-10-01"), path=p)
    web_stats.record("page_view", ip="2.2.2.2", now=_ts("2026-10-02"), path=p)
    s = web_stats.summary(7, now=_ts("2026-10-02"), path=p)
    assert len(s["days"]) == 7, "summary 应返回 7 天序列（含空天）"
    assert s["totals"]["pv"] == 2, f"PV 合计应为 2，实际 {s['totals']['pv']}"
    assert s["totals"]["uv"] == 2, f"UV 合计应为 2，实际 {s['totals']['uv']}"
    assert s["totals"]["resolve_ok"] == 1
    assert s["totals"]["resolve_fail"] == 1
    print("✅ summary：7 天序列含空天，totals 各项汇总正确")


def test_keep_days_trim():
    p = web_stats.default_path()
    web_stats.reset(p)
    base = time.mktime(time.strptime("2026-01-01 12:00:00", "%Y-%m-%d %H:%M:%S"))
    for i in range(95):
        web_stats.record("page_view", ip=f"10.0.0.{i % 250}",
                         now=base + i * 86400, path=p)
    n = len(json.loads(p.read_text(encoding="utf-8"))["days"])
    assert n <= web_stats.KEEP_DAYS, f"应只保留 {web_stats.KEEP_DAYS} 天，实际 {n}"
    assert n >= 85, f"裁剪后应接近 90 天，实际 {n}"
    print(f"✅ 滚动裁剪：95 天数据裁剪到 {n} 天")


def test_corrupt_file_tolerated():
    p = web_stats.default_path()
    p.write_text("{ not json", encoding="utf-8")
    assert web_stats.record("page_view", ip="1.1.1.1", now=_ts("2026-10-01"), path=p) is True
    d = web_stats.summary(1, now=_ts("2026-10-01"), path=p)["days"][0]
    assert d["pv"] == 1, "损坏文件应被当作空状态后正常计数"
    print("✅ 容错：统计文件损坏不影响写入与读取")


if __name__ == "__main__":
    test_pv_uv_same_day()
    test_no_ip_plaintext()
    test_uv_cross_day_not_linkable()
    test_event_whitelist()
    test_ref_and_device()
    test_summary_totals()
    test_keep_days_trim()
    test_corrupt_file_tolerated()
    print("🎉 网页访客统计回归测试全部通过")
