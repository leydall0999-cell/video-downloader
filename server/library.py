"""本地媒体库：扫描下载目录，返回已下载的媒体文件清单。

设计要点：
- 以磁盘文件为准（任务状态只存内存、重启即丢），所以媒体库 = 扫 DOWNLOAD_DIR。
- 条目 id = 相对路径的 base64urlsafe 编码（无斜杠、URL 安全、可逆），避免维护重启丢失的 id→path 映射。
- 缩略图懒生成：首次请求时由 ffmpeg 抽首帧并缓存到 .thumbs/；音频无缩略图。
- 安全：id 解码后必须 resolve 在 download_dir 内，否则一律视为不存在（防目录穿越）。
"""

from __future__ import annotations

import base64
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts", ".flv", ".mpeg", ".mpg"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus", ".wma"}
IMAGE_EXTS = {".gif", ".webp"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS

THUMB_DIR_NAME = ".thumbs"
_THUMB_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# id 编解码（base64urlsafe，可逆、URL 安全）
# --------------------------------------------------------------------------- #
def encode_id(rel_posix: str) -> str:
    return base64.urlsafe_b64encode(rel_posix.encode("utf-8")).decode("ascii").rstrip("=")


def decode_id(lib_id: str) -> str:
    pad = "=" * (-len(lib_id) % 4)
    return base64.urlsafe_b64decode((lib_id + pad).encode("ascii")).decode("utf-8")


# --------------------------------------------------------------------------- #
# 安全解析
# --------------------------------------------------------------------------- #
def _resolve_safe(download_dir: Path, lib_id: str) -> Path | None:
    """把 id 解码为真实文件，并严格限制在其 download_dir 内。"""
    try:
        rel = decode_id(lib_id)
        candidate = (download_dir / rel).resolve()
    except Exception:
        return None
    root = download_dir.resolve()
    if candidate == root or root in candidate.parents:
        if candidate.is_file():
            return candidate
    return None


def _load_sidecar(path: Path) -> dict[str, Any]:
    sidecar = path.with_name(path.stem + ".vdlmeta.json")
    if sidecar.is_file():
        try:
            return json.loads(sidecar.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {}
    return {}


def _in_progress(path: Path) -> bool:
    """yt-dlp 下载中会存在 <name>.part 兄弟文件，此时成品尚未就绪，跳过。"""
    return path.with_name(path.name + ".part").exists() or path.with_suffix(path.suffix + ".part").exists()


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
def scan_library(download_dir: Path) -> list[dict[str, Any]]:
    """递归扫描下载目录下的媒体文件，返回按修改时间倒序的清单。"""
    items: list[dict[str, Any]] = []
    if not download_dir.exists():
        return items
    for path in download_dir.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in MEDIA_EXTS:
            continue
        rel = path.relative_to(download_dir)
        # 跳过缩略图缓存目录与转换目录里的临时/派生文件（可选包含 conversions，但跳过 .part）
        if rel.parts and rel.parts[0] == THUMB_DIR_NAME:
            continue
        if _in_progress(path):
            continue
        meta = _load_sidecar(path)
        if suffix in AUDIO_EXTS:
            kind = "audio"
        elif suffix in IMAGE_EXTS:
            kind = "image"
        else:
            kind = "video"
        stat = path.stat()
        items.append(
            {
                "id": encode_id(rel.as_posix()),
                "name": path.name,
                "title": meta.get("title") or path.stem,
                "platform": meta.get("platform") or "",
                "uploader": meta.get("uploader") or "",
                "duration": int(meta.get("duration") or 0),
                "source_url": meta.get("source_url") or "",
                "thumbnail": meta.get("thumbnail") or "",  # 外链缩略图，可能失效，前端可回退
                "kind": kind,
                "ext": suffix.lstrip("."),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


# --------------------------------------------------------------------------- #
# 缩略图（懒生成）
# --------------------------------------------------------------------------- #
def _thumb_path(download_dir: Path, lib_id: str) -> Path:
    digest = encode_id(lib_id)[:24]
    return download_dir / THUMB_DIR_NAME / (digest + ".jpg")


def get_thumbnail(download_dir: Path, lib_id: str, ffmpeg_bin: str) -> Path | None:
    """为视频生成首帧缩略图（缓存）。音频返回 None；生成失败返回 None。"""
    item_path = _resolve_safe(download_dir, lib_id)
    if not item_path or item_path.suffix.lower() in AUDIO_EXTS:
        return None
    tp = _thumb_path(download_dir, lib_id)
    if tp.exists():
        return tp
    if not ffmpeg_bin:
        return None
    with _THUMB_LOCK:
        # 二次检查，避免并发重复生成
        if tp.exists():
            return tp
        tp.parent.mkdir(parents=True, exist_ok=True)
        tmp = tp.with_suffix(".tmp.jpg")
        cmd = [
            ffmpeg_bin, "-y", "-ss", "1", "-i", str(item_path),
            "-vf", "scale=320:-1", "-frames:v", "1", "-q:v", "4", str(tmp),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            return None
        if tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(tp)
            return tp
        return None


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #
def delete_item(download_dir: Path, lib_id: str) -> bool:
    """删除文件本体、元数据侧车、缩略图。返回是否删到了文件。"""
    path = _resolve_safe(download_dir, lib_id)
    if not path:
        return False
    sidecar = path.with_name(path.stem + ".vdlmeta.json")
    thumb = _thumb_path(download_dir, lib_id)
    for f in (path, sidecar, thumb):
        try:
            if f.exists():
                f.unlink()
        except OSError:
            pass
    return True
