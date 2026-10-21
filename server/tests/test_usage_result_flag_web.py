"""server/tests/test_usage_result_flag_web.py — 网页版「这次下载到底成功了没有」（2026-10-21）

背景（实测证据，不是推测）：后台「使用详情」里 18 条明细中 0 条带结果，
其中 7 条 id 形如 `af3a3dd4323b41df-download`（随机 hex）——那是**网页版**
上报的用量；桌面 App 的 ref 形如 `u<16hex>`。原因不是面板坏，而是网页版
当时压根没有回写结果的链路：`use_daily` 不接受 ref、上云 id 是随机键、
下载完成也不回报。于是这些记录永远只有「未上报」。

本文件钉死修复后的契约：
  1) use_daily 返回 ref，并把 ref 落到本机明细条目上（否则结果无从定位）；
  2) mark_usage_result(ref, True/False) 写回 r=1/0，**不改动次数**；
  3) 跨天归档（明细进 usage_history）后仍能按 ref 回写；
  4) 上云「计数」与「结果」两侧的幂等键必须逐字一致（否则云端找不到那条明细）；
  5) 下载器只在终态回写，成功判据＝completed 且成品文件真实存在；
  6) 任务把 usage_ref 持久化（重启后下完也能回写）；
  7) 浏览器直存（源站直链/双轨合并）由前端回报 /api/usage/result。
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tempfile

os.environ["VDL_OFFLINE_TESTS"] = "1"   # 禁止真实上云（本文件走非 pytest 脚本路径）

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

import membership as M  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else "  " + detail[:400]))
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
    d = tempfile.mkdtemp(prefix="vdl_uflag_web_")
    _TMP.append(d)
    return M.MembershipStore(path=pathlib.Path(d) / f"m_{tag}.json")


def _cleanup() -> None:
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)
    _TMP.clear()


def _det(st, res: str = "download") -> list[dict]:
    return (st._state.get("daily_details") or {}).get(res) or []


# ─────────────────────────────────────────────────────────────────────────── #
@_strict
def test_ref_recorded() -> None:
    print("\n[A] use_daily 返回 ref 并落到明细条目上（网页版原来完全没有）")
    st = _fresh("a")
    r = st.use_daily("download", 1, title="抬眸有光低眸含情", platform="小红书")
    ref = r.get("ref")
    check("返回 ref", bool(ref) and str(ref).startswith("u"), str(r))
    check("ref ≤64 字符（云端幂等键上限）", len(str(ref)) <= 64, str(ref))
    det = _det(st)
    check("明细带同一个 id", bool(det) and det[0].get("id") == ref, str(det))
    check("初始不带结果字段（区分「未上报」与「失败」）", bool(det) and "r" not in det[0], str(det))
    r2 = st.use_daily("download", 1, title="第二条", platform="小红书")
    check("每次消耗 ref 唯一", r2.get("ref") and r2.get("ref") != ref, str(r2))


@_strict
def test_mark_success_and_fail() -> None:
    print("\n[B] mark_usage_result 回写成功/失败（次数不变）")
    st = _fresh("b")
    ref = st.use_daily("download", 1, title="A", platform="小红书")["ref"]
    hit = st.mark_usage_result(ref, True, size=12_345_678)
    check("命中当天条目", hit is True)
    it = _det(st)[0]
    check("成功 → r=1", it.get("r") == 1, str(it))
    check("带上成品大小", it.get("sz") == 12_345_678, str(it))
    check("次数没有被结果回写改动", int(st._state["daily_usage"]["download"]) == 1,
          str(st._state["daily_usage"]))

    ref2 = st.use_daily("download", 1, title="B", platform="小红书")["ref"]
    st.mark_usage_result(ref2, False)
    items = _det(st)
    check("失败 → r=0", items[-1].get("r") == 0, str(items))
    check("失败不带大小", "sz" not in items[-1], str(items))
    check("两次消耗仍是 2 次", int(st._state["daily_usage"]["download"]) == 2,
          str(st._state["daily_usage"]))
    check("未知 ref 不假装成功", st.mark_usage_result("u_not_exist", True) is False)
    check("空 ref 直接返回 False", st.mark_usage_result("", True) is False)
    st.mark_usage_result(ref2, False)
    check("重复回写幂等（仍为 0）", _det(st)[-1].get("r") == 0, str(_det(st)))


@_strict
def test_cross_day_archive() -> None:
    print("\n[C] 跨天归档后（明细进了 usage_history）仍能回写")
    st = _fresh("c")
    st._ensure_loaded()   # 🔴 惰性加载：先落定再改 _state，否则内部加载会冲掉手工数据
    ref = "u" + "a" * 16
    st._state["usage_history"] = {
        "2026-10-19": {
            "download": 1,
            "details": {"download": [{"t": "A", "p": "小红书",
                                      "at": 1760800000, "id": ref}]},
        }
    }
    st._state["daily_details"] = {}
    hit = st.mark_usage_result(ref, True, size=1024)
    check("命中历史条目", hit is True)
    hist_item = st._state["usage_history"]["2026-10-19"]["details"]["download"][0]
    check("历史条目被标成 r=1", hist_item.get("r") == 1, str(hist_item))


@_strict
def test_cloud_idempotency_key_identical() -> None:
    print("\n[D] 上云「计数」与「结果」两侧幂等键必须一致")
    src = (ROOT / "server" / "cloud_link.py").read_text(encoding="utf-8")
    check("计数侧 id = <ref>-<res 前16>", '{"res": res, "n": n, "id": f"{ev}-{res[:16]}"}' in src)
    check("结果侧 id = <ref>-<res 前16>", '"id": f"{key}-{res[:16]}"' in src)
    check("结果侧 n=0（只更新结果、不重复计数）", '"n": 0, "r": rv' in src)
    check("ref 为空才退回随机键（老行为）", 'ev = str(ref or "").strip()[:64] or uuid.uuid4().hex[:16]' in src)


@_strict
def test_downloader_terminal_hook() -> None:
    print("\n[E] 下载器只在终态回写；成功＝completed 且成品真实存在")
    import downloader as D
    from tasks import DownloadTask

    d = tempfile.mkdtemp(prefix="vdl_ok_")
    _TMP.append(d)
    p = pathlib.Path(d) / "out.mp4"
    p.write_bytes(b"x" * 2048)

    t = DownloadTask(id="a" * 16, url="u", title="t", platform="小红书",
                     quality="best", workdir=pathlib.Path(d), filepath=p)
    t.status = "completed"
    check("completed + 文件在 → 成功", D._usage_result_ok(t) == (True, 2048),
          str(D._usage_result_ok(t)))

    t2 = DownloadTask(id="b" * 16, url="u", title="t", platform="小红书",
                      quality="best", workdir=pathlib.Path(d), filepath=pathlib.Path(d) / "gone.mp4")
    t2.status = "completed"
    check("completed 但成品已被清理 → 不算成功", D._usage_result_ok(t2) == (False, 0),
          str(D._usage_result_ok(t2)))

    t3 = DownloadTask(id="c" * 16, url="u", title="t", platform="小红书",
                      quality="best", workdir=pathlib.Path(d), filepath=p)
    t3.status = "failed"
    check("failed → 不算成功", D._usage_result_ok(t3) == (False, 0), str(D._usage_result_ok(t3)))

    # 同一任务只报一次
    st = _fresh("e")
    ref = st.use_daily("download", 1, title="A", platform="小红书")["ref"]
    t4 = DownloadTask(id="d" * 16, url="u", title="t", platform="小红书",
                      quality="best", workdir=pathlib.Path(d), filepath=p)
    t4.status = "completed"
    t4.usage_ref = ref
    t4._usage_store = st
    D._note_usage_result_by_task(t4)
    check("终态回写命中账本", _det(st)[0].get("r") == 1, str(_det(st)))
    st.mark_usage_result(ref, False)          # 外部又改一次（例如前端取消保存）
    D._note_usage_result_by_task(t4)          # 内部不允许再覆盖
    check("同一任务只报一次（外部改成失败后，内部不会覆盖回成功）",
          _det(st)[0].get("r") == 0, str(_det(st)))

    src = (ROOT / "server" / "downloader.py").read_text(encoding="utf-8")
    check("钩子在 finally 里（成功/失败/取消/被终止都报）",
          "finally:\n        # 2026-10-21：任务终态回写" in src)


@_strict
def test_task_persists_usage_ref() -> None:
    print("\n[F] 任务持久化 usage_ref（重启后下完也能回写）")
    from tasks import TaskStore
    d = pathlib.Path(tempfile.mkdtemp(prefix="vdl_task_"))
    _TMP.append(str(d))
    store = TaskStore(d)
    task = store.create(url="https://example.com/v", title="t", platform="小红书",
                        quality="best")
    task.usage_ref = "u" + "b" * 16
    store._persist_locked(force=True)
    raw = (d / "tasks_state.json").read_text(encoding="utf-8")
    check("落盘文件里带 usage_ref", "usage_ref" in raw, raw[:200])
    reloaded = TaskStore(d)
    restored = reloaded.get(task.id)
    check("重启后能读回同一个 ref", restored and restored.usage_ref == task.usage_ref,
          str(getattr(restored, "usage_ref", None)))


@_strict
def test_api_and_frontend_wiring() -> None:
    print("\n[G] 接口 + 前端接线（浏览器直存也要回报）")
    router_src = (ROOT / "server" / "routers" / "membership.py").read_text(encoding="utf-8")
    check("新增 /api/usage/result 接口", '"/api/usage/result"' in router_src)
    check("按 task_id 取 ref（前端无法伪造别人的 ref）",
          'ref = str(getattr(task, "usage_ref", "") or "") or ref' in router_src)
    check("设备归属校验", "app._require_task(tid, _req_device(request))" in router_src)

    core_src = (ROOT / "server" / "routers" / "core.py").read_text(encoding="utf-8")
    check("单条下载把 ref 挂到任务", "_attach_usage_ref(task, _mq_raw, request, gate)" in core_src)
    check("批量逐条收集 ref（否则前 N-1 条永远未上报）",
          "results=_mq_results" in core_src and "_attach_usage_ref(_btask, _mqr" in core_src)

    web_src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    check("前端保存成功后回报", "reportUsageResult(true, 0)" in web_src)
    check("前端取消保存也回报（诚实标未完成）", "reportUsageResult(false, 0)" in web_src)
    check("回报走 /api/usage/result", "`${apiBase}/api/usage/result`" in web_src)


@_strict
def test_online_payload_pair() -> None:
    """真跑一遍「计数上报 → 结果上报」两条线程，抓实际发出的 items。

    这是本次故障的核心：两侧 id 只要有一个字符不同，云端就找不到那条明细，
    表现就是面板永远「未上报」。所以必须抓**实际载荷**比对，不能只看源码。
    """
    print("\n[H] 真实上云载荷：计数与结果必须指向同一个 id")
    import time as _t
    import license_client
    import cloud_link

    sent: list[list[dict]] = []
    orig = getattr(license_client, "daily_remote", None)

    def fake_daily_remote(token, items, base_url=None, timeout=None, opener=None):
        sent.append([dict(x) for x in items])
        return {"ok": True, "applied": 1, "authority": {}}

    license_client.daily_remote = fake_daily_remote
    os.environ.pop("VDL_OFFLINE_TESTS", None)   # 放开上云闸门（只到假客户端）
    os.environ["VDL_CLOUD_LINK"] = "1"
    try:
        st = _fresh("h")
        st._ensure_loaded()
        st._state.setdefault("meta", {})["account"] = {"email": "probe@self.test",
                                                       "token": "tok-probe"}
        ref = st.use_daily("download", 1, title="抬眸有光低眸含情",
                           platform="小红书")["ref"]
        for _ in range(60):
            if sent:
                break
            _t.sleep(0.05)
        st.mark_usage_result(ref, True, size=2048)
        for _ in range(60):
            if len(sent) >= 2:
                break
            _t.sleep(0.05)
    finally:
        os.environ["VDL_OFFLINE_TESTS"] = "1"
        if orig is not None:
            license_client.daily_remote = orig

    check("两条上报都真的发出去了", len(sent) >= 2, str(sent))
    if len(sent) >= 2:
        count_item = sent[0][0]
        result_item = sent[-1][0]
        check("计数侧带 n=1 与 id", count_item.get("n") == 1 and count_item.get("id"), str(count_item))
        check("结果侧 n=0（不重复计数）", result_item.get("n") == 0, str(result_item))
        check("结果侧 r=1", result_item.get("r") == 1, str(result_item))
        check("🔴 两侧 id 逐字一致（不一致就显得永远「未上报」）",
              count_item.get("id") == result_item.get("id"),
              f"{count_item.get('id')} != {result_item.get('id')}")


def main() -> int:
    print("=" * 74)
    print("网页版「下载是否成功」结果回写 —— 离线契约测试（2026-10-21）")
    print("=" * 74)
    try:
        test_ref_recorded()
        test_mark_success_and_fail()
        test_cross_day_archive()
        test_cloud_idempotency_key_identical()
        test_downloader_terminal_hook()
        test_task_persists_usage_ref()
        test_api_and_frontend_wiring()
        test_online_payload_pair()
    finally:
        _cleanup()
    print("\n" + ("✅ 全部通过" if not FAILED else f"❌ 失败 {len(FAILED)} 项：" + "；".join(FAILED)))
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
