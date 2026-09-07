"""server/routers/voice_studio.py — VoiceStudio 本机语音服务集成路由。

通过 HTTP 调用户本机运行的 VoiceStudio（OpenAI 兼容音频 API），不捆绑其源码（AGPL 不传染）。
提供：状态探测、声音列表、TTS 合成、STT 转写、配置读写。
商用合规：启用前需 commercial_ack=true（用户确认所用引擎可商用，对齐 VDL 铁律 #5 的 opt-in 许可）。
"""
import os
import tempfile
from pathlib import Path

import app
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

import voice_studio_client as _vs
import voice_studio_config as _vsc

router = APIRouter()

_COMMERCIAL_NOTE = _vsc.COMMERCIAL_NOTE


class VoiceStudioConfigRequest(BaseModel):
    enabled: bool = False
    base_url: str = ""
    tts_model: str = ""
    asr_model: str = ""
    default_voice: str = ""
    default_speed: float = 1.0
    response_format: str = "mp3"
    timeout: int = 120
    commercial_ack: bool = False


class TTSRequest(BaseModel):
    text: str
    voice: str = ""
    model: str = ""
    speed: float = 1.0
    response_format: str = "mp3"


def _not_ready() -> dict:
    return {"ok": False, "error": "未启用或未确认商用合规", "commercial_note": _COMMERCIAL_NOTE}


@router.get("/api/voice-studio/status")
def voice_studio_status() -> dict:
    cfg = _vsc.get_voice_studio_config()
    if cfg.get("enabled"):
        health = _vs.VoiceStudioClient().health()
    else:
        health = {"ok": False, "error": "未启用"}
    return {
        "enabled": bool(cfg.get("enabled")),
        "commercial_ack": bool(cfg.get("commercial_ack")),
        "ready": _vsc.is_ready(),
        "base_url": cfg.get("base_url"),
        "tts_model": cfg.get("tts_model"),
        "health": health,
        "commercial_note": _COMMERCIAL_NOTE,
    }


@router.get("/api/voice-studio/voices")
def voice_studio_voices() -> dict:
    if not _vsc.is_ready():
        return _not_ready()
    try:
        return {"ok": True, "voices": _vs.VoiceStudioClient().list_voices()}
    except _vs.VoiceStudioError as e:
        return {"ok": False, "error": e.message}


@router.post("/api/voice-studio/tts")
def voice_studio_tts(req: TTSRequest, background_tasks: BackgroundTasks):
    if not _vsc.is_ready():
        return _not_ready()
    try:
        audio = _vs.VoiceStudioClient().tts(
            text=req.text,
            voice=req.voice or None,
            model=req.model or None,
            speed=req.speed,
            response_format=req.response_format,
        )
    except _vs.VoiceStudioError as e:
        return {"ok": False, "error": e.message}

    ext = (req.response_format or "mp3").lower()
    media = {"wav": "audio/wav", "ogg": "audio/ogg"}.get(ext, "audio/mpeg")
    fd, path = tempfile.mkstemp(prefix="vdl_vs_tts_", suffix="." + ext)
    os.close(fd)
    Path(path).write_bytes(audio)
    background_tasks.add_task(os.unlink, path)  # 响应后清理临时文件
    return app.FileResponse(path, media_type=media, filename=f"voice_studio_tts.{ext}")


@router.post("/api/voice-studio/transcribe")
async def voice_studio_transcribe(file: app.UploadFile = app._FastAPIFile(...)):
    if not _vsc.is_ready():
        return _not_ready()
    data = await file.read()
    try:
        text = _vs.VoiceStudioClient().transcribe(
            audio_bytes=data, filename=file.filename or "audio.wav"
        )
    except _vs.VoiceStudioError as e:
        return {"ok": False, "error": e.message}
    return {"ok": True, "text": text}


@router.get("/api/voice-studio/config")
def voice_studio_config_get() -> dict:
    cfg = _vsc.get_voice_studio_config()
    return {**cfg, "commercial_note": _COMMERCIAL_NOTE}


@router.post("/api/voice-studio/config")
def voice_studio_config_save(req: VoiceStudioConfigRequest) -> dict:
    data = {
        "enabled": bool(req.enabled),
        "base_url": (req.base_url or "").strip() or _vsc.DEFAULT_BASE_URL,
        "tts_model": (req.tts_model or "").strip(),
        "asr_model": (req.asr_model or "").strip(),
        "default_voice": (req.default_voice or "").strip(),
        "default_speed": req.default_speed,
        "response_format": (req.response_format or "mp3").strip(),
        "timeout": req.timeout,
        "commercial_ack": bool(req.commercial_ack),
    }
    saved = _vsc.save_voice_studio_config(data)
    return {"ok": True, "ready": _vsc.is_ready(), "config": {k: saved[k] for k in saved}}
