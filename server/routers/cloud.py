"""server/routers/cloud.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/cloud/providers")
def cloud_providers() -> dict:
    """列出本实例可用的云盘类型与百度授权地址。"""
    providers = ["webdav"]
    if app.BAIDU_ENABLED:
        providers.append("baidu")
    return {
        "providers": providers,
        "baidu_available": app.BAIDU_ENABLED,
        "baidu_auth_url": app.baidu_auth_url(app.BAIDU_REDIRECT_URI, app.BAIDU_APP_KEY, app_id=app.BAIDU_APP_ID) if app.BAIDU_ENABLED else "",
    }

@router.get("/api/cloud/baidu/auth_url")
def cloud_baidu_auth_url() -> dict:
    if not app.BAIDU_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
    state = app.secrets.token_urlsafe(16)
    now = app.time.time()
    with app._BAIDU_STATES_LOCK:
        app._BAIDU_STATES[state] = now + app._BAIDU_STATE_TTL
        # 顺手清理过期条目，避免长期运行堆积
        expired = [s for s, exp in app._BAIDU_STATES.items() if exp < now]
        for s in expired:
            app._BAIDU_STATES.pop(s, None)
    return {"auth_url": app.baidu_auth_url(app.BAIDU_REDIRECT_URI, app.BAIDU_APP_KEY, state, app_id=app.BAIDU_APP_ID)}

@router.get("/api/cloud/baidu/callback")
def cloud_baidu_callback(code: str = "", state: str = ""):
    """OAuth 回调：用 code 换取 access_token，返回把令牌回传给 opener 的页面（服务端不存令牌）。"""
    if not app.BAIDU_ENABLED:
        return app.HTMLResponse(app._baidu_callback_html(error="该实例未配置百度网盘凭据"))
    # state 校验：拒绝被诱导发起的授权（CSRF），过期/缺失/不匹配均拒绝
    with app._BAIDU_STATES_LOCK:
        exp = app._BAIDU_STATES.pop(state, None)
    if exp is None or app.time.time() > exp:
        return app.HTMLResponse(app._baidu_callback_html(error="授权状态校验失败，请重新点击授权"))
    try:
        token = app.baidu_exchange_token(code, app.BAIDU_REDIRECT_URI, app.BAIDU_APP_KEY, app.BAIDU_APP_SECRET)
    except app.CloudError as exc:
        return app.HTMLResponse(app._baidu_callback_html(error=exc.message))
    app.save_baidu_token(token)
    return app.HTMLResponse(app._baidu_callback_html(token=token.get("access_token", "")))

@router.get("/api/cloud/baidu/list")
def cloud_baidu_list(path: str = "/", token: str = ""):
    """浏览用户网盘目录：返回归一化文件列表（文件夹在前）。"""
    if not app.BAIDU_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
    if not token:
        raise app.HTTPException(status_code=400, detail="缺少 access_token（请先完成百度授权）")
    try:
        data = app._baidu_provider.list_files(token, path)
    except app.CloudError as exc:
        raise app.HTTPException(status_code=502, detail=exc.message + (("：" + exc.hint) if exc.hint else ""))
    items = data.get("list") or []
    files = [
        {
            "fs_id": it.get("fs_id"),
            "path": it.get("path"),
            "name": it.get("server_filename") or it.get("filename") or "",
            "size": it.get("size", 0),
            "isdir": bool(it.get("isdir")),
            "mtime": it.get("server_mtime", 0),
        }
        for it in items
    ]
    files.sort(key=lambda x: (not x["isdir"], -x["mtime"]))
    return {"path": path or "/", "list": files, "has_more": bool(data.get("has_more"))}

@router.post("/api/cloud/baidu/download")
def cloud_baidu_download(payload: app.BaiduDownloadRequest):
    """把网盘文件下载到本机 ~/Downloads/VideoDownloader/baidu/，后台线程跑，轮询进度。"""
    if not app.BAIDU_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
    token = (payload.token or "").strip()
    if not token or not payload.fs_id or not payload.path:
        raise app.HTTPException(status_code=400, detail="缺少 token / fs_id / path")
    name = app._baidu_safe_name(payload.name) or app._baidu_safe_name(payload.path)
    tid = app.secrets.token_hex(8)
    with app._baidu_dl_lock:
        app._baidu_dl_tasks[tid] = {
            "status": "pending", "progress": 0, "total": 0,
            "error": "", "name": name, "filepath": "",
        }

    def _worker() -> None:
        dest = app.DOWNLOAD_DIR / "baidu" / name
        try:
            with app._baidu_dl_lock:
                app._baidu_dl_tasks[tid].update(status="downloading")
            if dest.exists():
                # 同名文件加序号，避免覆盖已下好的
                stem = dest.stem
                suffix = dest.suffix
                i = 1
                while dest.exists():
                    dest = dest.with_name(f"{stem}({i}){suffix}")
                    i += 1

            def _prog(done: int, total: int) -> None:
                with app._baidu_dl_lock:
                    t = app._baidu_dl_tasks[tid]
                    t["progress"] = done
                    if total:
                        t["total"] = total

            app._baidu_provider.download(
                token, int(payload.fs_id), payload.path, dest,
                progress=_prog, backend=payload.backend or "auto",
            )
            with app._baidu_dl_lock:
                app._baidu_dl_tasks[tid].update(
                    status="completed",
                    progress=app._baidu_dl_tasks[tid]["total"] or app._baidu_dl_tasks[tid]["progress"],
                    filepath=str(dest),
                )
        except app.CloudError as exc:
            with app._baidu_dl_lock:
                app._baidu_dl_tasks[tid].update(
                    status="failed",
                    error=exc.message + (("：" + exc.hint) if exc.hint else ""),
                )
        except Exception as exc:  # noqa: BLE001 — 兜底，避免后台线程静默崩溃
            with app._baidu_dl_lock:
                app._baidu_dl_tasks[tid].update(status="failed", error=str(exc))

    app.threading.Thread(target=_worker, name=f"vdl-baidudl-{tid}", daemon=True).start()
    return {"task_id": tid, "name": name}

@router.get("/api/cloud/baidu/task/{tid}")
def cloud_baidu_task(tid: str):
    with app._baidu_dl_lock:
        t = app._baidu_dl_tasks.get(tid)
    if not t:
        raise app.HTTPException(status_code=404, detail="下载任务不存在")
    return t


@router.get("/api/cloud/baidu/token")
def cloud_baidu_token_get():
    if not app.BAIDU_ENABLED:
        return {"logged_in": False, "reason": "未配置百度网盘凭据"}
    data = app.load_baidu_token() or {}
    tok = data.get("access_token") or ""
    return {
        "logged_in": bool(tok),
        "access_token": tok,
        "expires_in": data.get("expires_in"),
        "scope": data.get("scope"),
    }

@router.post("/api/cloud/baidu/token")
def cloud_baidu_token_set(payload: app.BaiduTokenSet):
    tok = (payload.access_token or "").strip()
    if not tok:
        raise app.HTTPException(status_code=400, detail="缺少 access_token")
    app.save_baidu_token(payload.model_dump())
    return {"ok": True}

@router.delete("/api/cloud/baidu/token")
def cloud_baidu_token_del():
    app.clear_baidu_token()
    return {"ok": True}

@router.get("/api/cloud/baidu/qr/create")
def cloud_baidu_qr_create() -> dict:
    try:
        return app.baidu_qr_create()
    except app.CloudError as e:
        raise app.HTTPException(status_code=400, detail=str(e))

@router.get("/api/cloud/baidu/qr/poll")
def cloud_baidu_qr_poll(sign: str = "") -> dict:
    if not sign:
        raise app.HTTPException(status_code=400, detail="缺少 sign 参数")
    return app.baidu_qr_poll(sign)

@router.get("/api/cloud/baidu/qr/status")
def cloud_baidu_qr_status() -> dict:
    return app.baidu_qr_status()

@router.post("/api/cloud/save")
def cloud_save(payload: app.CloudSaveRequest, request: app.Request) -> dict:
    subscribed, free_used, free_daily = app._check_cloud_quota(request)
    task = app._require_task(payload.task_id)
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
    elif provider == "baidu":
        if not app.BAIDU_ENABLED:
            raise app.HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
        inst = app._baidu_provider
        creds = payload.baidu or {}
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
