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
