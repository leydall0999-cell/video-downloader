"""Windows：让本进程启动的子进程不弹控制台黑窗。

## 为什么需要这个模块

桌面版是 PyInstaller 打出来的 **GUI 程序**（windowed，无控制台）。Windows 有一条
容易忽略的规则：**从一个「没有控制台」的进程启动一个「控制台程序」时，系统会为
子进程新建一个控制台窗口**（黑框），除非显式传 `CREATE_NO_WINDOW`。

本产品的子进程几乎都是控制台程序：
  - `aria2c.exe`（外部多线程下载器，yt-dlp 按名调用）
  - `ffmpeg` / `ffprobe`（转码、抽帧、探测）
  - `deno`（YouTube 的 JS 挑战）
  - 各类 AI 引擎（mlx / onnx 运行时）

用户侧症状（2026-10-18 实锤）：装完打开、点一次下载就**弹出好几个黑窗**，
标题都是 `C:\\Program Files\\视频工坊\\bin\\aria2c.exe`，关不掉、看着像中毒。
数量＝并发子进程数：一个 DASH 任务会分 video / audio（+字幕或封面）几路，
所以常见 2~3 个。

这些窗口对用户零价值（进度在 App 界面里），还会挡住操作。修法是给
`subprocess.Popen` 打补丁：**调用方没显式指定 `creationflags` 时，默认补上
`CREATE_NO_WINDOW`**，这样所有子进程（含 yt-dlp 内部 fork 的 aria2c）都静默运行，
不需要逐个改调用点、也不用改第三方库。

## 边界

- **只在 Windows 生效**（`os.name == "nt"`）。POSIX 下 `creationflags` 会直接抛
  `ValueError`，绝不能带过去。
- 调用方**显式**传了非 0 `creationflags` 时不覆盖（尊重调用方意图，例如需要
  `DETACHED_PROCESS` 的场景）。
- 逃生口：`VDL_SHOW_CONSOLE=1` 时不安装（排查子进程问题时需要看到它的输出）。
- 幂等：重复调用只装一次（靠 `_vdl_no_window` 标记）。

## 与 `_ensure_stdio()` 的区别

`desktop_launcher._ensure_stdio()` 修的是**本进程**的 `sys.stdout is None`
（uvicorn 配置日志时 `isatty()` 崩）；本模块修的是**子进程**要不要新开窗口。
两件事互不相干，都需要。
"""

from __future__ import annotations

import os
import subprocess

# Win32 进程创建标志（winbase.h）。不 import ctypes/win32con，保持零依赖。
CREATE_NO_WINDOW = 0x08000000

_SHOW_CONSOLE_ENV = "VDL_SHOW_CONSOLE"
_PATCH_MARK = "_vdl_no_window"

_TRUTHY = ("1", "true", "yes", "on")


def should_install(os_name: str | None = None, env: dict | None = None,
                   already_installed: bool = False) -> bool:
    """是否应该安装补丁（纯决策函数，便于离线单测）。"""
    name = os.name if os_name is None else os_name
    environ = os.environ if env is None else env
    if name != "nt":
        return False
    if str(environ.get(_SHOW_CONSOLE_ENV) or "").strip().lower() in _TRUTHY:
        return False
    if already_installed:
        return False
    return True


def patch_popen(popen_cls=None) -> bool:
    """给 Popen 类打补丁：未指定 creationflags 时补 CREATE_NO_WINDOW。

    返回 True 表示本次真的装了补丁，False 表示已装过（幂等）。
    """
    cls = subprocess.Popen if popen_cls is None else popen_cls
    if getattr(cls, _PATCH_MARK, False):
        return False
    original_init = cls.__init__

    def __init__(self, *args, **kwargs):
        # 只在调用方没指定（或指定为 0/None）时补；显式标志一律尊重。
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = CREATE_NO_WINDOW
        original_init(self, *args, **kwargs)

    cls.__init__ = __init__
    setattr(cls, _PATCH_MARK, True)
    return True


def install() -> bool:
    """在 Windows 上安装补丁。非 Windows 或已装过返回 False。"""
    if not should_install(already_installed=getattr(subprocess.Popen, _PATCH_MARK, False)):
        return False
    return patch_popen(subprocess.Popen)
