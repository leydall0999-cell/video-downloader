"""server/routers/llm.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/llm/providers")
def llm_providers() -> dict:
    """返回可用的提供商预设（供前端下拉菜单）。"""
    return {"providers": app.PROVIDER_PRESETS, "default": app.DEFAULT_PROVIDER}

@router.get("/api/llm/config")
def llm_config_get() -> dict:
    """返回当前 LLM 配置（前端面板回填）。api_key 脱敏返回，仅显示首尾各 4 位。"""
    cfg = app.get_llm_config()
    key = cfg.get("api_key", "")
    if len(key) > 8:
        cfg["api_key"] = key[:4] + "****" + key[-4:]
    return cfg

@router.post("/api/llm/config")
def llm_config_save(req: app.LLMConfigRequest) -> dict:
    """保存 LLM 配置。如果前端传了脱敏的 api_key(含 ****)则沿用已有 Key 不覆盖。"""
    current = app.get_llm_config()
    data = {
        "provider": req.provider,
        "api_key": req.api_key if "****" not in (req.api_key or "") else current.get("api_key", ""),
        "base_url": req.base_url,
        "model": req.model,
        "max_tokens": current.get("max_tokens", 4096),
        "temperature": current.get("temperature", 0.7),
    }
    app.save_llm_config(data)
    return {"ok": True}
