#!/usr/bin/env python3
"""发布包「必备二进制核查」——防止发出静默降级的包。

为什么需要这个脚本
------------------
yt-dlp 的默认 JS 运行时是 `js_runtimes={'deno': {}}`；YouTube 的 >1080P 只有 DASH，
必须解 nsig 挑战 ⇒ **没有 deno 就拿不到高码率**。而 PyInstaller 不会分发 venv 的
scripts 目录，deno 是靠 `--add-binary` 显式塞进包的。

2026-10-21 核查发现：`build_mac.sh` / `build_win.sh` 在找不到 deno 时**只打一句 ⚠️
就继续构建**，CI 也没有任何断言。后果是可以发出一份「YouTube 高码率静默降级」的
包，而构建日志一片绿——这正是「越改越差但没人知道」的形态。

本脚本把「包内该有的东西在不在、还跑不跑得起来」变成**构建期的硬断言**。

判据（为什么不用体积阈值）
--------------------------
最初想用「deno > 5MB / ffmpeg > 1MB」做下限，2026-10-21 实测被推翻：本项目的
ffmpeg 是 `--enable-shared` 动态链接版（8.1.2 LGPL），主可执行文件只有 **444KB**，
主体在 `Contents/Resources/bin/libavcodec.dylib` 等库里，193 个编码器全可用。
用体积卡会假拦真包。

所以判据改成三条，全部确定性：
  1. 文件存在；
  2. 非空（> 1KB，拦掉 0 字节 / Git LFS 指针 / 空壳占位）；
  3. **真跑一次 `<bin> --version` 返回 0** —— 这一步同时捕获「动态库缺失」
     「架构不匹配」「文件损坏」，是体积阈值做不到的。
交叉平台构建（在 mac 上验 Windows 包）无法执行 .exe ⇒ 自动跳过第 3 条并注明，
不会把「跑不了」误报成「文件有问题」。

查什么
------
必需（缺 ⇒ 退出码 1）：
  deno     —— YouTube nsig 求解（>1080P/DASH 必需）
  ffmpeg   —— 音视频合并 / 转码
  ffprobe  —— 解说管线取时长
可选（缺 ⇒ 只警告）：
  yt_dlp_ejs —— 本地 EJS 求解脚本；缺了 yt-dlp 会尝试从 GitHub 拉远程组件
                （离线或被墙时降级，因此不拦发布）

用法
----
  python desktop/ci_assert_bundle_binaries.py <包目录> [--platform mac|win]
                                              [--no-smoke] [--json]

  例：
  python desktop/ci_assert_bundle_binaries.py dist/VideoDownloader.app
  python desktop/ci_assert_bundle_binaries.py dist/VideoDownloader --platform win

退出码：0 = 全通过（允许有警告）；1 = 有必需项缺失/异常；2 = 用法错误。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# 可执行文件后缀（按**目标平台**，不看当前机器）
_EXE_SUFFIX = {"mac": "", "win": ".exe"}

# 必需项：名字 -> 冒烟参数（用来证明它真能执行）
REQUIRED = {
    "deno": ["--version"],
    "ffmpeg": ["-version"],
    "ffprobe": ["-version"],
}

# 可选项目录名
OPTIONAL_DIRS = ["yt_dlp_ejs"]

# 非空判据：小于此值视为空壳/指针（ffmpeg 精简可执行文件 444KB 属正常，故阈值取 1KB）
MIN_BYTES = 1024

# 每类二进制可能落的位置（相对包根）。按顺序命中即止。
# macOS：--add-binary 落到 Contents/{Resources,Frameworks}/bin，ffmpeg 由脚本拷到 Contents/MacOS/bin。
# Windows：PyInstaller 6 onedir 的 _MEIPASS 是 _internal/，ffmpeg 由脚本拷到 exe 同级 bin/。
_BIN_ROOTS = {
    "mac": [
        "Contents/Resources/bin",
        "Contents/Frameworks/bin",
        "Contents/MacOS/bin",
    ],
    "win": [
        "_internal/bin",
        "bin",
    ],
}

# 可选项目录的搜索根
_DIR_ROOTS = {
    "mac": ["Contents/Resources", "Contents/Frameworks", "Contents/MacOS", "."],
    "win": ["_internal", "."],
}


def host_platform() -> str:
    return "win" if os.name == "nt" else "mac"


def detect_platform(bundle: Path) -> str:
    """按包结构判断目标平台（可交叉验证：macOS 上也能验 Windows 包）。"""
    if (bundle / "Contents").is_dir():
        return "mac"
    if (bundle / "_internal").is_dir():
        return "win"
    try:
        if bundle.with_suffix(".exe").is_file():
            return "win"
    except ValueError:
        pass
    if any(bundle.glob("*.exe")):
        return "win"
    return host_platform()


def _smoke(path: Path, args: list[str], timeout: float) -> tuple[bool, str]:
    """真执行一次 --version，确认能跑。返回 (ok, 说明)。"""
    try:
        proc = subprocess.run(
            [str(path), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return False, f"冒烟执行超时（>{timeout:g}s）"
    except OSError as exc:
        return False, f"无法执行：{exc}"
    if proc.returncode != 0:
        first = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        tail = first[0][:160] if first else ""
        return False, f"冒烟执行返回 {proc.returncode}：{tail}"
    out = (proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    return True, (out[0][:120] if out else "")


def _describe(bundle: Path, name: str, platform: str, do_smoke: bool, timeout: float) -> dict:
    exe = name + _EXE_SUFFIX[platform]
    roots = [r for r in _BIN_ROOTS[platform]]
    searched = [str(Path(r) / exe) for r in roots]
    base = {
        "name": name,
        "ok": False,
        "path": "",
        "size": 0,
        "version": "",
        "reason": "",
        "smoke": "skipped",
        "searched": searched,
    }

    for r in roots:
        cand = bundle / r / exe
        try:
            if not cand.is_file():
                continue
            size = cand.stat().st_size
        except OSError:
            continue

        info = dict(base, path=str(cand), size=size)
        if size < MIN_BYTES:
            info["reason"] = f"体积异常（{size} 字节 < {MIN_BYTES}）：疑似空壳/指针文件"
            return info
        if platform == "mac" and not os.access(cand, os.X_OK):
            info["reason"] = "缺少可执行权限（chmod +x 丢失）"
            return info
        if not do_smoke:
            info["ok"] = True
            info["smoke"] = "disabled"
            return info

        ok, detail = _smoke(cand, REQUIRED[name], timeout)
        info["smoke"] = "pass" if ok else "fail"
        info["version"] = detail if ok else ""
        if ok:
            info["ok"] = True
        else:
            info["reason"] = detail
        return info

    info = dict(base)
    info["reason"] = "未找到"
    return info


def _find_optional_dir(bundle: Path, dirname: str, platform: str) -> str:
    for r in _DIR_ROOTS[platform]:
        cand = bundle / r / dirname
        try:
            if cand.is_dir():
                return str(cand)
        except OSError:
            continue
    return ""


def check(bundle: Path, platform: str | None = None, do_smoke: bool = True,
          timeout: float = 20.0) -> dict:
    """核查包内必备二进制。platform 传 None 时按包结构自动识别。"""
    if platform is None:
        platform = detect_platform(bundle)
    # 交叉平台（如 mac 上验 win 包）无法执行 .exe ⇒ 自动跳过冒烟，避免误报
    cross = platform != host_platform()
    smoke_effective = do_smoke and not cross
    required = {
        name: _describe(bundle, name, platform, smoke_effective, timeout) for name in REQUIRED
    }
    optional = {
        d: {"name": d, "ok": bool(_find_optional_dir(bundle, d, platform)),
            "path": _find_optional_dir(bundle, d, platform)}
        for d in OPTIONAL_DIRS
    }
    failed = [r for r in required.values() if not r["ok"]]
    return {
        "bundle": str(bundle),
        "platform": platform,
        "host": host_platform(),
        "smoke": "ok" if smoke_effective else ("cross-platform" if cross else "disabled"),
        "required": required,
        "optional": optional,
        "ok": not failed,
    }


def _reason_hint(name: str) -> str:
    if name == "deno":
        return "缺它 ⇒ YouTube >1080P（DASH）解不了 nsig，高码率静默降级"
    if name in ("ffmpeg", "ffprobe"):
        return "缺它 ⇒ 音视频合并/转码不可用"
    return ""


def _human(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f}MB"
    return f"{n / 1024:.0f}KB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="核查发布包内必备二进制（deno/ffmpeg/ffprobe）是否真的进包且能跑",
    )
    ap.add_argument("bundle", help="包目录，如 dist/VideoDownloader.app 或 dist/VideoDownloader")
    ap.add_argument(
        "--platform",
        choices=["auto", "mac", "win"],
        default="auto",
        help="目标平台；默认按包结构自动判断",
    )
    ap.add_argument(
        "--no-smoke",
        action="store_true",
        help="跳过 --version 冒烟执行（仅查存在+非空；交叉平台会自动跳过）",
    )
    ap.add_argument("--timeout", type=float, default=20.0, help="单个冒烟执行超时秒数，默认 20")
    ap.add_argument("--json", action="store_true", help="输出 JSON（供 CI 解析）")
    args = ap.parse_args(argv)

    bundle = Path(args.bundle).expanduser()
    if not bundle.exists():
        print(f"❌ 包目录不存在：{bundle}", file=sys.stderr)
        return 2
    if not bundle.is_dir():
        print(f"❌ 不是目录：{bundle}", file=sys.stderr)
        return 2

    platform = args.platform if args.platform != "auto" else detect_platform(bundle)
    result = check(bundle, platform, do_smoke=not args.no_smoke, timeout=args.timeout)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        smoke_note = {
            "ok": "已实跑 --version",
            "cross-platform": "交叉平台，跳过实跑",
            "disabled": "已按参数跳过实跑",
        }.get(result["smoke"], result["smoke"])
        print(f"▶ 包内必备二进制核查：{bundle}（目标平台 {platform}；{smoke_note}）")
        for name in REQUIRED:
            r = result["required"][name]
            try:
                rel = Path(r["path"]).relative_to(bundle) if r["path"] else ""
            except ValueError:
                rel = r["path"]
            if r["ok"]:
                ver = f"  [{r['version']}]" if r.get("version") else ""
                print(f"   ✔ {name:<7} {_human(r['size']):>8}  {rel}{ver}")
            else:
                print(f"   ❌ {name:<7} {r['reason']}" + (f"（{rel}）" if rel else ""))
                hint = _reason_hint(name)
                if hint:
                    print(f"       {hint}")
                if r["reason"] == "未找到":
                    print(f"       已搜索：{', '.join(r['searched'])}")
        for name in OPTIONAL_DIRS:
            o = result["optional"][name]
            if o["ok"]:
                try:
                    rel = Path(o["path"]).relative_to(bundle)
                except ValueError:
                    rel = o["path"]
                print(f"   ✔ {name:<7} {'':>8}  {rel}")
            else:
                print(f"   ⚠️  {name:<7} 未找到（yt-dlp 会尝试从 GitHub 拉远程 EJS 组件）")
        print("   → 通过" if result["ok"] else "   → 未通过：缺少可用二进制，已拦截发布")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
