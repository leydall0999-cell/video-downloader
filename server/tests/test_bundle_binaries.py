#!/usr/bin/env python3
"""离线测试：desktop/ci_assert_bundle_binaries.py（发布包必备二进制核查）。

为什么要有这个测试
------------------
这个脚本是「拦截残包」的闸门本身。闸门写错了比不写更危险：
  - 假阳性（真包被判缺）⇒ 每次构建都被拦，别人会去关掉闸门；
  - 假阴性（缺 deno 却判通过）⇒ 又回到「YouTube 高码率静默降级」。
所以两种方向都必须钉死。

用 shell 脚本（mac）／手写最小 PE（Windows 宿主验 win 包）当二进制替身
（exit 0 / exit 3），不依赖真实 deno/ffmpeg，测试是确定性的、毫秒级。
真实的 ffmpeg 是 --enable-shared 版，主可执行文件只有 434KB —— 这里也顺带
锁住「不许用体积阈值当判据」这条。
"""

from __future__ import annotations

import contextlib
import io
import os
import stat
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop"))

import ci_assert_bundle_binaries as gate  # noqa: E402

# 替身要大于 MIN_BYTES（1024），否则会被「空壳判据」先拦下
_PAD = "# " + "x" * 1200 + "\n"
OK_SCRIPT = "#!/bin/sh\nexit 0\n" + _PAD
FAIL_SCRIPT = "#!/bin/sh\nexit 3\n" + _PAD

_MAC_ROOTS = ("Contents/Resources/bin", "Contents/MacOS/bin")
_WIN_ROOTS = ("_internal/bin", "bin")


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _pe_stub(path: Path, exit_code: int = 0) -> None:
    """手写最小 x64 PE 替身：入口 `mov eax,<exit_code>; ret`，无导入表。

    为什么不用 python.exe 当替身（2026-10-25 踩坑记录）：
    闸门对 ffmpeg/ffprobe 用单横线 `-version`（ffmpeg 惯例），python.exe 遇到
    `-version` 退 2（Unknown option: -e），只有 deno 的 `--version` 才退 0；
    仓库里那个 169MB 的真 ffmpeg.exe 是未跟踪构建产物，不能当依赖。
    手写 PE 不依赖任何外部产物，对任何参数都返回固定退出码——冒烟只验
    「二进制能不能起来」，不验功能，与 mac 侧 `exit 0` 脚本替身同档保真。

    x64 PE32+，单 .text 节，文件布局：DOS 头 0x00-0x3F → PE 签名 + COFF 头
    （20B）+ 可选头（240B，16 个全零数据目录）+ 节头（40B）→ 0x200 对齐 →
    代码落 0x200。进程主线程的入口返回时，退出码就是入口返回值，
    所以 `mov eax,rc; ret` 即可，连 kernel32 导入表都不需要。
    """
    code = b"\xB8" + struct.pack("<I", exit_code & 0xFFFFFFFF) + b"\xC3"  # mov eax,rc; ret
    raw = 0x600  # 节在文件中的长度（含填充），总文件 0x800 字节 > MIN_BYTES
    dos = b"MZ" + b"\x00" * 58 + struct.pack("<I", 0x40)  # e_lfanew = 0x40
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 0xF0, 0x0022)
    opt = struct.pack(
        "<HBBIIIIIQIIHHHHHHIIIIHHQQQQII",
        0x020B, 14, 0,                       # Magic=PE32+, 链接器版本
        raw, 0, 0,                           # SizeOfCode / 已初始化数据 / 未初始化数据
        0x1000, 0x1000,                      # AddressOfEntryPoint, BaseOfCode（RVA）
        0x140000000,                         # ImageBase
        0x1000, 0x200,                       # SectionAlignment, FileAlignment
        6, 0, 0, 0, 6, 0,                    # OS / Image / Subsystem 版本号
        0, 0x2000, 0x200, 0,                 # Win32VersionValue, SizeOfImage, SizeOfHeaders, CheckSum
        2, 0,                                # Subsystem=GUI（不弹控制台窗口）, DllCharacteristics
        0x100000, 0x1000, 0x100000, 0x1000,  # 栈/堆 reserve+commit
        0, 16,                               # LoaderFlags, NumberOfRvaAndSizes
    ) + b"\x00" * 128                        # 16 个全零数据目录：无导入、无重定位
    sect = struct.pack(
        "<8sIIIIIIHHI",
        b".text", len(code), 0x1000, raw, 0x200, 0, 0, 0, 0,
        0x60000020,                          # CODE | EXECUTE | READ
    )
    blob = dos + b"PE\x00\x00" + coff + opt + sect
    blob += b"\x00" * (0x200 - len(blob))    # 头部补齐到 FileAlignment
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob + code + b"\x00" * (raw - len(code)))


def _win_stub_exe(path: Path, exit_code: int = 0) -> None:
    """Windows 宿主上写一个**真能执行**的 .exe 替身。

    win 包在 Windows 宿主上**不是**交叉平台——闸门会真跑冒烟（这正是它该做的：
    真包里的 ffmpeg.exe 是能执行的）。`#!/bin/sh` 脚本起 .exe 名在 Windows 上
    根本起不来（CreateProcess 直接 WinError 216），所以必须给真 PE。
    exit_code=3 用于「冒烟返回非 0 → 拦截」用例（对应 mac 侧 FAIL_SCRIPT）。
    """
    _pe_stub(path, exit_code)


def _make_bundle(base: Path, platform: str, *, skip: tuple[str, ...] = (), body: str = OK_SCRIPT,
                 win_rc: int = 0) -> Path:
    bundle = base / ("VideoDownloader.app" if platform == "mac" else "VideoDownloader")
    roots = _MAC_ROOTS if platform == "mac" else _WIN_ROOTS
    suffix = "" if platform == "mac" else ".exe"
    for name in gate.REQUIRED:
        if name in skip:
            continue
        # deno 放第一个根，ffmpeg/ffprobe 放最后一个根——覆盖「分散在两个目录」的真实布局
        root = roots[0] if name == "deno" else roots[-1]
        target = bundle / root / f"{name}{suffix}"
        if platform == "win" and gate.host_platform() == "win":
            _win_stub_exe(target, win_rc)   # Windows 宿主跑 win 包：冒烟真执行，替身必须是真 PE
        else:
            _write_exec(target, body)
    return bundle


def main() -> int:
    failures: list[str] = []

    def expect(label: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  ✔ {label}")
        else:
            print(f"  ❌ {label}" + (f" — {detail}" if detail else ""))
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="vdl_gate_", ignore_cleanup_errors=True) as tmp:
        # ignore_cleanup_errors：Windows 宿主上 win 包冒烟会**真执行** PE 替身，
        # 执行过的镜像文件被系统/安全层持有句柄，临时目录卸载时删不掉（WinError 5）。
        # 残留的只是 2KB 的替身 PE，不该因此染红构建。
        base = Path(tmp)

        # [1] macOS 齐备 → 通过，且真的跑过冒烟
        b = _make_bundle(base / "mac_ok", "mac")
        r = gate.check(b)
        expect("[1] mac 齐备 → ok", r["ok"], str(r["required"]))
        expect("[1] 自动识别 mac", r["platform"] == "mac", r["platform"])
        expect("[1] 冒烟已执行", r["smoke"] == "ok" if gate.host_platform() == "mac" else r["smoke"] != "ok")

        # [2] macOS 缺 deno → 拦截，且指明是 deno（不能只报「有问题」）
        b = _make_bundle(base / "mac_no_deno", "mac", skip=("deno",))
        r = gate.check(b)
        expect("[2] 缺 deno → 拦截", not r["ok"])
        expect("[2] 失败项正好是 deno", r["required"]["deno"]["reason"] == "未找到",
               r["required"]["deno"]["reason"])
        expect("[2] ffmpeg 仍判通过", r["required"]["ffmpeg"]["ok"])

        # [3] 空壳/LFS 指针（< MIN_BYTES）→ 拦截，理由必须是体积异常而非「能跑」
        b = _make_bundle(base / "mac_lfs", "mac", skip=("deno",))
        ptr = b / "Contents/Resources/bin/deno"
        ptr.parent.mkdir(parents=True, exist_ok=True)
        ptr.write_text("version https://git-lfs.github.com/spec/v1\noid sha256:abc\n", encoding="utf-8")
        r = gate.check(b)
        expect("[3] LFS 指针 → 拦截", not r["ok"])
        expect("[3] 理由为体积异常", "体积异常" in r["required"]["deno"]["reason"], r["required"]["deno"]["reason"])

        # [4] 能执行但返回非 0 → 拦截（体积正常，靠冒烟抓出来）
        if gate.host_platform() == "mac":
            b = _make_bundle(base / "mac_smoke_fail", "mac", body=FAIL_SCRIPT)
            r = gate.check(b)
            expect("[4] 冒烟返回非 0 → 拦截", not r["ok"])
            expect("[4] 理由提到返回码", "冒烟执行返回 3" in r["required"]["deno"]["reason"],
                   r["required"]["deno"]["reason"])

        # [4w] 同上，但走 Windows 宿主真执行 PE 替身的路径（2026-10-25 补：
        #      此前 mac 宿主机上 win 包一律「交叉平台跳过冒烟」，这条拦截路径
        #      在 Windows 回归里从来没被验证过——mac 假设就是这样腐烂的）
        if gate.host_platform() == "win":
            b = _make_bundle(base / "win_smoke_fail", "win", win_rc=3)
            r = gate.check(b)
            expect("[4w] win 冒烟返回非 0 → 拦截", not r["ok"])
            expect("[4w] 理由提到返回码", "冒烟执行返回 3" in r["required"]["deno"]["reason"],
                   r["required"]["deno"]["reason"])

        # [5] Windows 包（本机为 mac 时属交叉平台）→ 自动跳过冒烟、不误报
        b = _make_bundle(base / "win_ok", "win")
        r = gate.check(b)
        expect("[5] win 齐备 → ok", r["ok"], str(r["required"]))
        expect("[5] 自动识别 win", r["platform"] == "win", r["platform"])
        if gate.host_platform() != "win":
            expect("[5] 交叉平台自动跳过冒烟", r["smoke"] == "cross-platform", r["smoke"])

        # [6] 缺 ffprobe → 拦截（三分支都要独立成立，不能只盯 deno）
        b = _make_bundle(base / "mac_no_ffprobe", "mac", skip=("ffprobe",))
        r = gate.check(b)
        expect("[6] 缺 ffprobe → 拦截", not r["ok"])

        # [7] 假阴性守卫：不许因为「体积大」就判通过——大文件但冒烟失败仍要拦
        if gate.host_platform() == "mac":
            b = _make_bundle(base / "mac_big_broken", "mac", skip=("deno",))
            big = b / "Contents/Resources/bin/deno"
            big.parent.mkdir(parents=True, exist_ok=True)
            big.write_bytes(b"\x00" * (8 * 1024 * 1024))
            big.chmod(0o755)
            r = gate.check(b)
            expect("[7] 8MB 垃圾字节 → 拦截（不许只按体积放过）", not r["ok"])

        # [8] --no-smoke 时只查存在+非空；缺文件仍必须拦
        b = _make_bundle(base / "mac_nos_smoke", "mac")
        r = gate.check(b, do_smoke=False)
        expect("[8] --no-smoke 仍能判定齐备", r["ok"])
        # mac 包在 Windows 宿主上属「交叉平台」：冒烟本来就没启用，闸门按约定报
        # cross-platform（语义上比 disabled 更准确）；mac 宿主上才是 disabled。
        # 两种宿主要各钉各的值，否则 Windows 回归必红。
        if gate.host_platform() == "mac":
            expect("[8] --no-smoke 标记为 disabled", r["smoke"] == "disabled", r["smoke"])
        else:
            expect("[8] --no-smoke 跨平台标记为 cross-platform",
                   r["smoke"] == "cross-platform", r["smoke"])
        b = _make_bundle(base / "mac_nos_missing", "mac", skip=("deno",))
        r = gate.check(b, do_smoke=False)
        expect("[8] --no-smoke 缺文件仍拦", not r["ok"])

        # [9] 退出码契约（构建脚本靠退出码拦发布，错了等于没拦）
        #      main() 会打印结果，这里吞掉 stdout 保持测试输出可读
        def _rc(argv: list[str]) -> int:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                return gate.main(argv)

        b = _make_bundle(base / "rc_ok", "mac")
        expect("[9] 齐备 → 退出码 0", _rc([str(b), "--json"]) == 0)
        b = _make_bundle(base / "rc_bad", "mac", skip=("deno",))
        expect("[9] 缺失 → 退出码 1", _rc([str(b), "--json"]) == 1)
        expect("[9] 目录不存在 → 退出码 2", _rc([str(base / "nope")]) == 2)

        # [10] 真实布局回归：mac 的 ffmpeg 在 Contents/MacOS/bin，deno 在 Contents/Resources/bin
        #      两个目录都要被搜到（2026-10-21 实测本机包就是这个分布）
        b = _make_bundle(base / "layout", "mac")
        expect("[10] deno 落在 Resources/bin", (b / "Contents/Resources/bin/deno").is_file())
        expect("[10] ffmpeg 落在 MacOS/bin", (b / "Contents/MacOS/bin/ffmpeg").is_file())
        r = gate.check(b)
        expect("[10] 跨目录布局 → ok", r["ok"])

        # [11] MIN_BYTES 不许回到「按体积估真实体积」：真实 ffmpeg 仅 434KB 必须放过
        expect("[11] MIN_BYTES 允许 434KB 的共享链接版 ffmpeg", gate.MIN_BYTES < 444176, str(gate.MIN_BYTES))

    if failures:
        print(f"\n❌ {len(failures)} 项失败：" + ", ".join(failures))
        return 1
    print("\n✔ test_bundle_binaries 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
