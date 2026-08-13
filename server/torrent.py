"""桌面版种子下载（libtorrent 集成）。

设计要点：
- libtorrent 为**可选依赖**：未安装时 `available()` 返回 False，整个功能禁用并优雅降级，
  不会因 import 失败导致进程起不来（与媒体库/保险箱等桌面特性「依赖缺失即关」原则一致）。
- 下载落到 DOWNLOAD_DIR（或其子目录），自动进入媒体库（library.scan_library 递归扫 DOWNLOAD_DIR）。
  某个种子完成时在本地下载目录的媒体文件旁写 `.vdlmeta.json` 侧车，便于媒体库显示平台=torrent。
- 种子状态只存内存（handle map），重启即丢 —— 与项目「任务状态只存内存」原则一致（不持久化，避免规避合规）。
- 后台线程定期 `post_torrent_updates` + 处理完成 alert；其余状态由 `/api/torrents` 按需 `handle.status()` 拉取。
- 安全：magnet 必须含 `xt=urn:btih:`；http(s) .torrent 地址走 SSRF 护栏（拒绝私网/环回/链路本地/`.local`）；
  save_path 必须落在 DOWNLOAD_DIR 内，否则拒绝（防穿越）。

> 安装：桌面版目标环境（macOS 14+ 或 Linux/Windows，Python ≤3.13）执行
> `pip install "libtorrent==2.0.11"`（2.0.13 仅提供 macOS 14/15 的 wheel；2.0.11 额外提供 13_0 的 x86_64 wheel）。
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import sys
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:
    import libtorrent as lt
except Exception:  # pragma: no cover - 依赖缺失由 available() 处理
    lt = None  # type: ignore

# libtorrent 2.0 torrent_status.states 枚举顺序（用于把 int(state) 映射成可读名）
_STATE_NAMES = [
    "queued", "checking", "downloading", "seeding",
    "finished", "allocating", "checking_resume_data",
]
# 文件优先级：0 = 不下载（跳过），1..7 递增；普通下载用 4（libtorrent 默认普通优先级）
PRIORITY_SKIP = 0
PRIORITY_NORMAL = 4

# 完成时在媒体文件旁写侧车用的平台标识
PLATFORM_NAME = "torrent"


def available() -> bool:
    """libtorrent 或 aria2 任一可用即可（多后端自动选择）。"""
    return lt is not None or aria2_available()


# --------------------------------------------------------------------------- #
# 校验：URI / URL / save_path
# --------------------------------------------------------------------------- #
def _is_safe_url(url: str) -> bool:
    """SSRF 护栏：拒绝私网 / 环回 / 链路本地 / 保留网段 / .local/.internal 主机。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # 解析不到的域名不在此处拦截（交给 libtorrent 统一报错），但下列解析结果必须都是公网
        return True
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            return False
    return True


def validate_uri(uri: str) -> str:
    """校验并规范化种子来源 URI。

    返回规范化后的 URI；非法则抛 ValueError。
    - magnet:?xt=urn:btih:<hash>  → 抽取并重组为最小合法 magnet
    - http(s)://...torrent        → 走 SSRF 护栏，放行公网地址
    """
    uri = (uri or "").strip()
    if not uri:
        raise ValueError("种子链接不能为空")
    if uri.startswith("magnet:"):
        parsed = urlparse(uri)
        if "xt=urn:btih:" not in parsed.query:
            raise ValueError("magnet 链接缺少 xt=urn:btih: 哈希值")
        # 重组为最小 magnet（只保留 xt 与可用字段，去掉潜在注入的 trackers 副作用由 libtorrent 处理）
        from urllib.parse import parse_qs, urlencode, quote
        qs = parse_qs(parsed.query, keep_blank_values=False)
        keep = {k: v[0] for k, v in qs.items() if k in ("xt", "dn", "tr", "xs", "as") and v}
        # 校验 tr/xs/as 等 URL 字段，拒绝指向内网/本地地址的 tracker 或源（防 SSRF）
        for key in ("tr", "xs", "as"):
            val = keep.get(key, "")
            if not val:
                continue
            if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", val) and not _is_safe_url(val):
                raise ValueError(f"magnet 中的 {key} 指向非公开地址，已拒绝")
        # 重组时保留冒号与斜杠（标准 magnet 形如 xt=urn:btih:... / tr=http://...），
        # 仅编码空格等不安全字符。
        return "magnet:?" + urlencode(keep, quote_via=lambda s, *a, **_kw: quote(s, safe=":/"))
    if uri.startswith("http://") or uri.startswith("https://"):
        if not _is_safe_url(uri):
            raise ValueError("仅支持公开可访问的 .torrent 链接（不能指向内网/本地地址）")
        return uri
    raise ValueError("仅支持 magnet: 或 http(s):// 的 .torrent 链接")


def safe_save_path(download_dir: Path, save_path: Optional[str]) -> Optional[Path]:
    """把 save_path 收敛到 DOWNLOAD_DIR 内，返回绝对路径；越界返回 None。"""
    base = download_dir.resolve()
    if not save_path:
        return base
    cand = Path(save_path)
    if not cand.is_absolute():
        cand = (base / save_path).resolve()
    else:
        cand = cand.resolve()
    if cand == base or base in cand.parents:
        return cand
    return None


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #
class TorrentManager:
    def __init__(self, download_dir: Path) -> None:
        self.download_dir = Path(download_dir)
        self._session: Any = None
        self._lock = threading.RLock()
        self._handles: dict[str, Any] = {}       # id -> libtorrent handle
        self._meta: dict[str, dict] = {}          # id -> 元数据（uri/name/added_at/save_path）
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._started = False

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if not available() or self._started:
            return
        with self._lock:
            if self._session is None:
                self._session = self._new_session()
            self._started = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="vdl-torrent")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        with self._lock:
            if self._session is not None:
                try:
                    self._session.pause()
                except Exception:
                    pass
                self._session = None
            self._started = False

    @staticmethod
    def _new_session() -> Any:
        ses = lt.session()
        try:
            ses.listen_on(6881, 6891)
        except Exception:
            pass
        try:
            ses.apply_settings({
                "enable_upnp": False,
                "enable_natpmp": False,
                "enable_dht": True,
                "enable_lsd": True,
                "announce_to_all_tiers": True,
            })
        except Exception:
            pass
        return ses

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._session is not None:
                    self._session.post_torrent_updates()
                    for alert in self._session.pop_alerts():
                        self._on_alert(alert)
            except Exception:
                pass
            self._stop.wait(1.0)

    def _on_alert(self, alert: Any) -> None:
        try:
            if not hasattr(alert, "handle"):
                return
            h = alert.handle
            st = h.status()
            finished = bool(getattr(st, "is_finished", False))
            state = _state_name(st)
            if finished or state in ("finished", "seeding"):
                self._write_sidecar_for(h)
        except Exception:
            pass

    # ---------- 增删改查 ----------
    def add(self, *, uri: Optional[str] = None, torrent_data: Optional[bytes] = None,
            save_path: Optional[str] = None, name: Optional[str] = None,
            paused: bool = False, file_priorities: Optional[dict[int, int]] = None) -> dict:
        if not available():
            raise RuntimeError("libtorrent 未安装")
        if self._session is None:
            self.start()
        sp = safe_save_path(self.download_dir, save_path)
        if sp is None:
            raise ValueError("save_path 必须位于下载目录内")
        params: dict[str, Any] = {
            "save_path": str(sp),
            "storage_mode": lt.storage_mode_t.storage_mode_sparse,
            "paused": bool(paused),
        }
        if uri:
            params["url"] = validate_uri(uri)
            src_label = params["url"]
        elif torrent_data:
            ti = lt.torrent_info(lt.bdecode(torrent_data))
            # 安全：恶意 .torrent 可能用 `../` 或绝对路径把文件写到 save_path 之外，
            # libtorrent 不会自动清洗内部文件路径，必须在此逐一拦截。
            for i in range(ti.num_files()):
                rel = ti.files().file_path(i)
                cand = (sp / rel).resolve()
                if not (cand == sp or sp in cand.parents):
                    raise ValueError(f"torrent 含非法文件路径（疑似目录穿越）：{rel}")
            params["ti"] = ti
            src_label = ti.name() or "torrent"
        else:
            raise ValueError("需提供 magnet/uri 或 torrent 文件")
        try:
            h = self._session.add_torrent(params)
        except Exception as e:
            raise RuntimeError(f"添加种子失败：{e}") from e
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self._handles[tid] = h
            self._meta[tid] = {
                "uri": src_label,
                "name": name or "",
                "added_at": time.time(),
                "save_path": str(sp),
            }
        if file_priorities and h.has_metadata():
            self._apply_file_priorities(h, file_priorities)
        return self.describe(tid, h)

    def get(self, tid: str) -> Optional[dict]:
        with self._lock:
            h = self._handles.get(tid)
        if h is None:
            return None
        try:
            return self.describe(tid, h)
        except Exception:
            return None

    def list(self) -> list[dict]:
        with self._lock:
            items = list(self._handles.items())
        out: list[dict] = []
        for tid, h in items:
            try:
                out.append(self.describe(tid, h))
            except Exception:
                continue
        out.sort(key=lambda x: x.get("added_at", 0), reverse=True)
        return out

    def pause(self, tid: str) -> bool:
        with self._lock:
            h = self._handles.get(tid)
        if h is None:
            return False
        try:
            h.pause()
        except Exception:
            return False
        return True

    def resume(self, tid: str) -> bool:
        with self._lock:
            h = self._handles.get(tid)
        if h is None:
            return False
        try:
            h.resume()
        except Exception:
            return False
        return True

    def remove(self, tid: str, delete_files: bool = False) -> bool:
        with self._lock:
            h = self._handles.get(tid)
            if h is None:
                return False
            del self._handles[tid]
            self._meta.pop(tid, None)
        try:
            opts = 0
            if delete_files and hasattr(lt, "options_t"):
                opts = int(getattr(lt.options_t, "delete_files", 0))
            self._session.remove_torrent(h, opts)
        except Exception:
            pass  # 即使 remove 抛错，内存里已摘除
        return True

    def set_file_priorities(self, tid: str, priorities: dict[int, int]) -> bool:
        with self._lock:
            h = self._handles.get(tid)
        if h is None:
            return False
        if not h.has_metadata():
            return False
        self._apply_file_priorities(h, priorities)
        return True

    @staticmethod
    def _apply_file_priorities(h: Any, priorities: dict[int, int]) -> None:
        ti = h.torrent_file()
        n = ti.num_files()
        for idx, prio in priorities.items():
            if 0 <= idx < n:
                try:
                    h.set_file_priority(idx, int(prio))
                except Exception:
                    continue

    # ---------- 状态描述 ----------
    def describe(self, tid: str, h: Any) -> dict:
        st = h.status()
        ti = h.torrent_file() if h.has_metadata() else None
        meta = self._meta.get(tid, {})
        name = meta.get("name") or ""
        if not name and ti is not None:
            name = ti.name()
        if not name:
            name = getattr(st, "name", "") or (meta.get("uri") or "")[:24]
        total = int(getattr(st, "total_wanted", 0) or 0)
        done = int(getattr(st, "total_wanted_done", 0) or 0)
        if total > 0:
            progress = round(done / total, 4)
        else:
            progress = 1.0 if bool(getattr(st, "is_finished", False)) else 0.0
        try:
            eta = int(st.eta())
        except Exception:
            eta = 0
        files: list[dict] = []
        if ti is not None:
            fp = []
            try:
                fp = h.file_progress()
            except Exception:
                fp = []
            fs = ti.files()
            for i in range(ti.num_files()):
                sz = int(fs.file_size(i))
                d = int(fp[i]) if i < len(fp) else 0
                prio = _safe_file_priority(h, i)
                files.append({
                    "index": i,
                    "path": fs.file_path(i),
                    "name": fs.file_name(i),
                    "size": sz,
                    "downloaded": d,
                    "priority": prio,
                    "skipped": prio == PRIORITY_SKIP,
                    "progress": round(d / sz, 4) if sz > 0 else 1.0,
                })
        err = _safe_error(st)
        return {
            "id": tid,
            "name": name,
            "info_hash": _info_hash(h),
            "state": _state_name(st),
            "progress": progress,
            "size": total,
            "downloaded": done,
            "download_speed": int(getattr(st, "download_rate", 0) or 0),
            "upload_speed": int(getattr(st, "upload_rate", 0) or 0),
            "peers": int(getattr(st, "num_peers", 0) or 0),
            "seeds": int(getattr(st, "num_seeds", 0) or 0),
            "eta": eta,
            "paused": bool(getattr(st, "paused", False)),
            "has_metadata": bool(h.has_metadata()),
            "save_path": str(getattr(st, "save_path", meta.get("save_path", ""))),
            "added_at": int(meta.get("added_at", 0)),
            "error": err,
            "files": files,
            "uri": meta.get("uri", ""),
        }

    # ---------- 完成侧车 ----------
    def _write_sidecar_for(self, h: Any) -> None:
        try:
            if not h.has_metadata():
                return
            ti = h.torrent_file()
            fs = ti.files()
            root = Path(getattr(h.status(), "save_path", str(self.download_dir)))
            uri = ""
            # 反查该 handle 对应的 uri（无严格必要，失败忽略）
            with self._lock:
                for th, th_h in self._handles.items():
                    if th_h is h:
                        uri = self._meta.get(th, {}).get("uri", "")
                        break
            for i in range(ti.num_files()):
                fp = root / fs.file_path(i)
                suf = fp.suffix.lower()
                from library import MEDIA_EXTS  # 延迟导入，避免循环依赖风险
                if suf not in MEDIA_EXTS:
                    continue
                if not fp.exists():
                    continue
                sidecar = fp.with_name(fp.stem + ".vdlmeta.json")
                try:
                    sidecar.write_text(
                        json.dumps({
                            "title": fp.stem,
                            "platform": PLATFORM_NAME,
                            "uploader": "",
                            "duration": 0,
                            "source_url": uri,
                            "thumbnail": "",
                            "completed_at": int(time.time()),
                        }, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception:
                    continue
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 小工具（与 libtorrent 解耦，便于在单元测试/mock 下复用）
# --------------------------------------------------------------------------- #
def _state_name(st: Any) -> str:
    try:
        idx = int(st.state)
        if 0 <= idx < len(_STATE_NAMES):
            return _STATE_NAMES[idx]
    except Exception:
        pass
    return "unknown"


def _safe_file_priority(h: Any, i: int) -> int:
    try:
        return int(h.file_priority(i))
    except Exception:
        return PRIORITY_NORMAL


def _safe_error(st: Any) -> str:
    try:
        ec = getattr(st, "error", None)
        if ec is None:
            return ""
        val = getattr(ec, "value", None)
        if val is not None and int(val) == 0:
            return ""
        s = str(ec)
        return s if s else ""
    except Exception:
        return ""


def _info_hash(h: Any) -> str:
    try:
        return str(h.info_hash())
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 多后端自动选择：aria2 兜底（libtorrent 缺失时，用系统 aria2c 的 JSON-RPC 驱动）
# --------------------------------------------------------------------------- #
_ARIA2_PORT = 16800
_ARIA2_RPC = f"http://127.0.0.1:{_ARIA2_PORT}/jsonrpc"


def _find_aria2c() -> Optional[str]:
    """定位 aria2c 二进制：打包态优先用随包自包含（<Resources>/bin/aria2c）；否则本机 brew 绝对路径 / PATH。

    打包后二进制与依赖 dylib 平铺在 Resources/bin/，依赖用 @loader_path 自包含，
    安装到任何同架构 mac 都能脱离本机 Homebrew 运行（见 desktop/bundle_aria2.py）。
    """
    # 1) 打包态：随包二进制在 <Resources>/bin/aria2c[.exe]
    exe = "aria2c.exe" if sys.platform == "win32" else "aria2c"
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(Path(meipass) / "bin" / exe)
    # 开发态 __file__ 在 server/，parent.parent = Resources
    candidates.append(Path(__file__).resolve().parent.parent / "bin" / exe)
    for c in candidates:
        if c.exists():
            return str(c)
    # 2) 系统安装（开发态 / 本机）
    if sys.platform == "win32":
        # 手动在 PATH 各目录查找，避免依赖仅 Windows 有的 _winapi 模块
        name = "aria2c.exe"
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if not d:
                continue
            cand = os.path.join(d, name)
            if os.path.exists(cand):
                return cand
        for cand in (r"C:\Program Files\aria2\aria2c.exe",
                     r"C:\aria2\aria2c.exe"):
            if os.path.exists(cand):
                return cand
        return None
    if os.path.exists("/opt/homebrew/bin/aria2c"):
        return "/opt/homebrew/bin/aria2c"
    return shutil.which("aria2c")


def aria2_available() -> bool:
    """系统是否可用 aria2c（支持磁力/种子下载）。"""
    return bool(_find_aria2c())


# ---- 最小 bencode 解码，仅用于校验 .torrent 内部文件路径（防目录穿越） ----
def _bdecode(data: bytes, pos: int = 0):
    c = data[pos:pos + 1]
    if c == b"d":
        d: dict = {}
        pos += 1
        while data[pos:pos + 1] != b"e":
            k, pos = _bdecode(data, pos)
            v, pos = _bdecode(data, pos)
            d[k] = v
        return d, pos + 1
    if c == b"l":
        lst: list = []
        pos += 1
        while data[pos:pos + 1] != b"e":
            v, pos = _bdecode(data, pos)
            lst.append(v)
        return lst, pos + 1
    if c == b"i":
        end = data.index(b"e", pos)
        return int(data[pos + 1:end]), end + 1
    # 字符串：<长度>:<内容>
    colon = data.index(b":", pos)
    length = int(data[pos:colon])
    s = colon + 1
    return data[s:s + length], s + length


def _torrent_inner_paths(data: bytes) -> list[str]:
    """返回 torrent 内部所有文件路径（相对），并拒绝目录穿越。"""
    info = _bdecode(data)[0].get(b"info", {})
    out: list[str] = []
    if b"files" in info:  # 多文件
        for f in info[b"files"]:
            parts = [p.decode("utf-8", "replace") for p in f.get(b"path", [])]
            rel = "/".join(parts)
            out.append(rel)
    else:  # 单文件
        out.append(info.get(b"name", b"").decode("utf-8", "replace"))
    for rel in out:
        cand = (Path("/") / rel).resolve()
        if ".." in rel.split("/") or rel.startswith("/") or cand.root == "/":
            # 归一化后再判越界
            if rel.startswith("/") or any(p == ".." for p in rel.split("/")):
                raise ValueError(f"torrent 含非法文件路径（疑似目录穿越）：{rel}")
    return out


class Aria2TorrentManager:
    """用 aria2c 的 JSON-RPC 实现的种子管理器，接口与 TorrentManager 对齐。"""

    def __init__(self, download_dir: Path) -> None:
        self.download_dir = Path(download_dir)
        self._bin = _find_aria2c()
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._gid_to_tid: dict[str, str] = {}
        self._tid_to_gid: dict[str, str] = {}
        self._meta: dict[str, dict] = {}
        self._sidecarred: set[str] = set()
        self._started = False

    # ---------- RPC ----------
    def _rpc(self, method: str, params: list) -> Any:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": "vdl", "method": method, "params": params}
        ).encode()
        req = urllib.request.Request(
            _ARIA2_RPC, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        last: Optional[Exception] = None
        for _ in range(20):  # 等待 aria2c RPC 就绪
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode())
                if "error" in body:
                    raise RuntimeError(body["error"].get("message", "aria2 rpc error"))
                return body.get("result")
            except urllib.error.URLError as e:
                last = e
                time.sleep(0.3)
        raise RuntimeError(f"无法连接 aria2c（{last}）")

    def _start_daemon(self) -> None:
        if self._bin is None:
            raise RuntimeError("未找到 aria2c")
        args = [
            self._bin, "--enable-rpc", f"--rpc-listen-port={_ARIA2_PORT}",
            "--rpc-listen-all=false", "--rpc-allow-origin-all",
            f"--dir={self.download_dir}", "--continue=true", "--seed-time=0",
            "--bt-enable-lpd=true", "--enable-dht=true", "--bt-seed-unverified=true",
            "--max-concurrent-downloads=5", "--bt-max-peers=50",
        ]
        self._proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._started or self._bin is None:
            return
        self._start_daemon()
        self._started = True

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        self._started = False

    # ---------- 增删改查 ----------
    def add(self, *, uri: Optional[str] = None, torrent_data: Optional[bytes] = None,
            save_path: Optional[str] = None, name: Optional[str] = None,
            paused: bool = False, file_priorities: Optional[dict[int, int]] = None) -> dict:
        if self._bin is None:
            raise RuntimeError("未找到 aria2c")
        if not self._started:
            self.start()
        options: dict[str, Any] = {"dir": str(self.download_dir)}
        if file_priorities:
            selected = [str(i) for i, p in file_priorities.items() if int(p) > 0]
            options["select-file"] = ",".join(selected) if selected else "0"
        if save_path:
            sp = safe_save_path(self.download_dir, save_path)
            if sp is None:
                raise ValueError("save_path 必须位于下载目录内")
            options["dir"] = str(sp)
        if paused:
            options["pause"] = "true"
        if uri:
            uri = validate_uri(uri)
            src_label = uri
            gid = self._rpc("aria2.addUri", [[uri], options])
        elif torrent_data:
            _torrent_inner_paths(torrent_data)  # 校验内部路径
            b64 = base64.b64encode(torrent_data).decode()
            src_label = "torrent"
            gid = self._rpc("aria2.addTorrent", [b64, [], options])
        else:
            raise ValueError("需提供 magnet/uri 或 torrent 文件")
        tid = uuid.uuid4().hex[:12]
        with self._lock:
            self._gid_to_tid[gid] = tid
            self._tid_to_gid[tid] = gid
            self._meta[tid] = {
                "uri": src_label, "name": name or "",
                "added_at": time.time(), "save_path": options["dir"],
            }
        return self._describe_from_status(tid, self._rpc("aria2.tellStatus", [gid, []]))

    def get(self, tid: str) -> Optional[dict]:
        gid = self._tid_to_gid.get(tid)
        if gid is None:
            return None
        try:
            st = self._rpc("aria2.tellStatus", [gid, []])
        except Exception:
            return None
        return self._describe_from_status(tid, st)

    def list(self) -> list[dict]:
        out: list[dict] = []
        for method, params in (
            ("aria2.tellActive", []),
            ("aria2.tellWaiting", [0, 1000]),
            ("aria2.tellStopped", [0, 1000]),
        ):
            try:
                items = self._rpc(method, params) or []
            except Exception:
                continue
            for st in items:
                gid = st.get("gid") if isinstance(st, dict) else st
                tid = self._gid_to_tid.get(gid)
                if not tid:
                    continue
                try:
                    full = self._rpc("aria2.tellStatus", [gid, []])
                except Exception:
                    continue
                d = self._describe_from_status(tid, full)
                if d:
                    out.append(d)
        out.sort(key=lambda x: x.get("added_at", 0), reverse=True)
        return out

    def pause(self, tid: str) -> bool:
        gid = self._tid_to_gid.get(tid)
        if gid is None:
            return False
        try:
            self._rpc("aria2.pause", [gid])
        except Exception:
            pass  # 已暂停等错误按幂等处理
        return True

    def resume(self, tid: str) -> bool:
        gid = self._tid_to_gid.get(tid)
        if gid is None:
            return False
        try:
            self._rpc("aria2.unpause", [gid])
        except Exception:
            pass
        return True

    def remove(self, tid: str, delete_files: bool = False) -> bool:
        gid = self._tid_to_gid.get(tid)
        if gid is None:
            return False
        with self._lock:
            self._gid_to_tid.pop(gid, None)
            self._tid_to_gid.pop(tid, None)
            self._meta.pop(tid, None)
        try:
            self._rpc("aria2.forceRemove", [gid])
        except Exception:
            try:
                self._rpc("aria2.remove", [gid])
            except Exception:
                pass
        return True

    def set_file_priorities(self, tid: str, priorities: dict[int, int]) -> bool:
        gid = self._tid_to_gid.get(tid)
        if gid is None:
            return False
        selected = [str(i) for i, p in priorities.items() if int(p) > 0]
        sel = ",".join(selected) if selected else "0"
        try:
            self._rpc("aria2.changeOption", [gid, {"select-file": sel}])
            # aria2 改选文件需暂停→恢复才生效
            try:
                self._rpc("aria2.forcePause", [gid])
                self._rpc("aria2.unpause", [gid])
            except Exception:
                pass
        except Exception:
            return False
        return True

    # ---------- 状态描述 ----------
    @staticmethod
    def _extract_info_hash(uri: str) -> str:
        m = re.search(r"xt=urn:btih:([A-Za-z0-9]+)", uri or "")
        return m.group(1) if m else ""

    def _describe_from_status(self, tid: str, st: dict) -> dict:
        meta = self._meta.get(tid, {})
        bti = st.get("bittorrent") or {}
        info = bti.get("info") or {}
        name = meta.get("name") or info.get("name") or ""
        files_raw = st.get("files") or []
        if not name and files_raw:
            name = os.path.basename(files_raw[0].get("path", ""))
        if not name:
            name = (meta.get("uri") or "")[:24]
        total = int(st.get("totalLength") or 0)
        done = int(st.get("completedLength") or 0)
        progress = (round(done / total, 4) if total > 0
                    else (1.0 if st.get("status") == "complete" else 0.0))
        state = {
            "active": "downloading", "waiting": "queued", "paused": "paused",
            "error": "error", "complete": "finished", "removed": "removed",
        }.get(st.get("status"), "unknown")
        files: list[dict] = []
        for i, f in enumerate(files_raw):
            fsz = int(f.get("length") or 0)
            fdone = int(f.get("completedLength") or 0)
            fpath = f.get("path", "")
            selected = f.get("selected", True)
            prio = PRIORITY_NORMAL if selected else PRIORITY_SKIP
            files.append({
                "index": i, "path": fpath, "name": os.path.basename(fpath),
                "size": fsz, "downloaded": fdone, "priority": prio,
                "skipped": not selected,
                "progress": round(fdone / fsz, 4) if fsz > 0 else 1.0,
            })
        err = st.get("errorMessage") or ""
        if not err and st.get("errorCode"):
            err = f"错误码 {st.get('errorCode')}"
        has_meta = bool(bti) or bool(files_raw)
        if state in ("finished", "seeding", "complete") and tid not in self._sidecarred:
            self._write_sidecar(tid, st, name)
        return {
            "id": tid,
            "name": name,
            "info_hash": self._extract_info_hash(meta.get("uri", "")),
            "state": state,
            "progress": progress,
            "size": total,
            "downloaded": done,
            "download_speed": int(st.get("downloadSpeed") or 0),
            "upload_speed": int(st.get("uploadSpeed") or 0),
            "peers": int(st.get("connections") or 0),
            "seeds": 0,
            "eta": int(st.get("eta") or 0),
            "paused": state == "paused",
            "has_metadata": has_meta,
            "save_path": st.get("dir") or meta.get("save_path", ""),
            "added_at": int(meta.get("added_at", 0)),
            "error": err,
            "files": files,
            "uri": meta.get("uri", ""),
        }

    # ---------- 完成侧车 ----------
    def _write_sidecar(self, tid: str, st: dict, name: str) -> None:
        try:
            root = Path(st.get("dir") or str(self.download_dir))
            uri = self._meta.get(tid, {}).get("uri", "")
            for f in (st.get("files") or []):
                fp = Path(f.get("path", ""))
                if not fp.exists():
                    continue
                suf = fp.suffix.lower()
                try:
                    from library import MEDIA_EXTS  # 延迟导入，避免循环依赖
                except Exception:
                    MEDIA_EXTS = ()
                if suf not in MEDIA_EXTS:
                    continue
                sidecar = fp.with_name(fp.stem + ".vdlmeta.json")
                try:
                    sidecar.write_text(
                        json.dumps({
                            "title": fp.stem, "platform": PLATFORM_NAME,
                            "uploader": "", "duration": 0,
                            "source_url": uri, "thumbnail": "",
                            "completed_at": int(time.time()),
                        }, ensure_ascii=False),
                        encoding="utf-8",
                    )
                except Exception:
                    continue
            self._sidecarred.add(tid)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 管理器工厂（多后端自动选择）
# --------------------------------------------------------------------------- #
_manager_singleton: Optional[Any] = None


def get_manager(download_dir: Path) -> Optional[Any]:
    """返回可用的种子管理器：优先 libtorrent，否则 aria2；都不可用返回 None。"""
    global _manager_singleton
    if _manager_singleton is not None:
        return _manager_singleton
    if lt is not None:
        _manager_singleton = TorrentManager(download_dir)
    elif aria2_available():
        _manager_singleton = Aria2TorrentManager(download_dir)
    else:
        _manager_singleton = None
    return _manager_singleton

