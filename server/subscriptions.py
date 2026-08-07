"""订阅监控：持久化订阅源 + 用 yt-dlp 探查频道新视频。

设计要点：
- 订阅源以本地 JSON 文件持久化（默认 <DOWNLOAD_DIR>/.subscriptions.json）。
  任务状态只存内存、重启即丢，但"我关注的 UP 主/频道"是用户长期配置，必须落盘。
- 探查用 yt-dlp extract_flat（只拿元数据、不下载），与下载内核解耦；新建下载
  由调用方（app.py）走 store.create + scheduler.submit，不经过免费配额。
- 探查失败（网络/受限/付费墙）返回空列表，不向外抛，由上层记录，避免 watchdog 卡死。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional


@dataclass
class Subscription:
    id: str
    url: str
    name: str
    platform: str
    quality_key: str
    quality_label: str
    cookie: str = ""
    proxy: str = ""
    auto_check: bool = True
    last_video_ids: list[str] = field(default_factory=list)
    last_checked: float = 0.0
    created_at: float = 0.0

    def to_public_dict(self) -> dict:
        return asdict(self)


class SubscriptionStore:
    """本地 JSON 持久化的订阅源仓库（线程安全）。"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._subs: dict[str, Subscription] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                self._subs = {}
                return
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._subs = {
                    sid: Subscription(**item)
                    for sid, item in data.get("subscriptions", {}).items()
                }
            except Exception:
                # 损坏的文件不致命：以空仓库启动，下次写入会覆盖
                self._subs = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {"subscriptions": {sid: asdict(s) for sid, s in self._subs.items()}},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def add(self, sub: Subscription) -> Subscription:
        with self._lock:
            self._subs[sub.id] = sub
            self._save()
        return sub

    def get(self, sub_id: str) -> Optional[Subscription]:
        with self._lock:
            return self._subs.get(sub_id)

    def list_all(self) -> list[Subscription]:
        with self._lock:
            return sorted(self._subs.values(), key=lambda s: s.created_at, reverse=True)

    def remove(self, sub_id: str) -> bool:
        with self._lock:
            if sub_id in self._subs:
                del self._subs[sub_id]
                self._save()
                return True
            return False

    def update(self, sub_id: str, **fields: Any) -> Optional[Subscription]:
        with self._lock:
            sub = self._subs.get(sub_id)
            if not sub:
                return None
            for k, v in fields.items():
                if hasattr(sub, k):
                    setattr(sub, k, v)
            self._save()
            return sub


def probe_channel(url: str, cookie: str = "", proxy: str = "", limit: int = 100) -> list[dict]:
    """用 yt-dlp extract_flat 探查频道/播放列表条目（仅元数据，不下载）。

    返回最新视频列表（按发布时间倒序，最多 limit 条），每项 {id, title, url, uploader}。
    url 优先用 webpage_url（视频页链接），便于交给 parse_source 二次解析后下载。
    受限/付费条目被忽略；探查整体失败返回空列表，不抛异常。
    """
    try:
        from yt_dlp import YoutubeDL  # 延迟导入：避免无 yt_dlp 影响模块加载
    except Exception:
        return []

    opts: dict[str, Any] = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "no_color": True,
        "socket_timeout": 30,
        "retries": 2,
        "playlistend": limit,
        "simulate": True,
    }
    if cookie:
        cval = cookie if not cookie.lower().startswith("cookie:") else cookie.split(":", 1)[1].strip()
        opts["http_headers"] = {"Cookie": cval}
    if proxy:
        opts["proxy"] = proxy

    items: list[dict] = []
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
        for entry in (info.get("entries") or []):
            if not entry:
                continue
            vid = entry.get("id")
            if not vid:
                continue
            items.append({
                "id": vid,
                "title": entry.get("title") or "未命名视频",
                "url": entry.get("webpage_url") or entry.get("url") or "",
                "uploader": entry.get("uploader") or entry.get("channel") or entry.get("uploader_id") or "",
            })
    except Exception:
        return []
    return items[:limit]


def new_videos(sub: Subscription, limit: int = 100) -> tuple[list[dict], int]:
    """探查频道，返回 (新视频列表, 探查到的视频总数)。新视频 = id 不在已知基线内。"""
    items = probe_channel(sub.url, sub.cookie, sub.proxy, limit=limit)
    known = set(sub.last_video_ids)
    new = [it for it in items if it["id"] not in known]
    return new, len(items)
