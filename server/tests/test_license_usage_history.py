"""server/tests/test_license_usage_history.py — 账号用量「近一年按天历史」（2026-10-18）

背景：授权中心 `user["daily"]` 只存**当天**计数（跨天直接覆盖），后台「使用详情」
因此只能看到今天，翻不了历史。本次在日切那一刻把前一天归档进 `usage_history`，
保留 366 天，并由 `usage_impl` 一并下发给后台面板。

⚠️ 边界（测试也按这个边界写，别把「补历史」当成能补过去）：
   · 归档只发生在**下一次用量上报时的日切**，所以数据从上线当天起累积；
   · 过去已被覆盖的日期无法补录 —— 这组用例只验证「往后不再丢」。

覆盖：
  [A] _archive_daily：只留 >0、键截断、空 counts 不留行、同日幂等；
  [B] daily_impl 跨天时**自动**归档，且当天计数从 0 重新开始；
  [C] _usage_history_view：新的在前、把「今天」补进视图、上限 366；
  [D] usage_impl 响应里带 usage_history（后台面板的数据源）。
"""
from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[2]

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else "  " + detail[:300]))
    if not cond:
        FAILED.append(name)


def _strict(fn):
    """让 pytest 收集时也能看见失败（本套件同时用 run_one / run_pytest_root）。"""
    def wrapper(*a, **kw):
        before = len(FAILED)
        fn(*a, **kw)
        if len(FAILED) > before:
            raise AssertionError("失败项：" + "；".join(FAILED[before:]))
    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    return wrapper


def _load(tmp: pathlib.Path):
    p = REPO / "deploy" / "license_server.py"
    if not p.exists():
        return None
    spec = importlib.util.spec_from_file_location("vdl_license_usage_hist", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.DATA_PATH = tmp / "cards.json"
    m.EVENT_LOG_PATH = tmp / "events.jsonl"
    m.SECRET = "test-secret"
    m.ADMIN_TOKEN = "test-admin-token"
    return m


def _login(m, st: dict, email: str, now: float) -> str:
    m.register_impl(st, email, "pass1234", now, m.SECRET, {"fp": "fp-a", "name": "Mac"})
    out = m.login_impl(st, email, "pass1234", now, m.SECRET, {"fp": "fp-a", "name": "Mac"})
    # 登录响应把 token 放在**顶层**（out["token"]），account 里只有档案字段
    return str(out.get("token") or "")


@_strict
def test_archive_primitive() -> None:
    print("\n[A] _archive_daily 归档原语")
    with tempfile.TemporaryDirectory(prefix="vdl_hist_a_") as td:
        m = _load(pathlib.Path(td))
        if m is None:
            print("  ⏭ deploy/license_server.py 不在本树，跳过")
            return

        user: dict = {}
        m._archive_daily(user, {"date": "2026-10-17", "counts": {"download": 2, "matting": 1}})
        hist = user.get("usage_history") or []
        check("归档出一条", len(hist) == 1, str(hist))
        check("日期正确", hist[0]["date"] == "2026-10-17", str(hist))
        check("计数正确", hist[0]["counts"] == {"download": 2, "matting": 1}, str(hist))

        # 幂等：同一天再归档一次，不应出现两条
        m._archive_daily(user, {"date": "2026-10-17", "counts": {"download": 2, "matting": 1}})
        check("同日重复归档不重复", len(user["usage_history"]) == 1, str(user["usage_history"]))

        # 数值清洗：0 / 负数 / 非数值丢弃
        m._archive_daily(user, {"date": "2026-10-18",
                                "counts": {"download": 0, "sr": -3, "compress": "abc", "sr2": 1}})
        last = user["usage_history"][-1]
        check("0 与负数与非数值被丢弃", last["counts"] == {"sr2": 1}, str(last))

        # 空 counts 不产生空行
        before = len(user["usage_history"])
        m._archive_daily(user, {"date": "2026-10-19", "counts": {}})
        check("空 counts 不留空行", len(user["usage_history"]) == before, str(user["usage_history"]))

        # 无 date 直接忽略
        m._archive_daily(user, {"counts": {"download": 1}})
        check("无 date 直接忽略", len(user["usage_history"]) == before)

        # 键超长截断到 32
        m._archive_daily(user, {"date": "2026-10-20", "counts": {"x" * 60: 1}})
        k = list(user["usage_history"][-1]["counts"].keys())[0]
        check("超长键截断到 32", len(k) == 32, str(len(k)))


@_strict
def test_rollover_archives_automatically() -> None:
    print("\n[B] daily_impl 跨天自动归档")
    with tempfile.TemporaryDirectory(prefix="vdl_hist_b_") as td:
        m = _load(pathlib.Path(td))
        if m is None:
            return
        st: dict = {}
        now = time.time()
        token = _login(m, st, "hist@qq.com", now)
        check("拿到登录 token", bool(token))

        r1 = m.daily_impl(st, token, [{"res": "download", "n": 2, "id": "e1"}], now, m.SECRET)
        check("当天上报成功", r1.get("applied") == 2, str(r1))
        check("首日不进历史（当天还在 daily 里）",
              not (m._users(st)[next(iter(m._users(st)))].get("usage_history")), "不该归档当天")

        # 模拟「第二天又用了一次」→ 触发日切，把昨天归档
        tomorrow = now + 86400
        r2 = m.daily_impl(st, token, [{"res": "download", "n": 1, "id": "e2"}], tomorrow, m.SECRET)
        check("次日上报成功", r2.get("applied") == 1, str(r2))

        user = m._users(st)[next(iter(m._users(st)))]
        hist = user.get("usage_history") or []
        check("跨天后历史里有 1 条（昨天）", len(hist) == 1, str(hist))
        if hist:
            check("昨天计数 = 2", hist[0]["counts"] == {"download": 2}, str(hist[0]))
            check("归档日期不是今天", hist[0]["date"] != m._bj_day(tomorrow), str(hist[0]))
        check("当天计数重新从 1 开始",
              (user.get("daily") or {}).get("counts") == {"download": 1}, str(user.get("daily")))


@_strict
def test_view_shape_and_cap() -> None:
    print("\n[C] _usage_history_view：倒序 + 含今天 + 上限 366")
    with tempfile.TemporaryDirectory(prefix="vdl_hist_c_") as td:
        m = _load(pathlib.Path(td))
        if m is None:
            return
        now = time.time()
        today = m._bj_day(now)
        user = {
            "usage_history": [
                {"date": "2026-10-15", "counts": {"download": 1}},
                {"date": "2026-10-17", "counts": {"download": 3}},
            ],
            "daily": {"date": today, "counts": {"matting": 2}},
        }
        view = m._usage_history_view(user, now)
        check("新的在前", [x["date"] for x in view][0] == today, str(view))
        check("历史日期都在", {"2026-10-15", "2026-10-17"}.issubset({x["date"] for x in view}), str(view))
        check("今天的实时计数被补进视图",
              next((x["counts"] for x in view if x["date"] == today), None) == {"matting": 2}, str(view))

        # 今天已在 history 里时不重复
        user2 = {"usage_history": [{"date": today, "counts": {"download": 9}}],
                 "daily": {"date": today, "counts": {"download": 9}}}
        v2 = m._usage_history_view(user2, now)
        check("今天不重复出现", sum(1 for x in v2 if x["date"] == today) == 1, str(v2))

        # 上限 366
        big = {"usage_history": [{"date": f"20{i//12:02d}-{i%12+1:02d}-01",
                                  "counts": {"download": 1}} for i in range(500)]}
        v3 = m._usage_history_view(big, now)
        check("历史被截到 366 天内", len(v3) <= m.USAGE_HISTORY_DAYS, str(len(v3)))
        check("USAGE_HISTORY_DAYS = 366", m.USAGE_HISTORY_DAYS == 366)

        # 空账号不炸
        check("空账号返回空列表", m._usage_history_view({}, now) == [])


@_strict
def test_usage_impl_exposes_history() -> None:
    print("\n[D] usage_impl 响应带 usage_history（后台面板数据源）")
    with tempfile.TemporaryDirectory(prefix="vdl_hist_d_") as td:
        m = _load(pathlib.Path(td))
        if m is None:
            return
        st: dict = {}
        now = time.time()
        token = _login(m, st, "hist2@qq.com", now)
        m.daily_impl(st, token, [{"res": "download", "n": 5, "id": "d1"}], now, m.SECRET)

        out = m.usage_impl(st, "hist2@qq.com", now)
        check("usage_impl 返回 ok", out.get("ok") is True, str(out)[:200])
        check("响应含 usage_history 字段", isinstance(out.get("usage_history"), list), str(type(out.get("usage_history"))))
        rows = out.get("usage_history") or []
        check("当天用量已在此列（不必等日切）", bool(rows) and rows[0]["counts"] == {"download": 5}, str(rows))

        # 老账号没有 usage_history 字段时，也必须返回 [] 而不是 None/报错
        out2 = m.usage_impl(st, "hist2@qq.com", now + 86400 * 2)
        check("无历史时返回空列表而不是 None", isinstance(out2.get("usage_history"), list), str(out2.get("usage_history")))


def main() -> int:
    print("== test_license_usage_history ==")
    try:
        test_archive_primitive()
        test_rollover_archives_automatically()
        test_view_shape_and_cap()
        test_usage_impl_exposes_history()
    except AssertionError as e:
        print("\n" + str(e))
        return 1
    if FAILED:
        print(f"\n{len(FAILED)} 项失败：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
