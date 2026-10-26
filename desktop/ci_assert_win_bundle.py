#!/usr/bin/env python3
"""Windows 包「必备资源齐全」硬断言——防止再发 1.0.95 那样的静默坏包。

为什么需要这个脚本
------------------
2026-10-26 事故：v1.0.95-win1（CI 构建）装完一点开就弹「本地服务启动失败」。
真机排查两层根因：

  1. pywebview 6.x 的 `webview/platforms/edgechromium.py` 在 **import 期**无条件
     遍历 `('win-arm64', 'win-x64', 'win-x86')` 三个目录，任一个不存在就直接
     `FileNotFoundError` ⇒ 原生窗口起不来，只剩一个「本地服务启动失败」弹窗，
     而后端其实可能已经起来了（极其误导）。
  2. `--collect-all webview` 把这些 .dll 归到 **binaries**，PyInstaller 按目标
     架构过滤 ⇒ 包里只剩 `win-x64`，arm64/x86 被静默丢掉，**构建日志全绿**。

配套修复见 `build_win.sh`（三目录显式 `--add-data`，data 不走架构过滤）；
本脚本是最后一道闸：在真 Windows runner 上对**产物**做硬断言，缺就红。

判据
----
  1. 文件存在；
  2. 非空（> 1KB，拦掉 0 字节 / 空壳占位）——不用体积下限卡版本差异。

复查项一并带上（同一类「data 没进包」缺陷）：
  * `_internal/server/assets` —— 1.0.95 的包里 `_internal/server` 是**空目录**，
    中文字体/扩展资源全丢（server/app.py 按 `__file__` 相对路径找 assets/fonts）。

非 Windows 上运行 → SKIP（rc=0），不误伤跨平台场景。
用法：python desktop/ci_assert_win_bundle.py dist/VideoDownloader
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Windows runner 的 stdout 是本地代码页（cp1252），任何中文都可能 UnicodeEncodeError。
# 与 ci_assert_bundle_binaries.py / ci_assert_win_no_window.py 一致：脚本内自保一层。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

MIN_BYTES = 1024


def _check_file(base: Path, rel: str, problems: list[str]) -> None:
    p = base / rel
    if not p.is_file():
        problems.append(f"missing: {rel}")
        print(f"  [FAIL] missing: {rel}")
        return
    size = p.stat().st_size
    if size < MIN_BYTES:
        problems.append(f"too small ({size}B): {rel}")
        print(f"  [FAIL] too small ({size}B): {rel}")
        return
    print(f"  [ OK ] {rel}  ({size} bytes)")


def _check_nonempty_dir(base: Path, rel: str, problems: list[str]) -> None:
    d = base / rel
    if not d.is_dir():
        problems.append(f"missing dir: {rel}")
        print(f"  [FAIL] missing dir: {rel}")
        return
    files = [f for f in d.rglob("*") if f.is_file()]
    if not files:
        problems.append(f"empty dir: {rel}")
        print(f"  [FAIL] empty dir: {rel}  (打包时 data 没进来？)")
        return
    print(f"  [ OK ] {rel}  ({len(files)} files)")


def main(argv: list[str]) -> int:
    if sys.platform != "win32":
        print("SKIP: 非 Windows 平台，跳过 Windows 包资源断言")
        return 0

    if len(argv) < 2:
        print("用法: python desktop/ci_assert_win_bundle.py <dist/VideoDownloader>")
        return 1

    dist = Path(argv[1])
    internal = dist / "_internal"
    if not dist.is_dir():
        print(f"[FAIL] 产物目录不存在: {dist}")
        return 1

    # PyInstaller onedir：老版本把 data 放 <dist>/，6.x 一律放 _internal/
    roots = [internal, dist] if internal.is_dir() else [dist]

    problems: list[str] = []
    print("=== 1) pywebview WebView2Loader 三平台运行时 ===")
    for plat in ("win-x64", "win-arm64", "win-x86"):
        rel = f"webview/lib/runtimes/{plat}/native/WebView2Loader.dll"
        if any((r / rel).is_file() for r in roots):
            for r in roots:
                if (r / rel).is_file():
                    _check_file(r, rel, problems)
                    break
        else:
            problems.append(f"missing: {rel}")
            print(f"  [FAIL] missing: {rel}  ← pywebview import 期就会 FileNotFoundError")

    print("=== 2) server 资源（assets；字体/扩展） ===")
    rel_srv = "server/assets"
    if any((r / rel_srv).is_dir() for r in roots):
        for r in roots:
            if (r / rel_srv).is_dir():
                _check_nonempty_dir(r, rel_srv, problems)
                break
    else:
        problems.append(f"missing dir: {rel_srv}")
        print(f"  [FAIL] missing dir: {rel_srv}  （1.0.95 的包就是这里整个空掉）")

    if problems:
        print("")
        print(f"❌ Windows 包资源断言失败（{len(problems)} 项）：")
        for i, msg in enumerate(problems, 1):
            print(f"   {i}. {msg}")
        print("")
        print("提示：pywebview 三平台运行时必须齐全（缺一个 ⇒ 启动时 FileNotFoundError）；")
        print("      检查 build_win.sh 的 --add-data 是否覆盖 win-x64/win-arm64/win-x86。")
        return 1

    print("")
    print("✅ Windows 包资源断言全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
