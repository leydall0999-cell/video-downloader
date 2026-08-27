"""统一视觉模型配置（多平台 / 多环境 provider 选择器）。

解说管线的两类视觉能力共用同一套 VDL_VISION_* 配置：
  - 片头集数卡 / 片名卡检测（intro_vision.detect_episode_card_end）
  - 逐帧画面理解（vision_analysis 生成画面描述注入解说词）

不同用户环境差异巨大，本模块做成一个**平台无关、provider 可切换、且任何环境都不硬失败**的抽象层：

Provider 预设：
  - auto      ：自动（推荐）。不注入任何云端配置，由流水线内置回退链决定——
               macOS → 本机 Apple Vision OCR（免费、离线、无需 key）；
               其他平台 → 无可用免费本地 OCR，自动降级到音频 VAD 检测。
  - ollama    ：本机 Ollama（免费、跨平台 Win/Mac/Linux）。需用户自行安装并 pull 视觉模型。
  - siliconflow / gemini / dashscope / volcengine ：各家云端多模态 API（需 Key）。
  - custom    ：任意 OpenAI 兼容端点（base_url + model 全手动）。

优雅降级原则：仅当 provider 为真实云端/本地 LLM 且（云端需 Key 非空 / Ollama 无需 Key）
时才注入 VDL_VISION_* 环境变量；否则不污染子进程环境，由流水线回退链兜底，永不因"没配好"而硬报错。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# ── 提供商预设 ─────────────────────────────────────────────────────────
# base_url / default_model 留空时由用户在 UI 填写；选定预设后前端自动回填这两个字段。
VISION_PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "auto": {
        "name": "自动（推荐：Mac 用本机 OCR，其他平台自动降级）",
        "base_url": "",
        "default_model": "",
        "needs_key": False,
        "note": "不依赖任何云端 Key。macOS 走本机 Apple Vision 离线识别；Windows/Linux 若无 Ollama 则降级到音频检测。",
    },
    "ollama": {
        "name": "本机 Ollama（免费 · 跨平台）",
        "base_url": "http://localhost:11434/v1",
        "default_model": "qwen2.5vl:7b",
        "needs_key": False,
        "note": "需先安装 Ollama 并 pull 视觉模型（如 ollama pull qwen2.5vl:7b）。完全本地运行，零成本、隐私。",
    },
    "siliconflow": {
        "name": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "Qwen/Qwen2.5-VL-72B-Instruct",
        "needs_key": True,
        "note": "硅基流动控制台获取的 API Key。",
    },
    "gemini": {
        "name": "Google Gemini（含免费额度）",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.5-flash",
        "needs_key": True,
        "note": "Google AI Studio 免费申请 Key；有免费调用额度，超出后价格极低，视觉能力强。",
    },
    "dashscope": {
        "name": "阿里百炼 DashScope",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-vl-max",
        "needs_key": True,
        "note": "阿里云百炼控制台申请 Key，有免费额度，中文 OCR 强。",
    },
    "volcengine": {
        "name": "火山方舟 Volcengine",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "",
        "needs_key": True,
        "note": "火山方舟推理接入点 ep-xxxx，模型名填你的接入点 ID；价格低。",
    },
    "custom": {
        "name": "自定义 OpenAI 兼容端点",
        "base_url": "",
        "default_model": "",
        "needs_key": True,
        "note": "任意暴露 /v1/chat/completions 的兼容服务（中转/自建均可用）。",
    },
}

VISION_DEFAULT_PROVIDER = "auto"

# ── 配置文件路径 ─────────────────────────────────────────────────────────
def _config_dir() -> Path:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def _config_path() -> Path:
    return _config_dir() / "vision_config.json"


# ── 配置读写 ─────────────────────────────────────────────────────────────
def _env_api_key() -> str:
    """从环境变量读视觉 API Key（支持 VDL_VISION_API_KEY 写法）。"""
    return (os.environ.get("VDL_VISION_API_KEY") or "").strip()


def get_vision_config() -> dict[str, Any]:
    """读取完整视觉配置，优先级：环境变量 > JSON 文件 > 硬编码默认值。

    环境变量为"最终覆盖"（服务端/容器部署不改文件），文件为"前端持久化"（桌面用户 UI 保存）。
    """
    cfg: dict[str, Any] = {
        "provider": VISION_DEFAULT_PROVIDER,
        "api_key": "",
        "base_url": "",
        "model": "",
    }

    # 1) JSON 文件（桌面版前端持久化）
    cp = _config_path()
    if cp.is_file():
        try:
            saved = json.loads(cp.read_text(encoding="utf-8"))
            for k in ("provider", "api_key", "base_url", "model"):
                if k in saved:
                    cfg[k] = saved[k]
        except (json.JSONDecodeError, OSError):
            pass

    # 2) 环境变量覆盖（最终裁决）
    env_key = _env_api_key()
    if env_key:
        cfg["api_key"] = env_key
    env_base = os.environ.get("VDL_VISION_BASE_URL", "").strip()
    if env_base:
        cfg["base_url"] = env_base.rstrip("/")
    env_model = os.environ.get("VDL_VISION_MODEL", "").strip()
    if env_model:
        cfg["model"] = env_model
    env_provider = os.environ.get("VDL_VISION_PROVIDER", "").strip().lower()
    if env_provider and env_provider in VISION_PROVIDER_PRESETS:
        cfg["provider"] = env_provider

    # 3) 填充缺失：从提供商预设补 base_url + model
    provider = cfg.get("provider", VISION_DEFAULT_PROVIDER)
    preset = VISION_PROVIDER_PRESETS.get(provider, VISION_PROVIDER_PRESETS[VISION_DEFAULT_PROVIDER])
    if not cfg["base_url"]:
        cfg["base_url"] = preset.get("base_url", "")
    if not cfg["model"]:
        cfg["model"] = preset.get("default_model", "")

    return cfg


def save_vision_config(data: dict[str, Any]) -> None:
    """持久化视觉配置到 JSON 文件（API Key 仅存此文件，权限 0600）。"""
    cd = _config_dir()
    cd.mkdir(parents=True, exist_ok=True)
    cp = _config_path()
    # 写入临时文件后原子 rename，避免断电/崩溃产生半截 JSON
    tmp = cp.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(cp)


def inject_vision_env(env: dict[str, str]) -> None:
    """把当前视觉配置注入到环境变量字典（供子进程 env= 使用）。

    设计要点（平台无关 + 永不硬失败）：
      - provider == 'auto'：不注入任何云端配置，交给流水线内置回退链
        （macOS 本机 Apple Vision OCR / 其他平台音频检测）。
      - provider == 'ollama'：注入 base_url + model；Key 缺省补 dummy（Ollama 兼容端点通常接受任意 Key），
        让本机免费视觉模型可用。
      - 云端 / custom：仅当 api_key 非空才注入（key + base_url + model），
        否则跳过——避免空 Key 触发流水线报错，自动降级到本地/音频。
    """
    cfg = get_vision_config()
    provider = cfg.get("provider", VISION_DEFAULT_PROVIDER)

    if provider == "auto":
        # 交给流水线内置回退链，不污染环境
        return

    base_url = cfg.get("base_url", "").strip().rstrip("/")
    model = cfg.get("model", "").strip()
    key = cfg.get("api_key", "").strip()

    # 至少要有 base_url + model 才能构造一次有效请求
    if not base_url or not model:
        return

    # 本机 provider（Ollama / 自建 localhost 端点）：绕过 HTTP 代理直连，
    # 避免子进程继承 http_proxy 后把 localhost:11434 请求发到代理导致 502。
    if "localhost" in base_url or "127.0.0.1" in base_url:
        env["no_proxy"] = "localhost,127.0.0.1"
        env["NO_PROXY"] = "localhost,127.0.0.1"

    if provider == "ollama":
        env["VDL_VISION_API_KEY"] = key or "ollama"
        env["VDL_VISION_BASE_URL"] = base_url
        env["VDL_VISION_MODEL"] = model
        env["VDL_VISION_PROVIDER"] = "ollama"
        return

    # 云端 / custom：必须有 Key 才注入，否则降级
    if not key:
        return
    env["VDL_VISION_API_KEY"] = key
    env["VDL_VISION_BASE_URL"] = base_url
    env["VDL_VISION_MODEL"] = model
    env["VDL_VISION_PROVIDER"] = provider
