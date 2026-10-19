"""server/tests/test_usage_details_ledger.py — 用量明细台账（2026-10-19）

需求来源：用户反馈后台「使用详情」只显示「下载任务 2」，看不出具体下载了什么
（例如「少帅 第5集」），用户来投诉时无法定位。要求明细**按平台分类**展示。

本文件钉死：
  1) `use_daily(title=..., platform=...)` 会把明细写进 `daily_details`；
  2) 没有标题时不写（无标题的条目对排障无价值，且会白白增大云端负载）；
  3) 每资源最多保留 50 条（防止无限增长）；
  4) 跨天日切时明细随计数一起归档进 `usage_history`（否则历史只有次数）；
  5) 上云钩子 `report_usage_async` 必须把标题/平台带进 items。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

import membership as M  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else "  " + detail[:300]))
    if not cond:
        FAILED.append(name)


def _strict(fn):
    def wrapper(*args, **kwargs):
        before = len(FAILED)
        fn(*args, **kwargs)
        if len(FAILED) > before:
            raise AssertionError("失败项：" + "；".join(FAILED[before:]))
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    return wrapper


_TMP: list[str] = []


def _fresh(tag: str):
    d = tempfile.mkdtemp(prefix="vdl_det_")
    _TMP.append(d)
    return M.MembershipStore(path=pathlib.Path(d) / f"m_{tag}.json")


def _cleanup() -> None:
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)
    _TMP.clear()


# ─────────────────────────────────────────────────────────────────────────── #
@_strict
def test_records_details() -> None:
    print("\n[A] use_daily 带标题与平台 → 写入 daily_details")
    st = _fresh("a")
    r = st.use_daily("download", 1, title="少帅 第5集", platform="douyin.com")
    check("计费成功", r.get("ok") is True, str(r))
    det = (st._state.get("daily_details") or {}).get("download") or []
    check("明细落账", len(det) == 1, str(det))
    check("标题原样保留", det and det[0]["t"] == "少帅 第5集", str(det))
    check("平台原样保留", det and det[0]["p"] == "douyin.com", str(det))
    check("带时间戳", det and int(det[0].get("at") or 0) > 0, str(det))

    r2 = st.use_daily("download", 1, title="第6集", platform="douyin.com")
    det2 = (st._state.get("daily_details") or {}).get("download") or []
    check("第二次累计成 2 条", len(det2) == 2, str(det2))

    r3 = st.use_daily("download", 1)
    det3 = (st._state.get("daily_details") or {}).get("download") or []
    check("无标题不记明细（但次数照计）", r3.get("ok") is True and len(det3) == 2, str(det3))
    check("次数仍为 3", int(st._state["daily_usage"]["download"]) == 3,
          str(st._state["daily_usage"]))


@_strict
def test_cap_and_sanitize() -> None:
    print("\n[B] 上限 50 条 / 超长标题截断")
    st = _fresh("b")
    st._ensure_loaded()          # 先触发懒加载，否则下面改的 _state 会被磁盘覆盖
    st._state["daily_details"] = {
        "download": [{"t": f"old{i}", "p": "x.com", "at": 1} for i in range(50)]
    }
    st.use_daily("download", 1, title="x" * 300, platform="y" * 100)
    lst = (st._state.get("daily_details") or {}).get("download") or []
    check("保留最近 50 条（不无限增长）", len(lst) == 50, str(len(lst)))
    check("标题截断到 120 字符", lst[-1]["t"] == "x" * 120, str(len(lst[-1]["t"])))
    check("平台截断到 64 字符", lst[-1]["p"] == "y" * 64, str(len(lst[-1]["p"])))
    check("最旧的一条被挤出", lst[0]["t"] == "old1", str(lst[0]))


@_strict
def test_roll_daily_archives_details() -> None:
    print("\n[C] 跨天日切：明细随计数一起归档进 usage_history")
    import time
    st = _fresh("c")
    st.use_daily("download", 1, title="少帅 第5集", platform="douyin.com")
    st._ensure_loaded()
    # 把「今天」改成昨天，下一次 quota_state 触发 _roll_daily
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    st._state["daily_usage"]["date"] = yesterday
    st.quota_state("download")
    hist = st._state.get("usage_history") or {}
    check("旧一天已归档", yesterday in hist, str(list(hist)))
    row = hist.get(yesterday) or {}
    check("归档行带 details", isinstance(row.get("details"), dict), str(row))
    det = (row.get("details") or {}).get("download") or []
    check("归档明细含标题与平台",
          len(det) == 1 and det[0]["t"] == "少帅 第5集"
          and det[0]["p"] == "douyin.com", str(det))
    check("归档后今日明细已清空",
          not (st._state.get("daily_details") or {}).get("download"),
          str(st._state.get("daily_details")))
    # 计数口径不能被 details 污染（_counts 只认数值）
    counts = {k: v for k, v in row.items() if isinstance(v, int)}
    check("计数仍是 1（details 不参与计数）", counts.get("download") == 1, str(counts))


@_strict
def test_report_hook_carries_title() -> None:
    print("\n[D] 上云钩子把标题/平台带进 items（静态断言产品代码）")
    src = (ROOT / "server" / "routers" / "cloud_account.py").read_text(encoding="utf-8")
    check("report_usage_async 接受 title/platform",
          'def report_usage_async(store, resource: str, n: int = 1, title: str = "",\n'
          '                       platform: str = "") -> None:' in src)
    check("有标题时才带 t（避免无谓负载）", 'if _t:' in src and 'it["t"] = _t' in src)
    check("平台随 p 一起上报", 'it["p"] = str(platform or "").strip()[:64]' in src)
    m_src = (ROOT / "server" / "membership.py").read_text(encoding="utf-8")
    check("use_daily 把标题透传给上云钩子",
          "report_usage_async(self, resource, n, title=title, platform=platform)" in m_src)


def main() -> int:
    print("== test_usage_details_ledger ==")
    try:
        test_records_details()
        test_cap_and_sanitize()
        test_roll_daily_archives_details()
        test_report_hook_carries_title()
    except AssertionError as e:
        print("\n" + str(e))
        return 1
    finally:
        _cleanup()
    if FAILED:
        print(f"\n{len(FAILED)} 项失败：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
