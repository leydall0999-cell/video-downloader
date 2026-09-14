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
        "engine": str(cfg.get("engine") or "auto"),
        # 云端服务由管理员统一配置，用户界面只展示「是否就绪」，不接触凭据
        "managed": app.managed_status(),
    }

@router.get("/api/llm/managed")
def llm_managed() -> dict:
    """管理员受管配置状态（**不含明文 Key**），供设置面板展示「云端服务已就绪」。

    前端据此把原先的 provider / api_key / base_url / model 输入区替换成一行状态：
    用户只需在「纯云端」与「本机优先 + 云端配合」之间做选择。
    """
    return app.managed_status()

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
    cfg["api_key"] = app.mask_key(cfg.get("api_key", ""))
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
    # 基底用「用户配置文件本身」，**不是** get_llm_config()（后者已叠加管理员受管
    # 配置与环境变量）：否则每次保存都会把管理员下发的凭据写进用户文件，破坏
    # 「凭据只由管理员持有」的设计。
    current = app.load_user_config_raw()
    data = dict(current)
    # Key 语义：空值 / 脱敏值（含 ****）一律视为「本次不修改」，沿用已有 Key。
    # 历史缺陷：原判定只挡了脱敏值，**空字符串会把已配好的 Key 直接清空**——
    # 前端「AI 能力与密钥配置」面板在异步回填完成前点「保存」（输入框尚为空）
    # 即触发，后果是云端解说与视觉理解双双失效，界面没有任何提示。
    # 2026-09-15 实测：llm_config.json 与 vision_config.json 的 api_key 被同时清空
    #（前端 Promise.all 同时 POST 两个端点，故两个文件 mtime 一致）。
    # 语义对齐 cloud_matting_config_save 里已正确的 _merge()：空值也保留旧值。
    _new_key = (req.api_key or "").strip()
    if not _new_key or "****" in _new_key:
        _new_key = current.get("api_key", "")
    data.update({
        "api_key": _new_key,
        "reasoning_effort": req.reasoning_effort or "low",
        "offpeak_only": bool(req.offpeak_only),
        "local_priority": bool(req.local_priority),
        "local_model": req.local_model or "",
    })
    # 凭据三件套：只有「显式给出非空值」才更新（空值/未提交 = 本次不修改）。
    # 用户界面已不再暴露这些字段（由超级管理员统一配置），若沿用旧的无条件写入，
    # 一次保存就会把 provider 打回 openai、把 base_url/model 清空。
    for _f in ("provider", "base_url", "model"):
        _v = (getattr(req, _f, None) or "").strip()
        if _v:
            data[_f] = _v
    # 本机引擎字段：None = 本次不修改（后端 LLMConfigRequest 默认值），
    # 只有前端显式提交时才覆盖。
    if req.engine is not None:
        engine = (req.engine or "auto").strip().lower()
        # 用户可见档位只有两档：auto（本机优先 → 云端配合）/ cloud（纯云端）。
        # 旧版 mlx / ollama 强制档已按产品决策下线；写入时统一归一，避免出现
        # 「配置里是 mlx、界面上只有两档」的幽灵状态。
        data["engine"] = "cloud" if engine == "cloud" else "auto"
    if req.mlx_model_path is not None:
        data["mlx_model_path"] = (req.mlx_model_path or "").strip()
    if req.mlx_python is not None:
        data["mlx_python"] = (req.mlx_python or "").strip()
    if req.mlx_max_tokens is not None:
        data["mlx_max_tokens"] = int(req.mlx_max_tokens)
    app.save_llm_config(data)
    return {"ok": True}
