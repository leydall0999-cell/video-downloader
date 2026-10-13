#!/usr/bin/env python3
"""更新分发服务（8765）—— 静态分发 + App 内更新下载计数（2026-10-13 接续待办 #5）。

背景：App 内自动更新从 8765 直链下载 `latest.json` / 全量包（`VideoDownloader.app.zip`）
/ 增量补丁（`patch.delta`）。官网下载已由 web 服务计数（`installer` / `extension`
两个 bucket），但 **App 内更新下载此前不计入任何统计**。本服务在分发的同时，把包/补丁
的 GET 计入下载统计文件，bucket = `app_update` —— 与官网下载**分开计口径**。

统计文件与 web 侧 `dl_stats.py` **同一份**（`/opt/vdl-dl-stats/downloads.json`），
形状完全一致，授权中心 `stat_query` 已在读该文件、天然合并，无需改中心：

    {"installer": {"macos": {"total": n, "by_day": {"2026-10-13": n}}},
     "extension": {...},
     "app_update": {"macos": {...}}, "updated_at": ts}

只计**真实的 App 更新下载**：请求 UA 以 `VDL-Update/` 开头（App 的 `_http_download`
固定带 `User-Agent: VDL-Update/1.0`）。浏览器手动打开 8765 下载不算 app_update。

特性：GET/HEAD（下载管理器预检）、禁目录列表、防路径穿越、缺失文件 404；
**计数失败绝不影响分发**（统计是锦上添花）。

运行：
    VDL_UPDATE_DIR=/opt/vdl-update VDL_DL_STATS_PATH=/opt/vdl-dl-stats/downloads.json \
        python3 deploy/update_server.py --port 8765
systemd 示例见文件末尾。
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

UPDATE_DIR = Path(os.environ.get("VDL_UPDATE_DIR") or "/opt/vdl-update")
STATS_PATH = Path(os.environ.get("VDL_DL_STATS_PATH") or "/opt/vdl-dl-stats/downloads.json")
APP_UA_PREFIX = "VDL-Update/"          # App 更新下载的固定 UA（routers/system.py::_http_download）
_LOCK = threading.Lock()

# 计入下载的扩展名 → 平台（App 目前仅 macOS；Windows 包就绪后在此加映射）
_COUNTABLE_SUFFIXES: dict[str, str] = {".zip": "macos", ".delta": "macos"}


# ---------- 纯函数（可离线测试）----------

def countable(path_name: str) -> tuple[str, str] | None:
    """该文件名是否计入 App 更新下载统计；是则返回 (bucket, platform)。

    只计**包/补丁**（.zip/.delta）；latest.json 与 .sig 是元数据，不算下载。
    """
    suffix = Path(str(path_name or "")).suffix.lower()
    plat = _COUNTABLE_SUFFIXES.get(suffix)
    if plat is None:
        return None
    return ("app_update", plat)


def is_app_download(ua: str) -> bool:
    """请求是否来自 App 内自动更新（UA 固定 `VDL-Update/...`）。"""
    return str(ua or "").strip().startswith(APP_UA_PREFIX)


def safe_rel_path(raw_path: str) -> str | None:
    """把 URL 路径归一化为「更新目录内的相对路径」；越界/非法返回 None。

    ⚠️ 必须在归一化**之前**就拒掉 `..` 段：`posixpath.normpath` 会把根之上的 `..`
    直接丢掉（`/../a` → `/a`），虽然结果没逃出目录，但语义含混 —— 显式拒绝更清楚，
    也是防穿越的第一道闸（第二道是 `_resolve` 的 `relative_to` 复检）。
    """
    p = unquote(urlparse(str(raw_path or "")).path or "")
    p = p.replace("\\", "/")                       # Windows 分隔符一并当路径分隔
    parts = [seg for seg in p.split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        return None
    return "/".join(parts) or None


# ---------- 计数（与 web 侧 dl_stats 同形状）----------

def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _load() -> dict:
    try:
        data = json.loads(STATS_PATH.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATS_PATH)


def record(bucket: str, platform: str, n: int = 1) -> dict:
    """记一次下载。失败静默（返回当前读数），绝不让计数影响分发。"""
    try:
        with _LOCK:
            data = _load()
            slots = data.setdefault(bucket, {})
            slot = slots.setdefault(platform, {"total": 0, "by_day": {}})
            slot["total"] = int(slot.get("total") or 0) + int(n)
            by_day = slot.setdefault("by_day", {})
            day = _today()
            by_day[day] = int(by_day.get(day) or 0) + int(n)
            if len(by_day) > 90:                       # 只留最近 90 天（与 web 侧一致）
                for k in sorted(by_day)[:-90]:
                    by_day.pop(k, None)
            data["updated_at"] = time.time()
            _save(data)
            return data
    except Exception:  # noqa: BLE001
        return _load()


# ---------- HTTP ----------

class _Handler(BaseHTTPRequestHandler):
    server_version = "VDL-Update/1.0"

    def log_message(self, fmt, *args):  # 静音默认 access log（systemd 自带 journal）
        pass

    def _resolve(self) -> Path | None:
        rel = safe_rel_path(self.path)
        if rel is None:
            # 只有「根 / 空路径」才回退首页（保留原 `python -m http.server` 的 index.html
            # 行为）；含 `..` 的越界路径一律 404，绝不回退首页。
            raw = unquote(urlparse(str(self.path or "")).path or "").replace("\\", "/").strip("/")
            if raw:
                return None
            rel = "index.html"
        target = (UPDATE_DIR / rel).resolve()
        try:
            target.relative_to(UPDATE_DIR.resolve())
        except ValueError:
            return None                                  # 防路径穿越
        return target if target.is_file() else None

    def _serve(self, with_body: bool) -> None:
        target = self._resolve()
        if target is None:
            self.send_error(404, "Not Found")
            return
        try:
            size = target.stat().st_size
        except OSError:
            self.send_error(404, "Not Found")
            return
        # 计数：仅 GET（HEAD 是预检）+ 仅 App 更新 UA + 仅包/补丁
        if with_body and is_app_download(self.headers.get("User-Agent", "")):
            hit = countable(target.name)
            if hit is not None:
                record(hit[0], hit[1])
        self.send_response(200)
        self.send_header("Content-Type", self._ctype(target.name))
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not with_body:
            return
        with open(target, "rb") as f:
            while True:
                chunk = f.read(1024 * 256)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return                                # 客户端断开，正常

    @staticmethod
    def _ctype(name: str) -> str:
        low = name.lower()
        if low.endswith(".json"):
            return "application/json; charset=utf-8"
        if low.endswith(".zip"):
            return "application/zip"
        return "application/octet-stream"

    def do_GET(self):       # noqa: N802
        self._serve(with_body=True)

    def do_HEAD(self):      # noqa: N802
        self._serve(with_body=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="VDL 更新分发服务（8765）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("VDL_UPDATE_PORT") or 8765))
    ap.add_argument("--host", default=os.environ.get("VDL_UPDATE_HOST") or "0.0.0.0")
    args = ap.parse_args()
    if not UPDATE_DIR.is_dir():
        print(f"[update_server] 更新目录不存在：{UPDATE_DIR}", flush=True)
        return 1
    srv = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(f"[update_server] 分发 {UPDATE_DIR} @ {args.host}:{args.port}；"
          f"计数 → {STATS_PATH}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

# ── systemd 示例（/etc/systemd/system/vdl-update.service）─────────────────────
# [Unit]
# Description=VDL update distribution + download counting
# After=network.target
# [Service]
# Environment=VDL_UPDATE_DIR=/opt/vdl-update
# Environment=VDL_DL_STATS_PATH=/opt/vdl-dl-stats/downloads.json
# ExecStart=/usr/bin/python3 /opt/vdl-update/update_server.py --port 8765
# Restart=always
# User=root
# [Install]
# WantedBy=multi-user.target
