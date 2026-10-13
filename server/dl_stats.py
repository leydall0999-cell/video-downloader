#!/usr/bin/env python3
"""下载计数（2026-10-13，用户要求「app 加上安装包/扩展下载次数并统计不同平台的下载数据」）。

口径：
  · bucket  = "installer"（桌面安装包，platform = macos/windows/…）｜
              "extension"（浏览器扩展安装包，platform = 浏览器名 chrome/edge/brave/…）
  · platform：安装包按下载页参数（?p=macos），扩展按 User-Agent 推断浏览器；
  · 落盘    = /opt/vdl-dl-stats/downloads.json（授权中心 stat_query 同机直读合并，
              给管理面板「安装与插件」区块展示）。

形状（手写原子落盘，低频写、量级小，不需要数据库）：
  {"installer": {"macos": {"total": n, "by_day": {"2026-10-13": n}}},
   "extension": {"chrome": {...}}, "updated_at": ts}

路径可用 VDL_DL_STATS_PATH 覆盖（测试隔离）。任何异常都**不能让下载本身失败**
—— 统计是锦上添花，下载是正经功能。
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_STATS_PATH = Path(os.environ.get("VDL_DL_STATS_PATH")
                   or "/opt/vdl-dl-stats/downloads.json")
_LOCK = threading.Lock()

_UA_BROWSERS: tuple[tuple[str, str], ...] = (
    ("edg/", "edge"), ("edga/", "edge_android"), ("edgios/", "edge_ios"),
    ("opr/", "opera"), ("firefox/", "firefox"), ("crios/", "chrome_ios"),
    ("chrome/", "chrome"), ("safari/", "safari"),
)


def stats_path() -> Path:
    return _STATS_PATH


def browser_from_ua(ua: str) -> str:
    """从 User-Agent 推断浏览器名（小写枚举），认不出 → chromium 兜底。"""
    s = (ua or "").lower()
    for token, name in _UA_BROWSERS:
        if token in s:
            return name
    if "chrome" in s or "chromium" in s or "crios" in s:
        return "chromium"
    return "chromium"


def platform_from_ua(ua: str) -> str:
    s = (ua or "").lower()
    if "windows" in s:
        return "windows"
    if "mac os" in s or "macintosh" in s:
        return "macos"
    if "android" in s:
        return "android"
    if "iphone" in s or "ipad" in s or "ios" in s:
        return "ios"
    if "linux" in s:
        return "linux"
    return "unknown"


def _empty_slot() -> dict[str, Any]:
    return {"total": 0, "by_day": {}}


def _today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _load() -> dict[str, Any]:
    try:
        d = json.loads(_STATS_PATH.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception:  # noqa: BLE001 - 文件缺失/损坏都从零开始
        pass
    return {}


def _save(data: dict[str, Any]) -> None:
    try:
        _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_STATS_PATH.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _STATS_PATH)
    except Exception:  # noqa: BLE001 - 落盘失败不抛（下载不该因统计失败）
        pass


def record(bucket: str, platform: str, n: int = 1) -> dict[str, Any]:
    """记一次下载。返回落盘后的完整统计（便于测试断言）。失败返回原样读数。"""
    b = str(bucket or "").strip() or "unknown"
    p = str(platform or "").strip() or "unknown"
    day = _today()
    with _LOCK:
        data = _load()
        slots = data.setdefault(b, {})
        slot = slots.setdefault(p, _empty_slot())
        slot["total"] = int(slot.get("total") or 0) + int(n)
        by_day = slot.setdefault("by_day", {})
        by_day[day] = int(by_day.get(day) or 0) + int(n)
        # by_day 只留最近 90 天（统计够用，文件不膨胀）
        if len(by_day) > 90:
            for k in sorted(by_day)[:-90]:
                by_day.pop(k, None)
        data["updated_at"] = time.time()
        _save(data)
        return data


def read_summary() -> dict[str, Any]:
    """给授权中心 stat_query / 管理面板用的只读汇总（today 现算，不落盘）。"""
    data = _load()
    out: dict[str, Any] = {}
    day = _today()
    for bucket, slots in (data or {}).items():
        if not isinstance(slots, dict):
            continue
        b = out.setdefault(bucket, {})
        for plat, slot in slots.items():
            if not isinstance(slot, dict):
                continue
            by_day = slot.get("by_day") or {}
            b[plat] = {"total": int(slot.get("total") or 0),
                       "today": int(by_day.get(day) or 0)}
    return out


__all__ = ["record", "read_summary", "browser_from_ua", "platform_from_ua",
           "stats_path"]
