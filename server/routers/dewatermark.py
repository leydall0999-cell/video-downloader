"""server/routers/dewatermark.py — 需求文档模块二：PDF / 图片去水印路由。

复用 dewatermark_core 的核心逻辑；图片/PDF 各自一套 job + 状态 + 下载路由。
依赖（cv2/fitz）缺失时返回 503，功能优雅降级，不影响其余路由。
"""
import app
import json
import subprocess as _subprocess
import dewatermark_core as dwc
import dewatermark_ai as dwc_ai
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


def _run_image(job_id: str, src: str, regions, method: str, radius: int, engine: str = "opencv") -> None:
    job = app.DW_JOBS.get(job_id)
    if not job:
        return
    try:
        src_path = app.Path(src)
        ext = src_path.suffix.lower() or ".png"
        if ext not in DW_IMAGE_EXTS:
            ext = ".png"
        out_path = app.DW_DIR / f"dw_img_{job_id}{ext}"
        if engine == "ai":
            if not dwc_ai.available():
                raise RuntimeError("AI 去水印不可用（服务端未启用 onnxruntime / 模型未下载）")
            dwc_ai.ai_image_inpaint(src_path, out_path, regions)
        else:
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
    engine: str = app.Form("opencv"),
    request: app.Request = None,
) -> dict:
    """图片去水印：上传图片 + 多选区 regions（归一化 x/y/w/h + op: add/subtract）。

    优先解析 regions（前端多选区）；缺失时回退单个 x/y/w/h 区域（兼容旧客户端）。
    """
    if not dwc.available():
        raise app.HTTPException(status_code=503, detail="图片去水印不可用（缺少 OpenCV 依赖）")
    if engine not in ("opencv", "ai"):
        raise app.HTTPException(status_code=400, detail="engine 仅支持 opencv / ai")
    if engine == "ai" and not dwc_ai.available():
        raise app.HTTPException(status_code=503, detail="AI 去水印不可用（服务端未启用 onnxruntime / 模型未下载）")
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
    app.executor.submit(_run_image, job_id, str(save_path), regions_list, method, radius, engine)
    return {"job_id": job_id, "status": "running", "kind": "image"}


def _parse_segments(segments_json: str):
    """解析多段配置 `[{"start": s, "end": e, "regions": [...]}, ...]`。

    每段独立 normalize 自己的 regions；任一段 regions 无效则整批失败（避免静默漏处理）。
    返回空 list 表示调用方没传 segments（走单段回退）。
    """
    if not segments_json:
        return []
    try:
        parsed = json.loads(segments_json)
    except (ValueError, TypeError):
        raise app.HTTPException(status_code=400, detail="segments 不是合法 JSON")
    if not isinstance(parsed, list) or not parsed:
        return []
    if len(parsed) > 50:
        raise app.HTTPException(status_code=400, detail="segments 最多 50 段")
    out = []
    for i, seg in enumerate(parsed):
        if not isinstance(seg, dict):
            raise app.HTTPException(status_code=400, detail=f"segments[{i}] 必须是对象")
        try:
            s = float(seg.get("start") or 0)
            e = float(seg.get("end") or 0)
        except (TypeError, ValueError):
            raise app.HTTPException(status_code=400, detail=f"segments[{i}] 的 start/end 必须是数字")
        if s < 0 or e < 0:
            raise app.HTTPException(status_code=400, detail=f"segments[{i}] 的 start/end 不能为负数")
        if e > 0 and s > 0 and e < s:
            raise app.HTTPException(status_code=400, detail=f"segments[{i}] 的 end 需大于或等于 start")
        regs = dwc.normalize_regions(seg.get("regions") or [])
        if not regs:
            raise app.HTTPException(status_code=400, detail=f"segments[{i}] 缺少有效的框选区域")
        out.append({"start": s, "end": e, "regions": regs})
    return out


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


# ---------------------------------------------------------------- 视频去水印（B 档：逐帧 LaMa + 邻帧中值平滑）

DW_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v", ".wmv"}


def _run_video(job_id: str, src: str, regions, ffmpeg_bin: str, resolution: str, smooth: bool,
               start_sec: float = 0.0, end_sec: float = 0.0, segments=None) -> None:
    job = app.DW_JOBS.get(job_id)
    if not job:
        return
    try:
        if not dwc_ai.available():
            raise RuntimeError("AI 去水印不可用（缺少 onnxruntime 依赖或模型未下载）")
        out_path = app.DW_DIR / f"dw_vid_{job_id}.mp4"
        # 注意：frozen 桌面端 ai_video_inpaint 内部同进程跑（与图片模式一致），
        # 子进程隔离在桌面端派发不可靠，故不走 subprocess。
        dwc_ai.ai_video_inpaint(
            app.Path(src), out_path, regions, ffmpeg_bin,
            progress_cb=lambda done, total: job.__setitem__("progress", f"{done}/{total}"),
            resolution=resolution, smooth=smooth,
            start_sec=start_sec, end_sec=end_sec, segments=segments,
        )
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("视频去水印未产出有效文件")
        job["status"] = "completed"
        job["filename"] = out_path.name
        job["out_path"] = str(out_path)
        app.logger.info("dw video %s done -> %s", job_id, out_path.name)
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        app.logger.warning("dw video %s failed: %s", job_id, e)


@router.post("/api/dw/video")
def create_dw_video(
    file: app.UploadFile = app._FastAPIFile(...),
    regions: str = app.Form(""),
    x: float = app.Form(0.0),
    y: float = app.Form(0.0),
    w: float = app.Form(0.0),
    h: float = app.Form(0.0),
    resolution: str = app.Form("original"),
    smooth: int = app.Form(1),
    start_sec: float = app.Form(0.0),
    end_sec: float = app.Form(0.0),
    segments: str = app.Form(""),
    request: app.Request = None,
) -> dict:
    """视频去水印：上传视频 + 多选区 regions（归一化，整段视频套同一掩码）。

    逐帧跑 LaMa 推理 + 邻帧中值平滑降闪烁，ffmpeg 重编码混音输出 mp4。

    **时间分段（Segment，2026-08-29 加）**：start_sec/end_sec 指定水印出现的秒数区间
    （闭区间；end_sec<=0 表示到片尾）。区间内的帧才跑推理，区间外直接复制原帧。
    10 分钟视频只有 47 秒有水印时，推理量可从 100% 降到约 5%。

    **多段（segments，2026-08-29 加）**：JSON 数组
    `[{"start": s, "end": e, "regions": [...]}, ...]`，每段可带自己独立的框选区域。
    传了 segments 就以它为准（忽略顶层 regions/start_sec/end_sec）；不传则回退到单段参数。
    """
    if not dwc.available():
        raise app.HTTPException(status_code=503, detail="视频去水印不可用（缺少 OpenCV 依赖）")
    if not dwc_ai.available():
        raise app.HTTPException(status_code=503, detail="AI 去水印不可用（服务端未启用 onnxruntime / 模型未下载）")
    if resolution not in ("original", "720", "480"):
        raise app.HTTPException(status_code=400, detail="resolution 仅支持 original / 720 / 480")
    if start_sec < 0 or end_sec < 0:
        raise app.HTTPException(status_code=400, detail="start_sec / end_sec 不能为负数")
    if end_sec > 0 and start_sec > 0 and end_sec < start_sec:
        raise app.HTTPException(status_code=400, detail="end_sec 需大于或等于 start_sec")
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower()
    if suffix not in DW_VIDEO_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传视频文件（mp4/mov/mkv/webm/avi 等）")
    # 多段（segments）优先；为空则回退到单段参数
    segments_list = _parse_segments(segments)
    if segments_list:
        regions_list = []      # 多段模式下顶层 regions 不参与
        start_sec = end_sec = 0.0
    else:
        regions_list = _parse_regions(regions, x, y, w, h)
        if not regions_list:
            raise app.HTTPException(status_code=400, detail="请框选水印区域（regions 或 x/y/w/h / segments 需有效）")
    save_path = _save_upload(file, "dw_vid_up")
    job_id = app.uuid.uuid4().hex[:12]
    with app.DW_LOCK:
        app.DW_JOBS[job_id] = {
            "status": "running", "out_path": "", "error": "", "filename": "",
            "kind": "video", "progress": "",
        }
    app.executor.submit(_run_video, job_id, str(save_path), regions_list, app.FFMPEG_BIN,
                        resolution, bool(int(smooth)), float(start_sec), float(end_sec), segments_list)
    return {"job_id": job_id, "status": "running", "kind": "video"}


@router.get("/api/dw/video/{job_id}")
def dw_video_status(job_id: str) -> dict:
    job = app.DW_JOBS.get(job_id)
    if not job or job.get("kind") != "video":
        raise app.HTTPException(status_code=404, detail="视频去水印任务不存在")
    return {
        "status": job["status"], "error": job.get("error", ""),
        "filename": job.get("filename", ""), "progress": job.get("progress", ""),
    }


@router.get("/api/dw/video/{job_id}/file")
def dw_video_file(job_id: str) -> app.FileResponse:
    job = app.DW_JOBS.get(job_id)
    if not job or job.get("kind") != "video":
        raise app.HTTPException(status_code=404, detail="视频去水印任务不存在")
    if job["status"] != "completed":
        raise app.HTTPException(status_code=409, detail="处理尚未完成")
    out = app.Path(job["out_path"])
    if not out.exists():
        raise app.HTTPException(status_code=410, detail="结果文件已清理")
    return app.FileResponse(path=str(out), filename=out.name, media_type="video/mp4")


@router.post("/api/dw/video/thumbnail")
def create_dw_video_thumbnail(
    file: app.UploadFile = app._FastAPIFile(...),
    request: app.Request = None,
) -> app.FileResponse:
    """抽视频首帧作为 PNG 返回给前端做选区预览。

    2026-08-29 实测：macOS WKWebView 的 <video> 元素在不少 mp4（含某些 H.264 high@L4
    / HEVC）上 onloadeddata / play() 时机不可靠，canvas drawImage 拿不到有效帧。
    **根治：服务端用 ffmpeg 抽首帧，前端用 <img> 显示**（img 渲染稳定可靠，绕开所有
    video 元素渲染坑）。原始视频在用户提交时仍按原路径传给 /api/dw/video 走完整管线。
    """
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower()
    if suffix not in DW_VIDEO_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传视频文件（mp4/mov/mkv/webm/avi 等）")
    save_path = _save_upload(file, "dw_vid_thumb")
    out_path = save_path.with_suffix(".png")
    try:
        # 抽第 0 秒首帧。`-ss 0` 放 -i 前追求 seek 速度；不依赖首帧恰好是 keyframe。
        cmd = [app.FFMPEG_BIN, "-y", "-ss", "0", "-i", str(save_path),
               "-vframes", "1", "-f", "image2", str(out_path)]
        proc = _subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            err_tail = proc.stderr.decode("utf-8", "ignore")[-300:] if proc.stderr else "unknown"
            save_path.unlink(missing_ok=True)
            out_path.unlink(missing_ok=True)
            raise app.HTTPException(
                status_code=500,
                detail=f"ffmpeg 抽首帧失败：{err_tail or 'unknown'}"
            )
        # 抽完即删原视频（可能数百 MB），PNG 留给 FastAPI 读取用，响应后由系统清理
        save_path.unlink(missing_ok=True)
    except app.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        save_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        raise app.HTTPException(status_code=500, detail=f"首帧提取失败：{e}")
    return app.FileResponse(path=str(out_path), media_type="image/png")
