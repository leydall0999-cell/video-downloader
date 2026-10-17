"""Windows 无控制台启动崩溃守卫（2026-10-17 用户装机事故）。

事故：用户 Windows 装 1.0.73 后双击打不开——WebView2 窗口能起，但页面
127.0.0.1 拒绝连接。日志根因：PyInstaller windowed 冻结程序不分配控制台
⇒ sys.stdout is None ⇒ uvicorn 日志格式化器 isatty() 抛 AttributeError
⇒ 后端线程秒崩。CI 冒烟从 bash 起 exe（stdout 是 pipe 非 None）抓不到；
Mac app bundle bootloader 会重定向 stdout 也不中招 —— 只有 Windows 双击复现。

本守卫钉死四件事：
  A. 根因复现：sys.stdout=None 时 uvicorn.Config 必崩（子进程真跑；
     uvicorn 若升级自修这条会红，届时按「用例过期」删掉即可）
  B. 修复有效：先跑 _ensure_stdio() 补掉 None，Config 即可构造成功（子进程真跑）
  C. 不误伤：正常环境原样返回，不替换 sys.std*
  D. 接线棘轮：_run_server_with_retry 与 main() 都必须调用 _ensure_stdio()，
     且调用点在 import uvicorn 之前
"""
import ast
import io
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "desktop" / "desktop_launcher.py"
SERVER = ROOT / "server"
PYTHON = sys.executable

FAILED = []
_OUT = sys.stdout  # 固定原始输出流：本测试会临时替换 sys.stdout，print 不能跟着换


def check(name, cond, detail=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{detail}]" if detail and not cond else ""),
          file=_OUT)
    if not cond:
        FAILED.append(name)


def _extract(names):
    """ast 只抽出目标函数的源码执行，返回命名空间。"""
    src = LAUNCHER.read_text(encoding="utf-8")
    ns = {"sys": sys, "os": os, "io": io}
    found = set()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(ast.Module([node], []), str(LAUNCHER), "exec"), ns)  # noqa: S102
            found.add(node.name)
    return ns, found


_CHILD_A = """
import sys
sys.path.insert(0, {server!r})
import uvicorn
sys.stdout = None
sys.stderr = None
uvicorn.Config("app:app", host="127.0.0.1", port=18321, log_level="info")
""".strip()

_CHILD_B = """
import ast, io, os, pathlib, sys
LAUNCHER = {launcher!r}
src = pathlib.Path(LAUNCHER).read_text(encoding="utf-8")
ns = {{"sys": sys, "os": os, "io": io}}
for node in ast.parse(src).body:
    if isinstance(node, ast.FunctionDef) and node.name == "_ensure_stdio":
        exec(compile(ast.Module([node], []), LAUNCHER, "exec"), ns)
sys.stdout = None
sys.stderr = None
sys.stdin = None
ns["_ensure_stdio"]()
assert sys.stdout is not None and sys.stderr is not None and sys.stdin is not None
sys.path.insert(0, {server!r})
import uvicorn
uvicorn.Config("app:app", host="127.0.0.1", port=18322, log_level="info")
""".strip()


def main():
    # ---- A. 根因复现（子进程，退出码非 0 = 崩 = 复现成功）----
    print("== A. 根因复现：sys.stdout=None 时 uvicorn.Config 必崩 ==")
    ra = subprocess.run([PYTHON, "-c", _CHILD_A.format(server=str(SERVER))],
                        capture_output=True, timeout=120)
    check("None-stdout 下 Config 崩溃（复现事故）", ra.returncode != 0,
          f"rc={ra.returncode}")

    # ---- B. 修复有效（子进程：先补 stdio 再构造 Config，rc=0 即修好）----
    print("== B. 修复：_ensure_stdio() 补掉 None 后 Config 可构造 ==")
    rb = subprocess.run([PYTHON, "-c", _CHILD_B.format(launcher=str(LAUNCHER),
                                                       server=str(SERVER))],
                        capture_output=True, timeout=120)
    check("修复后 Config 构造成功（uvicorn 可用）", rb.returncode == 0,
          (rb.stderr.decode("utf-8", "replace")[-300:] if rb.stderr else ""))

    # ---- C. 不误伤：正常环境原样返回 ----
    print("== C. 不误伤：正常环境不动 sys.std* ==")
    ns, found = _extract({"_ensure_stdio"})
    check("launcher 定义了 _ensure_stdio", "_ensure_stdio" in found)
    ensure = ns.get("_ensure_stdio")
    if ensure:
        keep_out, keep_err, keep_in = object(), object(), object()
        sys.stdout, sys.stderr, sys.stdin = keep_out, keep_err, keep_in
        try:
            ensure()
            check("stdout 正常时不替换", sys.stdout is keep_out)
            check("stderr 正常时不替换", sys.stderr is keep_err)
            check("stdin 正常时不替换", sys.stdin is keep_in)
        finally:
            sys.stdout, sys.stderr, sys.stdin = sys.__stdout__, sys.__stderr__, sys.__stdin__

        # 只有部分为 None 时也只补 None 的那个
        only_out = object()
        sys.stdout, sys.stderr = None, only_out
        try:
            ensure()
            check("仅 stdout 为 None 时补齐它", sys.stdout is not None)
            check("仅 stdout 为 None 时不动 stderr", sys.stderr is only_out)
        finally:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__

    # ---- D. 接线棘轮 ----
    print("== D. 接线：_run_server_with_retry / main 都必须先补 stdio ==")
    src = LAUNCHER.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for fname in ("_run_server_with_retry", "main"):
        node = next((n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == fname), None)
        check(f"{fname} 存在", node is not None)
        if node is None:
            continue
        calls = [n.lineno for n in ast.walk(node)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_ensure_stdio"]
        check(f"{fname} 调用了 _ensure_stdio", len(calls) >= 1)

    rs = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "_run_server_with_retry"), None)
    if rs is not None:
        i_ensure = i_import = None
        for n in ast.walk(rs):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_ensure_stdio" and i_ensure is None:
                i_ensure = n.lineno
            if isinstance(n, ast.Import) and any(a.name == "uvicorn" for a in n.names) and i_import is None:
                i_import = n.lineno
        check("_ensure_stdio 在 import uvicorn 之前（病因在 uvicorn 初始化）",
              i_ensure is not None and i_import is not None and i_ensure < i_import,
              f"ensure@{i_ensure} import@{i_import}")

    print()
    if FAILED:
        print(f"存在失败用例（{len(FAILED)}）:")
        for f in FAILED:
            print(" -", f)
        sys.exit(1)
    print("全部通过 ✅")
    sys.exit(0)


if __name__ == "__main__":
    main()
