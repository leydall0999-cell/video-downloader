"""server/routers/convert.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.post("/api/convert")
def create_convert(payload: app.ConvertRequest, request: app.Request) -> dict:
    app._check_rate_limit(request)
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    task = app._require_task(payload.task_id)
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
        }
    app.executor.submit(app._run_convert, job_id, str(task.filepath), target, payload.resolution or "original")
    return {
        "job_id": job_id,
        "status": "running",
        "quota": {"subscribed": subscribed, "free_used": free_used, "free_daily": free_daily},
    }

@router.get("/api/convert/{job_id}")
def convert_status(job_id: str) -> dict:
    job = app.CONVERT_JOBS.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="转换任务不存在")
    return {"status": job["status"], "error": job.get("error", ""), "filename": job.get("filename", "")}

@router.get("/api/convert/{job_id}/file")
def convert_file(job_id: str) -> app.FileResponse:
    job = app.CONVERT_JOBS.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="转换任务不存在")
    if job["status"] != "completed":
        raise app.HTTPException(status_code=409, detail="转换尚未完成")
    out = app.Path(job["out_path"])
    if not out.exists():
        raise app.HTTPException(status_code=410, detail="转换文件已清理")
    return app.FileResponse(path=str(out), filename=out.name, media_type="application/octet-stream")
