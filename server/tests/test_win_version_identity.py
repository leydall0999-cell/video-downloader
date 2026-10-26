"""tests/test_win_version_identity.py —— Windows 上 /api/version 不得回落 'dev'。

背景（2026-10-26 真机验收 win-verify_20261025_190001 抓到）：
`server/routers/core.py::BUILD_VERSION` 是 `/api/version` 的唯一来源，
应用内更新、部署自校验、"装的是不是新版"全靠它。而它的候选路径只写了
**macOS 布局**（`<exe>/../../Resources/build_version.txt`）：
  · macOS：可执行文件在 Contents/MacOS ⇒ ../Resources 命中 ✅
  · Windows onedir：可执行文件在产物根 ⇒ ../Resources 变成 `C:\\Resources` ❌
⇒ Windows 上 `/api/version` 恒返回 **'dev'**（真机实测 got='dev' want='1.0.93'）。
而同仓 `server/routers/system.py::BUILD` **有** `<exe 同级>` 这个候选 —— 两端不同源，
于是「系统信息」对、「版本接口」错，极难察觉。

本文件把 `_read_build_version()` 用 ast 抽出来**真跑**（假的 sys.executable +
真实目录树），分别验证 macOS 布局与 Windows onedir 布局都能读到构建指纹。
变异锚点：删掉 `ep.parent / 'build_version.txt'` 那条 ⇒ Windows 用例变红。
"""
import ast
import os
import pathlib
import sys
import tempfile
import types

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(SERVER_DIR)
CORE = os.path.join(SERVER_DIR, "routers", "core.py")

FAILS = 0
CHECKS = 0


def check(desc, cond, extra=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print(f"  \u2705 {desc}")
    else:
        FAILS += 1
        print(f"  \u274c {desc} | {extra}")


def load_reader():
    """抽出 _read_build_version 并在带桩命名空间里编译（不 import 整个 core）。"""
    src = pathlib.Path(CORE).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_read_build_version"),
        None,
    )
    if fn is None:
        return None
    ns: dict = {"__file__": CORE}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), CORE, "exec"), ns)
    return ns["_read_build_version"]


def run_with(exe_path: str) -> str:
    """用假的 sys.executable 跑一次解析函数。"""
    fake_sys = types.SimpleNamespace(executable=exe_path)
    app_stub = types.SimpleNamespace(sys=fake_sys, Path=pathlib.Path)
    # 函数体里通过闭包外的全局名 app 取用；这里用 exec 注入同名全局
    g = {"app": app_stub, "__file__": CORE, "Path": pathlib.Path}
    src = pathlib.Path(CORE).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_read_build_version")
    exec(compile(ast.Module(body=[fn], type_ignores=[]), CORE, "exec"), g)
    return g["_read_build_version"]()


def main():
    reader = load_reader()
    if reader is None:
        check("core.py 存在 _read_build_version", False)
        return

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_verid_"))
    try:
        # —— macOS 布局：…/Contents/MacOS/App + …/Contents/Resources/build_version.txt ——
        mac_exe_dir = tmp / "App.app" / "Contents" / "MacOS"
        mac_exe_dir.mkdir(parents=True)
        mac_exe = mac_exe_dir / "VideoDownloader"
        mac_exe.write_text("", encoding="utf-8")
        (tmp / "App.app" / "Contents" / "Resources").mkdir(parents=True)
        (tmp / "App.app" / "Contents" / "Resources" / "build_version.txt").write_text(
            "v1.0.93 \u00b7 \u6784\u5efa abc1234", encoding="utf-8")
        got = run_with(str(mac_exe))
        check("macOS 布局能读到构建指纹", got.startswith("v1.0.93"), f"got={got!r}")

        # —— Windows onedir 布局：产物根/VideoDownloader.exe + 产物根/build_version.txt ——
        #    （build_win.sh 就是写在 $DIST_APP 根的；修复前缺这条候选 ⇒ 恒 'dev'）
        win_dir = tmp / "VideoDownloader"
        win_dir.mkdir(parents=True)
        win_exe = win_dir / "VideoDownloader.exe"
        win_exe.write_text("", encoding="utf-8")
        (win_dir / "build_version.txt").write_text(
            "v1.0.94 \u00b7 \u6784\u5efa def5678", encoding="utf-8")
        got = run_with(str(win_exe))
        check("Windows onedir 布局能读到构建指纹（不得回落 'dev'）",
              got.startswith("v1.0.94"), f"got={got!r}")

        # —— onedir 6.x：运行期文件收进 _internal/server ——
        win_dir2 = tmp / "VideoDownloader2"
        (win_dir2 / "_internal" / "server").mkdir(parents=True)
        win_exe2 = win_dir2 / "VideoDownloader.exe"
        win_exe2.write_text("", encoding="utf-8")
        (win_dir2 / "_internal" / "server" / "build_version.txt").write_text(
            "v1.0.95 \u00b7 \u6784\u5efa 999", encoding="utf-8")
        got = run_with(str(win_exe2))
        check("onedir 6.x（_internal/server）布局能读到构建指纹",
              got.startswith("v1.0.95"), f"got={got!r}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print(f"\n{CHECKS - FAILS}/{CHECKS} \u901a\u8fc7")
    sys.exit(1 if FAILS else 0)
