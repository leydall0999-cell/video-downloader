"""server/routers/commentary.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
import os
import sys
import subprocess
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/commentary/config")
def commentary_config_get() -> dict:
    """返回当前解说(配音/音量)设置，供前端面板回填。"""
    from commentary_config import get_commentary_config
    return get_commentary_config()


@router.get("/api/commentary/tts-status")
def commentary_tts_status() -> dict:
    """返回 TTS 引擎相关的本机配置/就绪状态，供前端动态推荐。

    - platform: darwin / win32 / linux
    - apple_silicon: 是否为 Apple Silicon Mac（MLX 仅在此推荐）
    - indextts_mlx_ready: 127.0.0.1:7866 是否可连接
    - minimax_configured / siliconflow_configured: 是否已配置 API Key
    """
    import json
    import os
    import platform as _platform
    import socket
    import sys

    _plat = sys.platform
    machine = _platform.machine().lower()
    apple_silicon = (_plat == "darwin") and (machine.startswith("arm") or "aarch64" in machine)

    indextts_mlx_ready = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.6)
            s.connect(("127.0.0.1", 7866))
            indextts_mlx_ready = True
    except Exception:
        indextts_mlx_ready = False

    tts_cfg: dict = {}
    try:
        tts_path = os.path.join(os.path.expanduser("~"), ".video-downloader", "tts_config.json")
        if os.path.exists(tts_path):
            with open(tts_path, encoding="utf-8") as f:
                tts_cfg = json.load(f)
    except Exception:
        tts_cfg = {}

    minimax_key = (os.environ.get("MINIMAX_API_KEY") or tts_cfg.get("minimax_api_key") or "").strip()
    siliconflow_key = (os.environ.get("SILICONFLOW_API_KEY") or tts_cfg.get("api_key") or "").strip()

    return {
        "platform": _plat,
        "machine": machine,
        "apple_silicon": apple_silicon,
        "indextts_mlx_ready": indextts_mlx_ready,
        "minimax_configured": bool(minimax_key),
        "siliconflow_configured": bool(siliconflow_key),
    }


@router.post("/api/commentary/stash")
async def create_commentary_stash(
    file: app.UploadFile = app._FastAPIFile(...),
) -> dict:
    """上传本地视频到本机「接收站」(cache-by-hash)：按 sha256(前 16 位) 去重，
    相同内容 0 字节传输。返回 {id: "stash:<sha>", size, from_cache}。

    前端拿到 id 后用 JSON 提交（不再 multipart），worker 通过 _resolve_source
    识别 stash: 前缀 → 取真路径 → 跑流水线。"""
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in app._STASH_VIDEO_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传视频文件")
    name = (file.filename or "").strip() or "upload.mp4"
    sha = app.hashlib.sha256()
    # 流式落盘到 .part 文件：同名 dest 已存在就提前 return(from_cache)
    # 我们故意先把 dest 路径算出来，存在=可能命中（按大小兜底校验）
    try:
        await file.seek(0)
    except Exception:
        pass
    # 同一进程可能并发多份 stash 文件，每个 stash 用独立临时名（按 hash 占位）
    placeholder_ext = suffix
    tmp = app.COMMENTARY_STASH_DIR / f".part-{app.uuid.uuid4().hex[:12]}{placeholder_ext}"
    try:
        with tmp.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                sha.update(chunk)
                fh.write(chunk)
    finally:
        try:
            await file.close()
        except Exception:
            pass
    sha_hex = sha.hexdigest()[:16]
    dest = app._stash_path_for(sha_hex, placeholder_ext)
    if dest.exists():
        # 内容哈希已落盘，删临时文件
        try:
            tmp.unlink()
        except OSError:
            pass
        from_cache = True
    else:
        try:
            tmp.rename(dest)
        except OSError:
            # 极端情况：另一进程刚刚才落盘——以 dest 为准
            tmp.unlink(missing_ok=True)
        from_cache = False
    stash_id = app._stash_register(sha_hex, placeholder_ext, name)
    return {
        "id": stash_id,
        "size": dest.stat().st_size if dest.exists() else 0,
        "name": name,
        "from_cache": from_cache,
    }


@router.post("/api/commentary/config")
def commentary_config_save(req: app.CommentaryConfigRequest) -> dict:
    """保存解说(配音/音量)手动可调设置，写入 commentary_config.json。"""
    from commentary_config import save_commentary_config
    normalized = save_commentary_config({
        "narration_loudness": req.narration_loudness,
        "original_duck": req.original_duck,
        "narration_boost": req.narration_boost,
    })
    return {"ok": True, "config": normalized}


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
    _title = app._commentary_title(payload, src_path)
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                   "steps": [], "logs": [], "src_path": src_path, "title": _title}
    app.executor.submit(app._commentary_run, job_id, src_path, payload.vertical, payload.voice or app.COMMENTARY_VOICE,
                    trim_start=payload.trim_start, trim_end=payload.trim_end,
                    mode=payload.mode, commentary_type=payload.commentary_type,
                    highlight_source=payload.highlight_source,
                    intro_highlight=payload.intro_highlight,
                    skip_intro_outro=payload.skip_intro_outro,
                    no_narrate_intro_outro=payload.no_narrate_intro_outro,
                    retain_pct=payload.retain_pct, web=payload.web,
                    vision=payload.vision, tts_provider=payload.tts_provider,
                    correct_transcript=payload.correct_transcript,
                    intro_sec=payload.intro_sec, outro_sec=payload.outro_sec,
                    drama_start_sec=payload.drama_start_sec, drama_end_sec=payload.drama_end_sec,
                    one_click=payload.one_click,
                    title=_title,
                    style=payload.style,
                    bgm=payload.bgm, bgm_file=payload.bgm_file, bgm_volume=payload.bgm_volume,
                    subtitle_size=payload.subtitle_size, subtitle_color=payload.subtitle_color,
                    subtitle_border=payload.subtitle_border, subtitle_pos=payload.subtitle_pos,
                    max_chars=payload.max_chars,
                    export_jianying=payload.export_jianying)
    return {"job_id": job_id, "status": "running"}

@router.post("/api/commentary/upload")
def create_commentary_upload(
    file: app.UploadFile = app._FastAPIFile(...),
    vertical: bool = app.Form(False),
    voice: str = app.Form(""),
    trim_start: float = app.Form(0.0),
    trim_end: float = app.Form(0.0),
    mode: str = app.Form("highlights"),
    vision: bool = app.Form(False),
    tts_provider: str = app.Form(""),
    correct_transcript: str = app.Form(""),
    title: str = app.Form(""),
    intro_sec: float = app.Form(None),
    outro_sec: float = app.Form(None),
    drama_start_sec: float = app.Form(None),
    drama_end_sec: float = app.Form(None),
    export_jianying: str = app.Form(""),
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
    # 片名前缀优先级（用户 2026-08-25 明确）：显式 title → 上传前的名字(有意义时)
    # → ffprobe 读 mp4 自带标题 → v<6hex> 短码。
    # 保证上传《少帅.mp4》→ 片名「少帅」；源文件叫 upload.mp4 这种无语义名不会上片名。
    stem = app.Path(file.filename).stem if file.filename else ""
    final_title = (title
                   or (stem if app._meaningful_stem(stem) else "")
                   or app._probe_video_title(dest)
                   or "")
    final_title = (final_title or "").strip()
    if not final_title:
        import secrets as _secrets
        final_title = "v" + _secrets.token_hex(3)
    # 保留用户上传时的原始文件名，UI「源视频: ...」优先展示它而不是 upload.mp4 这种占位名
    src_filename = (file.filename or "").strip() or app.Path(file.filename or "upload.mp4").name
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                   "steps": [], "logs": [], "src_path": str(dest), "title": final_title,
                                   "src_filename": src_filename}
    app.executor.submit(app._commentary_run, job_id, str(dest), vertical, voice or app.COMMENTARY_VOICE,
                    trim_start=trim_start, trim_end=trim_end, mode=mode,
                    vision=vision, tts_provider=tts_provider,
                    correct_transcript=correct_transcript,
                    intro_sec=intro_sec, outro_sec=outro_sec,
                    drama_start_sec=drama_start_sec, drama_end_sec=drama_end_sec,
                    title=final_title,
                    src_filename=src_filename,
                    export_jianying=export_jianying)
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
    intro_sec: float = app.Form(None),
    outro_sec: float = app.Form(None),
    drama_start_sec: float = app.Form(None),
    drama_end_sec: float = app.Form(None),
    retain_pct: float = app.Form(None),
    web: bool = app.Form(False),
    one_click: bool = app.Form(False),
    style: str = app.Form("none"),
    vision: bool = app.Form(False),
    tts_provider: str = app.Form(""),
    correct_transcript: str = app.Form(""),
    title: str = app.Form(""),
    export_jianying: str = app.Form(""),
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
    # 片名前缀优先级（用户 2026-08-25 明确）：显式 title → 上传前的名字(有意义时)
    # → ffprobe 读 mp4 自带标题 → v<6hex> 短码。
    # 保证上传《少帅.mp4》→ 片名「少帅」；源文件叫 upload.mp4 这种无语义名不会上片名。
    stem = app.Path(file.filename).stem if file.filename else ""
    final_title = (title
                   or (stem if app._meaningful_stem(stem) else "")
                   or app._probe_video_title(dest)
                   or "")
    final_title = (final_title or "").strip()
    if not final_title:
        import secrets as _secrets
        final_title = "v" + _secrets.token_hex(3)  # e.g. v3a8f1b
    # 保留用户上传时的原始文件名，UI「源视频: ...」优先展示它而不是 upload.mp4 这种占位名
    src_filename = (file.filename or "").strip() or app.Path(file.filename or "upload.mp4").name
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "script_path": "",
                                   "progress": [], "steps": [], "logs": [], "src_path": src_path,
                                   "title": final_title, "src_filename": src_filename}
    app.executor.submit(app._commentary_run, job_id, src_path, vertical, voice or app.COMMENTARY_VOICE, script_only=True,
                    trim_start=trim_start, trim_end=trim_end, mode=mode,
                    commentary_type=commentary_type, highlight_source=highlight_source,
                    intro_highlight=intro_highlight, skip_intro_outro=skip_intro_outro,
                    no_narrate_intro_outro=no_narrate_intro_outro,
                    retain_pct=retain_pct, web=web, one_click=one_click,
                    vision=vision, tts_provider=tts_provider,
                    correct_transcript=correct_transcript,
                    intro_sec=intro_sec, outro_sec=outro_sec,
                    drama_start_sec=drama_start_sec, drama_end_sec=drama_end_sec,
                    title=final_title,
                    style=style,
                    src_filename=src_filename,
                    export_jianying=export_jianying)
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
    """按修改时间倒序列出所有已生成的解说成片。

    每条返回：
    - id / name / size / mtime：成片元数据
    - bgm_state: {bgm, volume, source, ts}（manifest 缺失视为空 → 走默认 off）
    - bgm_previews: ["soft","light","epic"] 的子集 — 仅返回实际存在的预览 mp3
      （build() 渲染时已落 sidecar；旧版渲染可能为空）
    前端按 bgm_state 拆 draft/final；预览/试听通过 /api/commentary/bgm-preview/{cid}/{kind}。
    """
    import pathlib as _pl
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
            state = app._read_bgm_state(p) or {}
            # 哪些内置风格的 preview mp3 实际存在
            previews = []
            try:
                base = _pl.Path(key)
                stem = base.stem  # 去后缀的 basename（同名 .previews）
                previews_dir = base.parent / (stem + ".previews")
                if previews_dir.is_dir():
                    for kind in ("soft", "light", "epic"):
                        if (previews_dir / f"{kind}.mp3").is_file():
                            previews.append(kind)
            except Exception:
                pass
            items.append({
                "id": cid,
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": int(p.stat().st_mtime),
                "bgm_state": {
                    "bgm": state.get("bgm", "off"),
                    "volume": float(state.get("volume", 0.18) or 0.18),
                    "source": state.get("source", ""),
                    "ts": int(state.get("ts", 0) or 0),
                },
                "bgm_previews": previews,
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
              "source_duration": job.get("source_duration"),
              "src_filename": job.get("src_filename", "")}
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
    _title = app._commentary_title(payload, src_path)
    with app._commentary_lock:
        app.commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "script_path": "",
                                   "progress": [], "src_path": src_path, "title": _title}
    app.executor.submit(app._commentary_run, job_id, src_path, payload.vertical, payload.voice or app.COMMENTARY_VOICE,
                    script_only=True, trim_start=payload.trim_start, trim_end=payload.trim_end,
                    mode=payload.mode, commentary_type=payload.commentary_type,
                    highlight_source=payload.highlight_source,
                    intro_highlight=payload.intro_highlight,
                    skip_intro_outro=payload.skip_intro_outro,
                    no_narrate_intro_outro=payload.no_narrate_intro_outro,
                    retain_pct=payload.retain_pct, web=payload.web,
                    vision=payload.vision, tts_provider=payload.tts_provider,
                    correct_transcript=payload.correct_transcript,
                    intro_sec=payload.intro_sec, outro_sec=payload.outro_sec,
                    drama_start_sec=payload.drama_start_sec, drama_end_sec=payload.drama_end_sec,
                    one_click=payload.one_click,
                    title=_title,
                    style=payload.style,
                    bgm=payload.bgm, bgm_file=payload.bgm_file, bgm_volume=payload.bgm_volume,
                    subtitle_size=payload.subtitle_size, subtitle_color=payload.subtitle_color,
                    subtitle_border=payload.subtitle_border, subtitle_pos=payload.subtitle_pos,
                    max_chars=payload.max_chars,
                    export_jianying=payload.export_jianying)
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
def render_script(job_id: str, vertical: bool = app.Form(False), voice: str = app.Form(""),
                 export_jianying: str = app.Form(""),
                 bgm: str = app.Form("off"), bgm_file: str = app.Form(""),
                 bgm_volume: float = app.Form(0.18),
                 subtitle_size: float = app.Form(1.0), subtitle_color: str = app.Form("FFFFFF"),
                 subtitle_border: float = app.Form(1.0), subtitle_pos: str = app.Form("bottom"),
                 max_chars: int = app.Form(0)) -> dict:
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
    # 反查原始视频路径：优先用脚本任务记录的 src_path，避免按 title/job_id 在 input 目录里猜错。
    src_path = job.get("src_path")
    if not src_path:
        raise app.HTTPException(status_code=410, detail="源视频路径已丢失，请重新上传")
    # 片名前缀：取脚本生成时存的「原始 title」（用户上传时算出的 final_title，
    # 优先于 script.json 里的 LLM 推断剧名），避免成片名是 hash/upload。
    # 旧任务（未存 title）从 src_path 走同样的回退链重算。
    title = (job.get("title") or "").strip()
    if not title:
        stem = app.Path(src_path).stem if src_path else ""
        title = (stem if app._meaningful_stem(stem) else "") or app._probe_video_title(src_path) or ""
        title = (title or "").strip()
        if not title:
            import secrets as _secrets
            title = "v" + _secrets.token_hex(3)
    try:
        seg_data = app.json.loads(app.Path(script_path).read_text(encoding="utf-8"))
        saved = seg_data.get("options") or {}
    except Exception:
        saved = {}

    commentary_type = saved.get("commentary_type", "deep_hl")
    highlight_source = saved.get("highlight_source", "ai")
    intro_highlight = bool(saved.get("intro_highlight", False))
    skip_intro_outro = bool(saved.get("skip_intro_outro", False))
    no_narrate_intro_outro = bool(saved.get("no_narrate_intro_outro", True))
    retain_pct = saved.get("retain_pct")
    intro_sec = saved.get("intro_sec")
    outro_sec = saved.get("outro_sec")
    drama_start_sec = saved.get("drama_start_sec")
    drama_end_sec = saved.get("drama_end_sec")
    web = bool(saved.get("web", False))
    one_click = bool(saved.get("one_click", False))

    # 复用父任务的裁剪参数：同一源+起止会命中确定性命名的裁剪文件，直接吃裁剪后视频渲染
    trim_start = float(job.get("trim_start", 0.0) or 0.0)
    trim_end = float(job.get("trim_end", 0.0) or 0.0)

    # 用新的 job_id 提交渲染（保留原 script 关联）
    render_job_id = app.uuid.uuid4().hex[:12]
    # 复用父任务上传时的原始文件名（render 任务不重新接收上传，沿用脚本任务的 src_filename）
    src_filename = (job.get("src_filename") or "").strip()
    with app._commentary_lock:
        app.commentary_jobs[render_job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                          "parent_script_job": job_id, "steps": [], "logs": [],
                                          "src_path": src_path, "title": title,
                                          "src_filename": src_filename}
    v = voice or job.get("voice", "") or app.COMMENTARY_VOICE
    app.executor.submit(app._commentary_run, render_job_id, src_path, vertical, v, edit_only=script_path,
                    trim_start=trim_start, trim_end=trim_end,
                    commentary_type=commentary_type, highlight_source=highlight_source,
                    intro_highlight=intro_highlight, skip_intro_outro=skip_intro_outro,
                    no_narrate_intro_outro=no_narrate_intro_outro,
                    retain_pct=retain_pct, web=web, one_click=one_click,
                    intro_sec=intro_sec, outro_sec=outro_sec,
                    drama_start_sec=drama_start_sec, drama_end_sec=drama_end_sec,
                    title=title,
                    src_filename=src_filename,
                    bgm=bgm, bgm_file=bgm_file, bgm_volume=bgm_volume,
                    subtitle_size=subtitle_size, subtitle_color=subtitle_color,
                    subtitle_border=subtitle_border, subtitle_pos=subtitle_pos,
                    max_chars=max_chars,
                    export_jianying=export_jianying)
    return {"job_id": render_job_id, "status": "running", "script_job": job_id}


def _run_remux_bgm(remux_id: str, output_path: str, bgm: str, bgm_file: str,
                   bgm_volume: float, bgm_clip_start=None, bgm_clip_end=None) -> None:
    """后台轻量换/加/移除 BGM：复用 _commentary_run 的同款子进程机制跑 process.py remux-bgm。

    不重渲成片，只跑 amix（秒级），成品就地替换（build 时已存无音乐 sidecar）。
    bgm_clip_start/end (秒)：锁住 BGM 文件片段——通过 ffmpeg atrim 完成。
    """
    last: list[str] = []

    def _append(line: str) -> None:
        line = (line or "").rstrip("\n")
        if not line:
            return
        last.append(line)
        if len(last) > 60:
            del last[: len(last) - 60]
        with app._commentary_lock:
            j = app.commentary_jobs.setdefault(remux_id, {})
            j["progress"] = list(last)
            app._commentary_log(j, line)

    try:
        _bundled = app._commentary_is_bundled()
        if _bundled:
            args = [sys.executable, "--vdl-commentary-worker", "remux-bgm", "--out", output_path]
        else:
            args = [app.COMMENTARY_RT.python, "-u", "process.py", "remux-bgm", "--out", output_path]
        args += ["--bgm", bgm, "--bgm-volume", str(bgm_volume)]
        if bgm == "user" and bgm_file:
            args += ["--bgm-file", bgm_file]
        if bgm_clip_start is not None:
            args += ["--bgm-clip-start", str(float(bgm_clip_start))]
        if bgm_clip_end is not None:
            args += ["--bgm-clip-end", str(float(bgm_clip_end))]
        run_env = app.COMMENTARY_RT.env()
        with app._commentary_lock:
            app.commentary_jobs.setdefault(remux_id, {})["status"] = "remuxing"
        proc = subprocess.Popen(
            args, cwd=str(app.COMMENTARY_DIR),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=run_env,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _append(line)
        ret = proc.wait(timeout=300)
        if ret != 0:
            tail = "\n".join(last[-20:]) or "无输出"
            raise RuntimeError(f"换音乐退出码 {ret}：\n{tail}")
        with app._commentary_lock:
            rj = app.commentary_jobs.get(remux_id)
            if rj:
                rj.update(status="completed", output_path=output_path, remuxed_bgm=bgm)
            # 同步原渲染任务，方便前端仅凭 render_job 轮询即可感知换音乐完成
            parent = rj.get("parent_render_job") if rj else None
            if parent and parent in app.commentary_jobs:
                app.commentary_jobs[parent].update(remuxed_bgm=bgm, remux_job=remux_id, status="completed")
        # 写 BGM 状态 manifest：bgm=off 视为"已移除配乐"=已成片(无配乐版)，bgm!=off 视为"已加配乐"=已成片
        # 即只要走过 remux 流程，无论 on/off 都算 final（用户主动确认过配乐决策）
        try:
            app._write_bgm_state(output_path, bgm=bgm, volume=bgm_volume, source="remux")
        except Exception:
            app.logger.exception("写 remux BGM manifest 失败：%s", output_path)
    except Exception as exc:  # noqa: BLE001
        with app._commentary_lock:
            j = app.commentary_jobs.setdefault(remux_id, {})
            j["status"] = "failed"
            j["error"] = str(exc)[:800]
        app.logger.exception("换音乐任务 %s 失败", remux_id)


@router.post("/api/commentary/remux-bgm/{cid}")
def remux_bgm(cid: str, bgm: str = app.Form("off"), bgm_file: str = app.Form(""),
             bgm_volume: float = app.Form(0.18),
             bgm_clip_start: float = app.Form(None),
             bgm_clip_end: float = app.Form(None)) -> dict:
    """渲染后单独换/加/移除 BGM（轻量 amix，不重渲成片）。

    用成片文件 id（与列表/下载/删除接口同款 cid）定位成片，避免依赖内存中的
    渲染任务（可能被清理）。返回独立 remux_id 供前端轮询进度；成品就地替换，
    output_path 不变。build() 已始终保留无音乐 sidecar + 三种内置预览 mp3，
    故可随时在 off/soft/light/epic/user 间自由切换；同时支持 bgm_clip_start/end
    锁定 BGM 文件的片段（ffmpeg atrim）。

    字段：
    - bgm: "off" 移除配乐；"soft"/"light"/"epic" 内置和弦；"user" 本地文件
    - bgm_file: bgm=user 时为本地音乐绝对路径
    - bgm_volume: BGM 音量(0.02~0.6, 默认 0.18)
    - bgm_clip_start/end: BGM 文件秒级片段（None = 用整条）
    """
    if not app.COMMENTARY_ENABLED:
        raise app.HTTPException(status_code=503, detail="该实例未启用解说功能")
    if app.COMMENTARY_MODE == "http":
        raise app.HTTPException(status_code=400, detail="换音乐暂不支持 HTTP worker 模式，请使用 local 模式")
    p = app._decode_commentary_id(cid)
    if not p or not p.is_file():
        raise app.HTTPException(status_code=404, detail="成片文件不存在")
    output_path = str(p)
    remux_id = app.uuid.uuid4().hex[:12]
    with app._commentary_lock:
        app.commentary_jobs[remux_id] = {"status": "remuxing", "error": "", "output_path": output_path,
                                          "progress": [], "parent_render_job": "", "logs": [],
                                          "remuxed_bgm": ""}
    app.executor.submit(_run_remux_bgm, remux_id, output_path, bgm, bgm_file,
                         bgm_volume, bgm_clip_start, bgm_clip_end)
    return {"job_id": remux_id, "status": "remuxing", "render_job": cid}


def _bgm_preview_mp3_path(mp4_path: "pathlib.Path", kind: str) -> "pathlib.Path | None":
    """返回 `<mp4>.previews/<kind>.mp3` 路径（若不存在则 None）。"""
    try:
        import pathlib as _pl
        p = _pl.Path(str(mp4_path))
        base = p.with_suffix("") if p.suffix else _pl.Path(str(p))
        previews_dir = base.parent / (base.name + ".previews")
        candidate = previews_dir / f"{kind}.mp3"
        return candidate if candidate.is_file() else None
    except Exception:
        return None


@router.get("/api/commentary/bgm-preview/{cid}/{kind}")
def commentary_bgm_preview(cid: str, kind: str):
    """返回成片（cid）对三种内置 BGM 风格的试听 mp3（build() 时已落 sidecar）。

    kind ∈ {"soft", "light", "epic"}。返回 audio/mpeg 流；找不到 404。
    """
    if kind not in ("soft", "light", "epic"):
        raise app.HTTPException(status_code=400, detail="kind 必须为 soft/light/epic")
    p = app._decode_commentary_id(cid)
    if not p or not p.is_file():
        raise app.HTTPException(status_code=404, detail="成片不存在，无法提供试听")
    mp3 = _bgm_preview_mp3_path(p, kind)
    if not mp3:
        raise app.HTTPException(status_code=404,
                                detail=f"该成片缺少 {kind} 风格试听（可能是旧版渲染，请重新渲染）")
    return app.FileResponse(path=str(mp3), filename=f"{kind}.mp3", media_type="audio/mpeg")


@router.get("/api/commentary/audio-preview")
def commentary_audio_preview(path: str):
    """提供本地音乐试听流（用户在桌面桥选完文件后，前端调本端点拉 mp3 进 <audio>）。

    安全：路径必须
    - 以 / 开头（绝对路径）
    - 位于当前用户家目录下（避免误读到系统目录）
    - 文件存在且扩展名在允许的音频列表（mp3/m4a/wav/aac/flac/ogg）
    """
    import pathlib as _pl
    if not path or not path.startswith("/"):
        raise app.HTTPException(status_code=400, detail="path 必须为绝对路径")
    home = _pl.Path.home()
    try:
        ap = _pl.Path(path).resolve()
        ap.relative_to(home)  # 必须在家目录下
    except ValueError:
        raise app.HTTPException(status_code=400, detail="path 必须在用户家目录下")
    except OSError:
        raise app.HTTPException(status_code=400, detail="path 无法解析")
    if not ap.is_file():
        raise app.HTTPException(status_code=404, detail="文件不存在")
    if ap.suffix.lower() not in (".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"):
        raise app.HTTPException(status_code=400, detail=f"不支持的音频扩展名 {ap.suffix}")
    media = {
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav",
        ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
    }[ap.suffix.lower()]
    return app.FileResponse(path=str(ap), filename=ap.name, media_type=media)


@router.post("/api/commentary/voice-preview")
def voice_preview(
    voice: str = app.Form(...),
    text: str = app.Form("你好，我是视频解说员。我将为你解说这段视频。"),
    loudness: str = app.Form(None),
    boost: str = app.Form(None),
) -> app.FileResponse:
    """配音试听：把指定文本用指定 voice 转成 mp3 返回给前端播放。

    若带 loudness/boost（来自「配音与音量」面板的「试听当前设置」），生成的旁白会
    再做 ffmpeg 响度标准化 + 增益，使试听与成片响度一致；不传则保持纯 TTS（音色试听）。
    """
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
    # loudness/boost 校验（仅当显式提供时）
    if loudness not in (None, "", "off"):
        try:
            lv = float(loudness)
        except ValueError:
            raise app.HTTPException(status_code=400, detail=f"loudness 必须为数字（-18~-10）或 off，当前: {loudness}")
        if not (-18 <= lv <= -10):
            raise app.HTTPException(status_code=400, detail=f"loudness 超出范围（-18~-10），当前: {loudness}")
    if boost not in (None, ""):
        try:
            bv = float(boost)
        except ValueError:
            raise app.HTTPException(status_code=400, detail=f"boost 必须为数字（1.0~1.6），当前: {boost}")
        if not (1.0 <= bv <= 1.6):
            raise app.HTTPException(status_code=400, detail=f"boost 超出范围（1.0~1.6），当前: {boost}")
    # 2. 再检查资源可用性
    if not app.COMMENTARY_DIR or not (app.COMMENTARY_DIR / "scripts" / "voice_preview.py").exists():
        raise app.HTTPException(status_code=503, detail="解说管线未配置（VDL_COMMENTARY_DIR 缺失或不含 voice_preview.py）")

    out_dir = app._commentary_root("work") / "voice_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{app.uuid.uuid4().hex[:12]}.mp3"
    try:
        app._run_voice_preview(text, voice, out_path, timeout=45,
                               loudness=loudness, boost=boost)
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
