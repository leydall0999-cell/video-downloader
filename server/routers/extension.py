"""浏览器扩展安装包下载（2026-09-28）。

把「视频工坊 媒体嗅探」Chrome MV3 扩展打包成 zip，通过下载模块对外提供：
    GET /api/extension/info      → 扩展元信息 + 安装步骤
    GET /api/extension/package   → 下载 .zip 安装包（Content-Disposition: attachment）

源码优先级（保证开发态最新、冻结包/网页版也能用）：
    1) 仓库根 <repo>/extension/        —— 开发态，永远最新
    2) <this>/assets/extension_src/    —— 冻结包 / 网页版兜底（已随包分发）

不依赖 app / 不触碰磁盘删除，可在沙盒单测中直接 import。
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

router = APIRouter()

_HERE = Path(__file__).resolve().parent            # server/routers
_SERVER = _HERE.parent                            # server
_REPO_EXT = _SERVER.parent / "extension"          # <repo>/extension
_BUNDLED = _SERVER / "assets" / "extension_src"   # 冻结/网页版兜底

_PKG_NAME = "视频工坊媒体嗅探"


def _source_dir() -> Path:
    if _REPO_EXT.is_dir():
        return _REPO_EXT
    if _BUNDLED.is_dir():
        return _BUNDLED
    raise FileNotFoundError("找不到扩展源码目录（extension/ 与 assets/extension_src/ 均不存在）")


def _manifest() -> dict:
    return json.loads((_source_dir() / "manifest.json").read_text(encoding="utf-8"))


def _build_zip(src: Path) -> bytes:
    """把扩展目录打成 zip（排除测试与 .DS_Store），返回内存字节。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(src)
            if rel.parts[0] == "tests":
                continue
            if p.name == ".DS_Store":
                continue
            z.writestr(str(rel), p.read_bytes())
    return buf.getvalue()


@router.get("/api/extension/info")
def extension_info() -> dict:
    m = _manifest()
    return {
        "name": m.get("name", _PKG_NAME),
        "version": m.get("version", "1.0.0"),
        "manifest_version": m.get("manifest_version", 3),
        "download_url": "/api/extension/package",
        "min_browser": "Chrome / Edge / Brave（支持 Manifest V3）",
        "install_steps": [
            "下载并解压扩展安装包",
            "浏览器地址栏打开 chrome://extensions（Edge 为 edge://extensions）",
            "右上角打开「开发者模式」开关",
            "点击「加载已解压的扩展程序」，选择解压出来的文件夹",
            "点击工具栏扩展图标，即可嗅探网页媒体并一键发回桌面端下载",
        ],
    }


@router.get("/api/extension/package")
def extension_package(request: Request = None) -> Response:
    src = _source_dir()
    data = _build_zip(src)
    m = _manifest()
    name = m.get("name", _PKG_NAME)
    version = m.get("version", "1.0.0")
    filename = f"{name}-{version}.zip"
    ascii_name = f"vdl-sniffer-extension-{version}.zip"
    disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    # 🔴 下载计数（2026-10-13「app 加上安装包/扩展下载次数并统计不同平台」）：
    #    平台维度 = User-Agent 推断的浏览器。统计失败绝不让下载失败。
    try:
        import dl_stats
        ua = (request.headers.get("user-agent") or "") if request is not None else ""
        dl_stats.record("extension", dl_stats.browser_from_ua(ua))
    except Exception:  # noqa: BLE001
        pass
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": disp,
                 "Cache-Control": "no-store"},
    )


# ── 桌面安装包下载（2026-10-13）────────────────────────────────────────────
# 用户要求「安装包下载次数并统计不同平台」。安装包本体在更新通道
# /opt/vdl-update/VideoDownloader.app.zip（与网页服务同机）——这里计数后
# 原样流式回给浏览器（FileResponse，不走 8765 直链：https 页面直跳 http
# 下载会被 Chrome 拦）。?p=macos 预留 windows 等后续包位。
_INSTALLER_FILES: dict[str, Path] = {
    "macos": Path(os.environ.get("VDL_INSTALLER_MACOS")
                  or "/opt/vdl-update/VideoDownloader.app.zip"),
}


@router.get("/download/installer")
def download_installer(p: str = "macos", request: Request = None) -> Response:
    plat = str(p or "").strip().lower()
    path = _INSTALLER_FILES.get(plat)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="该平台的安装包尚未提供")
    try:
        import dl_stats
        dl_stats.record("installer", plat)
    except Exception:  # noqa: BLE001
        pass
    return FileResponse(path, media_type="application/zip",
                        filename=path.name,
                        headers={"Cache-Control": "no-store"})
