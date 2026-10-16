"""下载两端点 HEAD 支持回归（2026-10-13 接续待办 #1）。

背景：`/api/extension/package`（扩展包）与 `/download/installer`（桌面安装包）
此前只声明 GET。下载管理器预检（发 HEAD 取 Content-Length/Content-Type 判断
是否续传 / 是否已是最新）会拿到 405，预检链路失效。

修复：两路由改 `@router.api_route(..., methods=["GET", "HEAD"])`。
（注意：FastAPI 的 `@router.get()` **不接受** `methods=` 参数；且实测
 `@router.get` 下的 HEAD 仍 405 —— Starlette 虽会给 GET 路由补 HEAD，但
 FastAPI 的 APIRoute 组合下并未放行，必须显式声明。）

本测试钉住三件事：
  1. HEAD → 200，body 为空，但 Content-Length / Content-Type /
     Content-Disposition 正确（预检所需）；
  2. **HEAD 不得计入下载统计**（否则「预检 + 真实下载」把计数虚高一倍），
     GET 正常计数；
  3. 未提供安装包的平台（HEAD/GET）仍 404。

不联网、不写真实统计文件（record 被替换为内存记录）、退出还原。
"""
import os
import sys
import tempfile
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_head_"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import dl_stats  # noqa: E402
from routers import extension as ext  # noqa: E402


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ext.router)
    return TestClient(app)


class _Recorder:
    """替换 dl_stats.record：只记调用，不落盘。"""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, bucket, platform, n=1):
        self.calls.append((bucket, platform))
        return {}


def _with_recorder(fn):
    rec = _Recorder()
    orig = dl_stats.record
    dl_stats.record = rec
    try:
        fn(rec)
    finally:
        dl_stats.record = orig


def _with_r2_unreachable(fn):
    """把 R2 探测钉成「不可达」，强制走本机文件回退分支（离线、不联网）。

    2026-10-16：安装包改托管 R2 后，路由在 R2 可达时是 **302 跳 R2**，
    本机 `_INSTALLER_FILES` 只在 R2 不可达时才用。测试若不钉死这层，
    结果会随「CI/本机能不能连上 R2」漂移（曾出现同一份用例时绿时红）。
    """
    orig = ext._r2_reachable
    ext._r2_reachable = lambda url: False
    try:
        return fn()
    finally:
        ext._r2_reachable = orig


def _with_r2_reachable(fn):
    """把 R2 探测钉成「可达」，用于断言 302 跳转契约（同样不真联网）。"""
    orig = ext._r2_reachable
    ext._r2_reachable = lambda url: True
    try:
        return fn()
    finally:
        ext._r2_reachable = orig


def test_extension_head_supported_and_header_complete():
    """/api/extension/package：HEAD → 200 + 空 body + 头部齐全。"""
    c = _client()
    r = c.head("/api/extension/package")
    assert r.status_code == 200, f"HEAD 应 200，实际 {r.status_code}"
    assert r.content == b"", "HEAD 不得返回 body"
    assert int(r.headers.get("content-length", "0")) > 0, "HEAD 必须带真实 Content-Length"
    assert r.headers.get("content-type", "").startswith("application/zip")
    assert "attachment" in r.headers.get("content-disposition", "").lower()


def test_installer_head_supported_and_404_unknown_platform():
    """/download/installer：HEAD → 200（R2 不可达回退本机）/ 404（无此平台）。

    2026-10-16 契约更新：有 R2 地址的平台先 302 跳 R2，本机文件只是 R2
    不可达时的回退。所以「200 + 真实 Content-Length」必须在 R2 不可达的
    前提下断言（见 `_with_r2_unreachable`）。
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.write(b"z" * 1234)
    tmp.close()
    orig = ext._INSTALLER_FILES
    ext._INSTALLER_FILES = {"macos": Path(tmp.name)}

    def body():
        c = _client()
        r = c.head("/download/installer?p=macos")
        assert r.status_code == 200, f"HEAD 应 200，实际 {r.status_code}"
        assert r.content == b"", "HEAD 不得返回 body"
        assert r.headers.get("content-length") == "1234", \
            f"Content-Length 应为 1234，实际 {r.headers.get('content-length')}"
        # 从未提供过的平台：HEAD 也要 404（不能因方法变更放行）
        # ⚠️ 不能用 windows —— 它自 2026-10-16 起已在 `_R2_INSTALLER_URLS` 里，
        #    不再属于「未提供」，拿它断言 404 会在 Windows 版上线后误红。
        r2 = c.head("/download/installer?p=linux")
        assert r2.status_code == 404, r2.status_code
    try:
        _with_r2_unreachable(body)
    finally:
        ext._INSTALLER_FILES = orig
        os.unlink(tmp.name)


def test_installer_head_302_to_r2():
    """R2 可达时 HEAD/GET 一律 302 跳 R2（用户直连边缘，不经 ECS 出口）。"""
    orig = ext._INSTALLER_FILES
    ext._INSTALLER_FILES = {}          # 无本机文件也能跳，证明跳的是 R2 而非本机

    def body():
        c = _client()
        for plat, url in ext._R2_INSTALLER_URLS.items():
            r = c.head(f"/download/installer?p={plat}", follow_redirects=False)
            assert r.status_code == 302, f"{plat} 应 302，实际 {r.status_code}"
            assert r.headers.get("location") == url, \
                f"{plat} 应跳 {url}，实际 {r.headers.get('location')}"
    try:
        _with_r2_reachable(body)
    finally:
        ext._INSTALLER_FILES = orig


def test_head_not_counted_get_counted():
    """HEAD 是预检，不得计数；GET 才是真实下载，计一次。"""
    def body(rec):
        c = _client()
        c.head("/api/extension/package")
        assert rec.calls == [], f"HEAD 不应计入下载统计，实际 {rec.calls}"
        c.get("/api/extension/package")
        assert len(rec.calls) == 1, f"GET 应计一次，实际 {rec.calls}"
        assert rec.calls[0][0] == "extension"
    _with_recorder(body)


def test_installer_head_not_counted_get_counted():
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.write(b"z" * 10)
    tmp.close()
    orig = ext._INSTALLER_FILES
    ext._INSTALLER_FILES = {"macos": Path(tmp.name)}

    def body(rec):
        c = _client()
        c.head("/download/installer?p=macos")
        assert rec.calls == [], f"HEAD 不应计入安装包统计，实际 {rec.calls}"
        c.get("/download/installer?p=macos")
        assert rec.calls == [("installer", "macos")], rec.calls
    try:
        # 钉死 R2 不可达：走本机文件分支，避免真联网导致的计数漂移
        _with_r2_unreachable(lambda: _with_recorder(body))
    finally:
        ext._INSTALLER_FILES = orig
        os.unlink(tmp.name)


def test_get_still_returns_full_body():
    """回归：加了 HEAD 不能把 GET 的 body 弄丢。"""
    c = _client()
    r = c.get("/api/extension/package")
    assert r.status_code == 200
    assert len(r.content) > 0, "GET 必须仍返回完整 zip body"
    assert r.content[:2] == b"PK", "应为合法 zip（PK 魔数）"


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
