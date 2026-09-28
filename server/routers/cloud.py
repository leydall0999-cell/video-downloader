"""server/routers/cloud.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter
from .core import _device_of

router = APIRouter()

@router.get("/api/cloud/providers")
def cloud_providers() -> dict:
    """列出本实例可用的云盘类型。"""
    return {"providers": ["webdav"]}

@router.post("/api/cloud/save")
def cloud_save(payload: app.CloudSaveRequest, request: app.Request) -> dict:
    subscribed, free_used, free_daily = app._check_cloud_quota(request)
    task = app._require_task(payload.task_id, _device_of(request))
    if task.status != "completed" or not task.filepath or not task.filepath.exists():
        raise app.HTTPException(status_code=409, detail="下载任务尚未完成，无法存到网盘")
    provider = payload.provider
    if provider == "webdav":
        inst = app._webdav_provider
        creds = payload.webdav or {}
        # SSRF 防护：拒绝指向内网 / 环回 / 云元数据的 WebDAV 地址，避免本服务被当跳板
        wurl = (creds.get("url") or "").strip()
        if wurl:
            try:
                app._assert_safe_url(wurl)
            except app.LinkError as exc:
                raise app.HTTPException(status_code=400, detail="WebDAV 地址不在允许范围内：" + exc.message)
    else:
        raise app.HTTPException(status_code=400, detail="不支持的网盘类型")
    job_id = app.uuid.uuid4().hex[:12]
    app._prune_cloud_jobs()
    with app.CLOUD_LOCK:
        app.CLOUD_JOBS[job_id] = {"status": "running", "error": "", "remote_path": "", "progress": 0.0}
    app.cloud_executor.submit(app._run_cloud, job_id, inst, str(task.filepath), payload.dest_path, creds)
    return {
        "job_id": job_id,
        "status": "running",
        "quota": {"subscribed": subscribed, "free_used": free_used, "free_daily": free_daily},
    }

@router.get("/api/cloud/status/{job_id}")
def cloud_status(job_id: str) -> dict:
    with app.CLOUD_LOCK:
        job = app.CLOUD_JOBS.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="云盘任务不存在")
    return {
        "status": job["status"],
        "error": job.get("error", ""),
        "remote_path": job.get("remote_path", ""),
        "progress": job.get("progress", 0.0),
    }
