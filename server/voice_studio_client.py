"""VoiceStudio 本地语音服务 HTTP 客户端（OpenAI 兼容音频 API）。

仅做网络调用，不捆绑 VoiceStudio 任何源码；AGPL 不传染。
所有方法在网络不可达/超时/4xx/5xx 时抛出 VoiceStudioError（含可读原因），由调用方降级。

端点（基于 VoiceStudio 官方 OpenAI 兼容音频 API）：
  GET  /v1/audio/voices             列举声音
  POST /v1/audio/speech             TTS 合成（body: model/input/voice/response_format/speed）
  POST /v1/audio/transcriptions     STT 转写（multipart: file + model）
"""
from __future__ import annotations

from typing import Any

import requests

from voice_studio_config import get_voice_studio_config


class VoiceStudioError(Exception):
    def __init__(self, message: str, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


def _conn_err(e: Exception) -> str:
    """把 requests 网络异常转成可读短信息（避免把超长 ConnectionError 抛给用户）。"""
    if isinstance(e, requests.exceptions.ConnectionError):
        return "连接被拒绝：VoiceStudio 未运行或地址错误"
    if isinstance(e, requests.exceptions.Timeout):
        return "请求超时（模型首包慢或地址不可达）"
    return f"网络请求失败：{str(e)[:160]}"


class VoiceStudioClient:
    def __init__(self, base_url: str | None = None, timeout: int | None = None):
        cfg = get_voice_studio_config()
        self.base_url = (base_url or cfg["base_url"]).rstrip("/")
        self.timeout = int(timeout or cfg.get("timeout") or 120)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    # ── 连通性 ──────────────────────────────────────────────
    def health(self) -> dict[str, Any]:
        """探测连通性。返回 {ok, base_url, http_status?, error?}。"""
        try:
            r = requests.get(self._url("/v1/audio/voices"), timeout=min(self.timeout, 10))
            if r.status_code < 500:
                return {"ok": True, "base_url": self.base_url, "http_status": r.status_code}
            return {"ok": False, "base_url": self.base_url, "error": f"HTTP {r.status_code}"}
        except requests.exceptions.ConnectionError:
            return {"ok": False, "base_url": self.base_url, "error": "连接被拒绝：VoiceStudio 未运行或地址错误"}
        except requests.exceptions.Timeout:
            return {"ok": False, "base_url": self.base_url, "error": "连接超时"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "base_url": self.base_url, "error": str(e)}

    # ── 声音列表 ────────────────────────────────────────────
    def list_voices(self) -> list[dict[str, Any]]:
        try:
            r = requests.get(self._url("/v1/audio/voices"), timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise VoiceStudioError(_conn_err(e))
        if r.status_code >= 400:
            raise VoiceStudioError(f"列举声音失败 HTTP {r.status_code}", r.status_code, r.text[:500])
        try:
            data = r.json()
        except Exception:
            raise VoiceStudioError("列举声音返回非 JSON", r.status_code, r.text[:500])
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        if isinstance(data, list):
            return data
        return []

    # ── TTS 合成 ───────────────────────────────────────────
    def tts(self, text: str, voice: str | None = None, model: str | None = None,
            speed: float = 1.0, response_format: str = "mp3") -> bytes:
        cfg = get_voice_studio_config()
        model = (model or cfg.get("tts_model") or "").strip()
        if not model:
            raise VoiceStudioError(
                "未指定 TTS 模型（且未配置默认可商用引擎 tts_model），请先设置 tts_model"
            )
        voice = (voice or cfg.get("default_voice") or "").strip()
        payload: dict[str, Any] = {
            "model": model,
            "input": text,
            "response_format": response_format,
            "speed": speed,
        }
        if voice:
            payload["voice"] = voice
        try:
            r = requests.post(self._url("/v1/audio/speech"), json=payload, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise VoiceStudioError(_conn_err(e))
        if r.status_code >= 400:
            raise VoiceStudioError(f"TTS 合成失败 HTTP {r.status_code}", r.status_code, r.text[:500])
        return r.content

    # ── STT 转写 ───────────────────────────────────────────
    def transcribe(self, audio_bytes: bytes, filename: str = "audio.wav",
                   model: str | None = None) -> str:
        cfg = get_voice_studio_config()
        model = (model or cfg.get("asr_model") or "").strip()
        files = {"file": (filename, audio_bytes)}
        data = {"model": model} if model else {}
        try:
            r = requests.post(
                self._url("/v1/audio/transcriptions"),
                files=files, data=data, timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            raise VoiceStudioError(_conn_err(e))
        if r.status_code >= 400:
            raise VoiceStudioError(f"转写失败 HTTP {r.status_code}", r.status_code, r.text[:500])
        try:
            j = r.json()
            if isinstance(j, dict):
                return j.get("text") or j.get("transcript") or ""
            return r.text
        except Exception:
            return r.text
