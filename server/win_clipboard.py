"""Windows 剪贴板读取（纯 ctypes，**零子进程、零窗口**）。

## 为什么要这个模块（2026-10-18 用户实测事故）

桌面版前端每 **2 秒**轮询一次剪贴板（`web/js/desktop-app.js` 的
`initClipboardWatcher`，用于「复制视频链接 → 切回 App → 一键下载」）。原实现每次
都 `subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"])`：

- 每次都要拉起一个 powershell.exe（冷启动 300~800ms、吃 CPU）
- 桌面版是 `--windowed` 的 GUI 进程，Windows 会给它 fork 的控制台程序新建窗口；
  用户看到的正是「**打开 App 一次，屏幕自己每隔一两秒闪一下、窗口不停在打开**」
- `win_no_window` 的 CREATE_NO_WINDOW 补丁理论上能压住，但只要补丁没生效
  （换构建、被第三方库提前 import、powershell 自己再 spawn 子进程…），
  就退化成持续闪窗，且**用户什么都没点**，极难自查

改成直接调 Win32 API（OpenClipboard / GetClipboardData / GlobalLock）：微秒级、
不建进程、不可能出现窗口。只有 API 调用异常时才回退旧的 powershell 路径。

## 契约

`read_text()`：
- 非 Windows → `None`（调用方自行走 pbpaste / xclip 分支）
- Windows 且读成功（含剪贴板为空）→ `str`（空字符串表示没有文本）
- Windows 但 API 不可用/异常 → `None`（调用方回退 powershell 兜底）
"""

from __future__ import annotations

import ctypes
import os
import time

# Win32 常量（winuser.h / winbase.h）
_CF_UNICODETEXT = 13
_OPEN_RETRIES = 3          # 别的程序短暂占用剪贴板时会 Open 失败，重试几次即可
_OPEN_RETRY_SLEEP = 0.01


def _load_apis():
    """拿到 user32 / kernel32 并显式声明签名（64 位下句柄必须按指针宽度处理）。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_int
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    user32.IsClipboardFormatAvailable.restype = ctypes.c_int

    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    return user32, kernel32


def read_text() -> str | None:
    """见模块 docstring 的契约说明。任何异常都吞掉并返回 None（绝不打断调用方）。"""
    if os.name != "nt":
        return None
    try:
        user32, kernel32 = _load_apis()
    except Exception:
        return None

    opened = False
    for attempt in range(_OPEN_RETRIES):
        try:
            if user32.OpenClipboard(None):
                opened = True
                break
        except Exception:
            return None
        if attempt + 1 < _OPEN_RETRIES:
            time.sleep(_OPEN_RETRY_SLEEP)
    if not opened:
        return None

    try:
        # 没有文本格式 ⇒ 剪贴板里是图片/文件等，按「无可用文本」处理，不必报错
        if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):
            return ""
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    except Exception:
        return None
    finally:
        try:
            user32.CloseClipboard()
        except Exception:
            pass
