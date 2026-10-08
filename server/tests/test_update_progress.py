"""更新下载「实时进度」离线测试（server/routers/system.py）。

背景（2026-10-08 用户反馈）：点「立即更新」后界面一直停在「正在下载中… 10%」，
下载 472MB 全量包的全过程毫无变化。

根因：进度是**硬编码三跳** —— _run_update 进下载阶段写死 10，下载过程一次都不上报，
下载完才跳到 applying=80、ready=100。而 _http_download 里其实早就算出了百分比，
却写成了 `_ = int(done * 100 / total)` 直接丢掉（注释还写着「留作扩展」）—— 半成品。

本测试覆盖三层，任何一层被改回旧样都立即红：
  1. _http_download 真的按分片回调进度（原缺陷：0 次回调）；
  2. _prepare_update 把下载进度映射到 [10, 80]（下载完恰好 80，交给 applying 接管）；
  3. _run_update 把回调接到 job 状态上 ⇒ /api/system/update/status 的 progress 会变
     （这一条直接对应用户看到的「卡在 10%」）。
"""
import os
import sys
import tempfile
from pathlib import Path

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from routers import system  # noqa: E402

MB = 1024 * 1024


def _fake_urlopen(chunks, total):
    """假 urlopen：按 chunks 逐次吐出，headers 带 Content-Length。"""
    class _Resp:
        def __init__(self):
            self.headers = {"Content-Length": str(total)}
            self._it = iter(chunks)

        def read(self, _n=-1):
            try:
                return next(self._it)
            except StopIteration:
                return b""

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    def _open(_req, timeout=None):  # noqa: ARG001
        return _Resp()
    return _open


def test_http_download_reports_progress():
    """① 下载过程必须真的多次回调进度（旧代码 0 次 —— 直接丢弃算好的百分比）。"""
    total = 10 * MB
    chunks = [b"x" * MB for _ in range(10)]
    seen = []
    orig = system.urllib.request.urlopen
    system.urllib.request.urlopen = _fake_urlopen(chunks, total)
    try:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "pkg.zip"
            system._http_download("http://example.invalid/pkg.zip", dest,
                                  on_progress=lambda d, t: seen.append((d, t)))
            assert dest.stat().st_size == total, "文件应完整落盘"
    finally:
        system.urllib.request.urlopen = orig

    assert len(seen) >= 2, f"下载过程必须多次上报进度（原缺陷是 0 次），实际 {len(seen)} 次"
    assert seen[-1][0] == total, "最后一次必须上报「全部下完」"
    pcts = [int(d * 100 / t) for d, t in seen]
    assert pcts == sorted(pcts), f"进度必须单调不减，实际 {pcts}"
    assert pcts[-1] == 100, f"最终必须到 100%，实际 {pcts[-1]}"


def test_http_download_without_callback_still_works():
    """①b 不传回调时不得报错（兼容既有调用方）。"""
    total = 2 * MB
    orig = system.urllib.request.urlopen
    system.urllib.request.urlopen = _fake_urlopen([b"y" * MB, b"y" * MB], total)
    try:
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "pkg.zip"
            system._http_download("http://example.invalid/pkg.zip", dest)
            assert dest.stat().st_size == total
    finally:
        system.urllib.request.urlopen = orig


def test_prepare_update_maps_download_to_10_80():
    """② 全量包下载进度应线性落在 [10, 80]，下完恰好 80。"""
    pcts = []
    calls = []

    def fake_dl(url_, dest_, timeout=None, on_progress=None):  # noqa: ARG001
        calls.append(url_)
        for d in (25, 50, 75, 100):  # 模拟 25% / 50% / 75% / 100% 四个分片节点
            if on_progress:
                on_progress(d, 100)
        raise RuntimeError("模拟下载后中断，避免真的去 ditto/codesign")

    orig = system._http_download
    system._http_download = fake_dl
    try:
        with tempfile.TemporaryDirectory() as td:
            # data 不带 from_version/patch_url ⇒ 直接走全量分支
            system._prepare_update({"url": "http://example.invalid/a.zip"},
                                   Path(td), Path("/nonexistent.app"), "9.9.9",
                                   on_pct=lambda p: pcts.append(p))
    finally:
        system._http_download = orig

    assert calls, "应调用过下载"
    assert pcts, "全量下载必须把进度上报出来（否则界面只能停在 10%）"
    assert all(10.0 - 1e-6 <= p <= 80.0 + 1e-6 for p in pcts), \
        f"全量下载进度应落在 [10, 80] 区间，实际 {pcts}"
    assert abs(pcts[-1] - 80.0) < 1e-6, \
        f"下载完成时应恰好到 80（其后由 applying / ready 接管到 100），实际 {pcts[-1]}"
    assert pcts == sorted(pcts), f"映射后仍须单调不减，实际 {pcts}"


def test_run_update_writes_moving_progress_into_job():
    """③ 端到端：下载期间 job 的 progress 必须是**变化**的 —— 直接对应「一直 10%」。"""
    job_id = "testjob-progress"
    system._UPDATE_JOBS[job_id] = {"status": "queued", "progress": 0, "error": ""}
    observed = []

    def fake_prepare(data, work, bundle, target_ver, on_pct=None):  # noqa: ARG001
        for p in (20.0, 35.5, 51.0, 67.0, 80.0):
            if on_pct:
                on_pct(p)
            observed.append(system._UPDATE_JOBS[job_id]["progress"])
        return None  # 走「更新准备失败」分支，避免真的动 /Applications

    orig = system._prepare_update
    system._prepare_update = fake_prepare
    try:
        with tempfile.TemporaryDirectory() as td:
            system._run_update(job_id, {}, Path(td), Path("/nonexistent-bundle"),
                               Path("/nonexistent.app"), "9.9.9")
    finally:
        system._prepare_update = orig

    assert observed == [20, 35, 51, 67, 80], \
        f"下载期间 /api/system/update/status 的 progress 必须逐步变化，实际序列 {observed}"
    assert len(set(observed)) >= 3, "进度不应是一两个固定值（旧实现只有写死的 10）"
    # 清理，避免污染其他用例
    system._UPDATE_JOBS.pop(job_id, None)


def test_update_status_endpoint_returns_job_progress():
    """③b 状态端点确实把 job 的 progress 透出去（前端轮询的取值口）。"""
    job_id = "testjob-endpoint"
    system._UPDATE_JOBS[job_id] = {"status": "downloading", "progress": 63, "error": ""}
    try:
        out = system.system_update_status(job=job_id)
        assert out.get("ok") is True, f"应返回 ok=True，实际 {out}"
        assert out.get("progress") == 63, f"progress 必须透出，实际 {out}"
        assert out.get("status") == "downloading", f"status 必须透出，实际 {out}"
    finally:
        system._UPDATE_JOBS.pop(job_id, None)


if __name__ == "__main__":
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fail = 0
    for fn in funcs:
        try:
            fn()
            print(f"  ✔ {fn.__name__}")
        except AssertionError as e:
            fail += 1
            print(f"  ✘ {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  ✘ {fn.__name__}: 异常 {type(e).__name__}: {e}")
    if fail:
        print(f"\n❌ 失败 {fail} 项")
        sys.exit(1)
    print(f"\n🎉 更新进度上报测试全部通过（{len(funcs)} 项）")
