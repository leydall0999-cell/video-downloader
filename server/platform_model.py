"""多平台能力模型（Platform capability model）。

把散落在各处的 `sys.frozen` / `sys.platform` 判断收敛成一个显式的「平台」概念，
为「网页版 / 桌面 App（macOS·Windows·Linux）/ 移动端（Android·iOS）」提供统一能力描述。

为什么需要它（对应改造目标）：
- 网页版稳定少改、App 频繁改版，且计划用多个会话窗口分别开发各端。
  把「当前是什么端 / 该端暴露哪些功能」做成显式数据，就能让 App 端新增功能
  **只进对应端的能力集、不进 WEB 集**，从机制上避免改动波及网页版。
- 后续扩 Windows/iOS/Android 时，只是「加配置 + 加独立构建」，不再重构。

两条语义必须区分：
- `is_frozen_binary()`：PyInstaller 打包态，用于资源/路径解析（_MEIPASS / .app Resources）。
  必须严格看 sys.frozen，**不受 VDL_PLATFORM 覆盖影响**——否则打包后路径会错。
- `current_platform()` / `is_desktop()`：用于「功能 / 能力画像」，可被 VDL_PLATFORM 显式覆盖
  （测试 / 特殊部署用）。普通运行下：未打包 = WEB，打包 = 对应桌面平台。
"""

from __future__ import annotations

import os
import sys
from enum import Enum


class Platform(Enum):
    WEB = "web"
    MACOS = "macos"
    WINDOWS = "windows"
    LINUX = "linux"
    ANDROID = "android"
    IOS = "ios"


# /api/nodes 顶层「功能组」键。标量字段（region / peer / china_domains /
# commentary_enabled / ads_enabled / authRequired）始终返回，不在此列。
NODE_GROUPS = (
    "convert",
    "download",
    "cloud",
    "library",
    "subscriptions",
    "retention",
    "archive",
    "crypto",
    "torrent",
    "ai_dewatermark",
    "narrato",
)

# 各平台默认暴露的功能组。初始：所有平台 = 完整集（网页版行为零变化）。
# 今后 App / 移动端专属功能：只加入对应平台集合、不加入 WEB，即可实现
# 「App 加功能不波及网页版」的隔离。
_PLATFORM_NODE_CAPS: dict[Platform, set[str]] = {p: set(NODE_GROUPS) for p in Platform}
# 「AI 解说体验」（NarratoAI 本地子进程）仅桌面 App 有，网页版不暴露
_PLATFORM_NODE_CAPS[Platform.WEB].discard("narrato")


def is_frozen_binary() -> bool:
    """PyInstaller 打包态：用于资源/路径解析，严格看 sys.frozen。"""
    return bool(getattr(sys, "frozen", False))


def _infer_from_binary() -> Platform:
    """未显式覆盖时，由运行环境推断平台。"""
    if not is_frozen_binary():
        return Platform.WEB
    if sys.platform == "win32":
        return Platform.WINDOWS
    if sys.platform == "darwin":
        return Platform.MACOS
    return Platform.LINUX


def current_platform() -> Platform:
    """功能/能力画像用的当前平台。可被 VDL_PLATFORM 显式覆盖（测试/特殊部署）。"""
    override = os.environ.get("VDL_PLATFORM", "").strip().lower()
    if override:
        try:
            return Platform(override)
        except ValueError:
            pass
    return _infer_from_binary()


def is_web() -> bool:
    return current_platform() == Platform.WEB


def is_desktop() -> bool:
    """是否桌面 App 打包态（功能画像层面）。普通运行下等价旧逻辑 getattr(sys,'frozen',False)。"""
    return current_platform() != Platform.WEB


def node_capabilities(platform: Platform | None = None) -> set[str]:
    """该平台在 /api/nodes 中暴露的功能组集合。"""
    p = platform or current_platform()
    return set(_PLATFORM_NODE_CAPS[p])
