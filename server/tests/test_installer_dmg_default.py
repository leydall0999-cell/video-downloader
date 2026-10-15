"""安装包端点「默认 DMG + ZIP 兜底」契约回归（2026-10-15）。

背景：`/download/installer?p=macos` 原默认返回 ZIP（2026-10-13 上线时只有
ZIP 一种包）。DMG 上传到更新通道后改为默认 DMG（macOS 标准安装形态），
保留 `?p=macos-zip` 兜底（DMG 挂载异常环境 / 历史链接）。

本测试钉住五件事：
  1. `?p=macos` → DMG：Content-Type 为 x-apple-diskimage（不是 zip）、
     Content-Disposition 文件名是 .dmg；
  2. `?p=macos-zip` → ZIP：Content-Type application/zip、文件名 .zip；
  3. 统计口径按平台归并：macos-zip 也记 "installer/macos"，不拆行；
  4. 默认映射源码级回归：macos 指向 .dmg、存在 macos-zip 键（防手滑改回）；
  5. 未知平台仍 404。

不联网、不写真实统计文件（record 替换为内存记录）、退出还原。
"""
import inspect
import os
import sys
import tempfile
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_dmg_"))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import dl_stats  # noqa: E402
from routers import extension as ext  # noqa: E402


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ext.router)
    return TestClient(app)


class _Recorder:
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


def _fake_packages():
    """造一个假 DMG + 假 ZIP，返回 (dmg_path, zip_path, 清理函数)。"""
    dmg = tempfile.NamedTemporaryFile(suffix=".dmg", delete=False)
    dmg.write(b"\x00" * 2048)  # DMG 无简单魔数，用零填充占位
    dmg.close()
    zp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    zp.write(b"PK\x03\x04" + b"z" * 100)
    zp.close()

    def cleanup():
        for p in (dmg.name, zp.name):
            try:
                os.unlink(p)
            except OSError:
                pass

    return Path(dmg.name), Path(zp.name), cleanup


def test_macos_default_returns_dmg():
    """p=macos → DMG：类型/文件名/体积都按 dmg 来。"""
    dmg, zp, cleanup = _fake_packages()
    orig = ext._INSTALLER_FILES
    ext._INSTALLER_FILES = {"macos": dmg, "macos-zip": zp}
    try:
        c = _client()
        r = c.head("/download/installer?p=macos")
        assert r.status_code == 200, f"HEAD 应 200，实际 {r.status_code}"
        ct = r.headers.get("content-type", "")
        assert ct.startswith("application/x-apple-diskimage"), \
            f"DMG 的 Content-Type 错（写死 zip 会让浏览器把 dmg 当 zip）: {ct}"
        disp = r.headers.get("content-disposition", "")
        assert disp.endswith('.dmg"'), f"文件名应是 .dmg: {disp}"
        assert r.headers.get("content-length") == "2048"
        # GET 的 body 也必须是真的 dmg 字节
        r2 = c.get("/download/installer?p=macos")
        assert r2.content == b"\x00" * 2048, "GET body 应为 DMG 内容"
    finally:
        ext._INSTALLER_FILES = orig
        cleanup()


def test_macos_zip_fallback():
    """p=macos-zip → ZIP：类型/文件名按 zip 来。"""
    dmg, zp, cleanup = _fake_packages()
    orig = ext._INSTALLER_FILES
    ext._INSTALLER_FILES = {"macos": dmg, "macos-zip": zp}
    try:
        c = _client()
        r = c.head("/download/installer?p=macos-zip")
        assert r.status_code == 200, r.status_code
        ct = r.headers.get("content-type", "")
        assert ct.startswith("application/zip"), f"ZIP 类型错: {ct}"
        disp = r.headers.get("content-disposition", "")
        assert disp.endswith('.zip"'), f"文件名应是 .zip: {disp}"
    finally:
        ext._INSTALLER_FILES = orig
        cleanup()


def test_stats_merged_by_platform():
    """计数口径：macos-zip 并入 macos，不按包装格式拆行。"""
    dmg, zp, cleanup = _fake_packages()
    orig = ext._INSTALLER_FILES
    ext._INSTALLER_FILES = {"macos": dmg, "macos-zip": zp}

    def body(rec):
        c = _client()
        c.head("/download/installer?p=macos-zip")
        assert rec.calls == [], f"HEAD 不应计数，实际 {rec.calls}"
        c.get("/download/installer?p=macos")
        c.get("/download/installer?p=macos-zip")
        assert rec.calls == [("installer", "macos"), ("installer", "macos")], \
            f"两种格式都应归并到 installer/macos，实际 {rec.calls}"
    try:
        _with_recorder(body)
    finally:
        ext._INSTALLER_FILES = orig
        cleanup()


def test_default_mapping_source_guard():
    """源码级回归：默认 macos→dmg、存在 macos-zip 键（防手滑改回 zip）。"""
    src = inspect.getsource(ext)
    assert '"macos-zip"' in src, "默认映射必须有 macos-zip 兜底键"
    assert "/opt/vdl-update/VideoDownloader.dmg" in src, \
        "macos 默认应指向 DMG（改回 ZIP 会让官网又发 zip）"
    assert "/opt/vdl-update/VideoDownloader.app.zip" in src, \
        "macos-zip 兜底应指向 app.zip"
    assert '".dmg": "application/x-apple-diskimage"' in src, \
        "DMG 媒体类型映射缺失"


def test_unknown_platform_still_404():
    """未知平台仍 404（不因新增 key 放行）。"""
    c = _client()
    r = c.get("/download/installer?p=windows")
    assert r.status_code == 404, r.status_code


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
            traceback.print_exc()
    print("FAILED" if failed else "ALL PASS", failed)
    sys.exit(1 if failed else 0)
