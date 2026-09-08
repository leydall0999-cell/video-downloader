"""server/routers/subtitle.py — 本地视频字幕提取（faster-whisper ASR，MIT 可商用）。

流程：本地视频 → ffmpeg 抽 16k mono 音频 → faster-whisper（CPU int8，VAD 句级分段）
→ SRT / TXT 输出。模型按需从 HF（默认走 hf-mirror）下载到用户缓存，不进 DMG。

job 机制独立于 CONVERT_JOBS：SUBTITLE_JOBS + app.executor，设备隔离与 convert 一致
（X-Device-Id 头查状态 / device= query 下载）。
"""
import app
import os
import time
import shutil
import subprocess
import threading
from pathlib import Path as _Path

from fastapi import APIRouter
from pydantic import BaseModel
from .core import _device_of

router = APIRouter()

# 国内加速：faster-whisper 模型走 hf-mirror（huggingface_hub 每次下载时读环境变量）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

SUBTITLE_JOBS: dict = {}
_SUBTITLE_LOCK = threading.Lock()
_SUBTITLE_MODELS: dict = {}          # model_size -> WhisperModel（进程级缓存，避免重复加载）
ALLOWED_MODELS = {"base", "small", "medium", "large-v3"}
_DEFAULT_MODEL = "small"


class SubtitleRequest(app.BaseModel):
    """桌面端本地视频字幕提取请求。"""
    local_path: str
    model_size: str = "small"        # base / small / medium / large-v3
    language: str = ""               # ""=自动检测 / "zh" / "en"
    to_library: bool = False


def _resolve_safe_local_path(path: str) -> _Path:
    p = _Path(path)
    if not p.is_file():
        raise app.HTTPException(status_code=400, detail=f"文件不存在或不是普通文件：{path}")
    try:
        resolved = p.resolve()
        if not str(resolved).startswith(("/Users/", "/home/", "/Volumes/", "C:\\")):
            raise app.HTTPException(status_code=400, detail=f"路径不在用户目录下：{path}")
        return resolved
    except app.HTTPException:
        raise
    except Exception:
        raise app.HTTPException(status_code=400, detail=f"无法解析路径：{path}")


def _get_model(model_size: str):
    """进程级模型缓存：首次加载/下载耗时，之后秒级。

    鲁棒性：HF 镜像强制覆盖（huggingface_hub 若已被提前 import，模块级
    constants 会固化默认端点，故同时改环境变量与 constants）；加载/下载
    失败自动重试 3 次，第 3 次回退本地缓存离线加载（之前下载过就能救回）。
    """
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    try:
        from huggingface_hub import constants as _hf_const
        if getattr(_hf_const, "ENDPOINT", "") != "https://hf-mirror.com":
            _hf_const.ENDPOINT = "https://hf-mirror.com"
    except Exception:
        pass

    with _SUBTITLE_LOCK:
        model = _SUBTITLE_MODELS.get(model_size)
        if model is None:
            from faster_whisper import WhisperModel
            last_err: Exception | None = None
            for attempt in range(1, 4):
                local_only = attempt >= 3   # 第 3 次尝试纯离线（命中本地缓存即成功）
                try:
                    model = WhisperModel(model_size, device="cpu", compute_type="int8",
                                         local_files_only=local_only)
                    break
                except Exception as e:
                    last_err = e
                    app.logger.warning("subtitle model %s load attempt %d failed: %s",
                                       model_size, attempt, e)
                    time.sleep(2 * attempt)
            if model is None:
                raise RuntimeError(
                    f"识别模型（{model_size}）加载/下载失败（已重试 3 次）：{last_err}。"
                    "首次使用需联网下载模型（base≈145MB / small≈484MB / medium≈1.5GB / large-v3≈3GB），"
                    "请检查网络或代理后重试；网络不稳可先换 base 模型。"
                )
            _SUBTITLE_MODELS[model_size] = model
        return model


def _fmt_ts(seconds: float) -> str:
    """SRT 时间戳 00:00:00,000。"""
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _run_subtitle(job_id: str, src: str, model_size: str, language: str, to_library: bool) -> None:
    """后台线程：抽音频 → ASR → SRT/TXT，更新 SUBTITLE_JOBS。"""
    job = SUBTITLE_JOBS.get(job_id)
    if not job:
        return
    srt_path = None
    txt_path = None
    wav_path = None
    try:
        out_dir = app.SUBTITLE_DIR
        stem = _Path(src).stem or "subtitle"
        srt_path = out_dir / f"sub_{job_id}_{stem}.srt"
        txt_path = out_dir / f"sub_{job_id}_{stem}.txt"
        wav_path = out_dir / f"sub_{job_id}.wav"

        # 1) 抽音频（16k mono pcm，whisper 标准输入）
        job["stage"] = "提取音频"
        job["progress"] = 5
        cmd = [app.FFMPEG_BIN, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", str(wav_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0 or not wav_path.exists():
            raise RuntimeError(f"音频提取失败：{(proc.stderr or '')[-300:]}")
        job["progress"] = 15

        # 2) 加载模型（首次含下载，可能数分钟）
        job["stage"] = f"加载模型（{model_size}，首次需下载）"
        model = _get_model(model_size)

        # 3) 转写：VAD 句级分段，逐句输出（对话切换处自然断句）
        job["stage"] = "识别中"
        segments, info = model.transcribe(
            str(wav_path),
            language=language or None,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 350},
            beam_size=5,
        )
        rows = []            # (start, end, text)
        total = info.duration or 0.0
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            rows.append((float(seg.start), float(seg.end), text))
            if total > 0:
                job["progress"] = 15 + int(min(80, max(0, seg.end / total * 80)))
        if not rows:
            raise RuntimeError("未识别到任何语音内容（视频可能没有对话/音轨）")

        # 4) 写 SRT + TXT
        job["stage"] = "生成字幕"
        job["progress"] = 96
        with open(srt_path, "w", encoding="utf-8") as fh:
            for i, (st, ed, text) in enumerate(rows, 1):
                fh.write(f"{i}\n{_fmt_ts(st)} --> {_fmt_ts(ed)}\n{text}\n\n")
        with open(txt_path, "w", encoding="utf-8") as fh:
            for _, _, text in rows:
                fh.write(text + "\n")

        if to_library:
            try:
                dest = app.DOWNLOAD_DIR / srt_path.name
                shutil.copy2(srt_path, dest)
            except Exception:
                pass

        job["status"] = "completed"
        job["progress"] = 100
        job["srt_file"] = str(srt_path)
        job["txt_file"] = str(txt_path)
        job["srt_name"] = f"{stem}.srt"
        job["txt_name"] = f"{stem}.txt"
        job["lines"] = len(rows)
        job["language"] = info.language or ""
        app.logger.info("subtitle %s done: %d lines (%s)", job_id, len(rows), info.language)
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        app.logger.warning("subtitle %s failed: %s", job_id, e)
    finally:
        if wav_path:
            try:
                wav_path.unlink(missing_ok=True)
            except Exception:
                pass


def _device_of_req(request):
    return _device_of(request)


@router.post("/api/subtitle/extract")
def subtitle_extract(payload: SubtitleRequest, request: app.Request) -> dict:
    app._check_rate_limit(request)
    resolved = _resolve_safe_local_path(payload.local_path)
    suffix = resolved.suffix.lower()
    if suffix not in app.UPLOAD_VIDEO_EXTS:
        raise app.HTTPException(status_code=409, detail="请选择视频文件")
    model_size = payload.model_size if payload.model_size in ALLOWED_MODELS else _DEFAULT_MODEL
    job_id = app.uuid.uuid4().hex[:12]
    with _SUBTITLE_LOCK:
        SUBTITLE_JOBS[job_id] = {
            "status": "running", "stage": "排队中", "progress": 0, "error": "",
            "srt_file": "", "txt_file": "", "srt_name": "", "txt_name": "",
            "lines": 0, "language": "",
            "device_id": _device_of_req(request),
        }
    app.executor.submit(_run_subtitle, job_id, str(resolved), model_size,
                        (payload.language or "").strip(), bool(payload.to_library))
    return {"job_id": job_id, "status": "running", "model": model_size}


@router.get("/api/subtitle/{job_id}")
def subtitle_status(job_id: str, request: app.Request) -> dict:
    job = SUBTITLE_JOBS.get(job_id)
    if not job or (job.get("device_id") and job["device_id"] != _device_of_req(request)):
        raise app.HTTPException(status_code=404, detail="任务不存在")
    return {"status": job["status"], "stage": job.get("stage", ""), "progress": job.get("progress", 0),
            "error": job.get("error", ""), "srt_name": job.get("srt_name", ""),
            "txt_name": job.get("txt_name", ""), "lines": job.get("lines", 0),
            "language": job.get("language", "")}


def _subtitle_file(job_id: str, kind: str, request: app.Request) -> _Path:
    job = SUBTITLE_JOBS.get(job_id)
    if not job or (job.get("device_id") and job["device_id"] != _device_of_req(request)):
        raise app.HTTPException(status_code=404, detail="任务不存在")
    key = "srt_file" if kind == "srt" else "txt_file"
    path = _Path(job.get(key, ""))
    if job["status"] != "completed" or not path.is_file():
        raise app.HTTPException(status_code=404, detail="字幕文件不存在")
    return path


@router.get("/api/subtitle/{job_id}/file")
def subtitle_file(job_id: str, request: app.Request, kind: str = "srt") -> app.FileResponse:
    path = _subtitle_file(job_id, kind if kind in ("srt", "txt") else "srt", request)
    return app.FileResponse(path, filename=path.name)
