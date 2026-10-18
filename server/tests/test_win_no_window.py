"""Windows 子进程不弹黑窗 + aria2c 在 Windows 上的启用策略回归测试（2026-10-18）。

事故背景：桌面版是 GUI 进程（无控制台），Windows 会给它 fork 出去的**控制台程序**
（aria2c.exe / ffmpeg / deno…）新建一个黑框窗口。用户装完打开、点一次下载就弹出
好几个黑窗，且一直累积（DASH 任务会分 video/audio 多路，每路一个 aria2c）。

三处修复，逐项钉死：
  1. `win_no_window`：Windows 下给 subprocess.Popen 默认补 CREATE_NO_WINDOW
     （0x08000000），所有子进程静默运行，含 yt-dlp 内部 fork 的 aria2c；
  2. `downloader._aria2c_path()`：候选路径补 `.exe` 后缀（旧版写死 "aria2c"，
     在 Windows 上 os.path.isfile 永远 False）；
  3. `downloader._download_options()`：Windows 上 `auto` **不启用** aria2c，
     一律回退 yt-dlp 原生下载器（带 http_chunk_size 分块提速）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp())

import downloader  # noqa: E402
from tasks import DownloadTask  # noqa: E402
import win_no_window  # noqa: E402

CREATE_NO_WINDOW = 0x08000000

_checks = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _checks
    _checks += 1
    if not cond:
        raise AssertionError(f"{name} 失败 {extra}")
    print(f"  ✅ {name}")


class _FakePopen:
    """替代真实 subprocess.Popen，只记录 __init__ 收到的参数。"""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


# ── A. should_install 决策 ──────────────────────────────────────────────
def test_should_install_matrix():
    print("▶ should_install 决策矩阵")
    check("Windows 上要装", win_no_window.should_install(os_name="nt", env={}) is True)
    check("POSIX 上不装（creationflags 会抛错）",
          win_no_window.should_install(os_name="posix", env={}) is False)
    check("VDL_SHOW_CONSOLE=1 时不装（排查逃生口）",
          win_no_window.should_install(os_name="nt", env={"VDL_SHOW_CONSOLE": "1"}) is False)
    check("VDL_SHOW_CONSOLE=TRUE 时不装（大小写不敏感）",
          win_no_window.should_install(os_name="nt", env={"VDL_SHOW_CONSOLE": "TRUE"}) is False)
    check("已装过则不重复装",
          win_no_window.should_install(os_name="nt", env={}, already_installed=True) is False)


# ── B. patch_popen 行为 ─────────────────────────────────────────────────
def test_patch_adds_flag():
    print("▶ patch_popen 补 CREATE_NO_WINDOW")
    win_no_window.patch_popen(_FakePopen)
    p = _FakePopen(["aria2c", "-x", "8"])
    check("未指定 creationflags 时自动补", p.kwargs.get("creationflags") == CREATE_NO_WINDOW,
          f"实际 {p.kwargs.get('creationflags')}")


def test_patch_respects_explicit_flag():
    print("▶ 显式 creationflags 不被覆盖")
    p = _FakePopen(["x"], creationflags=0x00000008)  # DETACHED_PROCESS
    check("调用方显式指定的标志保留", p.kwargs.get("creationflags") == 0x00000008)


def test_patch_idempotent():
    print("▶ 幂等（重复安装只包一层）")
    second = win_no_window.patch_popen(_FakePopen)
    check("第二次安装返回 False", second is False)
    check("标记已置位", getattr(_FakePopen, "_vdl_no_window", False) is True)


def test_patch_passes_args_through():
    print("▶ 位置参数/关键字仍透传")
    p = _FakePopen(["cmd", "a"], cwd="/tmp", stdout=subprocess.PIPE)
    check("args 透传", p.args[0] == ["cmd", "a"], f"实际 {p.args}")
    check("其他 kwarg 保留", p.kwargs.get("cwd") == "/tmp"
          and p.kwargs.get("stdout") is subprocess.PIPE)


def test_install_skips_on_posix():
    print("▶ 非 Windows 下 install 不应改动 subprocess.Popen")
    # 本套件在 macOS 上跑：install() 必须不改任何东西，否则 POSIX 起子进程会抛
    # ValueError（creationflags 在 POSIX 不被支持）。
    before = subprocess.Popen.__init__
    rc = win_no_window.install()
    check("POSIX 上 install 返回 False", rc is False)
    check("Popen.__init__ 未被替换", subprocess.Popen.__init__ is before)


# ── C. _aria2c_path 的 .exe 后缀 ────────────────────────────────────────
def _mk_bundle(root: Path, name: str) -> Path:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / name
    exe.write_bytes(b"")
    exe.chmod(0o755)
    return exe


def _with_fake_env(is_win: bool, bundle_name: str | None):
    """构造假的冻结环境：PATH 里没有 aria2c、_MEIPASS 指向临时目录。"""
    root = Path(tempfile.mkdtemp())
    made = None
    if bundle_name:
        made = _mk_bundle(root, bundle_name)
    orig_which = shutil.which
    orig_meipass = getattr(sys, "_MEIPASS", None)
    orig_is_win = downloader._is_windows
    shutil.which = lambda n: None          # 排除本机 brew aria2c 干扰
    sys._MEIPASS = str(root)
    downloader._is_windows = lambda: is_win
    return root, made, (orig_which, orig_meipass, orig_is_win)


def _restore_env(saved, root: Path):
    orig_which, orig_meipass, orig_is_win = saved
    shutil.which = orig_which
    if orig_meipass is None:
        try:
            del sys._MEIPASS
        except AttributeError:
            pass
    else:
        sys._MEIPASS = orig_meipass
    downloader._is_windows = orig_is_win
    shutil.rmtree(root, ignore_errors=True)


def test_aria2c_path_windows_exe():
    print("▶ Windows：内置 aria2c.exe 应被找到")
    root, made, saved = _with_fake_env(True, "aria2c.exe")
    try:
        got = downloader._aria2c_path()
        check("返回带 .exe 的路径", got == str(made), f"实际 {got}")
    finally:
        _restore_env(saved, root)


def test_aria2c_path_windows_no_suffix_not_found():
    print("▶ Windows：只有无后缀 aria2c 时不算可用（旧代码的漏网点）")
    root, _made, saved = _with_fake_env(True, "aria2c")
    try:
        got = downloader._aria2c_path()
        check("无 .exe 时返回 None", got is None, f"实际 {got}")
    finally:
        _restore_env(saved, root)


def test_aria2c_path_posix_plain():
    print("▶ POSIX：无后缀的 aria2c 应被找到")
    root, made, saved = _with_fake_env(False, "aria2c")
    try:
        got = downloader._aria2c_path()
        check("返回无后缀路径", got == str(made), f"实际 {got}")
    finally:
        _restore_env(saved, root)


# ── D. _download_options 的 Windows 启用策略 ────────────────────────────
def _opts(is_win: bool, downloader_type: str = "") -> dict:
    tmp = Path(tempfile.mkdtemp())
    task = DownloadTask(id="t1", url="https://example.com/v", title="t",
                        platform="other", quality="best")
    task.workdir = tmp
    reporter = type("R", (), {"on_postprocess": staticmethod(lambda *a, **k: None)})()
    orig_is_win = downloader._is_windows
    orig_a2 = downloader._aria2c_path
    orig_env = downloader.VDL_DOWNLOADER
    downloader._is_windows = lambda: is_win
    downloader._aria2c_path = lambda: "/opt/bin/aria2c" + (".exe" if is_win else "")
    downloader.VDL_DOWNLOADER = "auto"
    try:
        return downloader._download_options(task, "best", reporter,
                                            concurrent_fragments=8,
                                            downloader_type=downloader_type)
    finally:
        downloader._is_windows = orig_is_win
        downloader._aria2c_path = orig_a2
        downloader.VDL_DOWNLOADER = orig_env
        shutil.rmtree(tmp, ignore_errors=True)


def test_windows_auto_no_aria2c():
    print("▶ Windows + auto：不启用 aria2c（事故修复）")
    o = _opts(True)
    check("未设置 external downloader", "downloader" not in o, f"实际 {o.get('downloader')}")
    check("走原生分块提速", o.get("http_chunk_size", 0) > 0)


def test_posix_auto_uses_aria2c():
    print("▶ 非 Windows + auto：仍启用 aria2c（别误伤 Mac/Linux）")
    o = _opts(False)
    check("启用 aria2c", o.get("downloader") == "aria2c", f"实际 {o.get('downloader')}")
    check("带并发参数", "aria2c" in (o.get("downloader_args") or {}))


def test_windows_explicit_aria2c_honored():
    print("▶ Windows + 显式 aria2c：仍按用户指定启用")
    o = _opts(True, downloader_type="aria2c")
    check("显式指定生效", o.get("downloader") == "aria2c", f"实际 {o.get('downloader')}")


def test_windows_explicit_native():
    print("▶ Windows + 显式 native：走原生")
    o = _opts(True, downloader_type="native")
    check("未启用 aria2c", "downloader" not in o)


def main():
    print("▶ Windows 子进程无窗口 + aria2c 启用策略回归测试")
    test_should_install_matrix()
    test_patch_adds_flag()
    test_patch_respects_explicit_flag()
    test_patch_idempotent()
    test_patch_passes_args_through()
    test_install_skips_on_posix()
    test_aria2c_path_windows_exe()
    test_aria2c_path_windows_no_suffix_not_found()
    test_aria2c_path_posix_plain()
    test_windows_auto_no_aria2c()
    test_posix_auto_uses_aria2c()
    test_windows_explicit_aria2c_honored()
    test_windows_explicit_native()
    print(f"🎉 Windows 无窗口回归测试全部通过（{_checks} 项断言）")


if __name__ == "__main__":
    main()
