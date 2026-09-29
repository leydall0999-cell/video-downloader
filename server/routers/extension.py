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
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parent.parent                      # server/routers/extension.py -> server

_PKG_NAME = "视频工坊媒体嗅探"


def _candidate_dirs():
    """扩展源码可能出现的多处位置（开发态 / 冻结包 / PyInstaller _MEIPASS），逐个试，命中 manifest.json 即用。

    冻结后 __file__ 解析出的 _SERVER 不一定等于 Contents/Resources/server，故不能只依赖单条相对路径。
    """
    dirs = []
    server = _SERVER
    dirs.append(server.parent / "extension")                  # 开发态：<repo>/extension
    dirs.append(server / "assets" / "extension_src")          # 冻结/网页版兜底
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        mp = Path(mp)
        dirs.append(mp / "extension")                         # --add-data "$REPO/extension:extension" 目标
        dirs.append(mp / "server" / "assets" / "extension_src")
        try:  # 兜底：在 _MEIPASS 下任意位置找带 manifest.json 的 extension 目录
            for hit in mp.glob("**/extension/manifest.json"):
                dirs.append(hit.parent)
                break
        except Exception:
            pass
    try:  # 开发态若 server 在 _MEIPASS 之外，也扫一层
        for hit in server.parent.glob("extension/manifest.json"):
            dirs.append(hit.parent)
            break
    except Exception:
        pass
    seen = set()
    out = []
    for d in dirs:
        d = Path(d)
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _source_dir() -> Path:
    tried = []
    for d in _candidate_dirs():
        tried.append(str(d))
        if d.is_dir() and (d / "manifest.json").is_file():
            return d
    raise FileNotFoundError("找不到扩展源码目录（已尝试：" + " ; ".join(tried) + "）")


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
def extension_package() -> Response:
    src = _source_dir()
    data = _build_zip(src)
    m = _manifest()
    name = m.get("name", _PKG_NAME)
    version = m.get("version", "1.0.0")
    filename = f"{name}-{version}.zip"
    ascii_name = f"vdl-sniffer-extension-{version}.zip"
    disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": disp},
    )
