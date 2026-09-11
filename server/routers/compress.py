"""server/routers/compress.py — 图片/视频高质量压缩（2026-09-11 新增，桌面端优先）。

图片走 Pillow 高质量重编码：
- 原格式：PNG 无损优化（optimize）、JPG 质量档+渐进式、WebP 质量档；
- 转格式：WebP（更小）、AVIF（极致压缩，需 pillow-avif-plugin，import 守护）；
视频走 ffmpeg 重编码：
- H.264（VideoToolbox→libopenh264→mpeg4 自动选择，LGPL 合规）；
- HEVC/H.265（hevc_videotoolbox，同画质体积再小约 30-45%，硬件加速）。
- **码率受控**：改用「源码率钳制 + 分辨率建议码率取小」算目标码率，再以
  VBR 受限（-b:v / -maxrate / -bufsize）编码。恒定质量（-q:v）模式对已经压过的
  低码率源会把体积放大 2~3 倍（实测 629kbps 源 → 284%），白等一场后只能回退原文件。
音轨优先原样复制（aac/mp3），其余自动 AAC 160k；进度经 ``-progress pipe:1`` 实时解析。
压缩后反而更大时保留原文件（saving=0）。

⚠️ WebP/AVIF/HEVC 均为「视觉无损」（人眼难辨，像素不完全一致），并非逐字节无损。
PNG 原格式 optimize 才是严格无损。

handler 通过 ``app.<name>`` 访问共享内核（与 convert.py 等路由约定一致）。
"""
import app
import json
import os
import shutil
import subprocess
import threading
import time
from fastapi import APIRouter
from pydantic import BaseModel
from .core import _device_of
from stats import record_event

try:
    import pillow_avif  # AVIF 编解码（pillow-avif-plugin）；缺失时 AVIF 选项自动不可用
except Exception:  # pragma: no cover - 缺失属正常运行态
    pillow_avif = None

router = APIRouter()

# --------------------------------------------------------------------------- #
# 常量与档位
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".ts",
              ".wmv", ".mpeg", ".mpg", ".3gp"}
# 图片输入格式（与 choose_files('any') 白名单一致；加 .avif 允许「AVIF 进、AVIF 出」）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".avif"}

# 视频编码：h264（默认，兼容最好）/ hevc（同画质更小、macOS 硬件加速）
VIDEO_CODECS = {"h264", "hevc"}
# 图片输出格式：keep（按源格式重编码）/ webp（更小）/ avif（极致压缩）
IMAGE_OUT_FORMATS = {"keep", "webp", "avif"}

# level -> 图片质量档（jpeg_q/webp_q，AVIF 复用 webp_q 作为质量档）
LEVELS = {
    "high":     {"jpeg_q": 88, "webp_q": 88},
    "balanced": {"jpeg_q": 82, "webp_q": 80},
    "strong":   {"jpeg_q": 74, "webp_q": 72},
}
_DEFAULT_LEVEL = "balanced"

COMPRESS_JOBS: dict = {}
_LOCK = threading.Lock()

_COPY_AUDIO_CODECS = {"aac", "mp3"}

# 视频转码并发闸门（2026-09-11）
# VideoToolbox 编码器不支持 ffmpeg 层多线程（`-h encoder=h264_videotoolbox` 显示
# Threading capabilities: none），且底层是**共享的硬件编码引擎**。实测 3 路并发
# 压同一个文件时每路耗时从 1.45s 涨到 4.23s（慢 2.9 倍）——多文件一起提交反而
# 更慢。这里与下载共用的 8 worker 池隔离，限制同时转码 2 路，其余排队等待。
_TRANSCODE_SEM = threading.Semaphore(2)


class LocalCompressRequest(BaseModel):
    """桌面端本地文件压缩请求。"""
    local_path: str
    level: str = _DEFAULT_LEVEL
    codec: str = "h264"          # 视频编码：h264 / hevc
    output_format: str = "keep"  # 图片输出格式：keep / webp / avif


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


def _probe_video_meta(path: str) -> dict:
    """一次 ffprobe 取全部视频元信息（合并探测，省一次进程开销）。

    返回 ``{duration, bit_rate, width, height, acodec}``，任何字段失败均降级为
    0 / 空串，绝不抛错 —— 压缩主流程不应因为探测失败而中断。

    码率取值优先级（这决定了码率钳制是否准确）：
    1. 视频流 ``bit_rate``（最准，不含音频）；
    2. ``format.bit_rate - 音频流码率``（容器只给总码率时扣掉音频）；
    3. ``文件体积 * 8 / 时长``（连容器码率都没有时按体积估算）。
    """
    meta = {"duration": 0.0, "bit_rate": 0, "width": 0, "height": 0, "acodec": ""}
    try:
        r = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-show_entries",
             "format=duration,bit_rate:stream=codec_type,codec_name,width,height,bit_rate",
             "-of", "json", path],
            capture_output=True, text=True, timeout=25)
        data = json.loads(r.stdout or "{}")
    except Exception:
        return meta

    def _num(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    fmt = data.get("format") or {}
    meta["duration"] = _num(fmt.get("duration"))
    fmt_br = _num(fmt.get("bit_rate"))
    video_br = audio_br = 0.0
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        if kind == "video" and not meta["width"]:
            meta["width"] = int(_num(st.get("width")))
            meta["height"] = int(_num(st.get("height")))
            video_br = _num(st.get("bit_rate"))
        elif kind == "audio" and not meta["acodec"]:
            meta["acodec"] = (st.get("codec_name") or "").lower()
            audio_br = _num(st.get("bit_rate"))
    br = video_br or max(0.0, fmt_br - audio_br) or fmt_br
    if br <= 0 and meta["duration"] > 0:
        try:
            br = app.Path(path).stat().st_size * 8 / meta["duration"]
        except Exception:
            br = 0.0
    meta["bit_rate"] = int(br)
    return meta


def _register_job(device_id: str, src_name: str) -> str:
    job_id = app.uuid.uuid4().hex[:12]
    with _LOCK:
        COMPRESS_JOBS[job_id] = {
            "status": "running", "stage": "排队中", "progress": 0, "error": "",
            "out_path": "", "filename": "", "src_name": src_name,
            "device_id": device_id,
            "size_before": 0, "size_after": 0, "saving": 0.0, "note": "",
            "codec": "", "output_format": "",
            "src_kbps": 0, "target_kbps": 0,   # 视频压缩的码率依据/目标（前端可展示）
            "elapsed": 0.0, "eta": 0.0,        # 已用时间 / 预计剩余（秒），让等待有预期
        }
    return job_id


def _compress_image(job: dict, src: str, out_path: app.Path, level: str,
                    output_format: str = "keep") -> None:
    """图片压缩：原格式重编码 / 转 WebP / 转 AVIF。

    - keep  ：按源格式重编码（PNG 无损 optimize；JPG 质量档+渐进式；WebP 质量档）
    - webp  ：统一输出 WebP（质量档来自 level，method=6 最佳压缩）
    - avif  ：统一输出 AVIF（质量档来自 level；需 pillow_avif，缺失即报错）
    """
    from PIL import Image
    cfg = LEVELS.get(level, LEVELS[_DEFAULT_LEVEL])
    with Image.open(src) as im:
        if output_format == "webp":
            save_im = im.convert("RGB") if im.mode == "P" else im
            save_im.save(out_path, format="WEBP", quality=cfg["webp_q"], method=6)
            return
        if output_format == "avif":
            if pillow_avif is None:
                raise RuntimeError("当前环境未启用 AVIF 支持（缺少 pillow-avif-plugin）；"
                                   "请改用 WebP 或原格式输出")
            save_im = im.convert("RGB") if im.mode == "P" else im
            # AVIF 用 webp_q 作为质量档（Pillow AVIF 的 quality 同样 0-100，越大越好）
            save_im.save(out_path, format="AVIF", quality=cfg["webp_q"])
            return
        # keep：按源格式重编码
        fmt = (im.format or "").upper()
        if fmt == "PNG":
            im.save(out_path, format="PNG", optimize=True)          # 无损：仅重压Deflate
        elif fmt in ("JPEG", "MPO"):
            im.convert("RGB").save(out_path, format="JPEG", quality=cfg["jpeg_q"],
                                   optimize=True, progressive=True)
        elif fmt == "WEBP":
            im.save(out_path, format="WEBP", quality=cfg["webp_q"], method=6)
        elif fmt == "AVIF":
            if pillow_avif is None:
                raise RuntimeError("当前环境未启用 AVIF 支持（缺少 pillow-avif-plugin）")
            im.save(out_path, format="AVIF", quality=cfg["webp_q"])
        else:
            raise ValueError(f"暂不支持压缩该图片格式：{fmt}")


def _compress_video(job: dict, src: str, out_path: app.Path, level: str,
                    codec: str = "h264") -> None:
    """视频压缩：LGPL 安全 H.264 / HEVC 重编码，输出 MP4，音轨优先原样复制；
    进度经 ``-progress pipe:1`` 解析。

    ⚠️ 发行版 ffmpeg 为 LGPL 编译、无 libx264/libx265，必须走 codec_utils
    （VideoToolbox 系统框架，不碰被禁的 GPL 库）；不支持 -crf/-preset。

    **码率受控**（2026-09-11）：目标码率 = min(分辨率建议码率, 源码率 x 钳制系数)，
    再以 -b:v / -maxrate / -bufsize 做 VBR 受限编码。这样产物必然小于源文件，
    避免「恒定质量模式把已压过的低码率源重编成 2~3 倍体积、白等一场」。
    """
    quality = {"high": "high", "balanced": "balanced", "strong": "fast"}.get(level, "balanced")
    meta = _probe_video_meta(src)
    duration = meta["duration"]
    acodec = meta["acodec"]
    src_kbps = int(meta["bit_rate"] / 1000) if meta["bit_rate"] else 0
    short_side = min(meta["width"], meta["height"]) if (meta["width"] and meta["height"]) else 0
    from codec_utils import rate_controlled_args, target_bitrate_kbps
    target = target_bitrate_kbps(src_kbps, short_side, quality=quality, codec=codec)
    job["src_kbps"] = src_kbps
    job["target_kbps"] = target
    cmd = [app.FFMPEG_BIN, "-y", "-nostdin", "-i", src]
    cmd += rate_controlled_args(app.FFMPEG_BIN, codec=codec, target_kbps=target,
                                pix_fmt="yuv420p")
    if codec == "hevc":
        cmd += ["-tag:v", "hvc1"]          # 广兼容标签（QuickTime/Safari/Chrome 友好）
    cmd += ["-movflags", "+faststart"]
    cmd += (["-c:a", "copy"] if acodec in _COPY_AUDIO_CODECS
            else ["-c:a", "aac", "-b:a", "160k"])
    cmd += ["-progress", "pipe:1", "-nostats", str(out_path)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # stderr 用独立线程抽干：既避免管道写满把 ffmpeg 卡死，又能在失败时给出真实原因
    # （旧版丢 DEVNULL，只报一个 rc，无从排查）
    err_lines: list[str] = []

    def _drain(pipe) -> None:
        try:
            for ln in pipe:
                err_lines.append(ln.rstrip())
                if len(err_lines) > 60:
                    del err_lines[:30]
        except Exception:
            pass

    threading.Thread(target=_drain, args=(proc.stderr,), daemon=True).start()
    job["stage"] = "压缩中"
    started = time.time()
    try:
        for line in proc.stdout:
            # -progress 输出形如 out_time_us=1234567 / progress=continue / progress=end
            if duration > 0 and line.startswith("out_time_us="):
                try:
                    cur_us = float(line.split("=", 1)[1].strip() or 0)
                except ValueError:
                    continue
                if cur_us > 0:
                    pct = min(99, int(cur_us / 1e6 / duration * 100))
                    job["progress"] = pct
                    el = time.time() - started
                    job["elapsed"] = round(el, 1)
                    # 进度 >=3% 后按当前速率外推剩余时间（前期样本抖动大，不外推）
                    if pct >= 3:
                        job["eta"] = round(max(0.0, el * (100 - pct) / pct), 1)
    finally:
        rc = proc.wait()
    if rc != 0:
        tail = " | ".join(err_lines[-6:])[-400:]
        raise RuntimeError(f"ffmpeg 压缩失败（rc={rc}），编码={codec}，目标码率={target}k，"
                           f"音轨={acodec or '未知'}" + (f"；{tail}" if tail else ""))
    job["progress"] = 100
    job["elapsed"] = round(time.time() - started, 1)
    job["eta"] = 0.0


def _run_compress(job_id: str, src: str, kind: str, level: str, src_is_temp: bool,
                 codec: str = "h264", output_format: str = "keep") -> None:
    """后台线程：执行压缩并回写状态。"""
    job = COMPRESS_JOBS.get(job_id)
    if not job:
        return
    job["codec"] = codec
    job["output_format"] = output_format
    out_path = None
    acquired = False
    try:
        if kind == "video":
            # 转码闸门：VideoToolbox 硬编引擎共享，多路并发互相拖慢（实测 3 路慢 2.9 倍）
            job["stage"] = "排队中"
            _TRANSCODE_SEM.acquire()
            acquired = True
        src_path = app.Path(src)
        if kind == "video":
            ext = ".mp4"                       # H.264 / HEVC 均封装为 MP4
        elif output_format == "webp":
            ext = ".webp"
        elif output_format == "avif":
            ext = ".avif"
        else:
            ext = src_path.suffix.lower() or ".png"
        out_path = app.CONVERT_DIR / f"compress_{job_id}{ext}"
        job["size_before"] = src_path.stat().st_size
        if kind == "video":
            _compress_video(job, src, out_path, level, codec=codec)
        else:
            job["stage"] = "压缩中"
            _compress_image(job, src, out_path, level, output_format=output_format)
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
        record_event("compress", {"kind": kind, "level": level, "codec": codec,
                                  "output_format": output_format,
                                  "saving": job["saving"]})
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
    finally:
        if acquired:
            _TRANSCODE_SEM.release()
        if src_is_temp:
            try:
                app.Path(src).unlink(missing_ok=True)
            except Exception:
                pass


def _validate_level(level: str) -> str:
    return level if level in LEVELS else _DEFAULT_LEVEL


def _validate_codec(codec: str) -> str:
    return codec if codec in VIDEO_CODECS else "h264"


def _validate_output_format(output_format: str) -> str:
    return output_format if output_format in IMAGE_OUT_FORMATS else "keep"


def _submit_compress(src: str, level: str, device_id: str,
                     src_name: str = "", src_is_temp: bool = False,
                     codec: str = "h264", output_format: str = "keep") -> str:
    job_id = _register_job(device_id, src_name or app.Path(src).name)
    kind = "video" if app.Path(src).suffix.lower() in VIDEO_EXTS else "image"
    app.executor.submit(_run_compress, job_id, src, kind, level, src_is_temp,
                        codec=codec, output_format=output_format)
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
                                detail="仅支持视频（mp4/mov/mkv/webm/avi/flv/ts/wmv/mpeg/3gp）与图片（PNG/JPG/WebP/AVIF）")
    level = _validate_level(payload.level)
    codec = _validate_codec(payload.codec)
    output_format = _validate_output_format(payload.output_format)
    job_id = _submit_compress(str(resolved), level, _device_of(request),
                              src_name=resolved.name, src_is_temp=False,
                              codec=codec, output_format=output_format)
    record_event("compress_submit", {"level": level, "codec": codec,
                                     "output_format": output_format, "src": "local"})
    return {"job_id": job_id, "status": "running", "level": level,
            "codec": codec, "output_format": output_format,
            "quota": {"subscribed": subscribed, "free_used": free_used,
                      "free_daily": free_daily}}


@router.post("/api/compress/finish")
def compress_finish(
    upload_id: str = app.Form(...),
    total: int = app.Form(...),
    filename: str = app.Form("upload.mp4"),
    level: str = app.Form(_DEFAULT_LEVEL),
    codec: str = app.Form("h264"),
    output_format: str = app.Form("keep"),
    request: app.Request = None,
) -> dict:
    """分片上传收尾（压缩专用）：合并分片 → 提交压缩 job。"""
    app._check_rate_limit(request)
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    if not app.re.fullmatch(r"[0-9a-z]+", upload_id or "") or total <= 0 or total > 4096:
        raise app.HTTPException(status_code=400, detail="分片参数非法")
    level = _validate_level(level)
    codec = _validate_codec(codec)
    output_format = _validate_output_format(output_format)
    suffix = app.Path(filename or "upload.mp4").suffix.lower()
    if suffix not in VIDEO_EXTS and suffix not in IMAGE_EXTS:
        raise app.HTTPException(status_code=409,
                                detail="仅支持视频与图片（PNG/JPG/WebP/AVIF）文件")
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
                              src_name=filename, src_is_temp=True,
                              codec=codec, output_format=output_format)
    record_event("compress_submit", {"level": level, "codec": codec,
                                     "output_format": output_format, "src": "upload"})
    return {"job_id": job_id, "status": "running", "level": level,
            "codec": codec, "output_format": output_format,
            "quota": {"subscribed": subscribed, "free_used": free_used,
                      "free_daily": free_daily}}


@router.get("/api/compress/{job_id}")
def compress_status(job_id: str, request: app.Request) -> dict:
    """查询压缩进度。

    ⚠️ 这里**故意不做限流**（2026-09-11 修复）：前端每 1.5s 轮询一次进度，一个
    90 秒的转码任务约 60 次请求，而限流额度是 30 次/小时。若把轮询计入配额，
    任务跑到一半就会被 429 掐断，进度条永久停在某个百分比（实测用户看到卡在
    32%），表现为「压缩好慢」，实际编码早已跑完。convert / matting / dewatermark
    等路由的状态端点本就不限流，此处与它们对齐。
    """
    with _LOCK:
        job = COMPRESS_JOBS.get(job_id)
        if not job:
            raise app.HTTPException(status_code=404, detail="压缩任务不存在或已过期")
        return dict(job)


@router.get("/api/compress/{job_id}/file")
def compress_file(job_id: str, request: app.Request) -> app.FileResponse:
    """下载压缩结果（同 status：只读取件，不计入限流配额）。"""
    with _LOCK:
        job = COMPRESS_JOBS.get(job_id)
    if not job or job.get("status") != "completed" or not job.get("out_path"):
        raise app.HTTPException(status_code=404, detail="压缩结果不存在")
    p = app.Path(job["out_path"])
    if not p.is_file():
        raise app.HTTPException(status_code=404, detail="压缩结果文件已被清理")
    return app.FileResponse(p, filename=job.get("filename") or p.name,
                            media_type="application/octet-stream")
