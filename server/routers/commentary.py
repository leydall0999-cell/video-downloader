"""server/routers/commentary.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.post("/api/commentary")
def create_commentary(payload: app.CommentaryRequest) -> dict:
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        if not app.COMMENTARY_ENDPOINT:
            raise app.HTTPException(status_code=503, detail="解说 worker 未配置（VDL_COMMENTARY_MODE=http 但缺少 VDL_COMMENTARY_ENDPOINT）")
    else:
        if not app.COMMENTARY_DIR or not (app.COMMENTARY_DIR / "process.py").exists():
            raise app.HTTPException(status_code=503, detail="解说管线未配置（VDL_COMMENTARY_DIR 缺失或不含 process.py）")

    src_path = app._resolve_source(payload)

    job_id = app.uuid.uuid4().hex[:12]
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                   "steps": [], "logs": []}
    app.executor.submit(app._commentary_run, job_id, src_path, payload.vertical, payload.voice or app.COMMENTARY_VOICE,
                    trim_start=payload.trim_start, trim_end=payload.trim_end,
                    mode=payload.mode, commentary_type=payload.commentary_type,
                    highlight_source=payload.highlight_source,
                    intro_highlight=payload.intro_highlight,
                    skip_intro_outro=payload.skip_intro_outro,
                    no_narrate_intro_outro=payload.no_narrate_intro_outro,
                    retain_pct=payload.retain_pct, web=payload.web,
                    one_click=payload.one_click,
                    title=app._commentary_title(payload, src_path),
                    style=payload.style)
    return {"job_id": job_id, "status": "running"}

@router.post("/api/commentary/upload")
def create_commentary_upload(
    file: app.UploadFile = app._FastAPIFile(...),
    vertical: bool = app.Form(False),
    voice: str = app.Form(""),
    trim_start: float = app.Form(0.0),
    trim_end: float = app.Form(0.0),
    mode: str = app.Form("highlights"),
    title: str = app.Form(""),
) -> dict:
    """上传本地视频 → 直接生成解说成片。"""
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mkv", ".mov", ".webm", ".avi"}:
        raise app.HTTPException(status_code=409, detail="请上传视频文件")
    work_dir = app._commentary_work_dir()
    dest = work_dir / f"upload{suffix}"
    try:
        with dest.open("wb") as fh:
            app.shutil.copyfileobj(file.file, fh)
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()

    job_id = app.uuid.uuid4().hex[:12]
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                   "steps": [], "logs": []}
    app.executor.submit(app._commentary_run, job_id, str(dest), vertical, voice or app.COMMENTARY_VOICE,
                    trim_start=trim_start, trim_end=trim_end, mode=mode,
                    title=(title or app.Path(file.filename).stem if file.filename else ""))
    return {"job_id": job_id, "status": "running"}

@router.post("/api/commentary/script-only/upload")
def create_script_only_upload(
    file: app.UploadFile = app._FastAPIFile(...),
    vertical: bool = app.Form(False),
    voice: str = app.Form(""),
    trim_start: float = app.Form(0.0),
    trim_end: float = app.Form(0.0),
    mode: str = app.Form("highlights"),
    commentary_type: str = app.Form("deep_hl"),
    highlight_source: str = app.Form("ai"),
    intro_highlight: bool = app.Form(False),
    skip_intro_outro: bool = app.Form(False),
    no_narrate_intro_outro: bool = app.Form(True),
    retain_pct: float = app.Form(None),
    web: bool = app.Form(False),
    one_click: bool = app.Form(False),
    style: str = app.Form("none"),
    title: str = app.Form(""),
) -> dict:
    """上传本地视频 → 只生成脚本不渲染成片。"""
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        raise app.HTTPException(status_code=400, detail="脚本审核模式暂不支持 HTTP worker，请使用 local 模式")
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mkv", ".mov", ".webm", ".avi"}:
        raise app.HTTPException(status_code=409, detail="请上传视频文件")
    work_dir = app._commentary_work_dir()
    dest = work_dir / f"upload{suffix}"
    try:
        with dest.open("wb") as fh:
            app.shutil.copyfileobj(file.file, fh)
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()

    job_id = app.uuid.uuid4().hex[:12]
    src_path = str(dest)
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "script_path": "",
                                   "progress": [], "steps": [], "logs": [], "src_path": src_path}
    app.executor.submit(app._commentary_run, job_id, src_path, vertical, voice or app.COMMENTARY_VOICE, script_only=True,
                    trim_start=trim_start, trim_end=trim_end, mode=mode,
                    commentary_type=commentary_type, highlight_source=highlight_source,
                    intro_highlight=intro_highlight, skip_intro_outro=skip_intro_outro,
                    no_narrate_intro_outro=no_narrate_intro_outro,
                    retain_pct=retain_pct, web=web, one_click=one_click,
                    title=(title or app.Path(file.filename).stem if file.filename else ""),
                    style=style)
    return {"job_id": job_id, "status": "running"}

@router.get("/api/commentary/diagnostics")
def commentary_diagnostics() -> dict:
    """返回解说运行环境诊断信息，让桌面用户一眼看清 python / ffprobe 是否就绪。"""
    return {
        "enabled": app.COMMENTARY_ENABLED,
        "mode": app.COMMENTARY_MODE,
        "dir": str(app.COMMENTARY_DIR) if app.COMMENTARY_DIR else None,
        "python": app.COMMENTARY_RT.python,
        "python_ok": app.COMMENTARY_RT.python_ok,
        "deps_ok": app.COMMENTARY_RT.deps_ok,
        "ffmpeg_dir": app.COMMENTARY_RT.ffmpeg_dir,
        "ffprobe_ok": app.COMMENTARY_RT.ffprobe_ok,
        "ready": app.COMMENTARY_RT.ready(),
        "issues": app.COMMENTARY_RT.issues,
        "frozen": getattr(app.sys, "frozen", False),
    }

@router.get("/api/commentary/list")
def commentary_list() -> dict:
    """按修改时间倒序列出所有已生成的解说成片。"""
    items = []
    seen: set[str] = set()
    for root in app._commentary_roots():
        for p in root.iterdir():
            if not p.is_file() or p.suffix.lower() not in (".mp4", ".mkv", ".mov", ".webm"):
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            cid = app.base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii")
            items.append({
                "id": cid,
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": int(p.stat().st_mtime),
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items}

@router.get("/api/commentary/file/{cid}")
def commentary_file_by_id(cid: str) -> app.FileResponse:
    p = app._decode_commentary_id(cid)
    return app.FileResponse(str(p), filename=p.name, media_type="video/mp4")

@router.delete("/api/commentary/file/{cid}")
def commentary_delete_by_id(cid: str) -> dict:
    """把已生成的解说成片移入系统回收站，拒绝直接硬删用户资产。"""
    p = app._decode_commentary_id(cid)
    allowed_roots = app._commentary_roots()
    if not allowed_roots:
        raise app.HTTPException(status_code=503, detail="解说输出目录未配置")
    try:
        resolved = p.resolve()
    except Exception:
        resolved = p
    in_allowed_root = any(
        resolved == root.resolve() or root.resolve() in resolved.parents
        for root in allowed_roots
    )
    if not in_allowed_root:
        raise app.HTTPException(status_code=403, detail="文件路径不在解说输出目录内")
    if not resolved.is_file():
        raise app.HTTPException(status_code=404, detail="成片文件不存在")
    if not app.retention_mod.trash_available():
        raise app.HTTPException(status_code=503, detail="系统回收站不可用，拒绝直接删除")
    # 先记录大小：移动成片到回收站后原路径已不存在，再 stat 会抛异常导致 500。
    try:
        file_size = resolved.stat().st_size
    except Exception:
        file_size = 0
    if not app.retention_mod.move_to_trash(resolved):
        raise app.HTTPException(status_code=500, detail="移入回收站失败")
    return {"deleted": True, "trashed": True, "name": p.name, "size": file_size}

@router.put("/api/commentary/file/{cid}")
def commentary_rename_by_id(cid: str, payload: app.CommentaryRenameReq) -> dict:
    """重命名已生成的解说成片（仅改文件名，不移动目录，保留原扩展名）。"""
    p = app._decode_commentary_id(cid)
    allowed_roots = app._commentary_roots()
    if not allowed_roots:
        raise app.HTTPException(status_code=503, detail="解说输出目录未配置")
    resolved = p.resolve()
    in_allowed_root = any(
        resolved == root.resolve() or root.resolve() in resolved.parents
        for root in allowed_roots
    )
    if not in_allowed_root:
        raise app.HTTPException(status_code=403, detail="文件路径不在解说输出目录内")
    if not resolved.is_file():
        raise app.HTTPException(status_code=404, detail="成片文件不存在")

    new_name = (payload.name or "").strip()
    if not new_name:
        raise app.HTTPException(status_code=400, detail="新文件名不能为空")
    # 去掉可能用于路径穿越的字符
    new_name = new_name.replace("/", "_").replace("\\", "_").replace("..", "_").strip()
    if not new_name:
        raise app.HTTPException(status_code=400, detail="新文件名非法")
    # 保留原扩展名（若新名未带扩展名）
    if "." not in new_name:
        new_name = new_name + p.suffix
    # 防止覆盖已有文件：重名自动追加 (1)/(2)…
    dest = resolved.parent / new_name
    n = 1
    while dest.exists():
        stem, suffix = new_name.rsplit(".", 1) if "." in new_name else (new_name, "")
        dest = resolved.parent / f"{stem} ({n}){('.' + suffix) if suffix else ''}"
        n += 1
    try:
        resolved.rename(dest)
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"重命名失败：{e}")
    new_cid = app.base64.urlsafe_b64encode(str(dest.resolve()).encode("utf-8")).decode("ascii")
    return {"renamed": True, "id": new_cid, "name": dest.name}

@router.post("/api/commentary/file/{cid}/save")
def commentary_save_to_downloads(cid: str) -> dict:
    """把已生成的解说成片复制到「下载」文件夹。

    桌面版 WebView 的 <a download> 在 cocoa/WKWebView 下不会触发本机保存，
    而 pywebview 原生 Api 也未暴露 save_commentary_file 桥接；因此由本地
    FastAPI 后端（与 app 同机同用户运行，有权限写 ~/Downloads）直接复制文件，
    前端点击「下载」时调用本接口即可真正把成片落到下载目录。
    """
    p = app._decode_commentary_id(cid)
    if not p.is_file():
        raise app.HTTPException(status_code=404, detail="成片文件不存在")
    downloads = app.Path.home() / "Downloads"
    try:
        downloads.mkdir(parents=True, exist_ok=True)
    except Exception:
        raise app.HTTPException(status_code=500, detail="无法访问下载文件夹")
    # 避免覆盖已有同名文件：dst 已存在则追加 (1)/(2)…
    stem, suffix = p.stem, p.suffix
    dst = downloads / p.name
    n = 1
    while dst.exists():
        dst = downloads / f"{stem} ({n}){suffix}"
        n += 1
    try:
        app.shutil.copy2(str(p), str(dst))
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"复制失败：{e}")
    return {"saved": True, "path": str(dst), "name": dst.name}

@router.get("/api/commentary/{job_id}")
def commentary_status(job_id: str) -> dict:
    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="解说任务不存在或已过期")
    # script_ready 也算就绪态（脚本已生成，等待人工确认后渲染）
    ready = job["status"] in ("completed", "script_ready")
    steps = job.get("steps") or []
    if not steps:
        # 旧任务或 HTTP 模式可能还没初始化步骤，兜底生成一个简单时间线
        steps = [{"name": "处理中", "status": "running" if job["status"] == "running" else "done",
                  "detail": "", "created_at": app.time.time(), "updated_at": app.time.time()}]
    result = {"job_id": job_id, "status": job["status"], "error": job.get("error", ""),
              "ready": ready, "progress": job.get("progress", []),
              "steps": steps, "logs": job.get("logs", []),
              "eta_remaining": job.get("eta_remaining"),
              "eta_done_at": job.get("eta_done_at"),
              "started_at": job.get("started_at"),
              "source_duration": job.get("source_duration")}
    if job.get("script_path"):
        result["script_path"] = job["script_path"]
    return result

@router.get("/api/commentary/{job_id}/file")
def commentary_file(job_id: str) -> app.FileResponse:
    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if job and job["status"] == "completed" and job.get("output_path"):
        path = app.Path(job["output_path"])
        if path.exists():
            return app.FileResponse(path=str(path), filename=path.name, media_type="application/octet-stream")
        # 任务标记完成但成片文件已丢失/被清理 → 410 Gone，不应再走 cid 解码分支（否则误报 409）
        raise app.HTTPException(status_code=410, detail="成片文件已清理或丢失")
    # 兼容「已生成成片列表」卡片的 cid（base64 编码的文件路径标识）：
    # 桌面版桥接 save_commentary_file(cid) 会请求 /api/commentary/{cid}/file，
    # 命中本路由；此处按 cid 解码并校验后直接返回文件，使列表卡片也能下载。
    try:
        p = app._decode_commentary_id(job_id)
    except Exception:
        raise app.HTTPException(status_code=409, detail="成片尚未就绪或标识无效")
    return app.FileResponse(str(p), filename=p.name, media_type="application/octet-stream")

@router.post("/api/commentary/script-only")
def create_script_only(payload: app.CommentaryRequest) -> dict:
    """只做转写+解说词生成，不渲染成片。返回 job_id 供前端轮询，
    拿到 script.json 后展示可编辑解说词面板。"""
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        if not app.COMMENTARY_ENDPOINT:
            raise app.HTTPException(status_code=503, detail="解说 worker 未配置")
        raise app.HTTPException(status_code=400, detail="脚本审核模式暂不支持 HTTP worker，请使用 local 模式")

    src_path = app._resolve_source(payload)
    job_id = app.uuid.uuid4().hex[:12]
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "script_path": "",
                                   "progress": [], "src_path": src_path}
    app.executor.submit(app._commentary_run, job_id, src_path, payload.vertical, payload.voice or app.COMMENTARY_VOICE,
                    script_only=True, trim_start=payload.trim_start, trim_end=payload.trim_end,
                    mode=payload.mode, commentary_type=payload.commentary_type,
                    highlight_source=payload.highlight_source,
                    intro_highlight=payload.intro_highlight,
                    skip_intro_outro=payload.skip_intro_outro,
                    no_narrate_intro_outro=payload.no_narrate_intro_outro,
                    retain_pct=payload.retain_pct, web=payload.web,
                    one_click=payload.one_click,
                    title=app._commentary_title(payload, src_path),
                    style=payload.style)
    return {"job_id": job_id, "status": "running"}

@router.get("/api/commentary/script/{job_id}")
def get_script(job_id: str) -> dict:
    """获取已生成脚本文件内容（script.json）。"""
    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="任务不存在或已过期")
    if job["status"] != "script_ready" or not job.get("script_path"):
        raise app.HTTPException(status_code=409, detail="脚本尚未就绪（当前状态: " + job["status"] + "）")
    script_path = app.Path(job["script_path"])
    if not script_path.exists():
        raise app.HTTPException(status_code=410, detail="脚本文件已被清理")
    try:
        data = app.json.loads(script_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"读取脚本文件失败：{e}")
    return {"job_id": job_id, "title": data.get("title", ""),
            "voice": data.get("voice", ""),
            "segments": data.get("segments", []), "segment_count": len(data.get("segments", []))}

@router.put("/api/commentary/script/{job_id}")
def update_script(job_id: str, payload: app.ScriptUpdateRequest) -> dict:
    """人工修改后提交更新脚本（写回 script.json）。
    保留原始时间戳（前端发 start/end=0），只更新 narration / voice。
    """
    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="任务不存在或已过期")
    if job["status"] != "script_ready" or not job.get("script_path"):
        raise app.HTTPException(status_code=409, detail="脚本尚未就绪，无法修改（当前状态: " + job["status"] + "）")
    script_path = app.Path(job["script_path"])

    # 读现有脚本以保留时间戳
    try:
        existing = app.json.loads(script_path.read_text(encoding="utf-8"))
    except Exception:
        existing = {}
    existing_segs = existing.get("segments", [])

    # 合并：payload 里每个 seg 的 narration 写回对应 idx 的原始 seg
    merged = []
    for i, pseg in enumerate(payload.segments):
        orig = existing_segs[i] if i < len(existing_segs) else {}
        merged.append({
            "start": orig.get("start", pseg.get("start", 0)),
            "end": orig.get("end", pseg.get("end", 0)),
            "narration": pseg.get("narration", orig.get("narration", "")),
            "note": pseg.get("note", orig.get("note", "")),
        })

    data = {
        "title": payload.title or existing.get("title", ""),
        "voice": payload.voice or existing.get("voice", job.get("voice", "")),
        "segments": merged,
        # 保留原始 mode（如高光解说模式），否则保存后再渲染会丢失高光标记
        "mode": existing.get("mode", ""),
        # 保留原始 options（剪辑选项：解说类型/高光来源/片头高光/联网/保留时长等），
        # 否则审核后渲染会丢失用户在面板上的剪辑选择
        "options": existing.get("options", {}),
    }
    try:
        script_path.write_text(app.json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"写入脚本失败：{e}")

    # 更新内存中的 voice 偏好（渲染时会用到）
    if payload.voice:
        with app._commentary_lock:
            app.commentary_jobs[job_id]["voice"] = payload.voice

    return {"job_id": job_id, "status": "updated", "segment_count": len(payload.segments)}

@router.post("/api/commentary/render/{job_id}")
def render_script(job_id: str, vertical: bool = app.Form(False), voice: str = app.Form("")) -> dict:
    """用已审核的脚本渲染成片（process.py --edit-only）。

    剪辑选项直接沿用 script.json 中已保存的 options（生成脚本时写入、人工审核时可改），
    避免用默认值覆盖用户当初的选择（例如一键生成的全片深入+联网会被 deep_hl 默认值冲掉）。
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        raise app.HTTPException(status_code=400, detail="脚本渲染暂不支持 HTTP worker 模式，请使用 local 模式")

    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="任务不存在或已过期")
    if job["status"] != "script_ready" or not job.get("script_path"):
        raise app.HTTPException(status_code=409, detail="请先生成脚本再渲染（当前状态: " + job["status"] + "）")
    script_path = job["script_path"]
    # 从 script.json 的 segments 里找回原始视频名 + 已保存的剪辑选项
    try:
        seg_data = app.json.loads(app.Path(script_path).read_text(encoding="utf-8"))
        title = seg_data.get("title", "")
        saved = seg_data.get("options") or {}
    except Exception:
        title = ""
        saved = {}

    commentary_type = saved.get("commentary_type", "deep_hl")
    highlight_source = saved.get("highlight_source", "ai")
    intro_highlight = bool(saved.get("intro_highlight", False))
    skip_intro_outro = bool(saved.get("skip_intro_outro", False))
    no_narrate_intro_outro = bool(saved.get("no_narrate_intro_outro", True))
    retain_pct = saved.get("retain_pct")
    web = bool(saved.get("web", False))
    one_click = bool(saved.get("one_click", False))

    # 反查原始视频路径：优先用脚本任务记录的 src_path，避免按 title/job_id 在 input 目录里猜错。
    src_path = job.get("src_path")
    if not src_path:
        # 兼容旧任务：按 title 或 job_id 在 input 目录搜索（已不推荐）
        base_name = title or job_id
        in_dir = app._commentary_root("input")
        src_candidates = list(in_dir.glob(f"{base_name}.*")) or list(in_dir.glob(f"{job_id}.*"))
        if not src_candidates:
            raise app.HTTPException(status_code=404, detail=f"找不到原始视频文件（input/{base_name}.* 或 input/{job_id}.*）")
        src_path = str(src_candidates[0])

    # 复用父任务的裁剪参数：同一源+起止会命中确定性命名的裁剪文件，直接吃裁剪后视频渲染
    trim_start = float(job.get("trim_start", 0.0) or 0.0)
    trim_end = float(job.get("trim_end", 0.0) or 0.0)

    # 用新的 job_id 提交渲染（保留原 script 关联）
    render_job_id = app.uuid.uuid4().hex[:12]
    with app._commentary_lock:
        app.commentary_jobs[render_job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                          "parent_script_job": job_id, "steps": [], "logs": []}
    v = voice or job.get("voice", "") or app.COMMENTARY_VOICE
    app.executor.submit(app._commentary_run, render_job_id, src_path, vertical, v, edit_only=script_path,
                    trim_start=trim_start, trim_end=trim_end,
                    commentary_type=commentary_type, highlight_source=highlight_source,
                    intro_highlight=intro_highlight, skip_intro_outro=skip_intro_outro,
                    no_narrate_intro_outro=no_narrate_intro_outro,
                    retain_pct=retain_pct, web=web, one_click=one_click)
    return {"job_id": render_job_id, "status": "running", "script_job": job_id}

@router.post("/api/commentary/voice-preview")
def voice_preview(
    voice: str = app.Form(...),
    text: str = app.Form("你好，我是视频解说员。我将为你解说这段视频。"),
) -> app.FileResponse:
    """配音试听：把指定文本用指定 voice 转成 mp3 返回给前端播放。"""
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        raise app.HTTPException(status_code=400, detail="配音试听需在 local 模式使用")
    # 1. 先校验输入（不等 COMMENTARY_DIR 挂掉）
    voice = (voice or "").strip()
    if not voice.startswith("zh-"):
        raise app.HTTPException(status_code=400, detail=f"voice 必须是 zh-CN-* 音色，当前: {voice}")
    # FastAPI Form() 有 bug：空字符串会落回默认值（即使前端显式发 text=），所以加 fallback
    text = (text or "").strip()[:500] if text else ""
    if not text:
        text = "你好，我是视频解说员。我将为你解说这段视频。"
    # 2. 再检查资源可用性
    if not app.COMMENTARY_DIR or not (app.COMMENTARY_DIR / "scripts" / "voice_preview.py").exists():
        raise app.HTTPException(status_code=503, detail="解说管线未配置（VDL_COMMENTARY_DIR 缺失或不含 voice_preview.py）")

    out_dir = app._commentary_root("work") / "voice_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{app.uuid.uuid4().hex[:12]}.mp3"
    try:
        app._run_voice_preview(text, voice, out_path, timeout=45)
    except RuntimeError as e:
        raise app.HTTPException(status_code=500, detail=f"试听生成失败：{e}")
    # 加过期清理保护：定时任务会清理 work/voice_preview/ 下超过 1 天的文件（用户本机 .cleanup）
    return app.FileResponse(path=str(out_path), filename="preview.mp3", media_type="audio/mpeg")

@router.post("/api/commentary/preview/{job_id}")
def preview_segments(
    job_id: str,
    voice: str = app.Form(""),
    max_segments: int = app.Form(3),
) -> app.FileResponse:
    """用当前 voice 朗读 script.json 里前 N 段的 narration，拼接成一段 mp3 返回。
    主要给「预览全部」按钮用——按全脚本生成太长，前 3 段够判断音色和节奏。
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        raise app.HTTPException(status_code=400, detail="预览需在 local 模式使用")
    # 1. 先校验 job 状态（不等资源检查）
    with app._commentary_lock:
        job = app.commentary_jobs.get(job_id)
    if not job or job["status"] != "script_ready" or not job.get("script_path"):
        raise app.HTTPException(status_code=409, detail="请先生成脚本再预览（当前状态: " + (job or {}).get("status", "missing") + "）")
    script_path = app.Path(job["script_path"])
    if not script_path.exists():
        raise app.HTTPException(status_code=410, detail="脚本文件已被清理")
    try:
        data = app.json.loads(script_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"读取脚本失败：{e}")
    segs = [s for s in (data.get("segments") or []) if (s.get("narration") or "").strip()]
    if not segs:
        raise app.HTTPException(status_code=409, detail="脚本里没有可朗读的 narration")
    n = max(1, min(int(max_segments), len(segs), 6))
    chosen = segs[:n]
    v = (voice or "").strip() or data.get("voice") or job.get("voice", "") or app.COMMENTARY_VOICE
    if not v.startswith("zh-"):
        raise app.HTTPException(status_code=400, detail=f"voice 必须是 zh-CN-* 音色")
    # 2. 再检查资源可用性
    if not app.COMMENTARY_DIR or not (app.COMMENTARY_DIR / "scripts" / "voice_preview.py").exists():
        raise app.HTTPException(status_code=503, detail="解说管线未配置（VDL_COMMENTARY_DIR 缺失或不含 voice_preview.py）")

    out_dir = app._commentary_root("work") / "voice_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / app.uuid.uuid4().hex[:12]
    tmp_dir.mkdir(parents=True, exist_ok=True)
    final_mp3 = tmp_dir / "preview.mp3"
    # 逐段生成
    try:
        clips = []
        for i, seg in enumerate(chosen, 1):
            seg_mp3 = tmp_dir / f"seg{i:02d}.mp3"
            narration = (seg.get("narration") or "").strip()
            if not narration:
                continue
            try:
                app._run_voice_preview(narration, v, seg_mp3, timeout=45)
            except RuntimeError as e:
                raise RuntimeError(f"第 {i} 段朗读失败：{e}")
            clips.append(seg_mp3)
        if not clips:
            raise RuntimeError("没有可用 narration 可朗读")
        # 用 ffmpeg concat demuxer 拼接
        list_file = tmp_dir / "concat.txt"
        list_file.write_text("\n".join(f"file '{p.name}'" for p in clips), encoding="utf-8")
        concat_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                      "-i", str(list_file), "-c", "copy", str(final_mp3)]
        proc = app.subprocess.run(concat_cmd, cwd=str(tmp_dir), capture_output=True, text=True, timeout=60,
                              env={"PATH": app.COMMENTARY_RT.ffmpeg_dir + app.os.pathsep + app.os.environ.get("PATH", "")})
        if proc.returncode != 0 or not final_mp3.exists():
            # concat 失败时退到单段：直接把第一段作为 preview（保证有声音）
            try:
                app.shutil.copyfile(clips[0], final_mp3)
            except Exception as e:
                raise RuntimeError(f"拼接失败且回退也失败：{e}; 原 stderr: {(proc.stderr or '')[:200]}")
    except RuntimeError as e:
        # 清理临时目录（保留 final_mp3 不存在路径下不会报错）
        try: app.shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception: pass
        raise app.HTTPException(status_code=500, detail=f"预览生成失败：{e}")
    # 返回临时文件，让客户端下载/播放
    return app.FileResponse(path=str(final_mp3), filename="preview.mp3", media_type="audio/mpeg")
