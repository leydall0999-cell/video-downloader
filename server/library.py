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
import os
import subprocess
import threading
import time
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
# 内部工作区（2026-10-20 实测发现）：DOWNLOAD_DIR 下除了用户素材，还躺着各功能的
# 临时工作目录。它们全是中间产物，绝不能当媒体库条目列出来 —— 实测一次 30s 视频去水印
# 会往 `dewatermark/dw_work_<job>/frames/` 写 450+ 张 PNG，媒体库 456 条里 451 条是帧图，
# 真正的片子被埋掉，且用户误点删除会毁掉正在跑的任务。
#   · dw_work_*     去水印逐帧工作区（抽帧/掩码/中间视频）
#   · preview_cache 去水印预览片段缓存
#   · uploads       分片上传暂存（上传完成即落库，暂存件无需展示）
# 产物本身（dw_img_* / dw_vid_* / dw_pdf_*）仍保留在库里，用户能找到成品。
_SKIP_DIR_NAMES = {"preview_cache", "uploads"}
_SKIP_DIR_PREFIXES = ("dw_work_",)
# 去水印与抠像共用 DW_DIR（dewatermark/）放中间件：上传原件、缩略图、胶卷图、预览片段、
# 分析/拆分临时图。只有 `dw_img_* / dw_vid_<job> / dw_pdf_<job> / mat_<job>.png` 是成品。
_SKIP_FILE_PREFIXES = (
    "dw_up_", "dw_pdf_up_", "dw_vid_up_", "dw_prev_up_",
    "dw_vid_thumb_", "dw_vid_film_", "dw_prev_",
    "mat_up_", "mat_anl_", "mat_spl_",
)
_THUMB_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# 扫描缓存（性能）—— 见下方 scan_library 顶部注释
# --------------------------------------------------------------------------- #
# 前端搜索框每输入一次（防抖 300ms）就请求一次 /api/library；旧实现每次都
# 递归 rglob 全盘 + 逐文件读 .vdlmeta.json + stat ⇒ 大库下「输入/切页签」明显卡。
# 两级缓存：
#   ① 扫描结果缓存（stale-while-revalidate）：命中直接返内存列表；过期时先返
#      旧结果、后台刷新，搜索与切页签永不阻塞在磁盘扫描上。
#   ② sidecar 记忆：(媒体 mtime/size + sidecar mtime/size) 未变则复用已解析 meta。
_SCAN_TTL = 8.0
_SCAN_CACHE: dict[str, dict[str, Any]] = {}
_SCAN_INFLIGHT: set[str] = set()
_SCAN_LOCK = threading.Lock()
_SIDECAR_MEMO: dict[str, tuple[Any, dict[str, Any]]] = {}
_SIDECAR_MEMO_CAP = 200_000


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
# 缓存辅助
# --------------------------------------------------------------------------- #
def _load_sidecar_memo(path: Path) -> dict[str, Any]:
    """带记忆的 sidecar 读取：sidecar 的 (mtime_ns, size) 未变则复用上次解析结果。

    避免每次扫描都把所有 `.vdlmeta.json` 重新读盘 + JSON 解析（大库下是主要开销）。
    """
    sidecar = path.with_name(path.stem + ".vdlmeta.json")
    try:
        sst = sidecar.stat()
        skey: Any = (sst.st_mtime_ns, sst.st_size)
    except OSError:
        skey = None
    k = str(path)
    hit = _SIDECAR_MEMO.get(k)
    if hit is not None and hit[0] == skey:
        return hit[1]
    meta: dict[str, Any] = {}
    if skey is not None:
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8") or "{}")
        except Exception:
            meta = {}
    if len(_SIDECAR_MEMO) >= _SIDECAR_MEMO_CAP:
        _SIDECAR_MEMO.clear()  # 软上限，防止长期运行无限增长
    _SIDECAR_MEMO[k] = (skey, meta)
    return meta


def invalidate_library_cache(download_dir: Path | None = None) -> None:
    """清除扫描缓存（删除/新增文件后调用，确保下次读取立即反映变化）。"""
    with _SCAN_LOCK:
        if download_dir is None:
            _SCAN_CACHE.clear()
            return
        try:
            _SCAN_CACHE.pop(str(download_dir.resolve()), None)
        except OSError:
            _SCAN_CACHE.pop(str(download_dir), None)


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
def _scan_impl(download_dir: Path) -> list[dict[str, Any]]:
    """真正遍历磁盘的实现：os.scandir 递归（DirEntry.stat 在 Windows 上零成本）。"""
    items: list[dict[str, Any]] = []
    try:
        root = download_dir.resolve()
    except OSError:
        return items
    if not root.is_dir():
        return items
    stack: list[str] = [str(root)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            continue
        # 同一目录内的名字集合：`.part` 判定无需额外 stat
        names = {e.name for e in entries}
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    nm = entry.name
                    # 点开头目录（含 .thumbs / .vault_tmp）与批量抽帧目录一律不下钻
                    if nm.startswith(".") or FRAMES_DIR_MARK in nm:
                        continue
                    # 功能内部工作区（去水印逐帧工作区、预览缓存、上传暂存）：不是用户素材
                    if nm in _SKIP_DIR_NAMES or nm.startswith(_SKIP_DIR_PREFIXES):
                        continue
                    stack.append(entry.path)
                    continue

                name = entry.name
                # 去水印的原始上传件 / 预览片段：中间产物，不进库
                if name.startswith(_SKIP_FILE_PREFIXES):
                    continue
                suffix = os.path.splitext(name)[1].lower()
                # yt-dlp 下载中会留 `<name>.part`，成品未就绪，跳过
                if (name + ".part") in names:
                    continue

                p = Path(entry.path)
                encrypted = False
                if suffix == ENCRYPTED_EXT:
                    try:
                        orig_name, orig_kind, orig_ext = crypto_mod.read_header(p)
                    except Exception:
                        continue  # 损坏/非法加密文件，跳过
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

                meta = _load_sidecar_memo(p)
                if encrypted:
                    title = meta.get("title") or Path(orig_name).stem
                else:
                    title = meta.get("title") or p.stem
                st = entry.stat(follow_symlinks=False)
                items.append(
                    {
                        "id": encode_id(p.relative_to(root).as_posix()),
                        "name": name,
                        "title": title,
                        "platform": meta.get("platform") or "",
                        "uploader": meta.get("uploader") or "",
                        "duration": int(meta.get("duration") or 0),
                        "source_url": meta.get("source_url") or "",
                        "thumbnail": meta.get("thumbnail") or "",
                        "kind": kind,
                        "ext": ext,
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                        "encrypted": encrypted,
                    }
                )
            except OSError:
                continue
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def _schedule_refresh(key: str, download_dir: Path) -> None:
    """缓存过期时的后台刷新（stale-while-revalidate），同一目录只跑一个。"""
    with _SCAN_LOCK:
        if key in _SCAN_INFLIGHT:
            return
        _SCAN_INFLIGHT.add(key)

    def _run() -> None:
        try:
            items = _scan_impl(download_dir)
            with _SCAN_LOCK:
                _SCAN_CACHE[key] = {"ts": time.monotonic(), "items": items}
        finally:
            with _SCAN_LOCK:
                _SCAN_INFLIGHT.discard(key)

    threading.Thread(target=_run, name="vdl-lib-refresh", daemon=True).start()


def scan_library(download_dir: Path, force: bool = False) -> list[dict[str, Any]]:
    """返回媒体库清单（按修改时间倒序），带缓存。

    force=True：跳过缓存同步重扫（供前端「刷新」按钮使用）。
    其余情况：命中缓存直接返回；过期则先返回旧结果并后台刷新。
    """
    try:
        key = str(download_dir.resolve())
    except OSError:
        key = str(download_dir)
    with _SCAN_LOCK:
        ent = _SCAN_CACHE.get(key)
    if ent is not None and not force:
        if time.monotonic() - ent["ts"] < _SCAN_TTL:
            return ent["items"]
        _schedule_refresh(key, download_dir)
        return ent["items"]
    items = _scan_impl(download_dir)
    with _SCAN_LOCK:
        _SCAN_CACHE[key] = {"ts": time.monotonic(), "items": items}
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
    # 删除后清缓存，下一次读取立即反映（不必等 TTL 过期）
    invalidate_library_cache(download_dir)
    return True
