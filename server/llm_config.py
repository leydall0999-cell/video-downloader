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
import time
import urllib.request
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
        "default_model": "deepseek-chat",
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
# 推理强度：解说管线 Stage2 默认低推理（省钱且质量够用）；关闭=最省 token，高=最佳质量最贵。
DEFAULT_REASONING_EFFORT = "low"
# 避开峰时：北京工作日 09-12 / 14-18 为 DeepSeek 峰时（价格约 2 倍），开启后调度到闲时再生成。
DEFAULT_OFFPEAK_ONLY = False

# ── 配置文件路径 ─────────────────────────────────────────────────────────
def _config_dir() -> Path:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def _config_path() -> Path:
    return _config_dir() / "llm_config.json"


# ── 本机 Ollama 探测（本地优先开关用）─────────────────────────────────────
_ollama_cache: dict = {"ts": 0.0, "val": None}
_OLLAMA_CACHE_TTL = 10.0  # 秒；探测本机端口，缓存避免高频重复请求


def detect_ollama(timeout: float = 2.0) -> dict[str, Any]:
    """探测本机 Ollama 是否在运行，返回可用模型名列表。

    返回 {"running": bool, "models": [str], "error": str|None}。
    Ollama 未安装/未启动时连接被拒（ConnectionRefused），属"正常运行中"分支，秒级返回。
    """
    global _ollama_cache
    now = time.time()
    if _ollama_cache["val"] is not None and now - _ollama_cache["ts"] < _OLLAMA_CACHE_TTL:
        return _ollama_cache["val"]
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        models = [m.get("name") for m in data.get("models", []) if m.get("name")]
        val = {"running": True, "models": models, "error": None}
    except Exception as e:  # noqa: BLE001
        val = {"running": False, "models": [], "error": str(e)}
    _ollama_cache = {"ts": now, "val": val}
    return val


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
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "offpeak_only": DEFAULT_OFFPEAK_ONLY,
        "local_priority": False,   # 本地优先：Ollama 在跑则整体切本机，零成本
        "local_model": "",         # 本地优先时选用的本机模型（留空=用检测到的第一个）
    }

    # 1) JSON 文件（桌面版前端持久化）
    cp = _config_path()
    if cp.is_file():
        try:
            saved = json.loads(cp.read_text(encoding="utf-8"))
            for k in ("provider", "api_key", "base_url", "model", "max_tokens",
                      "temperature", "reasoning_effort", "offpeak_only",
                      "local_priority", "local_model"):
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
    # 推理强度 / 避开峰时：环境变量为最终裁决（便于运维/容器覆盖 UI 设置）
    env_re = os.environ.get("VDL_LLM_REASONING_EFFORT", "").strip()
    if env_re:
        cfg["reasoning_effort"] = env_re
    env_off = os.environ.get("LLM_OFFPEAK_ONLY", "").strip()
    if env_off and env_off.lower() in ("1", "true", "yes", "on"):
        cfg["offpeak_only"] = True
    # 本地优先：环境变量为最终裁决（容器/运维可强制开启）
    env_lp = os.environ.get("VDL_LLM_LOCAL_PRIORITY", "").strip()
    if env_lp and env_lp.lower() in ("1", "true", "yes", "on"):
        cfg["local_priority"] = True

    # 2.5) 本地优先解析：Ollama 在跑则整体切到本机，零成本
    # 仅当 local_priority 开启时才探测（避免每次调用都打本机端口；UI 用 /api/llm/status 显式探测）
    cfg["_ollama_running"] = False
    if cfg.get("local_priority"):
        det = detect_ollama()
        if det.get("running"):
            cfg["_ollama_running"] = True
            cfg["provider"] = "ollama"
            cfg["base_url"] = PROVIDER_PRESETS["ollama"]["base_url"]
            local_model = cfg.get("local_model") or (det.get("models") or [None])[0]
            cfg["model"] = local_model or ""
            cfg["api_key"] = ""  # Ollama 无需 Key

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

    规则：
    - Ollama 本机模式（base_url 含 11434）：即使无 Key 也注入 LLM_BASE_URL/LLM_MODEL，
      由本地优先开关驱动，零成本跑解说。
    - 云端模式：仅 api_key 非空时注入，无 Key 不污染子进程环境，
      由 commentary-pipeline 自己的守卫(process.py 检查 LLM_API_KEY)报清晰错误。
    """
    cfg = get_llm_config()
    base = (cfg.get("base_url", "") or "").strip()
    model = (cfg.get("model", "") or "").strip()
    key = (cfg.get("api_key", "") or "").strip()
    ollama = "11434" in base
    if not key and not ollama:
        return
    env["LLM_BASE_URL"] = base
    env["LLM_MODEL"] = model
    # Ollama 不校验 Key，但 process.py 守卫要求非空，塞占位避免误报；真正请求时 Ollama 忽略它。
    env["LLM_API_KEY"] = key or "ollama"
    # 推理强度（省钱旋钮）：注入 VDL_LLM_REASONING_EFFORT 供 llm_script.py 读取。
    # 默认 low，可在 UI 选 disabled(最省) / high(最佳质量)。
    env["VDL_LLM_REASONING_EFFORT"] = cfg.get("reasoning_effort", "low")
    # 避开峰时（省钱旋钮）：开启时注入 LLM_OFFPEAK_ONLY=1，llm_script 调度到闲时再调用。
    if cfg.get("offpeak_only"):
        env["LLM_OFFPEAK_ONLY"] = "1"
    # max_tokens / temperature 暂不注入——llm_script.py 有合理默认值，
    # 且这两个参数强绑定特定提示词策略，前端 UI 改可能造成脚本输出异常。
