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

import os
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import requests


def _resolve_ytdlp_dir() -> Path:
    """优先用 Railway 持久卷（部署重启不丢），否则回退本机 home（桌面端）。

    Railway ephemeral 容器每次部署重建，home 下的文件会清空；而自动更新的
    yt-dlp 若随部署丢失就白更新了。持久卷（RAILWAY_VOLUME_MOUNT_PATH）与
    cookie 池共用，重启后 bootstrap 仍能拿到最新解析器。
    """
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if mount:
        try:
            p = Path(mount) / "yt_dlp"
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            pass
    return Path.home() / ".videodownloader" / "yt_dlp"


VDL_YTDLP_DIR = _resolve_ytdlp_dir()
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


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """解压并防 Zip Slip：任何解析后落在 dest 之外的成员一律拒绝（跨 Python 版本稳健）。"""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    for name in zf.namelist():
        target = (dest / name).resolve()
        if target != dest and dest not in target.parents:
            raise RuntimeError(f"wheel 含越界路径，拒绝解压：{name}")
    zf.extractall(dest)


def update() -> dict:
    """下载并解压最新 yt-dlp 到本机目录，返回结果（需重启生效）。

    安全加固：
    - 校验 PyPI 公布的 sha256（防投毒 / 中间人篡改）；
    - 解压前逐成员校验路径（防 Zip Slip）；
    - 先解到临时目录，校验通过再原子替换，避免半解压导致下次启动崩溃。
    """
    ver = latest_version()
    if not ver:
        return {"ok": False, "error": "无法获取最新版本（请检查网络）"}
    # 已是最新（本机缓存标记一致）
    if _VERSION_FILE.exists() and _VERSION_FILE.read_text().strip() == ver:
        return {"ok": True, "updated": False, "version": ver, "restart_required": False}
    try:
        import shutil
        meta = requests.get(_PYPI_JSON, timeout=_PYPI_TIMEOUT).json()
        urls = meta.get("urls", [])
        wheel = next((u for u in urls if u.get("filename", "").endswith(".whl")), None)
        if not wheel:
            return {"ok": False, "error": "未找到可用的 yt-dlp wheel"}
        # 供应链校验：核对 PyPI 公布的 sha256，防投毒 / 中间人
        expected_sha = (wheel.get("digests") or {}).get("sha256")
        VDL_YTDLP_DIR.mkdir(parents=True, exist_ok=True)
        dl = requests.get(wheel["url"], timeout=180, stream=True)
        dl.raise_for_status()
        raw = dl.content
        if expected_sha:
            import hashlib
            if hashlib.sha256(raw).hexdigest() != expected_sha:
                return {"ok": False, "error": "校验和不匹配，疑似下载被篡改，已终止更新"}
        # 先解压到临时目录，成功后再原子替换，避免半解压导致下次启动崩溃
        tmp = VDL_YTDLP_DIR.with_name("yt_dlp_tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        _safe_extract(zipfile.ZipFile(BytesIO(raw)), tmp)
        old = VDL_YTDLP_DIR.with_name("yt_dlp_old")
        if VDL_YTDLP_DIR.exists():
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
            VDL_YTDLP_DIR.rename(old)
        tmp.rename(VDL_YTDLP_DIR)
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        _VERSION_FILE.write_text(ver)
        return {"ok": True, "updated": True, "version": ver, "restart_required": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"下载失败：{exc}"}
