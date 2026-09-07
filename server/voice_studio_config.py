"""VoiceStudio 本地语音服务配置（sidecar 集成，不捆绑任何 AGPL 源码）。

设计原则：
  - VDL 只通过 HTTP 调用用户本机运行的 VoiceStudio（OpenAI 兼容音频 API，默认 localhost:3900），
    不复制/链接其源码，因此不触发 AGPL 传染。
  - 商用合规：VoiceStudio 默认 OmniVoice 模型为 CC-BY-NC（不可商用）。
    本模块把 TTS/STT 做成「opt-in」——必须用户显式确认 commercial_ack=true 才能启用合成，
    且默认 tts_model/asr_model 留空，避免无声默许 NC 模型（对齐 VDL 可商用铁律 #5）。
  - 任何环境都不硬失败：未启用/未连接时返回清晰状态，由调用方降级。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://localhost:3900"
DEFAULT_TIMEOUT = 120  # 秒，长文本/首包模型可能慢

DEFAULTS: dict[str, Any] = {
    "enabled": False,            # 总开关（opt-in）
    "base_url": DEFAULT_BASE_URL,
    "tts_model": "",             # 必须为可商用引擎；留空时接口要求显式指定
    "asr_model": "",             # 转写（STT）模型，留空则交给 VoiceStudio 默认
    "default_voice": "",         # 留空则由 VoiceStudio 用其默认声音
    "default_speed": 1.0,
    "response_format": "mp3",
    "timeout": DEFAULT_TIMEOUT,
    "commercial_ack": False,     # 用户确认所用引擎可商用（NC 模型的 opt-in 许可）
}


def _config_dir() -> Path:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def _config_path() -> Path:
    return _config_dir() / "voice_studio_config.json"


def get_voice_studio_config() -> dict[str, Any]:
    """读取完整配置：环境变量 > JSON 文件 > 硬编码默认值。"""
    cfg: dict[str, Any] = dict(DEFAULTS)
    cp = _config_path()
    if cp.is_file():
        try:
            saved = json.loads(cp.read_text(encoding="utf-8"))
            for k in DEFAULTS:
                if k in saved:
                    cfg[k] = saved[k]
        except Exception:
            pass
    # 环境变量覆盖（便于容器/服务端部署，不改文件）
    env_url = os.environ.get("VDL_VOICESTUDIO_URL", "").strip().rstrip("/")
    if env_url:
        cfg["base_url"] = env_url
    if os.environ.get("VDL_VOICESTUDIO_ENABLED", "").strip().lower() in ("1", "true", "yes"):
        cfg["enabled"] = True
    env_to = os.environ.get("VDL_VOICESTUDIO_TIMEOUT", "").strip()
    if env_to.isdigit():
        cfg["timeout"] = int(env_to)
    # 模型名环境变量覆盖（便于 quicktest 不依赖 UI 直接指定可商用引擎）
    env_tts = os.environ.get("VDL_VOICESTUDIO_TTS_MODEL", "").strip()
    if env_tts:
        cfg["tts_model"] = env_tts
    env_asr = os.environ.get("VDL_VOICESTUDIO_ASR_MODEL", "").strip()
    if env_asr:
        cfg["asr_model"] = env_asr
    return cfg


def save_voice_studio_config(data: dict[str, Any]) -> dict[str, Any]:
    """持久化配置到 JSON（权限 0600），以现有文件为基底合并避免丢字段。"""
    cd = _config_dir()
    cd.mkdir(parents=True, exist_ok=True)
    cp = _config_path()
    merged = dict(DEFAULTS)
    if cp.is_file():
        try:
            merged.update(json.loads(cp.read_text(encoding="utf-8")))
        except Exception:
            pass
    for k in DEFAULTS:
        if k in data:
            merged[k] = data[k]
    tmp = cp.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(cp)
    return merged


def is_ready() -> bool:
    """配置层是否可调用（不含连通性）：已启用 且 已确认商用合规。"""
    cfg = get_voice_studio_config()
    return bool(cfg.get("enabled")) and bool(cfg.get("commercial_ack"))


COMMERCIAL_NOTE = (
    "VoiceStudio 默认语音模型 OmniVoice 为 CC-BY-NC（不可商用）。"
    "启用前请确认你在 VoiceStudio 中已切换为可商用引擎（如 MLX-Audio、Qwen3-TTS 等），"
    "否则产出不得用于商业用途。"
)
