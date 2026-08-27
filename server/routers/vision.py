"""server/routers/vision.py — 视觉模型 provider 配置路由（镜像 routers/llm.py）。

handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

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
