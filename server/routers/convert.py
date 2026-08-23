"""server/routers/convert.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
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
    return app.FileResponse(path=str(out), filename=out.name, media_type="application/octet-stream")


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
    累计字节超 UPLOAD_MAX_BYTES 即 413 并清理该 upload 全部已传分片。"""
    app._check_rate_limit(request)
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
    request: app.Request = None,
) -> dict:
    """分片上传收尾：校验分片齐全 → 顺序合并 → 精确校验总大小 → 提交转码 job。"""
    app._check_rate_limit(request)
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    if not _UPLOAD_ID_RE.match(upload_id) or total <= 0:
        raise app.HTTPException(status_code=400, detail="分片参数非法")
    if target not in app.CONVERT_TARGETS:
        raise app.HTTPException(status_code=400, detail="不支持的目标格式")
    suffix = app.Path(filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in app.UPLOAD_VIDEO_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传视频文件")
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
