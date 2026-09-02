"""server/routers/matting.py — 一键抠图（图片去背景）路由。

照 routers/dewatermark.py 的图片链路：上传图片 → 异步 job（executor 提交）
→ 轮询状态 → 下载透明 PNG。独立 job 存储，不与去水印混用。

模型权重首次使用时才下载（默认 birefnet-general · 927MB，MIT 可商用），
状态接口会带上下载进度，前端据此显示「首次下载模型 xx%」，避免长任务看起来像卡死。

依赖（onnxruntime/numpy/Pillow，App 内已打包）缺失时返回 503，优雅降级。
"""
import logging
import threading

import app
import matting_ai as mat
import vision_client
from fastapi import APIRouter

router = APIRouter()
logger = logging.getLogger("matting")

MAT_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
MAT_MODEL_EXTS = {".onnx"}

# 独立 job 存储（与去水印 DW_JOBS 隔离，避免 key 冲突）
MAT_JOBS: dict[str, dict] = {}
MAT_LOCK = threading.Lock()


def _save_upload(file, prefix: str) -> app.Path:
    """流式落盘上传文件到 DW_DIR，返回保存路径。"""
    suffix = app.Path(file.filename or "upload.bin").suffix.lower() or ".bin"
    save_path = app.DW_DIR / f"{prefix}_{app.uuid.uuid4().hex[:12]}{suffix}"
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
    except Exception as e:  # noqa: BLE001
        save_path.unlink(missing_ok=True)
        raise app.HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()
    return save_path


def _run_matting(job_id: str, src: list | None = None, box: list | None = None, model: str | None = None, vision_guide: bool = False, polygon: list | None = None, click: list | None = None) -> None:
    job = MAT_JOBS.get(job_id)
    if not job:
        return
    try:
        src_path = app.Path(src)
        out_path = app.DW_DIR / f"mat_{job_id}.png"
        # 诊断日志：记录选区原始参数，便于排查"圈了但结果不对"
        app.logger.info(
            "matting %s start: box=%s polygon=%s click=%s vision_guide=%s model=%s",
            job_id,
            box,
            f"{len(polygon)}pts" if polygon else None,
            click,
            vision_guide,
            model,
        )
        vision_box = None
        vision_used = False
        if vision_guide:
            try:
                det = vision_client.detect_subject(str(src_path))
                vb = det["subject"]["box"]
                vision_box = [float(v) for v in vb[:4]]
                vision_used = True
                job["vision_label"] = det["subject"].get("label", "")
            except Exception as e:  # noqa: BLE001
                logger.warning("vision_guide 失败，回退基础抠图: %s", e)
                vision_box = None
                vision_used = False
                job["vision_error"] = str(e)[:200]
        mat.matting_image(src_path, out_path, box=box, model=model, vision_box=vision_box, polygon=polygon, click=click)
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("抠图未产出有效文件")
        job["status"] = "completed"
        job["filename"] = out_path.name
        job["out_path"] = str(out_path)
        job["vision_used"] = vision_used
        # 记录本次实际使用的选区模式（点图 > 多边形 > AI 视觉 > 矩形框 > 自动），供前端状态展示
        job["mode"] = "click" if click else ("lasso" if polygon else ("vision" if vision_box else ("box" if box else "auto")))
        # 把 polygon 的 bbox 也记下来，方便和用户画的圈对照
        if polygon:
            xs = [p[0] for p in polygon]
            ys = [p[1] for p in polygon]
            job["polygon_bbox"] = [min(xs), min(ys), max(xs), max(ys)]
            job["polygon_pts"] = len(polygon)
        app.logger.info("matting %s done -> %s (mode=%s vision_used=%s)", job_id, out_path.name, job["mode"], vision_used)
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:500]
        app.logger.warning("matting %s failed: %s", job_id, e)


def _dl_snapshot() -> dict:
    """当前模型下载进度（MB 单位，便于前端直接展示）。"""
    d = mat.download_progress()
    mb = 1024 * 1024
    return {
        "active": bool(d.get("active")),
        "model": d.get("model", ""),
        "pct": float(d.get("pct") or 0.0),
        "done_mb": round(float(d.get("done") or 0) / mb, 1),
        "total_mb": round(float(d.get("total") or 0) / mb, 1),
        "error": d.get("error", ""),
    }


@router.post("/api/matting/image")
def create_matting_image(
    file: app.UploadFile = app._FastAPIFile(...),
    box: str = app.Form(None),
    model: str = app.Form(None),
    vision_guide: str = app.Form(None),
    polygon: str = app.Form(None),
    click_point: str = app.Form(None),
    request: app.Request = None,
) -> dict:
    """一键抠图：上传图片，返回 job_id；轮询 /api/matting/image/{job_id} 拿状态。

    box（可选）：JSON 字符串 "[x, y, w, h]"，归一化 0~1，表示用户手动框选的
    主体区域。给定时只抠框内主体（照片里有多个物体时可指定抠哪一个）。
    polygon（可选）：JSON 字符串 "[[x,y],[x,y],...]"（≥3 点，归一化 0~1），
    表示用户用**套索工具**自由圈出的多边形。比矩形框精确得多，能贴合不规则主体
    （如只圈主标题、避开紧贴的副标题/装饰）。给了 polygon 就以它为准。
    click_point（可选）：JSON 字符串 "[x,y]"（归一化 0~1），**点图抠图**——
    用户直接在预览图上单击某个元素（主标题/图案/按钮等），后端跑全图显著性，
    找到点击处所属的连通显著块并只抠那一块。优先级最高（用户点哪抠哪）。
    model（可选）：模型名（birefnet-general / birefnet-general-lite / rmbg-2.0）。
    给定时用指定模型，否则用全局默认（当前 birefnet-general，MIT 可商用）。
    rmbg-2.0 为 CC BY-NC 4.0 仅非商用，仅限个人 / 非商业场景显式选用。
    """
    if not mat.available():
        raise app.HTTPException(status_code=503, detail="一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.png").suffix.lower()
    if suffix not in MAT_IMAGE_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传图片文件（png/jpg/webp/bmp 等）")

    parsed_box = None
    if box:
        try:
            import json as _json

            raw = _json.loads(box)
            if isinstance(raw, (list, tuple)) and len(raw) >= 4:
                parsed_box = [float(v) for v in raw[:4]]
        except Exception:  # noqa: BLE001
            parsed_box = None  # 解析失败就退回整图抠图，不报错

    # 🪢 套索选区：[[x,y],...] 归一化点列表，≥3 点才有效（无效则忽略）
    parsed_polygon = None
    if polygon:
        try:
            import json as _json2

            praw = _json2.loads(polygon)
            if isinstance(praw, list) and len(praw) >= 3:
                pts = [[float(p[0]), float(p[1])] for p in praw if isinstance(p, (list, tuple)) and len(p) >= 2]
                if len(pts) >= 3:
                    parsed_polygon = pts
        except Exception:  # noqa: BLE001
            parsed_polygon = None  # 解析失败就忽略套索，退回框选/整图

    # 校验模型名（未知名字退回全局默认，不报错）
    sel_model = model if (model and model in mat.MODELS) else None
    # 🤖 AI 视觉定位开关：开启时后端先调 VLM 看懂图、自动框出主体再抠
    vg = (vision_guide or "").strip().lower() in ("1", "true", "yes", "on")

    # 👆 点图抠图：点击预览图上的元素（归一化 [x,y]），抠点击处所在显著块
    parsed_click = None
    if click_point:
        try:
            import json as _json3

            c = _json3.loads(click_point)
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                fx, fy = float(c[0]), float(c[1])
                if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
                    parsed_click = [fx, fy]
        except Exception:  # noqa: BLE001
            parsed_click = None  # 解析失败忽略，退回其它选区

    save_path = _save_upload(file, "mat_up")
    job_id = app.uuid.uuid4().hex[:12]
    with MAT_LOCK:
        MAT_JOBS[job_id] = {
            "status": "running", "out_path": "", "error": "", "filename": "",
            "kind": "matting",
        }
    app.executor.submit(_run_matting, job_id, str(save_path), parsed_box, sel_model, vg, parsed_polygon, parsed_click)
    return {"job_id": job_id, "status": "running", "kind": "matting", "box": parsed_box, "model": sel_model, "vision_guide": vg, "polygon": bool(parsed_polygon), "click": parsed_click}


@router.get("/api/matting/image/{job_id}")
def matting_image_status(job_id: str) -> dict:
    job = MAT_JOBS.get(job_id)
    if not job or job.get("kind") != "matting":
        raise app.HTTPException(status_code=404, detail="抠图任务不存在")
    return {
        "status": job["status"],
        "error": job.get("error", ""),
        "filename": job.get("filename", ""),
        # 首次使用会在这段时间下载模型权重，前端据此显示百分比
        "download": _dl_snapshot(),
        # 选区模式与诊断：套索/矩形/AI/自动，以及套索 bbox（便于核对坐标）
        "mode": job.get("mode", "auto"),
        "polygon_bbox": job.get("polygon_bbox"),
        "polygon_pts": job.get("polygon_pts"),
        # 🤖 AI 视觉定位结果：未配置/出错时自动回退基础抠图，前端据此提示
        "vision_used": job.get("vision_used", False),
        "vision_label": job.get("vision_label", ""),
        "vision_error": job.get("vision_error", ""),
    }


@router.get("/api/matting/image/{job_id}/file")
def matting_image_file(job_id: str) -> app.FileResponse:
    job = MAT_JOBS.get(job_id)
    if not job or job.get("kind") != "matting":
        raise app.HTTPException(status_code=404, detail="抠图任务不存在")
    if job["status"] != "completed":
        raise app.HTTPException(status_code=409, detail="处理尚未完成")
    out = app.Path(job["out_path"])
    if not out.exists():
        raise app.HTTPException(status_code=410, detail="结果文件已清理")
    return app.FileResponse(
        path=str(out),
        filename=out.name,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{out.name}"'},
    )


@router.get("/api/matting/models")
def matting_models() -> dict:
    """模型列表（含是否已下载）+ 当前下载进度 + 当前模型的备用下载 URL。

    当自动下载失败时，前端用 `download_urls` 给用户展示可手动打开的镜像链接。
    """
    cur = mat.current_model()
    meta = mat.MODELS.get(cur, {})
    return {
        "default": cur,
        "models": mat.list_models(),
        "download": _dl_snapshot(),
        "available": mat.available(),
        "download_urls": list(meta.get("urls", [])),
        "model_filename": meta.get("filename", ""),
        "model_dir": str(mat._model_dir()),
        "model_size_mb": meta.get("size_mb", 0),
    }


@router.post("/api/matting/model")
def matting_set_model(payload: dict) -> dict:
    """切换抠图模型。切换后若新模型未下载，首次抠图会自动下载。"""
    name = (payload or {}).get("name") or ""
    if name not in mat.MODELS:
        raise app.HTTPException(status_code=400, detail=f"未知模型：{name}")
    mat.set_model(name)
    return {"ok": True, "model": mat.current_model(), "models": mat.list_models()}


@router.post("/api/matting/warmup")
def matting_warmup() -> dict:
    """后台预热：下载模型 + 建 session（不阻塞请求，用 /api/matting/models 轮询进度）。"""
    if not mat.available():
        raise app.HTTPException(status_code=503, detail="一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")

    def _go():
        try:
            mat.warmup()
        except Exception as e:  # noqa: BLE001
            app.logger.warning("matting warmup failed: %s", e)

    threading.Thread(target=_go, name="matting-warmup", daemon=True).start()
    return {"ok": True, "model": mat.current_model(), "started": True}


@router.post("/api/matting/upload-model")
def matting_upload_model(
    file: app.UploadFile = app._FastAPIFile(...),
    payload: str = app.Form(None),
) -> dict:
    """用户在浏览器手动下载到本地后，从 App 内上传到正确目录（不再走命令行）。

    query/form: model=<name>（不指定则用当前选中的模型）
    form:     file=<下载的 .onnx>
    """
    target_name = ""
    if payload:
        try:
            import json as _json
            target_name = (_json.loads(payload) or {}).get("model", "") or ""
        except Exception:  # noqa: BLE001
            pass
    if not target_name:
        target_name = mat.current_model()
    if target_name not in mat.MODELS:
        raise app.HTTPException(status_code=400, detail=f"未知模型：{target_name}")

    # 文件名直接用 MODELS 里注册的标准名（避免用户下载时重命名）
    target_filename = mat.MODELS[target_name]["filename"]
    suffix = app.Path(file.filename or target_filename).suffix.lower() or ".onnx"
    if suffix not in MAT_MODEL_EXTS:
        raise app.HTTPException(status_code=409, detail=f"请上传 .onnx 文件（当前是 {suffix}）")

    dest = app.Path(mat._model_dir()) / target_filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".upload")

    written = 0
    try:
        with tmp.open("wb") as fh:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > app.UPLOAD_MAX_BYTES:
                    fh.close()
                    tmp.unlink(missing_ok=True)
                    raise app.HTTPException(status_code=413, detail="文件超过上传大小上限")
                fh.write(chunk)
    except app.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise app.HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()

    if written == 0:
        tmp.unlink(missing_ok=True)
        raise app.HTTPException(status_code=411, detail="上传文件为空")

    tmp.replace(dest)  # 原子落盘；下次 _get_session 自动命中
    mat.set_model(target_name)  # 切换到刚上传的模型
    app.logger.info("matting user-uploaded model %s -> %s (%d bytes)", target_name, dest.name, written)
    return {
        "ok": True,
        "model": target_name,
        "path": str(dest),
        "size_bytes": written,
        "size_mb": round(written / 1024 / 1024, 1),
    }
