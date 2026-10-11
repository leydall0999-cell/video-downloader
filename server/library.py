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

import atomic_io
import crypto_vault as crypto_mod

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts", ".flv", ".mpeg", ".mpg"}
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus", ".wma", ".m4r"}
IMAGE_EXTS = {".gif", ".webp", ".jpg", ".jpeg", ".png"}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS
# 库内保险箱加密文件后缀（内容 AES 加密，仅文件头明文保留原名/类型）。
ENCRYPTED_EXT = ".vdlenc"

THUMB_DIR_NAME = ".thumbs"
# 保险箱「解密播放」临时目录（在 DOWNLOAD_DIR 下）：里面是 .vdlenc 的**明文**产物。
# 🔴 安全审计 2026-10-11（P1-7）：扫描时必须排除，否则加密文件解出来的明文会被
#    当成普通媒体项列进媒体库、并可通过 /api/library/file/{id} 直接下载 ——
#    「加锁」形同虚设。这里连同所有点开头目录一并跳过（防御性）。
VAULT_TMP_DIR_NAME = ".vault_tmp"
# 批量抽帧产物目录（`<标题>.抽帧[.n]`）：里面动辄上百张图，不进媒体库列表，避免刷屏。
FRAMES_DIR_MARK = ".抽帧"
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
    """把 id 解码为真实文件，并严格限制在其 download_dir 内。

    🔴 安全审计 2026-10-11（P1-7）：额外拒绝落在**点开头目录**（`.vault_tmp` /
    `.thumbs` 等）里的路径 —— 保险箱解密播放的明文临时文件就在这里，
    只要构造出对应 id 就能把它当普通文件下载走，绕过「已加锁」。
    """
    try:
        rel = decode_id(lib_id)
        candidate = (download_dir / rel).resolve()
    except Exception:
        return None
    root = download_dir.resolve()
    if candidate == root or root in candidate.parents:
        try:
            parts = candidate.relative_to(root).parts
        except ValueError:
            return None
        if any(p.startswith(".") for p in parts):
            return None
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
        rel = path.relative_to(download_dir)
        # 跳过缩略图缓存目录与转换目录里的临时/派生文件（可选包含 conversions，但跳过 .part）
        if rel.parts and rel.parts[0] == THUMB_DIR_NAME:
            continue
        # 🔴 安全审计 2026-10-11（P1-7）：任何点开头目录（含保险箱解密临时目录
        #    `.vault_tmp`）都不进媒体库 —— 里面的 .dec 是加密文件的**明文**产物。
        if rel.parts and rel.parts[0] == VAULT_TMP_DIR_NAME:
            continue
        if any(part.startswith(".") for part in rel.parts[:-1]):
            continue
        # 跳过批量抽帧子目录（`<标题>.抽帧` / `<标题>.抽帧.2`）里的成百上千张帧图
        if any(FRAMES_DIR_MARK in part for part in rel.parts[:-1]):
            continue
        if _in_progress(path):
            continue
        # 库内保险箱加密项：读明文文件头还原原名/类型，标记 encrypted
        encrypted = False
        if suffix == ENCRYPTED_EXT:
            try:
                orig_name, orig_kind, orig_ext = crypto_mod.read_header(path)
            except Exception:
                continue  # 损坏/非法的加密文件，跳过
            kind = orig_kind
            ext = orig_ext or "vdlenc"
            name = orig_name
            encrypted = True
        elif suffix not in MEDIA_EXTS:
            continue
        else:
            if suffix in AUDIO_EXTS:
                kind = "audio"
            elif suffix in IMAGE_EXTS:
                kind = "image"
            else:
                kind = "video"
            ext = suffix.lstrip(".")
            name = path.name
        meta = _load_sidecar(path)
        if encrypted:
            title = meta.get("title") or Path(orig_name).stem
        else:
            title = meta.get("title") or path.stem
        stat = path.stat()
        items.append(
            {
                "id": encode_id(rel.as_posix()),
                "name": name,
                "title": title,
                "platform": meta.get("platform") or "",
                "uploader": meta.get("uploader") or "",
                "duration": int(meta.get("duration") or 0),
                "source_url": meta.get("source_url") or "",
                "thumbnail": meta.get("thumbnail") or "",  # 外链缩略图，可能失效，前端可回退
                "kind": kind,
                "ext": ext,
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "encrypted": encrypted,
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
    # 加密项无缩略图（内容是密文，抽帧无意义），前端显示锁图标
    if item_path.suffix.lower() == ENCRYPTED_EXT:
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
        # 唯一临时名（见 atomic_io）：固定名 `.tmp.jpg` 下两个并发写者互截断，
        # 半截 jpg 一旦被 replace 上线，`tp.exists()` 之后就永远当作有效缩略图。
        # 扩展名必须保留 `.jpg`：ffmpeg 靠它猜封装格式。
        tmp = atomic_io.unique_temp_path(tp)
        # 静态图片（封面/预览图）只有一帧，带任何 -ss（含 -ss 0）都会被 seek 掉导致输出为空，
        # 所以图片一律不加 -ss；视频先试 1 秒（跳过黑场片头），失败再回退到不 seek。
        is_image = item_path.suffix.lower() in IMAGE_EXTS
        seek_variants: list[list[str]] = [[]] if is_image else [["-ss", "1"], []]
        for seek in seek_variants:
            cmd = [
                ffmpeg_bin, "-y", *seek, "-i", str(item_path),
                "-vf", "scale=320:-1", "-frames:v", "1", "-q:v", "4", str(tmp),
            ]
            try:
                subprocess.run(cmd, capture_output=True, timeout=30)
            except Exception:
                continue
            if tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(tp)
                return tp
        tmp.unlink(missing_ok=True)   # 唯一临时名：失败路径必须自己清理，否则每次重试都留垃圾
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
