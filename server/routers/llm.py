"""server/routers/llm.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import os

import app
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/llm/providers")
def llm_providers() -> dict:
    """返回可用的提供商预设（供前端下拉菜单）。"""
    return {"providers": app.PROVIDER_PRESETS, "default": app.DEFAULT_PROVIDER}

@router.get("/api/llm/status")
def llm_status() -> dict:
    """返回本机 Ollama 探测结果 + 当前生效配置（本地优先开关 UI 用）。

    独立探测本机端口，不污染 get_llm_config 的热路径；前端据此展示
    Ollama 是否运行中、可用模型列表，并提示 `ollama pull <model>`。
    """
    det = app.detect_ollama()
    cfg = app.get_llm_config()
    return {
        "ollama_running": det.get("running", False),
        "ollama_models": det.get("models", []),
        "ollama_error": det.get("error"),
        "local_priority": bool(cfg.get("local_priority")),
        "effective_provider": cfg.get("provider"),
        "effective_base_url": cfg.get("base_url"),
        "effective_model": cfg.get("model"),
    }

@router.get("/api/llm/local-models")
def llm_local_models() -> dict:
    """本机 AI 引擎（MLX）的可用权重与运行时状态（「AI 能力与密钥配置」面板用）。

    只扫目录 + find_spec 探测，不做任何模型导入，可安全随面板打开调用。
    """
    cfg = app.get_llm_config()
    current = (cfg.get("mlx_model_path") or "").strip()
    models = app.list_local_models()
    runtime = app.local_runtime_status(cfg.get("mlx_python") or "")
    # 配置指向的目录可能已被删除/改名（例如权重放在 /tmp 又被系统清空）。
    # 如实回报，让面板能直接提示，而不是显示成"没选模型"让人摸不着头脑。
    return {
        "models": models,
        "current": current,
        "current_exists": bool(current) and os.path.isdir(current),
        "runtime": runtime,
        "models_dir": app.local_models_dir(),
        "total_mb": sum(int(m.get("size_mb") or 0) for m in models),
    }

@router.get("/api/llm/config")
def llm_config_get() -> dict:
    """返回当前 LLM 配置（前端面板回填）。api_key 脱敏返回，仅显示首尾各 4 位。"""
    cfg = app.get_llm_config()
    key = cfg.get("api_key", "")
    if len(key) > 8:
        cfg["api_key"] = key[:4] + "****" + key[-4:]
    # 补全本地优先相关字段（脱敏后可能缺，显式带上）
    cfg.setdefault("local_priority", False)
    cfg.setdefault("local_model", "")
    cfg.setdefault("_ollama_running", False)
    return cfg

@router.post("/api/llm/config")
def llm_config_save(req: app.LLMConfigRequest) -> dict:
    """保存 LLM 配置。如果前端传了脱敏的 api_key(含 ****)则沿用已有 Key 不覆盖。

    以「现有配置」为基底做增量更新。历史缺陷：本端点原先把字段逐个写进一个
    固定字典后整体覆盖文件，而 UI 只暴露其中一部分字段——用户在面板点一次
    「保存」，engine / mlx_model_path / mlx_python 就被静默抹掉，导致本机 MLX
    失效、回落到云端并消耗免费配额，且界面没有任何提示。改为增量合并后，
    未暴露的字段（以及未来新增的字段）都会被保留。
    """
    current = app.get_llm_config()
    data = dict(current)
    data.update({
        "provider": req.provider,
        "api_key": req.api_key if "****" not in (req.api_key or "") else current.get("api_key", ""),
        "base_url": req.base_url,
        "model": req.model,
        "reasoning_effort": req.reasoning_effort or "low",
        "offpeak_only": bool(req.offpeak_only),
        "local_priority": bool(req.local_priority),
        "local_model": req.local_model or "",
    })
    # 本机引擎字段：None = 本次不修改（后端 LLMConfigRequest 默认值），
    # 只有前端显式提交时才覆盖。
    if req.engine is not None:
        engine = (req.engine or "auto").strip().lower()
        # 白名单校验：非法值一律回落到 auto，避免写坏配置后引擎静默失效
        data["engine"] = engine if engine in ("auto", "cloud", "mlx", "ollama") else "auto"
    if req.mlx_model_path is not None:
        data["mlx_model_path"] = (req.mlx_model_path or "").strip()
    if req.mlx_python is not None:
        data["mlx_python"] = (req.mlx_python or "").strip()
    if req.mlx_max_tokens is not None:
        data["mlx_max_tokens"] = int(req.mlx_max_tokens)
    app.save_llm_config(data)
    return {"ok": True}
