"""server/tests/test_usage_result_flag.py — 「这次下载到底成功了没有」（2026-10-20）

需求来源：用户要求在后台「使用详情」里能看到每次下载是否成功。
成功的定义由用户给出：**下载完成、成品可以保存到本机**。

为什么不能只看「次数」：配额是**下单那一刻**就扣的（那时还不知道能不能下成），
所以旧的「下载任务 3」分不清「下了 3 个都成功」和「点了 3 次全失败」。

本文件钉死：
  1) use_daily 返回 ref，并把 ref 落到明细条目上（否则结果回写无从定位）；
  2) mark_usage_result(ref, True/False) 把 r=1/0 写回当天条目；
  3) 跨天归档后（明细进了 usage_history）仍能按 ref 回写；
  4) 本机账本 _usage_bundle 把 r 透传给面板；
  5) 授权中心 daily_impl：n=0 + r 只更新结果、**绝不重复计数**；找不到就静默放弃；
  6) 云端 _clean_details 不丢 id/r（丢了归档后结果就没了）；
  7) 下载器只在**终态**回写，成功判定要求成品文件真实存在且非空；
  8) 两端上云的幂等键必须一致（计数侧 / 结果侧同一个 id 串）；
  9) 前端明细渲染出 ✅已保存 / ❌未完成 / 未上报 三种标记。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import sys
import tempfile

os.environ["VDL_OFFLINE_TESTS"] = "1"   # 禁止真实上云（本文件会跑非 pytest 的脚本路径）

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
    d = tempfile.mkdtemp(prefix="vdl_uflag_")
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
    print("\n[A] use_daily 返回 ref 并落到明细条目上")
    st = _fresh("a")
    r = st.use_daily("download", 1, title="少帅 第5集", platform="douyin.com")
    ref = r.get("ref")
    check("返回 ref", bool(ref) and str(ref).startswith("u"), str(r))
    check("ref ≤64 字符（云端幂等键上限）", len(str(ref)) <= 64, str(ref))
    det = _det(st)
    check("明细带同一个 id", det and det[0].get("id") == ref, str(det))
    check("初始不带结果字段（区分「未上报」与「失败」）",
          det and "r" not in det[0], str(det))
    # 两次调用 ref 必须不同，否则会互相覆盖
    r2 = st.use_daily("download", 1, title="第6集", platform="douyin.com")
    check("每次消耗 ref 唯一", r2.get("ref") and r2.get("ref") != ref, str(r2))


@_strict
def test_mark_success_and_fail() -> None:
    print("\n[B] mark_usage_result 回写成功/失败（次数不变）")
    st = _fresh("b")
    ref = st.use_daily("download", 1, title="少帅 第5集", platform="douyin.com")["ref"]
    hit = st.mark_usage_result(ref, True, size=12_345_678)
    check("命中当天条目", hit is True)
    it = _det(st)[0]
    check("成功 → r=1", it.get("r") == 1, str(it))
    check("带上成品大小", it.get("sz") == 12_345_678, str(it))
    check("次数没有被结果回写改动", int(st._state["daily_usage"]["download"]) == 1,
          str(st._state["daily_usage"]))

    ref2 = st.use_daily("download", 1, title="第6集", platform="douyin.com")["ref"]
    st.mark_usage_result(ref2, False)
    items = _det(st)
    check("失败 → r=0", items[-1].get("r") == 0, str(items))
    check("失败不带大小", "sz" not in items[-1], str(items))
    check("两次消耗仍是 2 次", int(st._state["daily_usage"]["download"]) == 2,
          str(st._state["daily_usage"]))

    check("未知 ref 不假装成功", st.mark_usage_result("u_not_exist", True) is False)
    check("空 ref 直接返回 False", st.mark_usage_result("", True) is False)

    # 幂等：重复回写同一 ref 只覆盖成同一结果
    st.mark_usage_result(ref2, False)
    check("重复回写幂等（仍为 0）", _det(st)[-1].get("r") == 0, str(_det(st)))


@_strict
def test_cross_day_archive() -> None:
    print("\n[C] 跨天归档后（明细进了 usage_history）仍能回写")
    st = _fresh("c")
    st._ensure_loaded()   # 🔴 惰性加载：先落定再改 _state，否则 mark 内部加载会冲掉手工数据
    ref = "u" + "a" * 16
    st._state["usage_history"] = {
        "2026-10-19": {
            "download": 1,
            "details": {"download": [{"t": "少帅 第5集", "p": "douyin.com",
                                      "at": 1760800000, "id": ref}]},
        }
    }
    st._state["daily_details"] = {}       # 今天没有明细（说明任务跨天完成）
    hit = st.mark_usage_result(ref, True, size=1024)
    check("命中历史条目", hit is True)
    hist_item = st._state["usage_history"]["2026-10-19"]["details"]["download"][0]
    check("历史条目被标成 r=1", hist_item.get("r") == 1, str(hist_item))


@_strict
def test_usage_bundle_passes_r() -> None:
    print("\n[D] 本机账本 → 后台 _usage_bundle 透传 r")
    from routers import admin as A  # 延迟导入：避免污染其他用例

    st = _fresh("bundle")
    ref = st.use_daily("download", 1, title="少帅 第5集", platform="douyin.com")["ref"]
    st.mark_usage_result(ref, True, size=2048)
    st.use_daily("download", 1, title="第6集", platform="douyin.com")   # 未回写 → 未上报

    b = A._usage_bundle("u_probe", "probe@self.test", False, False, 0, st)
    days = b.get("usage_days") or []
    det = ((days[0] if days else {}).get("details") or {}).get("download") or []
    check("明细 2 条", len(det) == 2, str(det))
    ok_rows = [d for d in det if d.get("r") == 1]
    check("成功条目的 r 被透传", len(ok_rows) == 1, str(det))
    check("未回写的条目**不带** r（面板显示未上报，不能当成失败）",
          any("r" not in d for d in det), str(det))


# ─────────────────────────── 授权中心（云端）───────────────────────────────── #
def _load_license_server():
    spec = importlib.util.spec_from_file_location(
        "license_server_uflag", ROOT / "deploy" / "license_server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["license_server_uflag"] = mod
    spec.loader.exec_module(mod)
    return mod


@_strict
def test_cloud_result_does_not_double_count() -> None:
    print("\n[E] 授权中心 daily_impl：n=0 + r 只更新结果、不重复计数")
    mod = _load_license_server()
    SECRET = "unit-secret"
    NOW = 1760000000.0
    st: dict = {}
    reg = mod.register_impl(st, "flag@test.com", "secret123", NOW, SECRET,
                            {"fp": "a" * 32, "name": "Mac"})
    token = reg["token"]

    ID = "uAAAAAAAAAAAAAAAA-download"       # 与客户端一致：ref + "-" + res[:16]
    r1 = mod.daily_impl(st, token, [{"res": "download", "n": 1, "id": ID,
                                     "t": "少帅 第5集", "p": "douyin.com"}], NOW, SECRET)
    check("先计数成功", r1.get("ok") and r1.get("applied") == 1, str(r1)[:200])
    user = list(mod._users(st).values())[0]
    daily = user.get("daily") or {}
    check("当天计数 = 1", int((daily.get("counts") or {}).get("download") or 0) == 1,
          str(daily.get("counts")))
    item = ((daily.get("details") or {}).get("download") or [{}])[0]
    check("明细条目落 id", item.get("id") == ID, str(item))
    check("计数时还没有结果（未上报）", "r" not in item, str(item))

    # 结果回写：n=0 + r=1
    r2 = mod.daily_impl(st, token, [{"res": "download", "n": 0, "id": ID, "r": 1}],
                        NOW + 60, SECRET)
    check("结果回写成功返回", r2.get("ok") is True, str(r2)[:200])
    daily = list(mod._users(st).values())[0].get("daily") or {}
    check("🔴 计数没有被结果条目再加一次",
          int((daily.get("counts") or {}).get("download") or 0) == 1,
          str(daily.get("counts")))
    item = ((daily.get("details") or {}).get("download") or [{}])[0]
    check("条目被标成 r=1", item.get("r") == 1, str(item))

    # 失败回写 + 幂等重发
    ID2 = "uBBBBBBBBBBBBBBBB-download"
    mod.daily_impl(st, token, [{"res": "download", "n": 1, "id": ID2,
                                "t": "第6集", "p": "douyin.com"}], NOW + 120, SECRET)
    mod.daily_impl(st, token, [{"res": "download", "n": 0, "id": ID2, "r": 0}],
                   NOW + 180, SECRET)
    mod.daily_impl(st, token, [{"res": "download", "n": 0, "id": ID2, "r": 0}],
                   NOW + 240, SECRET)
    daily = list(mod._users(st).values())[0].get("daily") or {}
    check("失败 → 最后一条 r=0",
          ((daily.get("details") or {}).get("download") or [{}])[-1].get("r") == 0,
          str(daily.get("details")))
    check("两次计数仍是 2（结果重发不涨）",
          int((daily.get("counts") or {}).get("download") or 0) == 2,
          str(daily.get("counts")))

    # 结果先到、计数未到：静默放弃，不崩、不凭空造计数
    r3 = mod.daily_impl(st, token, [{"res": "download", "n": 0, "id": "uCCC-unknown", "r": 1}],
                        NOW + 300, SECRET)
    check("未知 id 的结果回写不报错", r3.get("ok") is True, str(r3)[:200])
    daily = list(mod._users(st).values())[0].get("daily") or {}
    check("未知 id 不增加计数",
          int((daily.get("counts") or {}).get("download") or 0) == 2,
          str(daily.get("counts")))

    # 完全空条目（无 n 无 r）不应该占用任何东西
    previous = int((daily.get("counts") or {}).get("download") or 0)
    mod.daily_impl(st, token, [{"res": "download"}], NOW + 360, SECRET)
    daily = list(mod._users(st).values())[0].get("daily") or {}
    check("既无次数也无结果的条目被丢弃",
          int((daily.get("counts") or {}).get("download") or 0) == previous,
          str(daily.get("counts")))


@_strict
def test_cloud_cross_day_result() -> None:
    print("\n[F] 授权中心：跨天完成的结果能标到已归档的历史条目上")
    mod = _load_license_server()
    SECRET = "unit-secret"
    NOW = 1760000000.0
    st: dict = {}
    reg = mod.register_impl(st, "flag2@test.com", "secret123", NOW, SECRET,
                            {"fp": "b" * 32, "name": "Mac"})
    token = reg["token"]
    ID = "uDDDDDDDDDDDDDDDD-download"
    mod.daily_impl(st, token, [{"res": "download", "n": 1, "id": ID,
                                "t": "跨天任务", "p": "bilibili"}], NOW, SECRET)
    # 第二天有新的用量 → daily_impl 先归档前一天
    mod.daily_impl(st, token, [{"res": "download", "n": 1, "id": "uEEEE-next-download",
                                "t": "新的一天", "p": "bilibili"}], NOW + 86400, SECRET)
    # 老任务这时才下载完成 → 回写结果
    mod.daily_impl(st, token, [{"res": "download", "n": 0, "id": ID, "r": 1}],
                   NOW + 86460, SECRET)
    user = list(mod._users(st).values())[0]
    hist = user.get("usage_history") or []
    old = next((h for h in hist if any(
        it.get("id") == ID for it in ((h.get("details") or {}).get("download") or []))), None)
    check("前一天被归档进 usage_history", old is not None, str(hist)[:400])
    item = next((it for it in (((old or {}).get("details") or {}).get("download") or [])
                 if it.get("id") == ID), {})
    check("历史条目被标成 r=1", item.get("r") == 1, str(item))
    daily = user.get("daily") or {}
    check("当天计数只有新那 1 次（结果没加次数）",
          int((daily.get("counts") or {}).get("download") or 0) == 1,
          str(daily.get("counts")))


@_strict
def test_clean_details_keeps_id_and_r() -> None:
    print("\n[G] 云端 _clean_details 保留 id/r（否则归档后结果丢失）")
    mod = _load_license_server()
    raw = {"download": [
        {"t": "少帅 第5集", "p": "douyin.com", "at": 1, "id": "u1-download", "r": 1},
        {"t": "第6集", "p": "douyin.com", "at": 2, "id": "u2-download", "r": 0},
        {"t": "旧数据", "p": "douyin.com", "at": 3},
    ]}
    out = mod._clean_details(raw)
    items = out.get("download") or []
    check("三条都保留", len(items) == 3, str(items))
    check("id 保留", items[0].get("id") == "u1-download", str(items))
    check("成功标记保留", items[0].get("r") == 1, str(items))
    check("失败标记保留（不会被假值过滤掉）", items[1].get("r") == 0, str(items))
    check("老数据不带 r（前端显示未上报）", "r" not in items[2], str(items))


# ─────────────────────────── 接线 / 前端（静态断言）────────────────────────── #
@_strict
def test_wiring() -> None:
    print("\n[H] 接线：幂等键一致、下载器只在终态回写、成功要求文件非空")
    cc = (ROOT / "server" / "routers" / "cloud_account.py").read_text(encoding="utf-8")
    check("有结果上云函数", "def report_usage_result_async(" in cc)
    check("计数侧幂等键 = ref + '-' + res[:16]",
          'it: dict = {"res": res, "n": n, "id": f"{ev}-{res[:16]}"}' in cc)
    check("结果侧幂等键必须与计数侧同形",
          '"id": f"{key}-{res[:16]}"' in cc)
    check("结果侧 n=0（只更新结果不计数）", '"n": 0, "r": rv' in cc)

    dl = (ROOT / "server" / "downloader.py").read_text(encoding="utf-8")
    check("有结果回写函数", "def _note_usage_result(" in dl)
    check("成功判定要求 completed", 'if task.status != "completed"' in dl)
    check("成功判定要求文件存在", "if not p.exists():" in dl)
    check("成功判定要求非空（>0 字节）", "return (sz > 0), (sz if sz > 0 else 0)" in dl)
    check("run_download 终态 finally 回写",
          "        _note_usage_result_by_task(task)\n" in dl)
    check("失败终态（停滞/超时/取消）也回写",
          "    # 2026-10-20：停滞/超时/用户取消都不算「保存到本机」，把失败结果补回账本" in dl)
    check("内部重试的中间失败不报（依据 _usage_reported 幂等）",
          'if getattr(task, "_usage_reported", False):' in dl)

    core = (ROOT / "server" / "routers" / "core.py").read_text(encoding="utf-8")
    check("下单时把 ref 挂到任务上", 'task.usage_ref = str(_charged["ref"])' in core)
    check("重试时同样挂 ref", 'task.usage_ref = str(_ch["ref"])' in core)

    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    check("前端保留 r 字段", "if (it.r === 0 || it.r === 1) row.r = it.r;" in app_js)
    check("前端渲染成功标记", "aud-res ok" in app_js and "✅ 已保存" in app_js)
    check("前端渲染失败标记", "aud-res fail" in app_js and "❌ 未完成" in app_js)
    check("前端渲染未上报标记", ">未上报</span>" in app_js)
    check("面板说明写清成功口径",
          "＝这次下载已完成、成品确实存到了用户本机" in app_js)

    css = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
    check("样式有 .aud-res.ok", ".admin-usage-dlg .aud-res.ok" in css)
    check("样式有 .aud-res.fail", ".admin-usage-dlg .aud-res.fail" in css)
    check("样式有 .aud-res.unk", ".admin-usage-dlg .aud-res.unk" in css)

    adm = (ROOT / "server" / "routers" / "admin.py").read_text(encoding="utf-8")
    check("admin 透传 r", 'if it.get("r") is not None:' in adm)


@_strict
def test_downloader_marks_result() -> None:
    """[I] 真调下载器的回写函数：completed+文件存在才算成功，空文件/失败都不算。"""
    print("\n[I] 下载器终态判定 → r=1 / r=0")
    import downloader as DL          # 较重（拉 yt_dlp），放在最后
    from tasks import DownloadTask

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_out_"))
    _TMP.append(str(tmp))

    def _mk(store, ref, status, fp=None):
        t = DownloadTask(id="t_" + ref[-6:], url="https://x/y", title="少帅 第5集",
                         platform="douyin.com", quality="1080", status=status,
                         usage_ref=ref)
        t.filepath = fp
        t._usage_store = store
        return t

    st = _fresh("dl1")
    ref = st.use_daily("download", 1, title="少帅 第5集", platform="douyin.com")["ref"]
    f = tmp / "out.mp4"
    f.write_bytes(b"x" * 4096)
    t = _mk(st, ref, "completed", f)
    DL._note_usage_result_by_task(t)
    check("completed + 文件存在 → r=1", _det(st)[-1].get("r") == 1, str(_det(st)))
    check("带上成品字节数", _det(st)[-1].get("sz") == 4096, str(_det(st)))
    before = dict(_det(st)[-1])
    DL._note_usage_result_by_task(t)          # 再来一次（幂等）
    check("重复回写不改变结果/不重复记账",
          _det(st)[-1] == before and len(_det(st)) == 1, str(_det(st)))

    st2 = _fresh("dl2")
    ref2 = st2.use_daily("download", 1, title="空文件", platform="p")["ref"]
    empty = tmp / "empty.mp4"
    empty.write_bytes(b"")
    DL._note_usage_result_by_task(_mk(st2, ref2, "completed", empty))
    check("completed 但文件 0 字节 → r=0（不把空文件当成功）",
          _det(st2)[-1].get("r") == 0, str(_det(st2)))

    st3 = _fresh("dl3")
    ref3 = st3.use_daily("download", 1, title="失败任务", platform="p")["ref"]
    DL._note_usage_result_by_task(_mk(st3, ref3, "failed", None))
    check("failed → r=0", _det(st3)[-1].get("r") == 0, str(_det(st3)))

    st4 = _fresh("dl4")
    ref4 = st4.use_daily("download", 1, title="已取消", platform="p")["ref"]
    DL._note_usage_result_by_task(_mk(st4, ref4, "canceled", None))
    check("canceled → r=0", _det(st4)[-1].get("r") == 0, str(_det(st4)))


def main() -> int:
    print("== test_usage_result_flag ==")
    try:
        test_ref_recorded()
        test_mark_success_and_fail()
        test_cross_day_archive()
        test_usage_bundle_passes_r()
        test_cloud_result_does_not_double_count()
        test_cloud_cross_day_result()
        test_clean_details_keeps_id_and_r()
        test_wiring()
        test_downloader_marks_result()
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
