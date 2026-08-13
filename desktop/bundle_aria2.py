#!/usr/bin/env python3
"""跨平台打包 aria2c 进 .app / .exe，使种子（磁力/种子）下载后端随安装包自包含。

三个平台的处理方式不同：

  - macOS  : 优先用本机 Homebrew 安装的 aria2c，递归收集其 brew dylib 依赖，平铺进
             <Resources>/bin/，把所有 brew 绝对路径改写为 @loader_path/<名>（相对被加载者自身），
             再 ad-hoc 重签名（arm64 macOS 要求），从而脱离本机 Homebrew 独立运行。
  - Windows: 从官方 GitHub release 下载 win-64bit 压缩包，解压平铺到 <Resources>/bin/。
             Windows 按 exe 同目录优先搜索 DLL，平铺即自包含，无需改 rpath。
  - Linux  : 从官方 GitHub release 下载 linux-gnu-64bit 压缩包，解压平铺到 <Resources>/bin/。
             （网页版/服务器部署用；ARM 服务器无官方 x86_64 包时，请改用系统 `apt install aria2`）

<Resources> 含义：
  - macOS 桌面版: dist/VideoDownloader.app/Contents/Resources
  - Windows 桌面版(onedir): dist/VideoDownloader   （PyInstaller onedir 的 _MEIPASS 即此目录）
  - 网页版部署: 把 server/ 所在目录当作 Resources，运行本脚本时传入该目录即可自带 aria2

用法：
    python3 desktop/bundle_aria2.py [RESOURCES_DIR]

打包失败（如本机无网络下载 Windows/Linux 包、且无本地 brew）时**优雅跳过**，不阻断整体构建；
此时种子功能在运行时自动禁用并优雅降级。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ARIA2_VERSION = "1.37.0"
_RELEASE_BASE = "https://github.com/aria2/aria2/releases/download/release-" + ARIA2_VERSION

# (文件名, 压缩格式) —— 仅提供官方 x86_64 构建（ARM 服务器请改用系统包管理器）
_DOWNLOAD = {
    "win32": ("aria2-%s-win-64bit-build1.zip" % ARIA2_VERSION, "zip"),
    "linux": ("aria2-%s-linux-gnu-64bit-build1.tar.bz2" % ARIA2_VERSION, "tbz2"),
    "darwin": ("aria2-%s-osx-darwin.tar.bz2" % ARIA2_VERSION, "tbz2"),
}

_SRC_CANDIDATES_MAC = [
    "/opt/homebrew/bin/aria2c",
    "/usr/local/bin/aria2c",
]


def _log(msg: str) -> None:
    print(f"   [aria2] {msg}")


# --------------------------------------------------------------------------- #
# macOS：本地 brew -> 改写 rpath -> 自包含
# --------------------------------------------------------------------------- #
def _otool_deps(path: Path) -> list[str]:
    out = subprocess.run(["otool", "-L", str(path)],
                         capture_output=True, text=True).stdout
    deps: list[str] = []
    for line in out.splitlines()[1:]:
        m = __import__("re").search(r"^\s+(\S+)", line)
        if m:
            deps.append(m.group(1))
    return deps


def _collect_brew_deps(main: Path) -> set[str]:
    found: set[str] = set()
    queue = [main]
    seen: set[str] = set()
    while queue:
        p = queue.pop(0)
        if str(p) in seen:
            continue
        seen.add(str(p))
        for d in _otool_deps(p):
            if d.startswith("/opt/homebrew"):
                found.add(d)
                if d not in seen:
                    queue.append(Path(d))
    return found


def _bundle_local_mac(bin_dir: Path) -> bool:
    src = next((Path(c) for c in _SRC_CANDIDATES_MAC if Path(c).exists()), None)
    if src is None:
        src_path = shutil.which("aria2c")
        if src_path:
            src = Path(src_path)
    if src is None:
        return False

    shutil.copyfile(src, bin_dir / "aria2c")
    (bin_dir / "aria2c").chmod(0o755)
    brew_deps = _collect_brew_deps(bin_dir / "aria2c")
    for d in sorted(brew_deps):
        dst = bin_dir / Path(d).name
        if not dst.exists():
            shutil.copyfile(d, dst)
            dst.chmod(0o644)

    targets = [bin_dir / "aria2c"] + [bin_dir / Path(d).name for d in sorted(brew_deps)]
    for p in targets:
        for dep in _otool_deps(p):
            if dep.startswith("/opt/homebrew"):
                subprocess.run(["install_name_tool", "-change", dep,
                                f"@loader_path/{Path(dep).name}", str(p)],
                               check=True, capture_output=True)
        if p.suffix == ".dylib":
            subprocess.run(["install_name_tool", "-id", f"@loader_path/{p.name}", str(p)],
                           check=True, capture_output=True)
        # arm64 macOS 改 load commands 后必须重签，否则进程 Killed:9
        subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(p)],
                       check=True, capture_output=True)
    _log(f"已用本机 brew aria2c 打包 + {len(brew_deps)} 个 dylib（@loader_path 自包含）")
    return True


# --------------------------------------------------------------------------- #
# Windows / Linux：下载官方 release -> 平铺解压
# --------------------------------------------------------------------------- #
def _download(url: str, dest: Path) -> bool:
    _log(f"下载 {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "vdl-bundle/1"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:  # noqa: BLE001 - 任何下载失败都优雅降级
        _log(f"⚠️ 下载失败：{e}")
        return False


def _extract_flat(archive: Path, kind: str, bin_dir: Path) -> None:
    """解压并把文件平铺到 bin_dir（去掉压缩包内的顶层目录）。"""
    if kind == "zip":
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                rel = info.filename.split("/", 1)[1] if "/" in info.filename else info.filename
                if not rel:
                    continue
                _spill(z.open(info), bin_dir / rel)
    else:  # tbz2
        with tarfile.open(archive, "r:bz2") as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
                if not rel:
                    continue
                _spill(t.extractfile(m), bin_dir / rel)


def _spill(src, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)
    if target.name in ("aria2c", "aria2c.exe"):
        target.chmod(0o755)


def _bundle_download(platform_key: str, bin_dir: Path) -> bool:
    fname, kind = _DOWNLOAD[platform_key]
    url = f"{_RELEASE_BASE}/{fname}"
    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / fname
        if not _download(url, archive):
            return False
        _extract_flat(archive, kind, bin_dir)
    return True


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def bundle(resources_dir: Path) -> bool:
    bin_dir = resources_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = "aria2c.exe" if sys.platform == "win32" else "aria2c"

    # 1) macOS 优先用本地 brew（自包含最稳）
    if sys.platform == "darwin":
        if _bundle_local_mac(bin_dir):
            return _selfcheck(bin_dir, exe)

    # 2) 其他平台 / macOS 无 brew：下载官方 release
    key = sys.platform
    if key not in _DOWNLOAD:
        _log(f"⚠️ 不支持的平台 {key}，跳过打包（种子功能将运行时禁用）")
        return False
    if _bundle_download(key, bin_dir):
        return _selfcheck(bin_dir, exe)

    _log("⚠️ 打包失败（无本地 brew 且未能下载官方二进制），种子功能将运行时禁用")
    return False


def _selfcheck(bin_dir: Path, exe: str) -> bool:
    exe_path = bin_dir / exe
    # 1) 无残留 brew 绝对路径（仅 macOS 本地分支适用，下载分支本就无 brew 路径）
    if sys.platform == "darwin":
        import re
        leftover = []
        for p in bin_dir.iterdir():
            if p.is_file():
                for d in _otool_deps(p):
                    if d.startswith("/opt/homebrew"):
                        leftover.append(d)
        if leftover:
            _log(f"⚠️ 仍有 {len(leftover)} 处 brew 引用未改写")
            return False
    # 2) 能打印版本（真正可运行）
    try:
        r = subprocess.run([str(exe_path), "--version"],
                           capture_output=True, text=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        _log(f"⚠️ 自包含校验失败：{e}")
        return False
    if r.returncode != 0:
        _log(f"⚠️ 自包含校验失败：{r.stderr[:200]}")
        return False
    _log(f"已打包 aria2c 到 {bin_dir}（自包含校验通过）")
    return True


if __name__ == "__main__":
    default = Path(__file__).resolve().parent.parent / "dist" / "VideoDownloader.app" / "Contents" / "Resources"
    resources = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    bundle(resources)  # 失败不阻断构建（功能回退禁用）
