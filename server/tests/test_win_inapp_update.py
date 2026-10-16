"""Windows 应用内更新链路守卫（2026-10-16 新增）。

背景：Windows 版此前一刀切禁用应用内更新（`PLATFORM != "macos"` 即返回
PLATFORM_UNSUPPORTED），根因是更新清单 latest.json 的 url 指向 macOS 的
VideoDownloader.app.zip（430MB）——Windows 用户点「检查更新」会去下个装不上的
mac 包。本次改为：Windows 走**独立清单** latest-win.json（R2 上的 Setup.exe），
下载后 sha256 校验，通过则派生 Inno Setup 静默覆盖安装。

本测试锁住这条链的契约（全部离线，不联网、不起真进程）：
  1. 清单按平台分流：windows → latest-win.json，其余 → latest.json
     （防回归：Windows 拿到 mac 清单 = 下 430MB 装不上的包）
  2. 验签随平台分流一起生效：Windows 清单被篡改同样拒绝（与 mac 同一条
     fail-closed 通道，不是另一套宽松逻辑）
  3. 安装链路 fail-closed：缺 url / 缺 sha256 / sha256 不符 → 一律不启动安装器，
     且下载物必须删除（启动来源不明的 exe 是最坏结果）
  4. 正常路径：以 [exe, /VERYSILENT, /NORESTART, /CLOSEAPPLICATIONS,
     /RESTARTAPPLICATIONS] 脱离父进程启动（本进程随后会被安装器关闭）
  5. 源码棘轮：installer.iss 的 RestartApplications=yes（/RESTARTAPPLICATIONS 的
     前提）、发布脚本签 .sig 且做版本单调性检查、路由层只放行 macos/windows

运行：cd server && python tests/test_win_inapp_update.py
"""
import base64
import os
import sys
import tempfile
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_DIR = os.path.dirname(_SERVER_DIR)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import importlib  # noqa: E402

sysmod = importlib.import_module("routers.system")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

# 测试期间patch的平台/打包态，finally 里统一还原（这些是进程级全局）
_ORIG = {"PLATFORM": sysmod.PLATFORM, "IS_FROZEN": sysmod.IS_FROZEN}


def _keypair():
    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    return key, pub_b64


# --------------------------------------------------------------------------- #
# 1) 清单按平台分流
# --------------------------------------------------------------------------- #
def test_manifest_name_routes_by_platform():
    old = sysmod.PLATFORM
    try:
        sysmod.PLATFORM = "windows"
        assert sysmod._manifest_name() == "latest-win.json", \
            "Windows 必须读独立清单，否则会拿到 macOS 的 .app.zip（430MB，装不上）"
        sysmod.PLATFORM = "macos"
        assert sysmod._manifest_name() == "latest.json"
        sysmod.PLATFORM = "linux"
        assert sysmod._manifest_name() == "latest.json"
    finally:
        sysmod.PLATFORM = old
    print("✅ 清单按平台分流：windows → latest-win.json，其余 → latest.json")


# --------------------------------------------------------------------------- #
# 2) Windows 清单同样过验签（防「换清单名绕过验签」的回归）
# --------------------------------------------------------------------------- #
def test_windows_manifest_signature_enforced():
    key, pub = _keypair()
    raw = b'{"version": "1.0.69", "sha256": "' + b"a" * 64 + b'", "url": "https://r2/x.exe"}'
    sig = base64.b64encode(key.sign(raw))

    requested = []

    def fake_get(url, timeout=15):
        requested.append(url)
        if url.endswith(sysmod.UPDATE_SIG_SUFFIX):
            return sig
        return raw

    old_plat, old_get = sysmod.PLATFORM, sysmod._http_get_bytes
    old_pub = sysmod.UPDATE_MANIFEST_PUBKEY_B64
    sysmod.PLATFORM = "windows"
    sysmod._http_get_bytes = fake_get
    sysmod.UPDATE_MANIFEST_PUBKEY_B64 = pub  # 内置公钥位，不走 env
    try:
        data = sysmod._fetch_manifest()
        assert data["version"] == "1.0.69"
        assert any(u.endswith("/latest-win.json") for u in requested), requested
        # 篡改内容（签名不变）→ 必须拒绝，证明 Windows 清单没有另一套宽松通道
        tampered = raw.replace(b"1.0.69", b"9.9.9")
        bad_sig_get = lambda url, timeout=15: (sig if url.endswith(sysmod.UPDATE_SIG_SUFFIX) else tampered)  # noqa: E731
        sysmod._http_get_bytes = bad_sig_get
        try:
            sysmod._fetch_manifest()
            raise AssertionError("Windows 清单被篡改时必须拒绝")
        except sysmod.ManifestError:
            pass
    finally:
        sysmod.PLATFORM = old_plat
        sysmod._http_get_bytes = old_get
        sysmod.UPDATE_MANIFEST_PUBKEY_B64 = old_pub
    print("✅ Windows 清单与 mac 走同一条验签 fail-closed 通道（含篡改拒绝）")


# --------------------------------------------------------------------------- #
# 3) 不可更新原因必须按平台给真话（历史缺陷：Windows 打包版曾被报成「开发模式」）
# --------------------------------------------------------------------------- #
def test_update_unsupported_reason_by_platform():
    old_frozen, old_plat = sysmod.IS_FROZEN, sysmod.PLATFORM
    try:
        sysmod.IS_FROZEN, sysmod.PLATFORM = False, "windows"
        assert "开发模式" in sysmod._update_unsupported_reason()
        sysmod.IS_FROZEN, sysmod.PLATFORM = True, "windows"
        assert sysmod._update_unsupported_reason() == "", "Windows 打包版应可更新"
        sysmod.IS_FROZEN, sysmod.PLATFORM = True, "macos"
        assert sysmod._update_unsupported_reason() == ""
        sysmod.IS_FROZEN, sysmod.PLATFORM = True, "linux"
        assert "linux" in sysmod._update_unsupported_reason()
    finally:
        sysmod.IS_FROZEN, sysmod.PLATFORM = old_frozen, old_plat
    print("✅ 不可更新原因按平台如实返回（windows/macos 可更新，linux 明确拦截）")


# --------------------------------------------------------------------------- #
# 4) _run_update_win：安装链路 fail-closed + 正常路径参数
# --------------------------------------------------------------------------- #
class _FakePopen:
    """捕获启动参数，不起真进程。"""

    def __init__(self, cmd, **kw):
        self.cmd = [str(x) for x in cmd]
        self.kw = kw
        _FakePopen.calls.append(self)


_FakePopen.calls = []


def _run_case(job_id, data, work, sha_value="a" * 64, dl_content=b"FAKE-EXE" * 64):
    """跑一次 _run_update_win（同步，不起线程），返回 (job, dest, calls)。"""
    _FakePopen.calls = []
    sysmod._UPDATE_JOBS[job_id] = {"status": "queued", "progress": 0, "error": "",
                                   "target_ver": "1.0.69", "created_at": 0.0}
    dest = work / "VideoDownloader-Setup-1.0.69.exe"

    def fake_dl(url, d, on_progress=None):
        Path(d).write_bytes(dl_content)
        if on_progress:
            on_progress(700, 1000)  # 进度回调必须被消费且不得中断下载

    old_dl, old_sha = sysmod._http_download, sysmod._sha256_of
    old_popen = sysmod.subprocess.Popen
    job_ref = sysmod._UPDATE_JOBS[job_id]  # _set 原地改这个 dict，先持引用再摘登记
    sysmod._http_download = fake_dl
    sysmod._sha256_of = lambda p: sha_value
    sysmod.subprocess.Popen = _FakePopen
    try:
        sysmod._run_update_win(job_id, data, work, "1.0.69")
    finally:
        sysmod._http_download, sysmod._sha256_of = old_dl, old_sha
        sysmod.subprocess.Popen = old_popen
        sysmod._UPDATE_JOBS.pop(job_id, None)
    return dict(job_ref or {}), dest, list(_FakePopen.calls)


def test_run_update_win_happy_path():
    work = Path(tempfile.mkdtemp(prefix="vdl_wupd_ok_"))
    try:
        job, dest, calls = _run_case(
            "happy", {"url": "https://r2/VideoDownloader-Setup-1.0.69.exe",
                      "sha256": "a" * 64}, work)
        assert job.get("status") == "ready" and job.get("progress") == 100, job
        assert len(calls) == 1, "校验通过必须启动一次安装器"
        assert calls[0].cmd == [str(dest), "/VERYSILENT", "/NORESTART",
                                "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"], calls[0].cmd
        # 必须脱离父进程：本进程随后会被 /CLOSEAPPLICATIONS 关闭，
        # 安装器若是子进程树成员会一并被杀（Windows 上走 creationflags，
        # 非 Windows 上等价物是 start_new_session）。
        assert calls[0].kw.get("start_new_session") is True, calls[0].kw
    finally:
        _rmtree(work)  # 内部断言目录确已消失（沙盒拦截静默失败会被抓到）
    print("✅ 正常路径：静默安装参数齐全 + 脱离父进程启动")


def test_run_update_win_fail_closed():
    work = Path(tempfile.mkdtemp(prefix="vdl_wupd_bad_"))
    try:
        # a) sha256 不符 → 不启动 + 删除下载物
        job, dest, calls = _run_case(
            "mismatch", {"url": "https://r2/x.exe", "sha256": "b" * 64}, work)
        assert job.get("status") == "error", job
        assert not calls, "sha256 不符绝不启动安装器"
        assert not dest.exists(), "校验失败的安装包必须删除（不能留在磁盘上）"
        # b) 清单缺 sha256 → 不校验不安装
        job2, _, calls2 = _run_case("nosha", {"url": "https://r2/x.exe"}, work)
        assert job2.get("status") == "error" and not calls2, job2
        # c) 清单缺 url → 直接取消
        job3, _, calls3 = _run_case("nourl", {"sha256": "a" * 64}, work)
        assert job3.get("status") == "error" and not calls3, job3
    finally:
        _rmtree(work)
    print("✅ fail-closed：sha 不符 / 缺 sha256 / 缺 url 都不启动安装器，且删除下载物")


# --------------------------------------------------------------------------- #
# 5) 源码棘轮（挡回归）
# --------------------------------------------------------------------------- #
def test_source_ratchets():
    iss = (Path(_REPO_DIR) / "desktop" / "installer.iss").read_text(encoding="utf-8")
    assert "RestartApplications=yes" in iss, \
        "installer.iss 必须 RestartApplications=yes，否则 /RESTARTAPPLICATIONS 无效，静默更新装完不会自动重启"
    assert "CloseApplications=yes" in iss, "覆盖安装前必须允许关闭占用 {app} 的本进程"

    pub = Path(_REPO_DIR) / "desktop" / "publish_update_win.sh"
    assert pub.is_file(), "缺少 Windows 清单发布脚本 desktop/publish_update_win.sh"
    src = pub.read_text(encoding="utf-8")
    assert "latest-win.json" in src, "发布脚本必须写 latest-win.json（与客户端 _manifest_name 一致）"
    assert ".sig" in src and "Ed25519" in src, "发布脚本必须签名（客户端 fail-closed）"
    assert "--force" in src, "发布脚本必须有版本单调性检查（防误发旧版本）"

    syssrc = sysmod.__file__ and Path(sysmod.__file__).read_text(encoding="utf-8")
    assert 'if PLATFORM not in ("macos", "windows"):' in syssrc, \
        "更新路由必须显式只放行 macos/windows（Linux 等拦停并给真原因）"
    print("✅ 源码棘轮：RestartApplications=yes / 发布脚本签名+单调性 / 路由平台白名单")


def _rmtree(p: Path):
    import shutil
    shutil.rmtree(p, ignore_errors=True)
    assert not p.exists(), f"临时目录删除失败（沙盒拦截？）：{p}"


if __name__ == "__main__":
    try:
        test_manifest_name_routes_by_platform()
        test_windows_manifest_signature_enforced()
        test_update_unsupported_reason_by_platform()
        test_run_update_win_happy_path()
        test_run_update_win_fail_closed()
        test_source_ratchets()
    finally:
        sysmod.PLATFORM = _ORIG["PLATFORM"]
        sysmod.IS_FROZEN = _ORIG["IS_FROZEN"]
    print("\n🎉 Windows 应用内更新链路测试全部通过（6 项）")
