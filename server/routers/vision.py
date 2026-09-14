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
    cfg["api_key"] = app.vision_mask_key(cfg.get("api_key", ""))
    cfg["managed"] = app.vision_managed_status()
    return cfg

@router.get("/api/vision/managed")
def vision_managed() -> dict:
    """管理员受管配置状态（脱敏，绝不返回明文 Key）。

    与 `GET /api/llm/managed` 对称：界面据此显示「视觉理解已由管理员配置」，
    而不是让用户自己去填 Key。
    """
    return app.vision_managed_status()

@router.get("/api/vision/status")
def vision_status() -> dict:
    """返回本机平台与本地 OCR 可用性，供前端显示针对性提示（如 Apple Silicon 的 Ollama 视觉崩溃警告）。"""
    return app.platform_status()

@router.post("/api/vision/config")
def vision_config_save(req: app.VisionConfigRequest) -> dict:
    """保存视觉模型配置。

    Key 语义：空值 / 脱敏值（含 ****）一律视为「本次不修改」，沿用已有 Key。
    历史缺陷：原判定只挡了脱敏值，空字符串会把已配好的 Key 直接清空——前端面板
    在异步回填完成前点「保存」即触发（同一时刻 llm_config.json 的 Key 也被清空，
    因为前端 Promise.all 同时 POST 两个端点）。语义对齐 cloud_matting_config_save。

    2026-09-15：基底改用「用户配置文件本身」而不是 get_vision_config()
    （后者已叠加受管层与环境变量）——否则一次保存就把管理员下发的凭据写进
    用户文件，破坏「凭据只由管理员持有」的设计。四字段统一「空值＝不修改」。
    """
    # 基底＝用户文件本身（不含受管层 / 环境变量）
    current = app.load_vision_config_raw()
    data = dict(current)
    _new_key = (req.api_key or "").strip()
    if not _new_key or "****" in _new_key:
        _new_key = current.get("api_key", "")
    # 没有 Key 且原文件也没有时，不写空的 "api_key": ""（排查时易被误读成 Key 被清空）
    if _new_key or "api_key" in current:
        data["api_key"] = _new_key
    else:
        data.pop("api_key", None)
    for _f in ("provider", "base_url", "model"):
        _v = (getattr(req, _f, None) or "").strip()
        if _v:
            data[_f] = _v
    app.save_vision_config(data)
    return {"ok": True}


# ───────────────────────────── 云端抠图（火山引擎）配置 ─────────────────────────────
class CloudMattingConfigRequest(BaseModel):
    access_key: str = ""
    secret_key: str = ""
    enabled: bool = False
    mediakit_api_key: str = ""
    enhance_version: str = ""  # ""=auto / off / standard / professional / max
    mat_output_hd: bool = False  # 高清输出：本地 2x 超分（更清晰但更慢）
    auto_vlm_classify: bool = True  # 自动模式 VLM 看图分类选引擎（默认开）


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
        "mat_output_hd": bool(cfg.get("mat_output_hd", False)),
        "auto_vlm_classify": bool(cfg.get("auto_vlm_classify", True)),
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
        "mat_output_hd": bool(req.mat_output_hd),
        "auto_vlm_classify": bool(req.auto_vlm_classify),
        "enabled": bool(req.enabled),
    }
    _cm.save_cloud_matting_config(data)
    return {"ok": True, "ready": _cm.is_cloud_matting_ready()}
