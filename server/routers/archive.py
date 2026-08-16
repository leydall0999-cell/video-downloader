"""server/routers/archive.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/archive/config")
def archive_config_get() -> dict:
    app._require_archive()
    cfg = app.archive_store.get()
    return {
        "config": cfg.to_dict(),
        "creds": app.archive_store.creds_masked(),
        "configured": app.archive_store.has_creds(cfg.provider),
        "providers": ["webdav"] + (["baidu"] if app.BAIDU_ENABLED else []),
        "tokens": app.archive_mod.TEMPLATE_TOKENS,
        "default_template": app.archive_mod.DEFAULT_TEMPLATE,
        "trash_available": app.retention_mod.trash_available(),
        "records": app.archive_store.records(30),
    }

@router.post("/api/archive/config")
def archive_config_set(req: app.ArchiveConfigRequest) -> dict:
    app._require_archive()
    data = req.model_dump()
    webdav = data.pop("webdav", None)
    baidu = data.pop("baidu", None)
    fields = {k: v for k, v in data.items() if v is not None}

    if fields.get("provider") and fields["provider"] not in ("webdav", "baidu"):
        raise app.HTTPException(status_code=400, detail="不支持的网盘类型")
    if fields.get("provider") == "baidu" and not app.BAIDU_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
    # 安全阀：没有可用回收站时不允许开「归档后删本地」，避免静默硬删用户资产
    if fields.get("delete_after") and not app.retention_mod.trash_available():
        raise app.HTTPException(status_code=400, detail="系统回收站不可用，无法开启「归档后删本地」（拒绝直接硬删）")

    if webdav is not None:
        url = (webdav.get("url") or "").strip()
        if url:
            try:
                app._assert_archive_url(url)
            except app.LinkError as exc:
                raise app.HTTPException(status_code=400, detail="WebDAV 地址不合法：" + exc.message)
        app.archive_store.set_creds("webdav", {
            "url": url,
            "user": (webdav.get("user") or "").strip(),
            "pass": webdav.get("pass") or "",
        })
    if baidu is not None:
        app.archive_store.set_creds("baidu", {"token": (baidu.get("token") or "").strip()})

    cfg = app.archive_store.update(**fields)
    return {
        "config": cfg.to_dict(),
        "creds": app.archive_store.creds_masked(),
        "configured": app.archive_store.has_creds(cfg.provider),
    }

@router.post("/api/archive/scan")
def archive_scan() -> dict:
    """只算不传：列出待归档文件与目标远端路径，前端必须先看这个再执行。"""
    app._require_archive()
    cfg = app.archive_store.get()
    items = app.library_mod.scan_library(app.DOWNLOAD_DIR)
    pend = app.archive_mod.pending_items(items, cfg, app.archive_store)
    return {
        "count": len(pend),
        "size": sum(p["size"] for p in pend),
        "size_text": app.archive_mod.human_size(sum(p["size"] for p in pend)),
        "items": pend[:200],
        "truncated": len(pend) > 200,
        "configured": app.archive_store.has_creds(cfg.provider),
        "provider": cfg.provider,
    }

@router.post("/api/archive/run")
def archive_run(req: app.ArchiveRunRequest) -> dict:
    app._require_archive()
    cfg = app.archive_store.get()
    upload_fn, creds = app._archive_provider(cfg)

    items = app.library_mod.scan_library(app.DOWNLOAD_DIR)
    pend = app.archive_mod.pending_items(items, cfg, app.archive_store)
    if req.lib_ids:
        wanted = set(req.lib_ids)
        pend = [p for p in pend if p["id"] in wanted]
    if not pend:
        raise app.HTTPException(status_code=409, detail="没有待归档的文件")

    job_id = app.uuid.uuid4().hex[:12]
    app._prune_archive_jobs()
    with app.ARCHIVE_LOCK:
        app.ARCHIVE_JOBS[job_id] = {
            "status": "running", "index": 0, "total": len(pend), "current": "",
            "file_percent": 0.0, "uploaded": 0, "failed": 0, "errors": [],
            "cancel": False, "started_at": int(app.time.time()),
        }
    app.cloud_executor.submit(app._run_archive_job, job_id, pend, cfg, upload_fn, creds)
    return {"job_id": job_id, "total": len(pend)}

@router.get("/api/archive/status/{job_id}")
def archive_status(job_id: str) -> dict:
    app._require_archive()
    with app.ARCHIVE_LOCK:
        job = app.ARCHIVE_JOBS.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="归档任务不存在")
    return {k: v for k, v in job.items() if k != "cancel"}

@router.post("/api/archive/cancel/{job_id}")
def archive_cancel(job_id: str) -> dict:
    app._require_archive()
    with app.ARCHIVE_LOCK:
        job = app.ARCHIVE_JOBS.get(job_id)
        if not job:
            raise app.HTTPException(status_code=404, detail="归档任务不存在")
        job["cancel"] = True
    return {"canceling": True}

@router.post("/api/archive/forget")
def archive_forget(req: app.ArchiveForgetRequest) -> dict:
    """清除归档记录，让文件下次重新上传（例如网盘那头被误删了）。"""
    app._require_archive()
    n = app.archive_store.forget(req.rel)
    return {"cleared": n}
