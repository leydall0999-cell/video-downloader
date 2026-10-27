#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并/推送前的文件语法校验（平台无关，秒级）。

为什么要有这个脚本：
  2026-10-27 一次「Mac 线 vs Windows 线」合并撞出 7 处冲突，其中 web/app.js
  有一行 `};` 被 git 判成「双方共有」，实际是两侧各自函数的闭合 ⇒ 直接拼接
  少一个闭合括号。两边单独都合法、合完就坏，肉眼看不出来，只有真解析一遍
  才能发现。合并完不校验就推，等于把坏包直接发出去。

覆盖范围（只校验「本次改动到的文件」，全量跑太慢且无必要）：
  - *.py            → py_compile
  - web/app.js 等 js → node --check（找不到 node 就 SKIP，不误伤）
  - *.yml / *.yaml  → yaml.safe_load（没装 pyyaml 就 SKIP）
  - *.sh            → bash -n（找不到 bash 就 SKIP）
                      🔴 2026-10-27 补：desktop/build_win.sh、build_mac.sh 都是
                      几千行的构建脚本，此前这道闸对 .sh 直接 SKIP —— 改完推上去
                      无人校验，等于闸门漏了半扇。

用法：
  python tools/check_syntax.py                    # 校验工作区相对 HEAD 的改动
  python tools/check_syntax.py --range A..B       # 校验某个区间的改动
  python tools/check_syntax.py --range HEAD^1..HEAD   # 合并后：校验这次合进来的
  python tools/check_syntax.py --files a.py b.js  # 直接指定文件

退出码：0 = 通过（含 SKIP）；1 = 有语法错误；2 = 用法/环境错误。
"""
from __future__ import annotations

import argparse
import py_compile
import shutil
import subprocess
import sys
import tempfile
import os

if hasattr(sys.stdout, "reconfigure"):  # Windows 控制台本地代码页会炸中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git"] + list(args), cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def collect_files(range_: str | None, files: list[str] | None) -> list[str]:
    if files:
        return list(files)
    if range_:
        raw = _git("diff", "--name-only", range_)
    else:
        # 工作区 + 暂存区，相对 HEAD
        raw = _git("diff", "--name-only", "HEAD") + _git("diff", "--cached", "--name-only")
    seen, out = set(), []
    for line in raw.splitlines():
        p = line.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def check_py(path: str) -> tuple[bool, str]:
    full = os.path.join(ROOT, path)
    if not os.path.isfile(full):
        return True, ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pyc", delete=True) as _:
            pass
        py_compile.compile(full, cfile=tempfile.mktemp(suffix=".pyc"), doraise=True)
        return True, ""
    except py_compile.PyCompileError as e:
        return False, str(e).strip().splitlines()[-1] if str(e).strip() else "语法错误"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _find_node() -> str | None:
    env = os.environ.get("VDL_NODE")
    if env and shutil.which(env):
        return env
    return shutil.which("node") or shutil.which("node.exe")


def check_js(path: str) -> tuple[bool, str]:
    full = os.path.join(ROOT, path)
    if not os.path.isfile(full):
        return True, ""
    node = _find_node()
    if not node:
        return True, "SKIP(未找到 node)"
    r = subprocess.run([node, "--check", full], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode == 0:
        return True, ""
    msg = (r.stderr or "").strip().splitlines()
    return False, " | ".join(msg[:3])


def _find_bash() -> str | None:
    env = os.environ.get("VDL_BASH")
    if env and (shutil.which(env) or os.path.isfile(env)):
        return env
    w = shutil.which("bash") or shutil.which("bash.exe")
    if w:
        return w
    # Windows：PATH 上没有时兜底找 Git Bash（CI 的 shell 自带，本机装 Git 就有）
    for p in (r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if os.path.isfile(p):
            return p
    return None


def check_sh(path: str) -> tuple[bool, str]:
    """bash -n：只解析不执行，能抓括号/引号/if-fi 不配对（不跑脚本本身）。"""
    full = os.path.join(ROOT, path)
    if not os.path.isfile(full):
        return True, ""
    bash = _find_bash()
    if not bash:
        return True, "SKIP(未找到 bash)"
    # MSYS/Git Bash 会把反斜杠当转义 ⇒ 统一成正斜杠，Windows 路径也能吃
    r = subprocess.run([bash, "-n", full.replace("\\", "/")], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode == 0:
        return True, ""
    msg = (r.stderr or "").strip().splitlines()
    return False, " | ".join(msg[:3])


def check_yaml(path: str) -> tuple[bool, str]:
    full = os.path.join(ROOT, path)
    if not os.path.isfile(full):
        return True, ""
    try:
        import yaml  # type: ignore
    except Exception:
        return True, "SKIP(未装 pyyaml)"
    try:
        with open(full, encoding="utf-8") as f:
            yaml.safe_load(f)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description="合并/推送前的文件语法校验")
    ap.add_argument("--range", default=None, help="git 区间，如 HEAD^1..HEAD / origin/app-dev..HEAD")
    ap.add_argument("--files", nargs="*", default=None, help="直接指定文件")
    args = ap.parse_args()

    files = collect_files(args.range, args.files)
    targets = [f for f in files if f.endswith((".py", ".js", ".yml", ".yaml", ".sh"))]
    if not targets:
        print("check_syntax: 本次没有需要校验的文件 —— SKIP")
        return 0

    errors: list[str] = []
    skipped: list[str] = []
    for p in targets:
        if p.endswith(".py"):
            ok, msg = check_py(p)
        elif p.endswith(".js"):
            ok, msg = check_js(p)
        elif p.endswith(".sh"):
            ok, msg = check_sh(p)
        else:
            ok, msg = check_yaml(p)
        if not ok:
            errors.append(f"  ❌ {p}: {msg}")
            print(f"  ❌ {p}: {msg}")
        elif msg.startswith("SKIP"):
            skipped.append(f"  ⚪ {p}: {msg}")
        else:
            print(f"  ✅ {p}")

    for s in skipped:
        print(s)

    if errors:
        print("\ncheck_syntax: 有语法错误，先修再推（合并冲突里「共有结尾行」最容易出这种坏包）")
        return 1
    print(f"check_syntax: 通过（{len(targets)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
