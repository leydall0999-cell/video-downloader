"""server/routers/convert.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
import json
import os
import shutil
import subprocess
import re
from fastapi import APIRouter
from .core import _device_of

router = APIRouter()

@router.post("/api/convert")
def create_convert(payload: app.ConvertRequest, request: app.Request) -> dict:
    app._check_rate_limit(request)
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    task = app._require_task(payload.task_id, _device_of(request))
    if task.status != "completed" or not task.filepath or not task.filepath.exists():
        raise app.HTTPException(status_code=409, detail="原任务文件尚未准备好，无法转换")
    target = payload.target
    if target not in app.CONVERT_TARGETS:
        raise app.HTTPException(status_code=400, detail="不支持的目标格式")
    job_id = app.uuid.uuid4().hex[:12]
    ext = app.CONVERT_EXT[target]
    out_path = app.CONVERT_DIR / f"{task.id}_conv_{job_id}.{ext}"
    with app.CONVERT_LOCK:
        app.CONVERT_JOBS[job_id] = {
            "status": "running",
            "out_path": str(out_path),
            "error": "",
            "filename": out_path.name,
            "device_id": _device_of(request),   # 设备隔离：转换文件仅创建者可见
        }
    app.executor.submit(app._run_convert, job_id, str(task.filepath), target, payload.resolution or "original")
    return {
        "job_id": job_id,
        "status": "running",
        "quota": {"subscribed": subscribed, "free_used": free_used, "free_daily": free_daily},
    }

@router.get("/api/convert/{job_id}")
def convert_status(job_id: str, request: app.Request) -> dict:
    job = app.CONVERT_JOBS.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="转换任务不存在")
    # 设备隔离：job 记录归属时校验（历史无归属 job 兼容可见）
    if job.get("device_id") and job["device_id"] != _device_of(request):
        raise app.HTTPException(status_code=404, detail="转换任务不存在")
    return {"status": job["status"], "error": job.get("error", ""),
            "filename": job.get("filename", ""), "library_id": job.get("library_id", ""),
            "progress": job.get("progress", 0), "stage": job.get("stage", "")}

@router.get("/api/convert/{job_id}/file")
def convert_file(job_id: str, request: app.Request) -> app.FileResponse:
    job = app.CONVERT_JOBS.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="转换任务不存在")
    # 设备隔离：转换文件仅创建者可下载（query device=，<a href> 无法带 header）
    if job.get("device_id") and job["device_id"] != _device_of(request):
        raise app.HTTPException(status_code=404, detail="转换任务不存在")
    if job["status"] != "completed":
        raise app.HTTPException(status_code=409, detail="转换尚未完成")
    out = app.Path(job["out_path"])
    if not out.exists():
        raise app.HTTPException(status_code=410, detail="转换文件已清理")
    # 下载文件名带参数前缀 `[格式]原名.ext`，与前端 download 属性一致；
    # 否则 FileResponse 的 Content-Disposition 会覆盖浏览器 <a download>，保存成 up_conv_xxx.mp4
    target = job.get("target", "")
    src_name = job.get("src_name", "")
    if target and src_name:
        dl_name = f"[{target.upper()}]{app.Path(src_name).stem}{out.suffix}"
    else:
        dl_name = out.name
    return app.FileResponse(path=str(out), filename=dl_name, media_type="application/octet-stream")


@router.post("/api/upload-convert")
def create_upload_convert(
    file: app.UploadFile = app._FastAPIFile(...),
    target: str = app.Form("mp4"),
    resolution: str = app.Form("original"),
    bitrate: str = app.Form(""),
    audio: bool = app.Form(True),
    rotate: int = app.Form(0),
    remux: bool = app.Form(False),
    to_library: bool = app.Form(False),
    request: app.Request = None,
) -> dict:
    """上传本地视频 → 直接转码（复用 ffmpeg 管线），无需先下载。
    参数：target 目标格式、resolution 分辨率、bitrate 视频码率、audio 是否保留音轨、
    rotate 竖屏旋转(0/90/180/270)、remux 仅换容器无损、to_library 完成后存入媒体库。
    """
    app._check_rate_limit(request)
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    if target not in app.CONVERT_TARGETS:
        raise app.HTTPException(status_code=400, detail="不支持的目标格式")
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in app.UPLOAD_VIDEO_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传视频文件")
    # 流式落盘并限制大小
    save_path = app.UPLOAD_TMP / f"up_{app.uuid.uuid4().hex[:12]}{suffix}"
    written = 0
    try:
        with save_path.open("wb") as fh:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > app.UPLOAD_MAX_BYTES:
                    fh.close()
                    save_path.unlink(missing_ok=True)
                    raise app.HTTPException(status_code=413, detail="文件超过上传大小上限")
                fh.write(chunk)
    except app.HTTPException:
        raise
    except Exception as e:
        save_path.unlink(missing_ok=True)
        raise app.HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()

    ext = app.CONVERT_EXT[target]
    job_id = app.uuid.uuid4().hex[:12]
    out_path = app.CONVERT_DIR / f"up_conv_{job_id}.{ext}"
    with app.CONVERT_LOCK:
        app.CONVERT_JOBS[job_id] = {
            "status": "running",
            "out_path": str(out_path),
            "error": "",
            "filename": out_path.name,
            "src_name": file.filename or "",   # 原始上传文件名（媒体库命名用）
            "target": target,                  # 目标格式（下载文件名 [格式]原名.ext 用）
            "stage": "排队中",
            "to_library": to_library,
            "library_id": "",
            "device_id": _device_of(request),   # 设备隔离：上传转换文件仅创建者可见
        }
    app.executor.submit(app._run_convert, job_id, str(save_path), target,
                        resolution, bitrate, audio, rotate, remux, src_is_temp=True)
    return {
        "job_id": job_id,
        "status": "running",
        "target": target,
        "filename": out_path.name,
        "quota": {"subscribed": subscribed, "free_used": free_used, "free_daily": free_daily},
    }


# ---- 分片上传（大文件提速）：前端 32MB/片 × 4 并发 → /api/upload-chunk → finish 合并转码 ----
_UPLOAD_ID_RE = app.re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _upload_parts(upload_id: str):
    return sorted(app.UPLOAD_TMP.glob(f"up_{upload_id}.p*"))


def _submit_convert_job(save_path, target, resolution, bitrate, audio, rotate, remux,
                        to_library, device_id, src_name="") -> tuple:
    """落盘完成后的公共收尾：登记 job + 提交线程池转码（整传/分片 finish 共用）。"""
    ext = app.CONVERT_EXT[target]
    job_id = app.uuid.uuid4().hex[:12]
    out_path = app.CONVERT_DIR / f"up_conv_{job_id}.{ext}"
    with app.CONVERT_LOCK:
        app.CONVERT_JOBS[job_id] = {
            "status": "running",
            "out_path": str(out_path),
            "error": "",
            "filename": out_path.name,
            "src_name": src_name,        # 原始上传文件名（用于媒体库命名 [格式]原名.ext）
            "target": target,            # 目标格式（下载文件名 [格式]原名.ext 用）
            "stage": "排队中",            # 前端据此显示「排队中…」（转码线程繁忙时）
            "to_library": to_library,
            "library_id": "",
            "device_id": device_id,   # 设备隔离：上传转换文件仅创建者可见
        }
    app.executor.submit(app._run_convert, job_id, str(save_path), target,
                        resolution, bitrate, audio, rotate, remux, src_is_temp=True)
    return job_id, out_path.name


@router.post("/api/upload-chunk")
def upload_chunk(
    upload_id: str = app.Form(...),
    index: int = app.Form(...),
    total: int = app.Form(...),
    file: app.UploadFile = app._FastAPIFile(...),
    request: app.Request = None,
) -> dict:
    """分片上传：单块（32MB）落盘 up_{id}.p{index}，支持并发。
    累计字节超 UPLOAD_MAX_BYTES 即 413 并清理该 upload 全部已传分片。

    注意：分片接口本身**不走**全局限流；finish/convert 等低频入口仍受限流保护，
    避免大文件几十个分片瞬间耗尽每小时 30 次的公共额度。"""
    if not _UPLOAD_ID_RE.match(upload_id) or total <= 0 or index < 0 or index >= total:
        raise app.HTTPException(status_code=400, detail="分片参数非法")
    part_path = app.UPLOAD_TMP / f"up_{upload_id}.p{index:04d}"
    written = 0
    try:
        with part_path.open("wb") as fh:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > app.UPLOAD_CHUNK_MAX:
                    fh.close()
                    part_path.unlink(missing_ok=True)
                    raise app.HTTPException(status_code=413, detail="单个分片超过大小上限")
                fh.write(chunk)
    finally:
        file.file.close()
    total_bytes = sum(p.stat().st_size for p in _upload_parts(upload_id))
    if total_bytes > app.UPLOAD_MAX_BYTES:
        for p in _upload_parts(upload_id):
            p.unlink(missing_ok=True)
        raise app.HTTPException(status_code=413, detail="文件超过上传大小上限")
    return {"ok": True, "received": written, "uploaded_bytes": total_bytes}


@router.post("/api/upload-chunk/abort")
def abort_upload_chunk(upload_id: str = app.Form(...), request: app.Request = None) -> dict:
    """取消分片上传：删除该 upload 已落盘的全部部分（前端删除上传中任务时调用）。
    正常路径 finish 已合并删除；此处兜底用户中途取消/删除，避免孤儿分片占磁盘。"""
    app._check_rate_limit(request)
    if not _UPLOAD_ID_RE.match(upload_id):
        raise app.HTTPException(status_code=400, detail="upload_id 非法")
    n = 0
    for p in _upload_parts(upload_id):
        try:
            p.unlink(missing_ok=True)
            n += 1
        except OSError:
            pass
    return {"ok": True, "removed": n}


@router.post("/api/upload-chunk/finish")
def finish_upload_chunk(
    upload_id: str = app.Form(...),
    total: int = app.Form(...),
    filename: str = app.Form("upload.mp4"),
    target: str = app.Form("mp4"),
    resolution: str = app.Form("original"),
    bitrate: str = app.Form(""),
    audio: bool = app.Form(True),
    rotate: int = app.Form(0),
    remux: bool = app.Form(False),
    to_library: bool = app.Form(False),
    mode: str = app.Form("convert"),
    request: app.Request = None,
) -> dict:
    """分片上传收尾：校验分片齐全 → 顺序合并 → 精确校验总大小 → 提交转码 job。

    mode='store' 时仅把合并后的文件落地为「拼接素材」（不转码），供 /api/concat 使用。"""
    app._check_rate_limit(request)
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    if not _UPLOAD_ID_RE.match(upload_id) or total <= 0:
        raise app.HTTPException(status_code=400, detail="分片参数非法")
    if target not in app.CONVERT_TARGETS:
        raise app.HTTPException(status_code=400, detail="不支持的目标格式")
    suffix = app.Path(filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in app.UPLOAD_VIDEO_EXTS and suffix not in app.UPLOAD_AUDIO_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传视频或音频文件")
    parts = _upload_parts(upload_id)
    if len(parts) != total:
        raise app.HTTPException(status_code=400, detail=f"分片不完整（{len(parts)}/{total}），请重试")
    save_path = app.UPLOAD_TMP / f"up_{app.uuid.uuid4().hex[:12]}{suffix}"
    written = 0
    try:
        with save_path.open("wb") as fh:
            for p in parts:
                written += p.stat().st_size
                if written > app.UPLOAD_MAX_BYTES:
                    raise app.HTTPException(status_code=413, detail="文件超过上传大小上限")
                with p.open("rb") as ph:
                    app.shutil.copyfileobj(ph, fh, 1024 * 1024)
                p.unlink(missing_ok=True)
    except app.HTTPException:
        save_path.unlink(missing_ok=True)
        for p in _upload_parts(upload_id):
            p.unlink(missing_ok=True)
        raise
    except Exception as e:
        save_path.unlink(missing_ok=True)
        for p in _upload_parts(upload_id):
            p.unlink(missing_ok=True)
        raise app.HTTPException(status_code=500, detail=f"合并上传文件失败：{e}")

    # mode='store'：仅落地为「拼接素材」，不转码（供 /api/concat 使用）
    if mode == "store":
        seg_id = app.uuid.uuid4().hex[:12]
        seg_name = f"seg_{seg_id}{suffix}"
        seg_path = app.UPLOAD_TMP / seg_name
        try:
            save_path.rename(seg_path)   # 同目录重命名，原子且快
        except Exception as e:
            raise app.HTTPException(status_code=500, detail=f"保存拼接素材失败：{e}")
        return {
            "ok": True, "mode": "store",
            "seg_id": seg_id, "seg_name": seg_name,
            "name": filename,
            "size": seg_path.stat().st_size,
        }

    job_id, out_name = _submit_convert_job(
        save_path, target, resolution, bitrate, audio, rotate, remux,
        to_library, _device_of(request), src_name=filename)
    return {
        "job_id": job_id,
        "status": "running",
        "target": target,
        "filename": out_name,
        "quota": {"subscribed": subscribed, "free_used": free_used, "free_daily": free_daily},
    }


# ---- 视频/音频桥接（拼接合并）：无损优先，编码不一致时自动转码兜底 ----
# 前端先把每个片段经分片上传 + finish(mode=store) 落地为 seg_{id}.ext，
# 再提交本接口按列表顺序合并。复用 CONVERT_JOBS 进度与下载机制。
from pydantic import BaseModel

class ConcatRequest(BaseModel):
    segments: list          # 已落地素材文件名列表（seg_{id}.ext），按拼接顺序
    out_format: str = "mp4"
    out_name: str = "merged"
    to_library: bool = False
    audio_only: bool = False   # 前端提示：所有素材均为音频


def _ffprobe_bin():
    """解析 ffprobe 路径（app 未定义全局 FFPROBE_BIN，这里兜底探测）。"""
    ffmpeg = getattr(app, "FFMPEG_BIN", "") or ""
    if ffmpeg:
        cand = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
        if os.path.exists(cand):
            return cand
    return shutil.which("ffprobe") or "ffprobe"


def _probe_streams(p):
    """用 ffprobe 探测单文件流类型/分辨率/时长。"""
    try:
        pp = subprocess.run([_ffprobe_bin(), "-v", "error",
                            "-show_entries", "stream=codec_type,width,height",
                            "-show_entries", "format=duration", "-of", "json", str(p)],
                           capture_output=True, text=True, timeout=30)
        d = json.loads(pp.stdout or "{}")
    except Exception:
        return {"has_video": False, "has_audio": False, "width": 0, "height": 0, "duration": 0.0}
    has_video = has_audio = False
    w = h = 0
    for s in d.get("streams", []):
        ct = s.get("codec_type")
        if ct == "video":
            has_video = True
            w = int(s.get("width") or 0) or w
            h = int(s.get("height") or 0) or h
        elif ct == "audio":
            has_audio = True
    dur = float((d.get("format") or {}).get("duration") or 0) or 0.0
    return {"has_video": has_video, "has_audio": has_audio, "width": w, "height": h, "duration": dur}


# 纯音频合并时的转码参数（按输出格式）
AUDIO_REENCODE = {
    "mp3":  ["-c:a", "libmp3lame", "-q:a", "4"],
    "m4a":  ["-c:a", "aac", "-b:a", "192k"],
    "wav":  ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
}


def _run_concat(job_id, seg_names, out_format, out_name, device_id, to_library, audio_only=False):
    """顺序合并多个片段。
    无损优先：编码/分辨率一致时走 concat demuxer -c copy；失败时回退 concat 滤镜重新编码，
    可处理编码/分辨率不一致、缺音轨、纯音频或音视频混合等情况。"""
    job = app.CONVERT_JOBS[job_id]
    ext = app.CONVERT_EXT.get(out_format, out_format)
    out_path = app.CONVERT_DIR / f"concat_{job_id}.{ext}"
    seg_paths = []
    for name in seg_names:
        p = app.UPLOAD_TMP / name
        if not p.exists():
            job["status"] = "failed"; job["error"] = f"素材缺失：{name}（可能已过期，请重新添加）"; return
        seg_paths.append(p)

    # 预探测各段流类型与时长
    probes = [_probe_streams(p) for p in seg_paths]
    total_dur = sum(p["duration"] for p in probes) or 0.0
    has_any_video = any(p["has_video"] for p in probes)
    all_audio = bool(audio_only) or (not has_any_video)

    _re_out = re.compile(r"^out_time_us=(-?\d+)")

    def _track(proc):
        for line in proc.stdout:
            line = line.strip()
            m = _re_out.match(line)
            if m and total_dur > 0:
                cur = int(m.group(1)) / 1_000_000
                job["progress"] = int(min(99, max(0, cur / total_dur * 100)))

    def _store_library():
        if not to_library:
            return
        dest = app.DOWNLOAD_DIR / f"[{out_format.upper()}]{out_name}.{ext}"
        if dest.exists() and dest.resolve() != out_path.resolve():
            dest = app.DOWNLOAD_DIR / f"[{out_format.upper()}]{out_name}_{app.uuid.uuid4().hex[:6]}.{ext}"
        app.shutil.copy2(out_path, dest)

    # ---- 阶段1：无损合并 ----
    job["stage"] = "拼接中"; job["progress"] = 0
    list_path = app.UPLOAD_TMP / f"concat_{job_id}.txt"
    with list_path.open("w", encoding="utf-8") as fh:
        for p in seg_paths:
            fh.write(f"file '{p}'\n")
    copy_cmd = [app.FFMPEG_BIN, "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
                "-c", "copy", str(out_path)]
    proc = subprocess.Popen(copy_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            bufsize=0, text=True)
    _track(proc)
    proc.wait()
    list_path.unlink(missing_ok=True)
    if proc.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
        job["status"] = "completed"; job["progress"] = 100
        _store_library()
        return

    # ---- 阶段2：重新编码兜底（处理编码/分辨率/缺流不一致）----
    out_path.unlink(missing_ok=True)
    try:
        if all_audio:
            n = len(seg_paths)
            ins = []
            for p in seg_paths:
                ins += ["-i", str(p)]
            filt = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"
            cmd = [app.FFMPEG_BIN, "-y"] + ins + ["-filter_complex", filt, "-map", "[a]"] \
                  + AUDIO_REENCODE.get(out_format, AUDIO_REENCODE["mp3"]) + [str(out_path)]
        else:
            n = len(seg_paths)
            max_w = max([p["width"] for p in probes if p["has_video"]] or [1280])
            max_h = max([p["height"] for p in probes if p["has_video"]] or [720])
            ins = []
            for p in seg_paths:
                ins += ["-i", str(p)]
            extra = []   # 补足缺失流的 lavfi 源（黑画面 / 静音）
            vrefs = []; arefs = []
            for k, p in enumerate(seg_paths):
                st = probes[k]
                if st["has_video"]:
                    vrefs.append(f"[{k}:v]")
                else:
                    dur = max(0.1, st["duration"] or 1)
                    extra += ["-f", "lavfi", "-i", f"color=c=black:s={max_w}x{max_h}:r=25:d={dur}"]
                    vrefs.append(f"[{n + len(extra) - 1}:v]")
                if st["has_audio"]:
                    arefs.append(f"[{k}:a]")
                else:
                    dur = max(0.1, st["duration"] or 1)
                    extra += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100:d={dur}"]
                    arefs.append(f"[{n + len(extra) - 1}:a]")
            pairs = "".join(vrefs[i] + arefs[i] for i in range(n))
            filt = pairs + f"concat=n={n}:v=1:a=1[v][a]"
            cmd = [app.FFMPEG_BIN, "-y"] + ins + extra + [
                "-filter_complex", filt, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k", str(out_path)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                bufsize=0, text=True)
        _track(proc)
        proc.wait()
    except Exception as e:
        out_path.unlink(missing_ok=True)
        job["status"] = "failed"; job["error"] = f"拼接失败（转码兜底异常）：{e}"; return
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        job["status"] = "failed"
        job["error"] = "拼接失败：素材编码差异过大或文件损坏，请检查后重试"
        return
    job["status"] = "completed"; job["progress"] = 100
    _store_library()


def _create_concat_job(segs, out_format, out_name, to_library, audio_only, request):
    """为 /api/concat 与 /api/concat/local 共用的 job 创建逻辑。"""
    if len(segs) < 2:
        raise app.HTTPException(status_code=400, detail="至少需要 2 个片段")
    probes = [_probe_streams(p) for p in segs]
    all_audio = bool(audio_only) or (not any(p["has_video"] for p in probes))
    if all_audio and out_format not in AUDIO_REENCODE:
        out_format = "mp3"
    job_id = app.uuid.uuid4().hex[:12]
    ext = app.CONVERT_EXT.get(out_format, out_format)
    out_path = app.CONVERT_DIR / f"concat_{job_id}.{ext}"
    with app.CONVERT_LOCK:
        app.CONVERT_JOBS[job_id] = {
            "status": "running",
            "out_path": str(out_path),
            "error": "",
            "filename": out_path.name,
            "src_name": out_name or "merged",
            "target": out_format,
            "stage": "排队中",
            "to_library": to_library,
            "library_id": "",
            "device_id": _device_of(request),
            "audio": all_audio,
        }
    seg_names = [str(p) for p in segs]
    app.executor.submit(_run_concat, job_id, seg_names, out_format, out_name or "merged",
                        _device_of(request), to_library, all_audio)
    return {"job_id": job_id, "status": "running"}


@router.post("/api/concat")
def concat_api(payload: ConcatRequest, request: app.Request) -> dict:
    """视频/音频桥接：接收已落地的片段列表，按顺序合并为单个文件（无损优先，必要时转码兜底）。"""
    app._check_rate_limit(request)
    if payload.out_format not in app.CONVERT_TARGETS:
        raise app.HTTPException(status_code=400, detail="不支持的输出格式")
    segs = payload.segments or []
    seg_paths = []
    for name in segs:
        p = app.UPLOAD_TMP / name
        if not p.exists():
            raise app.HTTPException(status_code=400, detail=f"素材不存在或已过期：{name}")
        seg_paths.append(p)
    return _create_concat_job(seg_paths, payload.out_format, payload.out_name,
                                payload.to_library, payload.audio_only, request)


class LocalConcatRequest(app.BaseModel):
    segments: list[str]
    out_format: str = "mp4"
    out_name: str = "merged"
    to_library: bool = False
    audio_only: bool = False


@router.post("/api/concat/local")
def concat_local_api(payload: LocalConcatRequest, request: app.Request) -> dict:
    """桌面版专用：直接接收本机绝对路径列表，跳过分片上传，本地读取后合并。"""
    app._check_rate_limit(request)
    if payload.out_format not in app.CONVERT_TARGETS:
        raise app.HTTPException(status_code=400, detail="不支持的输出格式")
    segs = payload.segments or []
    seg_paths = []
    for path in segs:
        p = app.Path(path)
        if not p.is_file():
            raise app.HTTPException(status_code=400, detail=f"文件不存在或不是普通文件：{path}")
        # 安全校验：拒绝明显可疑的路径（如系统根目录、父目录穿越）
        try:
            resolved = p.resolve()
            if not str(resolved).startswith(("/Users/", "/home/", "/Volumes/", "C:\\\\")):
                raise app.HTTPException(status_code=400, detail=f"路径不在用户目录下：{path}")
        except Exception:
            raise app.HTTPException(status_code=400, detail=f"无法解析路径：{path}")
        seg_paths.append(resolved)
    return _create_concat_job(seg_paths, payload.out_format, payload.out_name,
                              payload.to_library, payload.audio_only, request)
