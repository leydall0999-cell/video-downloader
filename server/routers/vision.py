"""server/routers/vision.py — 视觉模型 provider 配置路由（镜像 routers/llm.py）。

handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter
from pydantic import BaseModel

import cloud_matting_config as _cm

router = APIRouter()

@router.get("/api/vision/providers")
def vision_providers() -> dict:
    """返回可用的视觉模型提供商预设（供前端下拉菜单）。"""
    return {"providers": app.VISION_PROVIDER_PRESETS, "default": app.VISION_DEFAULT_PROVIDER}

@router.get("/api/vision/config")
def vision_config_get() -> dict:
    """返回当前视觉模型配置（前端面板回填）。api_key 脱敏返回，仅显示首尾各 4 位。"""
    cfg = app.get_vision_config()
    key = cfg.get("api_key", "")
    if len(key) > 8:
        cfg["api_key"] = key[:4] + "****" + key[-4:]
    return cfg

@router.get("/api/vision/status")
def vision_status() -> dict:
    """返回本机平台与本地 OCR 可用性，供前端显示针对性提示（如 Apple Silicon 的 Ollama 视觉崩溃警告）。"""
    return app.platform_status()

@router.post("/api/vision/config")
def vision_config_save(req: app.VisionConfigRequest) -> dict:
    """保存视觉模型配置。如果前端传了脱敏的 api_key(含 ****)则沿用已有 Key 不覆盖。"""
    current = app.get_vision_config()
    data = {
        "provider": req.provider,
        "api_key": req.api_key if "****" not in (req.api_key or "") else current.get("api_key", ""),
        "base_url": req.base_url,
        "model": req.model,
    }
    app.save_vision_config(data)
    return {"ok": True}


# ───────────────────────────── 云端抠图（火山引擎）配置 ─────────────────────────────
class CloudMattingConfigRequest(BaseModel):
    access_key: str = ""
    secret_key: str = ""
    enabled: bool = False
    mediakit_api_key: str = ""
    enhance_version: str = ""  # ""=auto / off / standard / professional / max


def _mask_key(k: str) -> str:
    if len(k) > 8:
        return k[:4] + "****" + k[-4:]
    return k


_ENHANCE_VERSIONS = ("", "auto", "off", "standard", "professional", "max")


@router.get("/api/cloud-matting/config")
def cloud_matting_config_get() -> dict:
    """返回云端抠图配置（AK/SK / MediaKit Key 脱敏，仅显示首尾各 4 位）。"""
    cfg = _cm.get_cloud_matting_config()
    return {
        "provider": cfg.get("provider", "volcengine"),
        "access_key": _mask_key(cfg.get("access_key", "")),
        "secret_key": _mask_key(cfg.get("secret_key", "")),
        "mediakit_api_key": _mask_key(cfg.get("mediakit_api_key", "")),
        "enhance_version": cfg.get("enhance_version", ""),
        "enabled": bool(cfg.get("enabled", False)),
        "ready": _cm.is_cloud_matting_ready(),
    }


@router.get("/api/cloud-matting/status")
def cloud_matting_status() -> dict:
    """返回云端抠图可用性（是否已配置可用）。"""
    return {"ready": _cm.is_cloud_matting_ready()}


@router.post("/api/cloud-matting/config")
def cloud_matting_config_save(req: CloudMattingConfigRequest) -> dict:
    """保存云端抠图配置。

    Key 类字段若传脱敏值（含 ****）或为空则沿用已有值不覆盖；
    必须以 current 为基底合并，否则会把未在表单里的字段（如 mediakit_api_key）抹掉。
    """
    current = _cm.get_cloud_matting_config()

    def _merge(new_val: str, key: str) -> str:
        v = (new_val or "").strip()
        if "****" in v or not v:
            return current.get(key, "")
        return v

    ev = (req.enhance_version or "").strip().lower()
    if ev not in _ENHANCE_VERSIONS:
        ev = ""
    data = {
        "provider": "volcengine",
        "access_key": _merge(req.access_key, "access_key"),
        "secret_key": _merge(req.secret_key, "secret_key"),
        "mediakit_api_key": _merge(req.mediakit_api_key, "mediakit_api_key"),
        "enhance_version": ev,
        "enabled": bool(req.enabled),
    }
    _cm.save_cloud_matting_config(data)
    return {"ok": True, "ready": _cm.is_cloud_matting_ready()}
