"""server/routers/dewatermark.py — 需求文档模块二：PDF / 图片去水印路由。

复用 dewatermark_core 的核心逻辑；图片/PDF 各自一套 job + 状态 + 下载路由。
依赖（cv2/fitz）缺失时返回 503，功能优雅降级，不影响其余路由。
"""
import app
import json
import dewatermark_core as dwc
from fastapi import APIRouter

router = APIRouter()

DW_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


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


def _run_image(job_id: str, src: str, regions, method: str, radius: int) -> None:
    job = app.DW_JOBS.get(job_id)
    if not job:
        return
    try:
        src_path = app.Path(src)
        ext = src_path.suffix.lower() or ".png"
        if ext not in DW_IMAGE_EXTS:
            ext = ".png"
        out_path = app.DW_DIR / f"dw_img_{job_id}{ext}"
        dwc.image_inpaint(src_path, out_path, regions, method, radius)
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("去水印未产出有效文件")
        job["status"] = "completed"
        job["filename"] = out_path.name
        job["out_path"] = str(out_path)
        logger = app.logger
        logger.info("dw image %s done -> %s", job_id, out_path.name)
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        app.logger.warning("dw image %s failed: %s", job_id, e)


def _run_pdf(job_id: str, src: str, mode: str, regions, method: str, radius: int, dpi: int) -> None:
    job = app.DW_JOBS.get(job_id)
    if not job:
        return
    try:
        src_path = app.Path(src)
        out_path = app.DW_DIR / f"dw_pdf_{job_id}.pdf"
        if mode == "raster":
            dwc.pdf_raster_remove(src_path, out_path, regions, method, radius, dpi)
        else:
            dwc.pdf_remove_annotations(src_path, out_path)
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("去水印未产出有效文件")
        job["status"] = "completed"
        job["filename"] = out_path.name
        job["out_path"] = str(out_path)
        app.logger.info("dw pdf %s done -> %s", job_id, out_path.name)
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        app.logger.warning("dw pdf %s failed: %s", job_id, e)


@router.post("/api/dw/image")
def create_dw_image(
    file: app.UploadFile = app._FastAPIFile(...),
    regions: str = app.Form(""),
    x: float = app.Form(0.0),
    y: float = app.Form(0.0),
    w: float = app.Form(0.0),
    h: float = app.Form(0.0),
    method: str = app.Form("telea"),
    radius: int = app.Form(3),
    request: app.Request = None,
) -> dict:
    """图片去水印：上传图片 + 多选区 regions（归一化 x/y/w/h + op: add/subtract）。

    优先解析 regions（前端多选区）；缺失时回退单个 x/y/w/h 区域（兼容旧客户端）。
    """
    if not dwc.available():
        raise app.HTTPException(status_code=503, detail="图片去水印不可用（缺少 OpenCV 依赖）")
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.png").suffix.lower()
    if suffix not in DW_IMAGE_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传图片文件（png/jpg/webp/bmp 等）")
    regions_list = _parse_regions(regions, x, y, w, h)
    if not regions_list:
        raise app.HTTPException(status_code=400, detail="请框选水印区域（regions 或 x/y/w/h 需有效）")
    if method not in ("telea", "ns"):
        raise app.HTTPException(status_code=400, detail="method 仅支持 telea / ns")
    if not (1 <= radius <= 20):
        raise app.HTTPException(status_code=400, detail="radius 需在 1..20 之间")
    save_path = _save_upload(file, "dw_up")
    job_id = app.uuid.uuid4().hex[:12]
    with app.DW_LOCK:
        app.DW_JOBS[job_id] = {
            "status": "running", "out_path": "", "error": "", "filename": "",
            "kind": "image",
        }
    app.executor.submit(_run_image, job_id, str(save_path), regions_list, method, radius)
    return {"job_id": job_id, "status": "running", "kind": "image"}


def _parse_regions(regions_json: str, x: float, y: float, w: float, h: float):
    """从 regions JSON 字段解析多区域；为空则回退单个 x/y/w/h。

    返回 normalize_regions 的结果（list），无效返回 None。
    """
    if regions_json:
        try:
            parsed = json.loads(regions_json)
        except (ValueError, TypeError):
            return None
        return dwc.normalize_regions(parsed)
    single = dwc.normalize_region({"x": x, "y": y, "w": w, "h": h})
    if not single:
        return None
    return [single]


@router.get("/api/dw/image/{job_id}")
def dw_image_status(job_id: str) -> dict:
    job = app.DW_JOBS.get(job_id)
    if not job or job.get("kind") != "image":
        raise app.HTTPException(status_code=404, detail="图片去水印任务不存在")
    return {"status": job["status"], "error": job.get("error", ""), "filename": job.get("filename", "")}


@router.get("/api/dw/image/{job_id}/file")
def dw_image_file(job_id: str) -> app.FileResponse:
    job = app.DW_JOBS.get(job_id)
    if not job or job.get("kind") != "image":
        raise app.HTTPException(status_code=404, detail="图片去水印任务不存在")
    if job["status"] != "completed":
        raise app.HTTPException(status_code=409, detail="处理尚未完成")
    out = app.Path(job["out_path"])
    if not out.exists():
        raise app.HTTPException(status_code=410, detail="结果文件已清理")
    return app.FileResponse(path=str(out), filename=out.name, media_type="application/octet-stream")


@router.post("/api/dw/pdf")
def create_dw_pdf(
    file: app.UploadFile = app._FastAPIFile(...),
    mode: str = app.Form("annotations"),
    regions: str = app.Form(""),
    x: float = app.Form(0.0),
    y: float = app.Form(0.0),
    w: float = app.Form(0.0),
    h: float = app.Form(0.0),
    method: str = app.Form("telea"),
    radius: int = app.Form(3),
    dpi: int = app.Form(150),
    request: app.Request = None,
) -> dict:
    """PDF 去水印：上传 PDF + 模式（annotations 注释型 / raster 栅格化）。

    annotations 模式无损删除 Watermark 注释；raster 模式栅格化后区域 inpaint 重排。
    raster 模式需要框选区域（regions 多选区或 x/y/w/h 单区域）。"""
    if not dwc.pdf_available():
        raise app.HTTPException(status_code=503, detail="PDF 去水印不可用（缺少 PyMuPDF 依赖）")
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.pdf").suffix.lower()
    if suffix != ".pdf":
        raise app.HTTPException(status_code=409, detail="请上传 PDF 文件")
    if mode not in ("annotations", "raster"):
        raise app.HTTPException(status_code=400, detail="mode 仅支持 annotations / raster")
    regions_list = None
    if mode == "raster":
        if not dwc.available():
            raise app.HTTPException(status_code=503, detail="栅格化去水印需要 OpenCV 依赖")
        regions_list = _parse_regions(regions, x, y, w, h)
        if not regions_list:
            raise app.HTTPException(status_code=400, detail="栅格化模式需要框选水印区域（regions 或 x/y/w/h）")
        if method not in ("telea", "ns"):
            raise app.HTTPException(status_code=400, detail="method 仅支持 telea / ns")
        if not (1 <= radius <= 20):
            raise app.HTTPException(status_code=400, detail="radius 需在 1..20 之间")
        if not (50 <= dpi <= 400):
            raise app.HTTPException(status_code=400, detail="dpi 需在 50..400 之间")
    save_path = _save_upload(file, "dw_pdf_up")
    job_id = app.uuid.uuid4().hex[:12]
    with app.DW_LOCK:
        app.DW_JOBS[job_id] = {
            "status": "running", "out_path": "", "error": "", "filename": "",
            "kind": "pdf",
        }
    app.executor.submit(_run_pdf, job_id, str(save_path), mode, regions_list, method, radius, dpi)
    return {"job_id": job_id, "status": "running", "kind": "pdf", "mode": mode}


@router.get("/api/dw/pdf/{job_id}")
def dw_pdf_status(job_id: str) -> dict:
    job = app.DW_JOBS.get(job_id)
    if not job or job.get("kind") != "pdf":
        raise app.HTTPException(status_code=404, detail="PDF 去水印任务不存在")
    return {"status": job["status"], "error": job.get("error", ""), "filename": job.get("filename", "")}


@router.get("/api/dw/pdf/{job_id}/file")
def dw_pdf_file(job_id: str) -> app.FileResponse:
    job = app.DW_JOBS.get(job_id)
    if not job or job.get("kind") != "pdf":
        raise app.HTTPException(status_code=404, detail="PDF 去水印任务不存在")
    if job["status"] != "completed":
        raise app.HTTPException(status_code=409, detail="处理尚未完成")
    out = app.Path(job["out_path"])
    if not out.exists():
        raise app.HTTPException(status_code=410, detail="结果文件已清理")
    return app.FileResponse(path=str(out), filename=out.name, media_type="application/pdf")
