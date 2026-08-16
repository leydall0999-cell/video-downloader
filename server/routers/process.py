"""server/routers/process.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.post("/api/process/run")
def process_run(req: app.ProcessRequest) -> dict:
    if not (app.plat.is_desktop() or app.os.environ.get("VDL_LIBRARY_ENABLED")):
        raise app.HTTPException(status_code=403, detail="当前部署未启用本地加工功能")
    if req.op not in ("audio", "gif", "trim", "crop", "compress", "upscale",
                      "frame", "frames", "sheet", "ringtone", "dewatermark",
                      "ai_dewatermark"):
        raise app.HTTPException(status_code=400, detail="不支持的处理类型")

    # 解析来源：lib_ids 批量优先，否则单个 lib_id
    skipped = []
    if req.lib_ids:
        sources = []
        skipped = []
        for lid in req.lib_ids:
            if not lid or not lid.strip():
                continue
            p = app.library_mod._resolve_safe(app.DOWNLOAD_DIR, lid.strip())
            if not p or not p.is_file():
                skipped.append(lid)
                continue
            sources.append((lid.strip(), p))
        if not sources:
            raise app.HTTPException(status_code=400, detail="lib_ids 中没有有效文件")
    elif req.lib_id:
        p = app.library_mod._resolve_safe(app.DOWNLOAD_DIR, req.lib_id)
        if not p or not p.is_file():
            raise app.HTTPException(status_code=404, detail="源文件不存在")
        sources = [(req.lib_id, p)]
    else:
        raise app.HTTPException(status_code=400, detail="请提供 lib_id 或 lib_ids")

    import uuid as _uuid
    jobs_out = []
    for lid, src_path in sources:
        jid = _uuid.uuid4().hex[:12]
        name = src_path.name
        app.process_queue.submit(jid, name, lid, req.op, app._run_process, jid, str(src_path), req.op, req.params or {})
        jobs_out.append({"job_id": jid, "lib_id": lid, "name": name})

    if len(jobs_out) == 1 and not skipped:
        return {"job_id": jobs_out[0]["job_id"], "status": "running"}
    result = {"jobs": jobs_out, "total": len(jobs_out), "status": "queued"}
    if skipped:
        result["skipped"] = skipped
        result["skipped_count"] = len(skipped)
    return result

@router.get("/api/process/queue")
def process_queue_list() -> dict:
    if not (app.plat.is_desktop() or app.os.environ.get("VDL_LIBRARY_ENABLED")):
        raise app.HTTPException(status_code=403, detail="当前部署未启用本地加工功能")
    return app.process_queue.get_queue()

@router.post("/api/process/concurrency")
def process_set_concurrency(req: dict = None) -> dict:
    if not (app.plat.is_desktop() or app.os.environ.get("VDL_LIBRARY_ENABLED")):
        raise app.HTTPException(status_code=403, detail="当前部署未启用本地加工功能")
    n = int((req or {}).get("n", app.process_queue.concurrency))
    app.process_queue.set_concurrency(n)
    return {"concurrency": app.process_queue.concurrency}

@router.get("/api/process/{job_id}")
def process_status(job_id: str) -> dict:
    with app.process_queue.lock:
        job = app.process_queue.jobs.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="处理任务不存在")
    steps = job.get("steps") or []
    if not steps:
        steps = [{"name": "处理中", "status": "running" if job["status"] == "running" else "done",
                  "detail": "", "created_at": app.time.time(), "updated_at": app.time.time()}]
    return {"status": job["status"], "error": job.get("error", ""),
            "lib_id": job.get("lib_id", ""), "name": job.get("name", ""),
            "count": job.get("count", 0), "is_dir": job.get("is_dir", False),
            "steps": steps, "logs": job.get("logs", [])}
