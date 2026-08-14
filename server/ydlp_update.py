"""yt-dlp 解析器自动更新。

把最新版 yt-dlp 下载到本机用户目录（~/.videodownloader/yt_dlp），下次启动前插入
sys.path 最前优先使用，从而在不重新打包的前提下获得最新站点解析器——新站点 / 改版
站点也能解析，软件「越用越能下」。

合规边界：
- 仅更新公开的站点解析器（yt-dlp 官方 wheel），不涉及任何账号凭证、不共享用户数据。
- 下载内容来自 PyPI 官方，仅替换解析逻辑，不动登录态 / Cookie。
- 生效需重启（已加载的旧版 yt-dlp 无法热替换），前端会提示「重启后生效」。
"""
from __future__ import annotations

import sys
import zipfile
from io import BytesIO
from pathlib import Path

import requests

VDL_YTDLP_DIR = Path.home() / ".videodownloader" / "yt_dlp"
_VERSION_FILE = VDL_YTDLP_DIR / "version.txt"
_PYPI_JSON = "https://pypi.org/pypi/yt-dlp/json"
_PYPI_TIMEOUT = 20


def bootstrap() -> None:
    """必须在 import yt_dlp 之前调用：若本机已存有新版 yt-dlp，插入 sys.path 最前优先使用。"""
    pkg = VDL_YTDLP_DIR / "yt_dlp"
    if pkg.is_dir() and (pkg / "__init__.py").exists():
        sys.path.insert(0, str(VDL_YTDLP_DIR))


def current_version() -> str:
    try:
        from importlib.metadata import version
        return version("yt-dlp")
    except Exception:
        return "未知"


def latest_version() -> str | None:
    try:
        r = requests.get(_PYPI_JSON, timeout=_PYPI_TIMEOUT)
        r.raise_for_status()
        return r.json()["info"]["version"]
    except Exception:
        return None


def update() -> dict:
    """下载并解压最新 yt-dlp 到本机目录，返回结果（需重启生效）。"""
    ver = latest_version()
    if not ver:
        return {"ok": False, "error": "无法获取最新版本（请检查网络）"}
    # 已是最新（本机缓存标记一致）
    if _VERSION_FILE.exists() and _VERSION_FILE.read_text().strip() == ver:
        return {"ok": True, "updated": False, "version": ver, "restart_required": False}
    try:
        meta = requests.get(_PYPI_JSON, timeout=_PYPI_TIMEOUT).json()
        urls = meta.get("urls", [])
        wheel = next((u for u in urls if u.get("filename", "").endswith(".whl")), None)
        if not wheel:
            return {"ok": False, "error": "未找到可用的 yt-dlp wheel"}
        VDL_YTDLP_DIR.mkdir(parents=True, exist_ok=True)
        # 清掉旧版，避免残留文件干扰
        old = VDL_YTDLP_DIR / "yt_dlp"
        if old.exists():
            import shutil
            shutil.rmtree(old, ignore_errors=True)
        dl = requests.get(wheel["url"], timeout=180, stream=True)
        dl.raise_for_status()
        z = zipfile.ZipFile(BytesIO(dl.content))
        z.extractall(VDL_YTDLP_DIR)
        _VERSION_FILE.write_text(ver)
        return {"ok": True, "updated": True, "version": ver, "restart_required": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"下载失败：{exc}"}
