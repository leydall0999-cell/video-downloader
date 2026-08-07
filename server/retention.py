"""时效自动清理（Retention）：按保留期 / 容量上限自动清理下载目录。

为什么需要：
- 长期下载会让磁盘无声爆掉，尤其是「批量抽帧」这种一次几百张图的派生产物，
  以及中断下载留下的 `.part` 碎片 —— 用户既看不见也删不掉。
- 但媒体本体是用户资产，绝不能默认删，所以按「可再生程度」分档，各自独立开关。

安全设计（重要，改代码前先读）：
1. **分档策略**
   - 临时残留 / 抽帧目录 / 缩略图缓存 = App 自己生成的可再生数据 → 直接删（进回收站
     反而白占空间）。
   - 媒体本体 = 用户资产 → 默认关闭；开启后删除**强制走系统回收站**，回收站不可用
     则整条跳过并记原因，绝不静默硬删。
2. **穿越防护**：每个候选路径 resolve 后必须仍在 download_dir 内，否则跳过（防符号
   链接把清理引到目录外）。
3. **保护名单**：配置类文件（.subscriptions.json / .retention.json 等）永不删；
   download_dir 本身永不删。
4. **dry_run 优先**：scan() 只算不删，前端必须先预览清单再执行。
5. **时间基准用 mtime**；目录取「目录内最新文件的 mtime」，避免误删刚生成的抽帧目录。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable

# 与 library.py 保持一致的标记（此处独立定义，避免循环导入）
THUMB_DIR_NAME = ".thumbs"
FRAMES_DIR_MARK = ".抽帧"

# 中断下载 / 转码留下的碎片
TEMP_SUFFIXES = (".part", ".ytdl", ".tmp", ".temp", ".download")
TEMP_PATTERNS = ("*.part", "*.ytdl", "*.part-Frag*", "*.tmp.jpg", "*.temp.*", "*.f*.mp4.part")

# 永不清理的文件名（配置 / 状态）
PROTECTED_NAMES = {
    ".retention.json",
    ".subscriptions.json",
    ".cleanup.json",
    ".DS_Store",
}

# 媒体本体的伴生文件：跟随主文件一起删，不单独统计
SIDECAR_SUFFIXES = (".vdlmeta.json", ".srt", ".vtt", ".ass", ".lrc", ".info.json", ".description")

MEDIA_EXTS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts", ".flv", ".mpeg", ".mpg",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".wav", ".opus", ".wma", ".m4r",
    ".gif", ".webp", ".jpg", ".jpeg", ".png",
}

CATEGORY_LABELS = {
    "temp": "中断下载的临时碎片",
    "frames": "批量抽帧目录",
    "thumbs": "缩略图缓存",
    "media": "媒体文件（超过保留期）",
    "quota": "媒体文件（超出容量上限）",
}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class RetentionConfig:
    """清理策略。天数 <= 0 视为「不限」（等同关闭该档）。"""

    # 总开关：关掉后后台守护不做任何事（手动清理仍可用）
    auto_enabled: bool = False
    # 后台检查间隔（小时）
    interval_hours: float = 6.0

    # 1) 临时碎片：默认开，2 天
    temp_enabled: bool = True
    temp_days: float = 2.0

    # 2) 抽帧目录：默认开，7 天
    frames_enabled: bool = True
    frames_days: float = 7.0

    # 3) 缩略图缓存：默认开，30 天
    thumbs_enabled: bool = True
    thumbs_days: float = 30.0

    # 4) 媒体本体：默认关（用户资产），开启后走回收站
    media_enabled: bool = False
    media_days: float = 30.0

    # 5) 容量上限（GB）：默认关；超额时按最旧删媒体，同样走回收站
    quota_enabled: bool = False
    quota_gb: float = 20.0

    # 媒体删除是否必须走回收站（强烈建议保持 True）
    media_use_trash: bool = True

    last_run: float = 0.0
    last_freed: int = 0
    last_removed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


class RetentionStore:
    """本地 JSON 持久化的清理策略（线程安全）。"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._cfg = RetentionConfig()
        self._load()

    def _load(self) -> None:
        with self._lock:
            if not self._path.exists():
                return
            try:
                data = json.loads(self._path.read_text(encoding="utf-8") or "{}")
                known = {f for f in RetentionConfig().to_dict()}
                self._cfg = RetentionConfig(**{k: v for k, v in data.items() if k in known})
            except Exception:
                # 配置损坏不致命：回落默认值（默认是最保守的：媒体不删）
                self._cfg = RetentionConfig()

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def get(self) -> RetentionConfig:
        with self._lock:
            return RetentionConfig(**self._cfg.to_dict())

    def update(self, **fields: Any) -> RetentionConfig:
        with self._lock:
            for k, v in fields.items():
                if v is None:
                    continue
                if hasattr(self._cfg, k):
                    setattr(self._cfg, k, v)
            self._save()
            return RetentionConfig(**self._cfg.to_dict())


# --------------------------------------------------------------------------- #
# 回收站（跨平台）
# --------------------------------------------------------------------------- #
_TRASH_AVAILABLE: bool | None = None

# 为什么不能只靠 `osascript`/Finder：
#   macOS 上 `tell application "Finder" to delete` 需要「自动化 → Finder」授权。
#   未授权时会报 -10004「发生权限违例」，而 shutil.which("osascript") 依然是 True ——
#   典型假阳性：UI 告诉用户「回收站可用、放心开媒体清理」，实际一条都删不掉。
#   所以这里改成多级兜底，并且可用性检测走「回收站目录真的可写」而不是查命令存在。


def _mount_point(path: Path) -> Path | None:
    """向上找到 path 所在卷的挂载点。"""
    try:
        p = path if path.is_dir() else path.parent
        dev = os.stat(p).st_dev
        while p.parent != p and os.stat(p.parent).st_dev == dev:
            p = p.parent
        return p
    except OSError:
        return None


def _trash_dir_for(path: Path) -> Path | None:
    """返回该路径应该进的回收站目录（同卷优先，避免跨卷复制几十 GB）。"""
    home = Path.home()
    sysname = platform.system()
    if sysname == "Darwin":
        home_trash = home / ".Trash"
        try:
            if os.stat(path.parent).st_dev == os.stat(home).st_dev:
                return home_trash
        except OSError:
            return home_trash
        mp = _mount_point(path)          # 外置卷 → <卷>/.Trashes/<uid>
        if mp:
            d = mp / ".Trashes" / str(os.getuid())
            try:
                d.mkdir(parents=True, exist_ok=True)
                return d
            except OSError:
                pass
        return home_trash                # 退化：跨卷复制，慢但不丢
    if sysname == "Windows":
        return None                      # Windows 走 PowerShell API
    base = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share")) / "Trash"
    return base / "files"


def _manual_trash(path: Path) -> bool:
    """零依赖兜底：手动移入回收站目录（重名自动加序号）。文件仍可从回收站找回。"""
    tdir = _trash_dir_for(path)
    if tdir is None:
        return False
    try:
        tdir.mkdir(parents=True, exist_ok=True)
        dest = tdir / path.name
        n = 1
        while dest.exists():
            dest = tdir / f"{path.stem} {n}{path.suffix}"
            n += 1
        # Linux 按 freedesktop 规范补 .trashinfo，回收站才能「还原」
        if platform.system() not in ("Darwin", "Windows"):
            info_dir = tdir.parent / "info"
            try:
                info_dir.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
                (info_dir / f"{dest.name}.trashinfo").write_text(
                    f"[Trash Info]\nPath={path}\nDeletionDate={stamp}\n", encoding="utf-8")
            except OSError:
                pass
        shutil.move(str(path), str(dest))
        return not path.exists()
    except Exception:
        return False


def trash_available() -> bool:
    """回收站通道是否真的可用（结果缓存）。检测的是「能不能写进回收站目录」。"""
    global _TRASH_AVAILABLE
    if _TRASH_AVAILABLE is not None:
        return _TRASH_AVAILABLE
    sysname = platform.system()
    if sysname == "Windows":
        _TRASH_AVAILABLE = shutil.which("powershell") is not None
        return _TRASH_AVAILABLE
    tdir = _trash_dir_for(Path.home() / "x")
    ok = False
    if tdir is not None:
        try:
            tdir.mkdir(parents=True, exist_ok=True)
            ok = os.access(tdir, os.W_OK)
        except OSError:
            ok = False
    if not ok and sysname != "Darwin":
        ok = shutil.which("gio") is not None or shutil.which("trash-put") is not None
    _TRASH_AVAILABLE = ok
    return _TRASH_AVAILABLE


def move_to_trash(path: Path) -> bool:
    """把文件/目录移入系统回收站。多级兜底，全失败返回 False（调用方须跳过删除，绝不硬删）。"""
    p = str(path)
    sysname = platform.system()

    if sysname == "Windows":
        try:
            ps = (
                "Add-Type -AssemblyName Microsoft.VisualBasic;"
                f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('{p}',"
                "'OnlyErrorDialogs','SendToRecycleBin')"
            )
            if path.is_dir():
                ps = ps.replace("DeleteFile", "DeleteDirectory")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, timeout=60)
            if r.returncode == 0 and not path.exists():
                return True
        except Exception:
            pass
        return False

    if sysname == "Darwin":
        # 1) PyObjC（有的话最正统，回收站里能「放回原处」）
        try:
            from Foundation import NSURL, NSFileManager  # type: ignore
            ok, _, _ = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
                NSURL.fileURLWithPath_(p), None, None)
            if ok and not path.exists():
                return True
        except Exception:
            pass
        # 2) brew 的 trash CLI
        if shutil.which("trash"):
            try:
                r = subprocess.run(["trash", p], capture_output=True, timeout=30)
                if r.returncode == 0 and not path.exists():
                    return True
            except Exception:
                pass
        # 3) Finder AppleScript（需自动化授权，未授权报 -10004）
        try:
            script = f'tell application "Finder" to delete POSIX file {json.dumps(p)}'
            r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=30)
            if r.returncode == 0 and not path.exists():
                return True
        except Exception:
            pass
        # 4) 手动移进 ~/.Trash（零依赖，永远能用）
        return _manual_trash(path)

    # Linux
    for cmd in (["gio", "trash", p], ["trash-put", p]):
        if shutil.which(cmd[0]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=30)
                if r.returncode == 0 and not path.exists():
                    return True
            except Exception:
                pass
    return _manual_trash(path)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _safe_inside(root: Path, target: Path) -> bool:
    """target resolve 后必须严格位于 root 内（不能等于 root）。"""
    try:
        r = root.resolve()
        t = target.resolve()
    except Exception:
        return False
    return t != r and r in t.parents


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    return total


def _dir_mtime(path: Path) -> float:
    """目录时间基准 = 目录内最新文件的 mtime（空目录退化为目录自身 mtime）。"""
    latest = 0.0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    latest = max(latest, p.stat().st_mtime)
                except OSError:
                    pass
    except Exception:
        pass
    if latest:
        return latest
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def _age_days(mtime: float, now: float) -> float:
    return max(0.0, (now - mtime) / 86400.0)


def _is_temp(path: Path) -> bool:
    name = path.name.lower()
    if any(name.endswith(s) for s in TEMP_SUFFIXES):
        return True
    return ".part-frag" in name or name.endswith(".tmp.jpg")


def _sidecars_of(path: Path) -> list[Path]:
    """媒体主文件的伴生文件（元信息 / 字幕），跟随主文件一起清理。"""
    out: list[Path] = []
    stem = path.stem
    parent = path.parent
    try:
        for sib in parent.iterdir():
            if not sib.is_file() or sib == path:
                continue
            if not sib.name.startswith(stem):
                continue
            low = sib.name.lower()
            if any(low.endswith(sfx) for sfx in SIDECAR_SUFFIXES):
                out.append(sib)
    except Exception:
        pass
    return out


def _entry(path: Path, root: Path, category: str, size: int, mtime: float, now: float) -> dict:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    return {
        "path": str(path),
        "rel": rel,
        "category": category,
        "is_dir": path.is_dir(),
        "size": size,
        "age_days": round(_age_days(mtime, now), 1),
    }


# --------------------------------------------------------------------------- #
# 扫描（dry run）
# --------------------------------------------------------------------------- #
def scan(download_dir: Path, cfg: RetentionConfig) -> dict[str, Any]:
    """按策略算出「将要清理什么」，不做任何删除。"""
    root = Path(download_dir)
    now = time.time()
    result: dict[str, list[dict]] = {k: [] for k in CATEGORY_LABELS}
    if not root.exists():
        return {"categories": result, "total_files": 0, "total_size": 0,
                "trash_available": trash_available(), "scanned_at": now}

    frames_dirs: list[Path] = []
    media_files: list[tuple[Path, float, int]] = []

    # 单遍扫描：分拣临时碎片 / 抽帧目录 / 缩略图缓存 / 媒体本体
    for path in root.rglob("*"):
        if path.name in PROTECTED_NAMES:
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue

        # 抽帧目录：只记目录本身，跳过其内部条目
        if any(FRAMES_DIR_MARK in part for part in rel_parts[:-1]):
            continue
        if path.is_dir() and FRAMES_DIR_MARK in path.name:
            frames_dirs.append(path)
            continue

        in_thumbs = bool(rel_parts) and rel_parts[0] == THUMB_DIR_NAME
        if path.is_dir():
            continue
        if not path.is_file():
            continue

        try:
            st = path.stat()
        except OSError:
            continue
        age = _age_days(st.st_mtime, now)

        # 3) 缩略图缓存
        if in_thumbs:
            if cfg.thumbs_enabled and cfg.thumbs_days > 0 and age >= cfg.thumbs_days:
                result["thumbs"].append(_entry(path, root, "thumbs", st.st_size, st.st_mtime, now))
            continue

        # 1) 临时碎片
        if _is_temp(path):
            if cfg.temp_enabled and cfg.temp_days > 0 and age >= cfg.temp_days:
                result["temp"].append(_entry(path, root, "temp", st.st_size, st.st_mtime, now))
            continue

        # 4/5) 媒体本体（伴生文件不单独计入）
        if path.suffix.lower() in MEDIA_EXTS:
            media_files.append((path, st.st_mtime, st.st_size))

    # 2) 抽帧目录
    if cfg.frames_enabled and cfg.frames_days > 0:
        for d in frames_dirs:
            mt = _dir_mtime(d)
            if _age_days(mt, now) >= cfg.frames_days:
                result["frames"].append(_entry(d, root, "frames", _dir_size(d), mt, now))

    # 4) 媒体保留期
    expired: set[Path] = set()
    if cfg.media_enabled and cfg.media_days > 0:
        for path, mt, size in media_files:
            if _age_days(mt, now) >= cfg.media_days:
                total = size + sum(_safe_size(s) for s in _sidecars_of(path))
                result["media"].append(_entry(path, root, "media", total, mt, now))
                expired.add(path)

    # 5) 容量上限：按最旧优先删，直到降到阈值内（不重复计已在 media 里的）
    if cfg.quota_enabled and cfg.quota_gb > 0:
        limit = int(cfg.quota_gb * 1024 ** 3)
        current = sum(size for _, _, size in media_files)
        freed = sum(e["size"] for e in result["media"])
        if current - freed > limit:
            for path, mt, size in sorted(media_files, key=lambda x: x[1]):
                if current - freed <= limit:
                    break
                if path in expired:
                    continue
                total = size + sum(_safe_size(s) for s in _sidecars_of(path))
                result["quota"].append(_entry(path, root, "quota", total, mt, now))
                freed += total

    total_files = sum(len(v) for v in result.values())
    total_size = sum(e["size"] for v in result.values() for e in v)
    return {
        "categories": result,
        "labels": CATEGORY_LABELS,
        "total_files": total_files,
        "total_size": total_size,
        "trash_available": trash_available(),
        "scanned_at": now,
    }


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def run(download_dir: Path, cfg: RetentionConfig, categories: Iterable[str] | None = None) -> dict[str, Any]:
    """按策略真实清理。categories 可限定只清某几档（默认全部）。"""
    root = Path(download_dir)
    plan = scan(root, cfg)
    wanted = set(categories) if categories else set(CATEGORY_LABELS)

    removed, failed, freed = 0, 0, 0
    errors: list[str] = []
    need_trash = {"media", "quota"}

    for cat, entries in plan["categories"].items():
        if cat not in wanted:
            continue
        use_trash = cfg.media_use_trash and cat in need_trash
        if use_trash and not trash_available():
            if entries:
                errors.append(f"{CATEGORY_LABELS[cat]}：系统回收站不可用，已跳过 {len(entries)} 项（拒绝硬删）")
            continue

        for e in entries:
            target = Path(e["path"])
            if not _safe_inside(root, target):
                errors.append(f"跳过（越界）：{e['rel']}")
                continue
            if target.name in PROTECTED_NAMES:
                continue
            victims = [target]
            if cat in need_trash:
                victims.extend(_sidecars_of(target))
            ok = True
            for v in victims:
                if not v.exists():
                    continue
                if use_trash:
                    ok = move_to_trash(v) and ok
                else:
                    try:
                        shutil.rmtree(v) if v.is_dir() else v.unlink()
                    except OSError as exc:
                        ok = False
                        errors.append(f"删除失败 {v.name}：{exc}")
            if ok:
                removed += 1
                freed += e["size"]
            else:
                failed += 1

    # 顺手收掉清空后的空抽帧目录
    _prune_empty_frame_dirs(root)

    return {
        "removed": removed,
        "failed": failed,
        "freed": freed,
        "errors": errors[:20],
        "ran_at": time.time(),
    }


def _prune_empty_frame_dirs(root: Path) -> None:
    try:
        for d in root.rglob("*"):
            if d.is_dir() and FRAMES_DIR_MARK in d.name:
                try:
                    next(d.iterdir())
                except StopIteration:
                    try:
                        d.rmdir()
                    except OSError:
                        pass
                except OSError:
                    pass
    except Exception:
        pass


def disk_usage(download_dir: Path) -> dict[str, Any]:
    """下载目录占用 + 所在磁盘剩余空间，给前端展示决策依据。"""
    root = Path(download_dir)
    used = _dir_size(root) if root.exists() else 0
    free = 0
    try:
        free = shutil.disk_usage(str(root if root.exists() else root.parent)).free
    except Exception:
        pass
    return {"dir_size": used, "disk_free": free, "path": str(root)}


def human_size(n: int) -> str:
    step = 1024.0
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < step:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= step
    return f"{val:.1f} PB"
