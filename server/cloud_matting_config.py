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

import atomic_io


def _config_dir() -> Path:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def _config_path() -> Path:
    return _config_dir() / "cloud_matting.json"


def _managed_path() -> Path:
    """管理员受管配置（0600）。与 gateway_config / vision_config 同构。

    🔴 2026-10-05 新增。此前本模块**只有用户文件**，没有下发通道 ⇒ AK/SK 只能
    手工写进 `cloud_matting.json`，换机 / 重装 / 分发给用户即丢，且用户界面
    已被明确要求「不填 Key」（输入框 hidden）。结果就是：唯一能用的云端抠图
    凭据**无法下发**给用户 —— 界面说「管理员已配好」，实际新装的机器上没有。
    优先级对齐另两个模块：**环境变量 > 管理员受管文件 > 用户文件**。
    """
    return _config_dir() / "cloud_matting_managed.json"


def managed_status() -> dict[str, Any]:
    """管理员受管配置状态（**绝不返回明文 Key**）。"""
    def _mask(v: str) -> str:
        v = (v or "").strip()
        return (v[:6] + "…" + v[-4:]) if len(v) > 12 else ("…" if v else "")

    m: dict[str, Any] = {}
    mp = _managed_path()
    if mp.is_file():
        try:
            loaded = json.loads(mp.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                m = loaded
        except (json.JSONDecodeError, OSError):
            m = {}
    ak = str(m.get("access_key") or "").strip()
    sk = str(m.get("secret_key") or "").strip()
    mk = str(m.get("mediakit_api_key") or "").strip()
    env_ak = os.environ.get("VDL_CLOUD_MAT_AK", "").strip()
    return {
        "configured": bool(ak and sk) or bool(env_ak and os.environ.get("VDL_CLOUD_MAT_SK", "").strip()),
        "source": "env" if env_ak else ("managed" if m else "user"),
        "access_key_masked": _mask(ak or env_ak),
        "secret_key_masked": _mask(sk or os.environ.get("VDL_CLOUD_MAT_SK", "")),
        "mediakit_key_masked": _mask(mk or os.environ.get("VDL_CLOUD_MAT_MEDIAKIT_KEY", "")),
        "mediakit_present": bool(mk or os.environ.get("VDL_CLOUD_MAT_MEDIAKIT_KEY", "").strip()),
        "managed_file": str(_managed_path()),
        "managed_present": bool(m),
    }


def save_managed_config(data: dict[str, Any]) -> None:
    """管理员下发云端抠图凭据（只写受管文件，不回显明文）。"""
    payload = {
        "access_key": str(data.get("access_key") or "").strip(),
        "secret_key": str(data.get("secret_key") or "").strip(),
        "mediakit_api_key": str(data.get("mediakit_api_key") or "").strip(),
        "enhance_version": str(data.get("enhance_version") or "").strip(),
        "enabled": bool(data.get("enabled", True)),
    }
    atomic_io.atomic_write_json(_managed_path(), payload)
    try:
        os.chmod(_managed_path(), 0o600)
    except OSError:
        pass


def get_cloud_matting_config() -> dict[str, Any]:
    """读取云端抠图配置。

    返回 {provider, access_key, secret_key, enabled, mediakit_api_key, ...}。
    优先级：**环境变量 > 管理员受管文件 > 用户文件**
    （受管层 2026-10-05 新增，见 `_managed_path`）。
    """
    cfg: dict[str, Any] = {
        "provider": "volcengine",
        "access_key": "",
        "secret_key": "",
        "enabled": False,
        "mediakit_api_key": "",
        "enhance_version": "professional",
        "mat_output_hd": False,
        "auto_vlm_classify": True,
    }
    # ① 用户文件（最低优先级）
    cp = _config_path()
    if cp.is_file():
        try:
            saved = json.loads(cp.read_text(encoding="utf-8"))
            for k in ("provider", "access_key", "secret_key", "enabled", "mediakit_api_key", "enhance_version", "mat_output_hd", "auto_vlm_classify"):
                if k in saved:
                    cfg[k] = saved[k]
        except (json.JSONDecodeError, OSError):
            pass
    # ② 管理员受管文件（覆盖用户文件）—— 与 gateway_config / vision_config 同构
    mp = _managed_path()
    if mp.is_file():
        try:
            managed = json.loads(mp.read_text(encoding="utf-8"))
            if isinstance(managed, dict):
                for k in ("access_key", "secret_key", "mediakit_api_key", "enhance_version", "enabled"):
                    if k in managed and managed[k] not in (None, ""):
                        cfg[k] = managed[k]
        except (json.JSONDecodeError, OSError):
            pass
    # ③ 环境变量（最终裁决）
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
    atomic_io.atomic_write_json(_config_path(), data)


def is_cloud_matting_ready() -> bool:
    """是否已配置可用的云端抠图（开关开 + AK/SK 非空）。"""
    cfg = get_cloud_matting_config()
    return bool(cfg.get("enabled")) and bool(cfg.get("access_key")) and bool(cfg.get("secret_key"))


def is_cloud_matting_mediakit_ready() -> bool:
    """是否已配置 AI MediaKit Bearer Key（通用软 alpha 抠图，豆包级）。"""
    cfg = get_cloud_matting_config()
    return bool((cfg.get("mediakit_api_key") or "").strip())
