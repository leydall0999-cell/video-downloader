"""云端抠图配置（火山引擎视觉智能 Visual Service）。

「说扣什么就抠什么」的像素级质量来自云端大模型（火山是豆包同款视觉后端）。
本模块只管 **AK/SK 的读取与持久化**，签名与调用见 cloud_matting.py。

与视觉定位（vision_config）解耦：VLM 定位复用 DashScope qwen-vl（用户已有 Key），
云端抠图单独用火山视觉智能的 AK/SK（同一账号在「访问控制→访问密钥」获取）。
配置存 ~/.video-downloader/cloud_matting.json，权限 0600。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _config_dir() -> Path:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def _config_path() -> Path:
    return _config_dir() / "cloud_matting.json"


def get_cloud_matting_config() -> dict[str, Any]:
    """读取云端抠图配置。

    返回 {provider, access_key, secret_key, enabled}。
    优先级：环境变量（VDL_CLOUD_MAT_AK / VDL_CLOUD_MAT_SK / VDL_CLOUD_MAT_ENABLED）> JSON 文件。
    """
    cfg: dict[str, Any] = {
        "provider": "volcengine",
        "access_key": "",
        "secret_key": "",
        "enabled": False,
        "mediakit_api_key": "",
        "enhance_version": "professional",
        "mat_output_hd": False,
    }
    cp = _config_path()
    if cp.is_file():
        try:
            saved = json.loads(cp.read_text(encoding="utf-8"))
            for k in ("provider", "access_key", "secret_key", "enabled", "mediakit_api_key", "enhance_version", "mat_output_hd"):
                if k in saved:
                    cfg[k] = saved[k]
        except (json.JSONDecodeError, OSError):
            pass
    # 环境变量覆盖（服务端/容器部署不改文件）
    ak = os.environ.get("VDL_CLOUD_MAT_AK", "").strip()
    if ak:
        cfg["access_key"] = ak
    sk = os.environ.get("VDL_CLOUD_MAT_SK", "").strip()
    if sk:
        cfg["secret_key"] = sk
    mk = os.environ.get("VDL_CLOUD_MAT_MEDIAKIT_KEY", "").strip()
    if mk:
        cfg["mediakit_api_key"] = mk
    en = os.environ.get("VDL_CLOUD_MAT_ENABLED", "").strip().lower()
    if en in ("1", "true", "yes", "on"):
        cfg["enabled"] = True
    elif en in ("0", "false", "no", "off"):
        cfg["enabled"] = False
    return cfg


def save_cloud_matting_config(data: dict[str, Any]) -> None:
    """持久化到 JSON（AK/SK 仅存此文件，权限 0600）。"""
    cd = _config_dir()
    cd.mkdir(parents=True, exist_ok=True)
    cp = _config_path()
    tmp = cp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(cp)


def is_cloud_matting_ready() -> bool:
    """是否已配置可用的云端抠图（开关开 + AK/SK 非空）。"""
    cfg = get_cloud_matting_config()
    return bool(cfg.get("enabled")) and bool(cfg.get("access_key")) and bool(cfg.get("secret_key"))


def is_cloud_matting_mediakit_ready() -> bool:
    """是否已配置 AI MediaKit Bearer Key（通用软 alpha 抠图，豆包级）。"""
    cfg = get_cloud_matting_config()
    return bool((cfg.get("mediakit_api_key") or "").strip())
