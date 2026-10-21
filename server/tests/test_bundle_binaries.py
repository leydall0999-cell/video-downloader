#!/usr/bin/env python3
"""离线测试：desktop/ci_assert_bundle_binaries.py（发布包必备二进制核查）。

为什么要有这个测试
------------------
这个脚本是「拦截残包」的闸门本身。闸门写错了比不写更危险：
  - 假阳性（真包被判缺）⇒ 每次构建都被拦，别人会去关掉闸门；
  - 假阴性（缺 deno 却判通过）⇒ 又回到「YouTube 高码率静默降级」。
所以两种方向都必须钉死。

用 shell 脚本当二进制替身（exit 0 / exit 3），不依赖真实 deno/ffmpeg，
测试是确定性的、毫秒级。真实的 ffmpeg 是 --enable-shared 版，主可执行文件
只有 434KB —— 这里也顺带锁住「不许用体积阈值当判据」这条。
"""

from __future__ import annotations

import contextlib
import io
import os
import stat
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


def _make_bundle(base: Path, platform: str, *, skip: tuple[str, ...] = (), body: str = OK_SCRIPT) -> Path:
    bundle = base / ("VideoDownloader.app" if platform == "mac" else "VideoDownloader")
    roots = _MAC_ROOTS if platform == "mac" else _WIN_ROOTS
    suffix = "" if platform == "mac" else ".exe"
    for name in gate.REQUIRED:
        if name in skip:
            continue
        # deno 放第一个根，ffmpeg/ffprobe 放最后一个根——覆盖「分散在两个目录」的真实布局
        root = roots[0] if name == "deno" else roots[-1]
        _write_exec(bundle / root / f"{name}{suffix}", body)
    return bundle


def main() -> int:
    failures: list[str] = []

    def expect(label: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"  ✔ {label}")
        else:
            print(f"  ❌ {label}" + (f" — {detail}" if detail else ""))
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="vdl_gate_") as tmp:
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
        expect("[8] --no-smoke 标记为 disabled", r["smoke"] == "disabled", r["smoke"])
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
