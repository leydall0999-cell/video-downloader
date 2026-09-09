"""VDL 使用统计埋点（后台管理面板数据源之一）。

极轻量本地事件计数：各功能成功分支调用 record_event(kind) 即可埋点，
数据落 ~/.video-downloader/stats.json。纯标准库、零依赖，可独立单测
（路径与时间均可注入）。

事件 kind 约定（随功能接入持续扩充）：
  download     解析/下载成功
  convert      视频/音频/图片格式转换成功
  matting      本地一键抠图成功
  matting_cloud 云端抠图成功（计费）
  dewatermark  图片/PDF 去水印成功
  subtitle     字幕提取成功
  register     注册新账号
  login        登录成功
  reset_pw     密码重置成功
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

_lock = threading.Lock()
_default_now: Callable[[], float] = time.time


def _base_dir() -> Path:
    # 覆盖位：与 server/auth_store._base_dir 保持一致（VDL_DATA_DIR 优先）。
    override = os.environ.get("VDL_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    else:
        base = Path.home() / ".video-downloader"
    return base


def default_path() -> Path:
    return _base_dir() / "stats.json"


def _empty_state() -> dict[str, Any]:
    return {"events": {}, "by_date": {}, "timeline": [], "first_at": 0.0, "last_at": 0.0}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return _empty_state()
    st = _empty_state()
    for k in ("events", "by_date"):
        if isinstance(data.get(k), dict):
            st[k] = data[k]
    if isinstance(data.get("timeline"), list):
        st["timeline"] = data["timeline"]
    st["first_at"] = float(data.get("first_at", 0) or 0)
    st["last_at"] = float(data.get("last_at", 0) or 0)
    return st


def _save(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def record_event(kind: str, meta: Optional[dict] = None,
                 now_fn: Callable[[], float] = _default_now,
                 path: Optional[Path] = None) -> None:
    """记录一次功能成功事件。线程安全；任何异常均静默（不影响主流程）。"""
    try:
        kind = (kind or "").strip()
        if not kind:
            return
        path = path or default_path()
        now = now_fn()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        with _lock:
            st = _load(path)
            st["events"][kind] = int(st["events"].get(kind, 0)) + 1
            bd = st["by_date"].setdefault(day, {})
            bd[kind] = int(bd.get(kind, 0)) + 1
            entry = {"ts": now, "kind": kind}
            if meta:
                entry["meta"] = meta
            st["timeline"].append(entry)
            st["timeline"] = st["timeline"][-500:]  # 仅保留最近 500 条明细
            if not st["first_at"]:
                st["first_at"] = now
            st["last_at"] = now
            _save(path, st)
    except Exception:  # noqa: BLE001
        # 统计埋点失败绝不应影响业务主流程
        pass


def get_stats(path: Optional[Path] = None,
              now_fn: Callable[[], float] = _default_now) -> dict[str, Any]:
    """聚合统计：总次数、按 kind 计数、最近 14 天日期直方图、最近明细。"""
    path = path or default_path()
    with _lock:
        st = _load(path)
    now = now_fn()
    # 最近 14 天日期序列
    days = []
    for i in range(13, -1, -1):
        d = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
        days.append(d)
    by_day = {d: st["by_date"].get(d, {}) for d in days}
    return {
        "total": int(sum(st["events"].values())),
        "by_kind": dict(st["events"]),
        "by_day": by_day,
        "recent": list(reversed(st["timeline"][-50:])),
        "first_at": st["first_at"],
        "last_at": st["last_at"],
    }


def reset_stats(path: Optional[Path] = None) -> None:
    """清空统计（后台面板「重置统计」用）。"""
    p = path or default_path()
    with _lock:
        _save(p, _empty_state())
