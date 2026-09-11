"""server/routers/compress.py — 图片/视频高质量压缩（2026-09-11 新增，桌面端优先）。

图片走 Pillow 高质量重编码：PNG 无损优化（optimize）、JPG 质量档+渐进式、WebP 质量档；
视频走 ffmpeg H.264 CRF 重编码（源音轨 aac/mp3 优先 -c:a copy，其余自动 AAC 160k），
进度经 ``-progress pipe:1`` 实时解析。压缩后反而更大时保留原文件（saving=0）。

handler 通过 ``app.<name>`` 访问共享内核（与 convert.py 等路由约定一致）。
"""
import app
import os
import shutil
import subprocess
import threading
from fastapi import APIRouter
from pydantic import BaseModel
from .core import _device_of
from stats import record_event

router = APIRouter()

# --------------------------------------------------------------------------- #
# 常量与档位
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".ts",
              ".wmv", ".mpeg", ".mpg", ".3gp"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# level -> 图片质量档（jpeg_q/webp_q）；视频档位映射见 _compress_video 的 quality 参数
LEVELS = {
    "high":     {"jpeg_q": 88, "webp_q": 88},
    "balanced": {"jpeg_q": 82, "webp_q": 80},
    "strong":   {"jpeg_q": 74, "webp_q": 72},
}
_DEFAULT_LEVEL = "balanced"

COMPRESS_JOBS: dict = {}
_LOCK = threading.Lock()

_COPY_AUDIO_CODECS = {"aac", "mp3"}


class LocalCompressRequest(BaseModel):
    """桌面端本地文件压缩请求。"""
    local_path: str
    level: str = _DEFAULT_LEVEL


def _ffprobe_bin() -> str:
    """解析 ffprobe 路径（与 convert._ffprobe_bin 同逻辑，独立兜底）。"""
    env = os.environ.get("VDL_FFPROBE_BIN")
    if env and os.path.isfile(env):
        return env
    ffmpeg = app.FFMPEG_BIN or ""
    cand = os.path.join(os.path.dirname(ffmpeg), "ffprobe") if ffmpeg else ""
    for p in (cand, shutil.which("ffprobe") or "",
              "/opt/homebrew/bin/ffprobe" if app.sys.platform == "darwin" else ""):
        if p and os.path.isfile(p):
            return p
    return "ffprobe"


def _probe_duration(path: str) -> float:
    """取视频时长（秒），失败返回 0（进度退化为不确定态）。"""
    try:
        r = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=20)
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0.0


def _probe_audio_codec(path: str) -> str:
    """取首条音频流编码名，失败返回空串。"""
    try:
        r = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=20)
        return (r.stdout.strip().lower())
    except Exception:
        return ""


def _register_job(device_id: str, src_name: str) -> str:
    job_id = app.uuid.uuid4().hex[:12]
    with _LOCK:
        COMPRESS_JOBS[job_id] = {
            "status": "running", "stage": "排队中", "progress": 0, "error": "",
            "out_path": "", "filename": "", "src_name": src_name,
            "device_id": device_id,
            "size_before": 0, "size_after": 0, "saving": 0.0, "note": "",
        }
    return job_id


def _compress_image(job: dict, src: str, out_path: app.Path, level: str) -> None:
    """图片压缩：PNG 无损优化 / JPG 质量档+渐进式 / WebP 质量档。"""
    from PIL import Image
    cfg = LEVELS.get(level, LEVELS[_DEFAULT_LEVEL])
    with Image.open(src) as im:
        fmt = (im.format or "").upper()
        if fmt == "PNG":
            im.save(out_path, format="PNG", optimize=True)          # 无损：仅重压Deflate
        elif fmt in ("JPEG", "MPO"):
            im.convert("RGB").save(out_path, format="JPEG", quality=cfg["jpeg_q"],
                                   optimize=True, progressive=True)
        elif fmt == "WEBP":
            im.save(out_path, format="WEBP", quality=cfg["webp_q"], method=6)
        else:
            raise ValueError(f"暂不支持压缩该图片格式：{fmt}")


def _compress_video(job: dict, src: str, out_path: app.Path, level: str) -> None:
    """视频压缩：LGPL 安全 H.264 重编码（VideoToolbox→libopenh264→mpeg4 自动选择），
    输出 MP4，音轨优先原样复制；进度经 ``-progress pipe:1`` 解析。

    ⚠️ 发行版 ffmpeg 为 LGPL 编译、无 libx264，必须走 codec_utils.h264_args，
    不支持 -crf/-preset 选项。
    """
    from codec_utils import h264_args
    quality = {"high": "high", "balanced": "balanced", "strong": "fast"}.get(level, "balanced")
    duration = _probe_duration(src)
    acodec = _probe_audio_codec(src)
    cmd = [app.FFMPEG_BIN, "-y", "-i", src]
    cmd += h264_args(app.FFMPEG_BIN, quality=quality, pix_fmt="yuv420p")
    cmd += ["-movflags", "+faststart"]
    cmd += (["-c:a", "copy"] if acodec in _COPY_AUDIO_CODECS
            else ["-c:a", "aac", "-b:a", "160k"])
    cmd += ["-progress", "pipe:1", "-nostats", str(out_path)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    job["stage"] = "压缩中"
    try:
        for line in proc.stdout:
            # -progress 输出形如 out_time_us=1234567 / progress=continue / progress=end
            if duration > 0 and line.startswith("out_time_us="):
                try:
                    cur_us = float(line.split("=", 1)[1].strip() or 0)
                except ValueError:
                    continue
                if cur_us > 0:
                    job["progress"] = min(99, int(cur_us / 1e6 / duration * 100))
    finally:
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg 压缩失败（rc={rc}），音轨编码={acodec or '未知'}")
    job["progress"] = 100


def _run_compress(job_id: str, src: str, kind: str, level: str, src_is_temp: bool) -> None:
    """后台线程：执行压缩并回写状态。"""
    job = COMPRESS_JOBS.get(job_id)
    if not job:
        return
    out_path = None
    try:
        src_path = app.Path(src)
        ext = ".mp4" if kind == "video" else src_path.suffix.lower()
        out_path = app.CONVERT_DIR / f"compress_{job_id}{ext}"
        job["size_before"] = src_path.stat().st_size
        if kind == "video":
            _compress_video(job, src, out_path, level)
        else:
            job["stage"] = "压缩中"
            _compress_image(job, src, out_path, level)
        job["size_after"] = out_path.stat().st_size
        # 压缩后反而更大 → 保留原文件副本，如实标注
        if job["size_after"] >= job["size_before"]:
            shutil.copyfile(src, out_path)
            job["size_after"] = job["size_before"]
            job["saving"] = 0.0
            job["note"] = "原文件已足够小，输出为原文件副本"
        else:
            job["saving"] = round((1 - job["size_after"] / job["size_before"]) * 100, 1)
        job["out_path"] = str(out_path)
        job["filename"] = f"[已压缩]{src_path.stem}{ext}"
        job["status"] = "completed"
        job["progress"] = 100
        record_event("compress", {"kind": kind, "level": level,
                                  "saving": job["saving"]})
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
    finally:
        if src_is_temp:
            try:
                app.Path(src).unlink(missing_ok=True)
            except Exception:
                pass


def _validate_level(level: str) -> str:
    return level if level in LEVELS else _DEFAULT_LEVEL


def _submit_compress(src: str, level: str, device_id: str,
                     src_name: str = "", src_is_temp: bool = False) -> str:
    job_id = _register_job(device_id, src_name or app.Path(src).name)
    kind = "video" if app.Path(src).suffix.lower() in VIDEO_EXTS else "image"
    app.executor.submit(_run_compress, job_id, src, kind, level, src_is_temp)
    return job_id


@router.post("/api/compress/local")
def compress_local(payload: LocalCompressRequest, request: app.Request) -> dict:
    """桌面版专用：本机绝对路径直接压缩（免上传）。"""
    app._check_rate_limit(request)
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    from .convert import _resolve_safe_local_path
    resolved = _resolve_safe_local_path(payload.local_path)
    suffix = resolved.suffix.lower()
    if suffix not in VIDEO_EXTS and suffix not in IMAGE_EXTS:
        raise app.HTTPException(status_code=409,
                                detail="仅支持视频（mp4/mov/mkv/webm/avi/flv/ts/wmv/mpeg/3gp）与图片（PNG/JPG/WebP）")
    level = _validate_level(payload.level)
    job_id = _submit_compress(str(resolved), level, _device_of(request),
                              src_name=resolved.name, src_is_temp=False)
    record_event("compress_submit", {"level": level, "src": "local"})
    return {"job_id": job_id, "status": "running", "level": level,
            "quota": {"subscribed": subscribed, "free_used": free_used,
                      "free_daily": free_daily}}


@router.post("/api/compress/finish")
def compress_finish(
    upload_id: str = app.Form(...),
    total: int = app.Form(...),
    filename: str = app.Form("upload.mp4"),
    level: str = app.Form(_DEFAULT_LEVEL),
    request: app.Request = None,
) -> dict:
    """分片上传收尾（压缩专用）：合并分片 → 提交压缩 job。"""
    app._check_rate_limit(request)
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    if not app.re.fullmatch(r"[0-9a-z]+", upload_id or "") or total <= 0 or total > 4096:
        raise app.HTTPException(status_code=400, detail="分片参数非法")
    level = _validate_level(level)
    suffix = app.Path(filename or "upload.mp4").suffix.lower()
    if suffix not in VIDEO_EXTS and suffix not in IMAGE_EXTS:
        raise app.HTTPException(status_code=409,
                                detail="仅支持视频与图片（PNG/JPG/WebP）文件")
    from .convert import _upload_parts
    parts = _upload_parts(upload_id)
    if len(parts) != total:
        raise app.HTTPException(status_code=400,
                                detail=f"分片不完整（{len(parts)}/{total}），请重试")
    save_path = app.UPLOAD_TMP / f"up_{app.uuid.uuid4().hex[:12]}{suffix}"
    try:
        with save_path.open("wb") as fh:
            for p in parts:
                with p.open("rb") as ph:
                    app.shutil.copyfileobj(ph, fh, 1024 * 1024)
                p.unlink(missing_ok=True)
    except Exception as e:
        save_path.unlink(missing_ok=True)
        for p in parts:
            p.unlink(missing_ok=True)
        raise app.HTTPException(status_code=500, detail=f"合并上传文件失败：{e}")
    job_id = _submit_compress(str(save_path), level, _device_of(request),
                              src_name=filename, src_is_temp=True)
    record_event("compress_submit", {"level": level, "src": "upload"})
    return {"job_id": job_id, "status": "running", "level": level,
            "quota": {"subscribed": subscribed, "free_used": free_used,
                      "free_daily": free_daily}}


@router.get("/api/compress/{job_id}")
def compress_status(job_id: str, request: app.Request) -> dict:
    app._check_rate_limit(request)
    with _LOCK:
        job = COMPRESS_JOBS.get(job_id)
        if not job:
            raise app.HTTPException(status_code=404, detail="压缩任务不存在或已过期")
        return dict(job)


@router.get("/api/compress/{job_id}/file")
def compress_file(job_id: str, request: app.Request) -> app.FileResponse:
    app._check_rate_limit(request)
    with _LOCK:
        job = COMPRESS_JOBS.get(job_id)
    if not job or job.get("status") != "completed" or not job.get("out_path"):
        raise app.HTTPException(status_code=404, detail="压缩结果不存在")
    p = app.Path(job["out_path"])
    if not p.is_file():
        raise app.HTTPException(status_code=404, detail="压缩结果文件已被清理")
    return app.FileResponse(p, filename=job.get("filename") or p.name,
                            media_type="application/octet-stream")
