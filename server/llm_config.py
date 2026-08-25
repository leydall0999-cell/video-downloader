"""统一 LLM 配置（服务商选择器）。

解说管线(llm_script.py) 和字幕翻译(subtitles.py) 原本各自硬编码 OpenAI 端点、从不同环境变量
读 Key——没有共享配置层、没有前端 UI。本模块集中管理：提供商预设、持久化 JSON 配置、注入环境变量。

Provider presets:
  - OpenAI / DeepSeek / 通义千问 / 智谱 GLM / Moonshot / Ollama(本机)
  - 「你的托管中转」自定义：base_url + model 都由用户填，endpoint + token 字段预留后期云增强。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# ── 提供商预设 ─────────────────────────────────────────────────────────
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-v4-flash",
    },
    "qwen": {
        "name": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
    },
    "zhipu": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
    },
    "moonshot": {
        "name": "Moonshot (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
    },
    "ollama": {
        "name": "Ollama (本机)",
        "base_url": "http://localhost:11434/v1",
        "default_model": "",
    },
    "custom": {
        "name": "你的托管中转",
        "base_url": "",
        "default_model": "",
    },
}

DEFAULT_PROVIDER = "openai"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.7

# ── 配置文件路径 ─────────────────────────────────────────────────────────
def _config_dir() -> Path:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def _config_path() -> Path:
    return _config_dir() / "llm_config.json"


# ── 配置读写 ─────────────────────────────────────────────────────────────
def _env_api_key() -> str:
    """从环境变量读 API Key（支持 LLM_API_KEY 和 LLM_APIKEY 两种写法）。"""
    return (
        os.environ.get("LLM_API_KEY") or os.environ.get("LLM_APIKEY") or ""
    ).strip()


def get_llm_config() -> dict[str, Any]:
    """读取完整 LLM 配置，优先级：环境变量 > JSON 文件 > 硬编码默认值。

    环境变量为"最终覆盖"（服务端/容器部署不改文件），文件为"前端持久化"（桌面用户 UI 保存）。
    互不冲突：用户在 UI 保存后会写入 JSON，但环境变量设了就会覆盖 JSON 里的同名字段。
    """
    cfg: dict[str, Any] = {
        "provider": DEFAULT_PROVIDER,
        "api_key": "",
        "base_url": "",
        "model": "",
        "max_tokens": DEFAULT_MAX_TOKENS,
        "temperature": DEFAULT_TEMPERATURE,
    }

    # 1) JSON 文件（桌面版前端持久化）
    cp = _config_path()
    if cp.is_file():
        try:
            saved = json.loads(cp.read_text(encoding="utf-8"))
            for k in ("provider", "api_key", "base_url", "model", "max_tokens", "temperature"):
                if k in saved:
                    cfg[k] = saved[k]
        except (json.JSONDecodeError, OSError):
            pass

    # 2) 环境变量覆盖（最终裁决）
    env_key = _env_api_key()
    if env_key:
        cfg["api_key"] = env_key
    env_base = os.environ.get("LLM_BASE_URL", "").strip()
    if env_base:
        cfg["base_url"] = env_base.rstrip("/")
    env_model = os.environ.get("LLM_MODEL", "").strip()
    if env_model:
        cfg["model"] = env_model
    env_tok = os.environ.get("LLM_MAX_TOKENS", "").strip()
    if env_tok:
        try:
            cfg["max_tokens"] = int(env_tok)
        except ValueError:
            pass

    # 3) 填充缺失：从提供商预设补 base_url + model
    provider = cfg.get("provider", DEFAULT_PROVIDER)
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS[DEFAULT_PROVIDER])
    if not cfg["base_url"]:
        cfg["base_url"] = preset["base_url"]
    if not cfg["model"]:
        cfg["model"] = preset["default_model"] or PROVIDER_PRESETS[DEFAULT_PROVIDER]["default_model"]

    return cfg


def save_llm_config(data: dict[str, Any]) -> None:
    """持久化 LLM 配置到 JSON 文件（API Key 仅存此文件，权限 0600）。"""
    cd = _config_dir()
    cd.mkdir(parents=True, exist_ok=True)
    cp = _config_path()
    # 写入临时文件后原子 rename，避免断电/崩溃产生半截 JSON
    tmp = cp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(cp)


def inject_llm_env(env: dict[str, str]) -> None:
    """把当前 LLM 配置注入到环境变量字典（供子进程 env= 使用）。

    仅在 api_key 非空时注入——无 Key 时不污染子进程环境，
    由 commentary-pipeline 自己的守卫(process.py 检查 LLM_API_KEY)报清晰的错误。
    """
    cfg = get_llm_config()
    key = cfg.get("api_key", "").strip()
    if not key:
        return
    env["LLM_API_KEY"] = key
    env["LLM_BASE_URL"] = cfg.get("base_url", "").strip()
    env["LLM_MODEL"] = cfg.get("model", "").strip()
    # max_tokens / temperature 暂不注入——llm_script.py 有合理默认值，
    # 且这两个参数强绑定特定提示词策略，前端 UI 改可能造成脚本输出异常。
