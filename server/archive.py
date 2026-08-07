"""一键归档网盘：把本地媒体库的文件按规则批量 / 自动上传到用户自己的网盘。

与 clouddrive.py 的分工：
- clouddrive.py 负责「怎么传」（WebDAV / 百度网盘协议细节）；
- archive.py 负责「传什么、传到哪、传过没有」（选取规则、路径模板、去重记录、自动巡检）。

设计要点：
- **只搬用户自己的文件到用户自己的网盘**，服务端不留存、不中转他人内容。
- 归档记录按「相对路径 + 大小 + mtime」指纹去重，文件没变就不会重复上传；
  文件被重新加工（大小/时间变了）会自动再传一次。
- 归档后删本地是**可选且默认关闭**的，开启时也强制走系统回收站（复用 retention 的四级兜底），
  回收站不可用就只归档不删，绝不静默硬删用户资产。
- 凭据（WebDAV 密码 / 百度 token）持久化在本机配置文件，文件权限收紧到 0600，
  接口返回时一律脱敏，不回显明文。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("vdl.archive")

# 路径模板可用占位符（前端展示用）
TEMPLATE_TOKENS = {
    "{filename}": "完整文件名（含扩展名）",
    "{title}": "标题（无扩展名）",
    "{ext}": "扩展名，如 mp4",
    "{platform}": "来源平台，如 youtube",
    "{uploader}": "作者 / UP 主",
    "{kind}": "类型：video / audio / image",
    "{date}": "文件日期 2026-08-07",
    "{year}": "年份 2026",
    "{month}": "月份 08",
}

DEFAULT_TEMPLATE = "VideoDownloader/{platform}/{date}/{filename}"

# 文件名里在各家网盘上普遍不安全的字符
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


@dataclass
class ArchiveConfig:
    """归档策略。凭据不在这里，单独存放以便脱敏返回。"""

    # 自动巡检总开关：关闭时只能手动点「立即归档」
    auto_enabled: bool = False
    interval_hours: float = 6.0

    provider: str = "webdav"          # webdav | baidu
    dest_template: str = DEFAULT_TEMPLATE

    # 只归档这些类型（抽帧封面之类的图片默认不传，免得把网盘刷爆）
    include_video: bool = True
    include_audio: bool = True
    include_image: bool = False

    # 刚下完/刚加工完的文件可能还在写，静置 N 分钟再传
    min_age_minutes: float = 3.0
    # 单文件体积上限（GB），超过跳过，避免误传超大文件跑满上行
    max_file_gb: float = 10.0

    # 归档成功后删本地（强制走回收站；回收站不可用时只归档不删）
    delete_after: bool = False

    # 运行统计（只读展示）
    last_run: int = 0
    last_uploaded: int = 0
    last_failed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def kinds(self) -> set[str]:
        ks: set[str] = set()
        if self.include_video:
            ks.add("video")
        if self.include_audio:
            ks.add("audio")
        if self.include_image:
            ks.add("image")
        return ks


def _mask(value: str) -> str:
    """凭据脱敏：只保留首尾各 2 字符。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


class ArchiveStore:
    """归档配置 + 凭据 + 已归档记录的持久化（JSON，单文件，权限 0600）。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._config = ArchiveConfig()
        self._creds: dict[str, dict] = {"webdav": {}, "baidu": {}}
        self._records: dict[str, dict] = {}
        self._load()

    # ---- 持久化 ---- #
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("归档配置损坏，按默认值启动：%s", self.path)
            return
        if not isinstance(raw, dict):
            return
        cfg_raw = raw.get("config") or {}
        valid = {f for f in ArchiveConfig().to_dict()}
        try:
            self._config = ArchiveConfig(**{k: v for k, v in cfg_raw.items() if k in valid})
        except Exception:
            logger.warning("归档配置字段异常，按默认值启动")
            self._config = ArchiveConfig()
        creds = raw.get("creds") or {}
        if isinstance(creds, dict):
            self._creds = {
                "webdav": creds.get("webdav") if isinstance(creds.get("webdav"), dict) else {},
                "baidu": creds.get("baidu") if isinstance(creds.get("baidu"), dict) else {},
            }
        recs = raw.get("records")
        if isinstance(recs, dict):
            self._records = {k: v for k, v in recs.items() if isinstance(v, dict)}

    def _save(self) -> None:
        data = {"config": self._config.to_dict(), "creds": self._creds, "records": self._records}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)  # 含明文凭据，收紧权限
            except OSError:
                pass
            tmp.replace(self.path)
        except OSError:
            logger.warning("归档配置写入失败：%s", self.path, exc_info=True)

    # ---- 配置 ---- #
    def get(self) -> ArchiveConfig:
        with self._lock:
            return ArchiveConfig(**self._config.to_dict())

    def update(self, **fields) -> ArchiveConfig:
        with self._lock:
            cur = self._config.to_dict()
            for k, v in fields.items():
                if k in cur and v is not None:
                    cur[k] = v
            self._config = ArchiveConfig(**cur)
            self._save()
            return ArchiveConfig(**cur)

    # ---- 凭据 ---- #
    def get_creds(self, provider: str) -> dict:
        with self._lock:
            return dict(self._creds.get(provider) or {})

    def set_creds(self, provider: str, creds: dict) -> None:
        """写入凭据。值为空字符串的字段保留原值 —— 支持前端「不改密码就留空」。"""
        with self._lock:
            cur = dict(self._creds.get(provider) or {})
            for k, v in (creds or {}).items():
                if v == "" and k in cur:
                    continue  # 留空 = 沿用旧值
                cur[k] = v
            self._creds[provider] = cur
            self._save()

    def creds_masked(self) -> dict:
        with self._lock:
            wd = self._creds.get("webdav") or {}
            bd = self._creds.get("baidu") or {}
            return {
                "webdav": {
                    "url": wd.get("url", ""),
                    "user": wd.get("user", ""),
                    "pass_set": bool(wd.get("pass")),
                    "pass_masked": _mask(wd.get("pass", "")),
                },
                "baidu": {
                    "token_set": bool(bd.get("token")),
                    "token_masked": _mask(bd.get("token", "")),
                },
            }

    def has_creds(self, provider: str) -> bool:
        c = self.get_creds(provider)
        if provider == "webdav":
            return bool((c.get("url") or "").strip())
        if provider == "baidu":
            return bool((c.get("token") or "").strip())
        return False

    # ---- 归档记录 ---- #
    def is_archived(self, fp: str) -> bool:
        with self._lock:
            return fp in self._records

    def record(self, fp: str, rel: str, remote: str, provider: str, size: int) -> None:
        with self._lock:
            self._records[fp] = {
                "rel": rel, "remote": remote, "provider": provider,
                "size": size, "at": int(time.time()),
            }
            self._prune_locked()
            self._save()

    def records(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = sorted(self._records.values(), key=lambda r: r.get("at", 0), reverse=True)
        return rows[:limit]

    def forget(self, rel: str = "") -> int:
        """清除归档记录（rel 为空则清空全部），下次会重新上传。"""
        with self._lock:
            if not rel:
                n = len(self._records)
                self._records = {}
            else:
                drop = [k for k, v in self._records.items() if v.get("rel") == rel]
                n = len(drop)
                for k in drop:
                    self._records.pop(k, None)
            self._save()
            return n

    def _prune_locked(self) -> None:
        """记录只增不删会无限膨胀；超过 5000 条时丢掉最旧的，只留最近 3000 条。"""
        if len(self._records) <= 5000:
            return
        rows = sorted(self._records.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)
        self._records = dict(rows[:3000])


# --------------------------------------------------------------------------- #
# 选取与路径
# --------------------------------------------------------------------------- #

def fingerprint(rel: str, size: int, mtime: int) -> str:
    """归档去重指纹：路径 + 大小 + 修改时间。文件被重新加工后指纹变化，会重新归档。"""
    return f"{rel}|{size}|{int(mtime)}"


def _safe_seg(text: str, fallback: str = "未分类") -> str:
    """把一段文本清洗成各网盘都能接受的路径段。"""
    s = _UNSAFE_CHARS.sub("_", (text or "").strip())
    s = s.strip(". ")           # 去掉首尾点与空格（Windows/部分网盘不接受）
    s = re.sub(r"_{2,}", "_", s)
    if len(s) > 80:
        s = s[:80].rstrip("_ ")
    return s or fallback


def render_dest(template: str, item: dict) -> str:
    """按模板渲染远端相对路径。每段单独安全化，'..' 会被清成 '_'。"""
    name = item.get("name") or "unnamed"
    stem = name.rsplit(".", 1)[0] if "." in name else name
    ts = int(item.get("mtime") or time.time())
    dt = datetime.fromtimestamp(ts)
    values = {
        "{filename}": name,
        "{title}": item.get("title") or stem,
        "{ext}": item.get("ext") or (name.rsplit(".", 1)[-1] if "." in name else ""),
        "{platform}": item.get("platform") or "未知平台",
        "{uploader}": item.get("uploader") or "未知作者",
        "{kind}": item.get("kind") or "video",
        "{date}": dt.strftime("%Y-%m-%d"),
        "{year}": dt.strftime("%Y"),
        "{month}": dt.strftime("%m"),
    }
    tpl = (template or DEFAULT_TEMPLATE).strip()
    out = tpl
    for token, val in values.items():
        out = out.replace(token, str(val))
    # '.' 与 '..' 段直接丢弃（而不是清洗成占位名），避免造出「未分类/未分类/」这种怪目录
    raw_segs = [s for s in out.replace("\\", "/").split("/")
                if s.strip() and s.strip(". ") != ""]
    segs = [_safe_seg(s) for s in raw_segs]
    if not segs:
        return _safe_seg(name, "unnamed")
    # 模板没带文件名时兜底补上，否则会把文件写成目录名
    if "{filename}" not in tpl and "{title}" not in tpl:
        segs.append(_safe_seg(name, "unnamed"))
    return "/".join(segs)


def pending_items(items: list[dict], cfg: ArchiveConfig, store: ArchiveStore,
                  now: float | None = None) -> list[dict]:
    """从媒体库清单里挑出「该归档但还没归档」的文件，并附上目标远端路径。"""
    now = now or time.time()
    kinds = cfg.kinds()
    min_age = max(0.0, float(cfg.min_age_minutes or 0)) * 60
    max_bytes = max(0.0, float(cfg.max_file_gb or 0)) * 1024 ** 3
    out: list[dict] = []
    for it in items:
        if it.get("kind") not in kinds:
            continue
        size = int(it.get("size") or 0)
        if max_bytes and size > max_bytes:
            continue
        mtime = int(it.get("mtime") or 0)
        if min_age and (now - mtime) < min_age:
            continue
        rel = ""
        try:
            import library as _lib  # 延迟导入，避免循环依赖
            rel = _lib.decode_id(it["id"])
        except Exception:
            rel = it.get("name", "")
        fp = fingerprint(rel, size, mtime)
        if store.is_archived(fp):
            continue
        out.append({
            "id": it["id"], "rel": rel, "name": it.get("name", ""),
            "title": it.get("title", ""), "kind": it.get("kind", ""),
            "size": size, "mtime": mtime, "fp": fp,
            "dest": render_dest(cfg.dest_template, it),
        })
    out.sort(key=lambda x: x["mtime"])  # 旧的先传
    return out


def human_size(num: float) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= step
    return f"{num:.1f} TB"


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #

def run_archive(
    download_dir: Path,
    targets: list[dict],
    cfg: ArchiveConfig,
    store: ArchiveStore,
    uploader: Callable[[Path, str, dict, Callable[[int, int], None] | None], str],
    creds: dict,
    on_progress: Callable[[dict], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    trash: Callable[[Path], bool] | None = None,
    trash_ok: Callable[[], bool] | None = None,
) -> dict:
    """逐个上传 targets。uploader 由调用方注入（app.py 传 clouddrive Provider.upload）。

    返回 {"uploaded", "failed", "skipped", "bytes", "deleted", "errors", "items"}。
    单个文件失败不中断整批 —— 网盘偶发抖动很常见，中断反而更糟。
    """
    root = download_dir.resolve()
    result: dict[str, Any] = {
        "uploaded": 0, "failed": 0, "skipped": 0, "bytes": 0,
        "deleted": 0, "errors": [], "items": [],
    }
    total = len(targets)
    for idx, t in enumerate(targets, 1):
        if should_stop and should_stop():
            result["skipped"] += total - idx + 1
            result["errors"].append("已被用户取消")
            break
        rel = t.get("rel") or ""
        path = (download_dir / rel).resolve()
        # 穿越防护：解析后必须仍在下载目录内，且确实是文件
        if not (root in path.parents) or not path.is_file():
            result["skipped"] += 1
            result["errors"].append(f"跳过（路径无效）：{rel}")
            continue
        if on_progress:
            on_progress({"index": idx, "total": total, "current": t.get("name", ""),
                         "file_percent": 0.0})
        try:
            def _p(sent: int, tot: int, _i=idx) -> None:
                if on_progress and tot:
                    on_progress({"index": _i, "total": total, "current": t.get("name", ""),
                                 "file_percent": round(sent / tot * 100, 1)})

            remote = uploader(path, t.get("dest") or path.name, creds, _p)
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            msg = getattr(exc, "message", None) or str(exc)
            hint = getattr(exc, "hint", "")
            result["failed"] += 1
            result["errors"].append(f"{t.get('name', rel)}：{msg}{('：' + hint) if hint else ''}"[:300])
            logger.warning("归档失败 %s: %s", rel, msg)
            continue

        size = int(t.get("size") or 0)
        result["uploaded"] += 1
        result["bytes"] += size
        store.record(t.get("fp") or fingerprint(rel, size, int(t.get("mtime") or 0)),
                     rel, remote, cfg.provider, size)
        entry = {"rel": rel, "name": t.get("name", ""), "remote": remote, "size": size,
                 "deleted": False}

        # 归档后删本地：默认关；开启也强制走回收站，回收站不可用就只归档不删
        if cfg.delete_after:
            if trash_ok and not trash_ok():
                if "系统回收站不可用，已归档但未删除本地文件" not in result["errors"]:
                    result["errors"].append("系统回收站不可用，已归档但未删除本地文件")
            elif trash:
                sidecar = path.with_name(path.stem + ".vdlmeta.json")
                gone = False
                for f in (path, sidecar):
                    if f.exists():
                        try:
                            if trash(f) and f == path:
                                gone = True
                        except Exception:
                            logger.warning("归档后删除失败：%s", f, exc_info=True)
                if gone:
                    result["deleted"] += 1
                    entry["deleted"] = True
        result["items"].append(entry)

    result["bytes_text"] = human_size(result["bytes"])
    result["ran_at"] = int(time.time())
    return result
