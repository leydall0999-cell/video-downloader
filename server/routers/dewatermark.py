"""server/routers/dewatermark.py — 需求文档模块二：PDF / 图片去水印路由。

复用 dewatermark_core 的核心逻辑；图片/PDF 各自一套 job + 状态 + 下载路由。
依赖（cv2/fitz）缺失时返回 503，功能优雅降级，不影响其余路由。
"""
import app
import json
import re as _re
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
        # 关键帧（可选）：[{t: 秒, regions: [...]}, ...]；段内水印位置按时间线性插值
        kfs = []
        for k in (seg.get("keyframes") or []):
            if not isinstance(k, dict):
                raise app.HTTPException(status_code=400, detail=f"segments[{i}] 的 keyframes 项必须是对象")
            try:
                kt = float(k.get("t") or 0)
            except (TypeError, ValueError):
                raise app.HTTPException(status_code=400, detail=f"segments[{i}] keyframe.t 必须是数字")
            kregs = dwc.normalize_regions(k.get("regions") or [])
            if not kregs:
                raise app.HTTPException(status_code=400, detail=f"segments[{i}] keyframe 缺少有效框选区域")
            kfs.append({"t": kt, "regions": kregs})
            if len(kfs) > 50:
                raise app.HTTPException(status_code=400, detail=f"segments[{i}] 关键帧最多 50 个")
        out.append({"start": s, "end": e, "regions": regs, "keyframes": kfs})
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


# ---------------------------------------------------------------- 转码编码器选择（预览/输出提速，2026-08-29）
def _ffprobe_bin() -> str:
    """从 ffmpeg 同级目录找 ffprobe，找不到再退回 PATH。"""
    fb = getattr(app, "FFMPEG_BIN", "") or ""
    if fb:
        cand = app.os.path.join(app.os.path.dirname(fb), "ffprobe")
        if app.os.path.exists(cand):
            return cand
    return app.shutil.which("ffprobe") or "ffprobe"


_VT_OK = None  # 缓存：本机 ffmpeg 是否带 h264_videotoolbox（macOS 桌面端有，Linux/Railway 无）


def _videotoolbox_available() -> bool:
    """探测 VideoToolbox 硬编是否可用（macOS 桌面端常见）。结果缓存，避免每次转码都跑。"""
    global _VT_OK
    if _VT_OK is None:
        try:
            proc = _subprocess.run(
                [app.FFMPEG_BIN, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=15,
            )
            _VT_OK = "h264_videotoolbox" in (proc.stdout or "")
        except Exception:  # noqa: BLE001
            _VT_OK = False
    return _VT_OK


def _probe_streams(src: str) -> dict:
    """用 ffprobe 读取视频/音频流关键信息，用于判断是否已是 WebKit 可直接播放的格式。"""
    info = {"v_codec": "", "v_profile": "", "v_level": 0.0,
            "v_pixfmt": "", "v_w": 0, "v_h": 0,
            "has_audio": False, "a_codec": ""}
    try:
        proc = _subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-show_entries",
             "stream=codec_name,profile,level,pix_fmt,width,height,codec_type",
             "-of", "json", str(src)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(proc.stdout or "{}")
        for s in data.get("streams", []):
            ctype = s.get("codec_type")
            if ctype == "video" and not info["v_codec"]:
                info["v_codec"] = (s.get("codec_name") or "").lower()
                info["v_profile"] = (s.get("profile") or "").lower()
                try:
                    info["v_level"] = float(s.get("level") or 0)
                except (TypeError, ValueError):
                    info["v_level"] = 0.0
                info["v_pixfmt"] = (s.get("pix_fmt") or "").lower()
                try:
                    info["v_w"] = int(s.get("width") or 0)
                    info["v_h"] = int(s.get("height") or 0)
                except (TypeError, ValueError):
                    info["v_w"] = info["v_h"] = 0
            elif ctype == "audio":
                info["has_audio"] = True
                info["a_codec"] = (s.get("codec_name") or "").lower()
    except Exception:  # noqa: BLE001
        pass
    return info


def _webkit_playable(info: dict) -> bool:
    """判断源是否已是 WKWebView <video> 可直接稳定播放的格式（2026-08-29 实测 main@L4.0+yuv420p 可播）。

    命中则预览可「流拷贝」跳过重新编码（瞬时），否则必须转码。
    """
    if info["v_codec"] != "h264":
        return False
    if info["v_pixfmt"] != "yuv420p":
        return False
    if info["v_profile"] not in ("main", "baseline", "constrained baseline"):
        return False
    if info["v_level"] > 40.0:  # level 4.0 已验证可播；更高保守重编码
        return False
    if info["has_audio"] and info["a_codec"] != "aac":
        return False
    return True


def _preview_bitrate(info: dict) -> str:
    """按分辨率给预览转码一个码率上限（约束体积，便于边下边播）。"""
    w, h = info.get("v_w", 0), info.get("v_h", 0)
    if w >= 3000 or h >= 1500:
        return "8000k"   # ~4K
    if w >= 1800 or h >= 1000:
        return "3000k"   # 1080p
    return "1500k"       # 720p 及以下


def _preview_encode_cmd(src: str, out: str, use_vt: bool, info: dict) -> list:
    """构造预览转码命令：VideoToolbox 硬编（macOS）或 libx264 软编（Linux/回退）。"""
    if use_vt:
        return [app.FFMPEG_BIN, "-y", "-i", str(src),
                "-c:v", "h264_videotoolbox",
                "-profile:v", "main", "-level", "4.0",
                "-b:v", _preview_bitrate(info),
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out)]
    return [app.FFMPEG_BIN, "-y", "-i", str(src),
            "-c:v", "libx264", "-profile:v", "main", "-level", "4.0",
            "-preset", "veryfast", "-crf", "26",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(out)]


def _cleanup_dw_job(job_id: str, remove_out: bool = False) -> None:
    """清理已暂停/取消/完成任务的磁盘中间产物（work_dir + 上传原件）。
    remove_out=True 时连结果文件也删（取消场景）。"""
    job = app.DW_JOBS.get(job_id, {})
    wd = job.get("work_dir")
    if wd and app.os.path.isdir(wd):
        app.shutil.rmtree(wd, ignore_errors=True)
    sp = job.get("src_path")
    if sp and app.os.path.exists(sp):
        try:
            app.os.remove(sp)
        except Exception:  # noqa: BLE001
            pass
    if remove_out:
        op = job.get("out_path")
        if op and app.os.path.exists(op):
            try:
                app.os.remove(op)
            except Exception:  # noqa: BLE001
                pass


def _run_video(job_id: str, src: str, regions, ffmpeg_bin: str, resolution: str, smooth: bool,
               start_sec: float = 0.0, end_sec: float = 0.0, segments=None,
               target_fps: float = 30.0,
               temporal_stride: int = 4,
               work_dir: str = None) -> None:
    """后台跑视频去水印。支持暂停/取消信号（帧边界检查），可被续跑（work_dir 持久化）。
    target_fps：目标输出帧率，默认 30，0 表示按源帧率（HFR/VFR 视频推荐 30）。"""
    job = app.DW_JOBS.get(job_id)
    if not job:
        return
    try:
        if not dwc_ai.available():
            raise RuntimeError("AI 去水印不可用（缺少 onnxruntime 依赖或模型未下载）")
        out_path = app.DW_DIR / f"dw_vid_{job_id}.mp4"

        def _cancel_check():
            return job.get("__signal__", "")

        # 注意：frozen 桌面端 ai_video_inpaint 内部同进程跑（与图片模式一致），
        # 子进程隔离在桌面端派发不可靠，故不走 subprocess。
        # phase_cb 同时承载 inpaint_count（如 "inpaint_count:204"），前端用它区分
        # "实际要推理的帧" vs "全视频总帧"。
        # progress 第三参数 kind ∈ {ai, copy, interp, skip}：用于前端做 AI 推理子进度
        # 与更准确的 ETA（区间外 / 插值复用帧不进 ai_done 计数）。
        def _on_progress(done, total, kind=""):
            job.__setitem__("progress", f"{done}/{total}")
            if kind == "ai":
                job["ai_done"] = int(job.get("ai_done", 0) or 0) + 1
        dwc_ai.ai_video_inpaint(
            app.Path(src), out_path, regions, ffmpeg_bin,
            progress_cb=_on_progress,
            resolution=resolution, smooth=smooth,
            start_sec=start_sec, end_sec=end_sec, segments=segments,
            target_fps=target_fps,
            temporal_stride=temporal_stride,
            work_dir=work_dir, cancel_check=_cancel_check,
            phase_cb=lambda p: (
                job.__setitem__("phase", p) if not (isinstance(p, str) and p.startswith("inpaint_count:")) else
                job.__setitem__("inpaint_count", int(p.split(":", 1)[1]) or 0)
            ),
        )
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("视频去水印未产出有效文件")
        job["status"] = "completed"
        job["filename"] = out_path.name
        job["out_path"] = str(out_path)
        job["__signal__"] = ""
        app.logger.info("dw video %s done -> %s", job_id, out_path.name)
        _cleanup_dw_job(job_id)  # 完成：清理中间产物与上传原件（结果保留供下载）
    except dwc_ai._DwPause:
        job["status"] = "paused"
        job["error"] = ""
        app.logger.info("dw video %s paused at %s", job_id, job.get("progress"))
    except dwc_ai._DwCancel:
        job["status"] = "cancelled"
        job["error"] = ""
        _cleanup_dw_job(job_id, remove_out=True)
        app.logger.info("dw video %s cancelled", job_id)
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
    target_fps: float = app.Form(15.0),  # 默认 15 fps — 静态水印 + 邻帧平滑下肉眼无差（2026-08-29 由 30 调到 15）
    temporal_stride: int = app.Form(4),  # 默认 4：每 4 帧推理 1 次（wave2 ② 时间稀疏）
    request: app.Request = None,
) -> dict:
    """视频去水印：上传视频 + 多选区 regions（归一化，整段视频套同一掩码）。

    逐帧跑 LaMa 推理 + 邻帧中值平滑降闪烁，ffmpeg 重编码混音输出 mp4。

    **目标帧率 target_fps（2026-08-29 加，默认 30）**：抽帧时强制按 target_fps 做等时间抽样，
    避免 HFR（240/960fps）、VFR（可变帧率）或 metadata 异常的源视频把 5s 内容抽成几千帧。
    水印视觉稳定 → 30 fps 完全够用。0 表示按源帧率（向后兼容）。

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
    if resolution not in ("original", "720", "1080", "480"):
        raise app.HTTPException(status_code=400, detail="resolution 仅支持 original / 720 / 1080 / 480")
    if start_sec < 0 or end_sec < 0:
        raise app.HTTPException(status_code=400, detail="start_sec / end_sec 不能为负数")
    if end_sec > 0 and start_sec > 0 and end_sec < start_sec:
        raise app.HTTPException(status_code=400, detail="end_sec 需大于或等于 start_sec")
    # target_fps 范围：0=按原帧率；1..120 区间防呆
    if target_fps < 0 or target_fps > 120:
        raise app.HTTPException(status_code=400, detail="target_fps 需在 0..120（0=按原帧率）")
    # temporal_stride 范围：1..30 区间防呆（1=逐帧，越大越快但插值时长越久）
    if temporal_stride < 1 or temporal_stride > 30:
        raise app.HTTPException(status_code=400, detail="temporal_stride 需在 1..30（1=逐帧，越大越快）")
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
    work_dir = str(app.DW_DIR / f"dw_work_{job_id}")
    with app.DW_LOCK:
        app.DW_JOBS[job_id] = {
            "status": "running", "out_path": "", "error": "", "filename": "",
            "kind": "video", "progress": "",
            "ai_done": 0,
            # 续跑所需：上传原件路径 + 持久化工作目录 + 原始参数 + 控制信号
            "src_path": str(save_path), "work_dir": work_dir, "__signal__": "",
            "regions": regions_list, "segments": segments_list,
            "resolution": resolution, "smooth": bool(int(smooth)),
            "start_sec": float(start_sec), "end_sec": float(end_sec),
            "target_fps": float(target_fps),
            "temporal_stride": int(temporal_stride),
        }
    app.executor.submit(_run_video, job_id, str(save_path), regions_list, app.FFMPEG_BIN,
                        resolution, bool(int(smooth)), float(start_sec), float(end_sec), segments_list,
                        float(target_fps), int(temporal_stride),
                        work_dir)
    return {"job_id": job_id, "status": "running", "kind": "video",
            "target_fps": float(target_fps)}


@router.post("/api/dw/ai/warmup")
def warmup_ai_engine(request: app.Request = None) -> dict:
    """后台预热 AI 去水印模型：用户选完视频后触发，把最耗时的模型加载从点击「开始」的
    关键路径挪到框选的空闲期，消除「点击开始后长时间 0 帧」的假死观感。

    模型在进程内缓存，重复调用几乎无成本；内存不足 / 模型缺失时静默失败不影响主流程。
    """
    app._check_rate_limit(request)
    app.executor.submit(dwc_ai.warmup)
    return {"ok": True, "status": "warming"}


@router.get("/api/dw/video/{job_id}")
def dw_video_status(job_id: str) -> dict:
    job = app.DW_JOBS.get(job_id)
    if not job or job.get("kind") != "video":
        raise app.HTTPException(status_code=404, detail="视频去水印任务不存在")
    return {
        "status": job["status"], "error": job.get("error", ""),
        "filename": job.get("filename", ""), "progress": job.get("progress", ""),
        "target_fps": float(job.get("target_fps", 30.0)),
        "phase": job.get("phase", ""),
        "inpaint_count": int(job.get("inpaint_count", 0) or 0),
        "ai_done": int(job.get("ai_done", 0) or 0),
    }


@router.post("/api/dw/video/{job_id}/pause")
def dw_video_pause(job_id: str) -> dict:
    """请求暂停：设信号，循环在下一帧边界优雅停止并保留中间产物。"""
    job = app.DW_JOBS.get(job_id)
    if not job or job.get("kind") != "video":
        raise app.HTTPException(status_code=404, detail="视频去水印任务不存在")
    if job["status"] != "running":
        raise app.HTTPException(status_code=409, detail="仅运行中的任务可暂停")
    job["__signal__"] = "pause"
    return {"status": "pausing"}


@router.post("/api/dw/video/{job_id}/resume")
def dw_video_resume(job_id: str) -> dict:
    """从持久化 work_dir 续跑：跳过已完成帧，继续剩余推理。"""
    job = app.DW_JOBS.get(job_id)
    if not job or job.get("kind") != "video":
        raise app.HTTPException(status_code=404, detail="视频去水印任务不存在")
    if job["status"] != "paused":
        raise app.HTTPException(status_code=409, detail="仅已暂停的任务可继续")
    job["__signal__"] = ""
    job["status"] = "running"
    app.executor.submit(_run_video, job_id, job["src_path"], job.get("regions"), app.FFMPEG_BIN,
                        job.get("resolution"), job.get("smooth"), job.get("start_sec", 0),
                        job.get("end_sec", 0), job.get("segments"), job.get("target_fps", 15.0),
                        job.get("temporal_stride", 4), job["work_dir"])
    return {"status": "running"}


@router.post("/api/dw/video/{job_id}/cancel")
def dw_video_cancel(job_id: str) -> dict:
    """取消任务：运行中设信号（帧边界清理）；已暂停则直接清理。"""
    job = app.DW_JOBS.get(job_id)
    if not job or job.get("kind") != "video":
        raise app.HTTPException(status_code=404, detail="视频去水印任务不存在")
    if job["status"] in ("completed", "cancelled"):
        raise app.HTTPException(status_code=409, detail="任务已完成或已取消，无法取消")
    if job["status"] == "paused":
        job["status"] = "cancelled"
        job["error"] = ""
        _cleanup_dw_job(job_id, remove_out=True)
        return {"status": "cancelled"}
    job["__signal__"] = "cancel"
    return {"status": "cancelling"}


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


@router.post("/api/dw/video/filmstrip")
def create_dw_video_filmstrip(
    file: app.UploadFile = app._FastAPIFile(...),
    frames: int = app.Form(20),
    request: app.Request = None,
):
    """抽 N 帧拼成横向缩略图条（filmstrip），返回 PNG。

    2026-08-29 实测：macOS WKWebView 的 `<video>` 元素对不少 mp4（H.264 high@L4 / HEVC）
    播放/首帧都不可靠，**video 元素作预览源（包括播放）整体不可依赖**。前端改用
    ffmpeg 抽帧拼成的静态缩略图条 + click-to-seek 完全绕开 video 元素，img 渲染稳定可靠。
    默认抽 20 帧（4..60），返回头 `X-Filmstrip-Frames` / `X-Filmstrip-Interval` 告诉前端
    每帧对应多少秒（前端 click 时按图宽等分定位时间）。
    """
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower()
    if suffix not in DW_VIDEO_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传视频文件（mp4/mov/mkv/webm/avi 等）")
    if not (4 <= int(frames) <= 60):
        raise app.HTTPException(status_code=400, detail="frames 需在 4..60 之间")
    save_path = _save_upload(file, "dw_vid_film")
    out_path = save_path.with_suffix(".png")
    try:
        # 1) 查时长
        probe = _subprocess.run([app.FFMPEG_BIN, "-i", str(save_path)], capture_output=True, timeout=30)
        m = _re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", probe.stderr.decode("utf-8", "ignore"))
        if not m:
            err_tail = probe.stderr.decode("utf-8", "ignore")[-300:] if probe.stderr else "unknown"
            raise RuntimeError(f"无法解析视频时长：{err_tail or 'unknown'}")
        hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
        duration = hh * 3600 + mm * 60 + ss
        if duration <= 0:
            raise RuntimeError("视频时长为 0 或无法解析")
        # 2) 抽 N 帧 + 等比缩放 + 1xN 拼图
        n = int(frames)
        # 2) 抽 N 帧 + 等比缩放 + Nx1 横条拼图（20 列 1 行 = 横向 filmstrip）
        #    横向布局：time 轴从左到右，click 位置 = 时间位置
        fps_value = n / duration
        cmd = [
            app.FFMPEG_BIN, "-y", "-i", str(save_path),
            "-vf", f"fps={fps_value},scale=160:-1,tile={n}x1",
            "-frames:v", "1", "-an", str(out_path),
        ]
        proc = _subprocess.run(cmd, capture_output=True, timeout=60)
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            err_tail = proc.stderr.decode("utf-8", "ignore")[-300:] if proc.stderr else "unknown"
            raise RuntimeError(f"ffmpeg filmstrip 失败：{err_tail or 'unknown'}")
        save_path.unlink(missing_ok=True)
        interval = duration / n
    except app.HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        save_path.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)
        raise app.HTTPException(status_code=500, detail=f"filmstrip 抽取失败：{e}")

    resp = app.FileResponse(path=str(out_path), media_type="image/png")
    resp.headers["X-Filmstrip-Frames"] = str(int(frames))
    resp.headers["X-Filmstrip-Interval"] = f"{interval:.3f}"
    return resp


# ---------------------------------------------------------------- 播放预览转码（让所有格式都能播）

def _preview_cache_key(src: str) -> str:
    """源文件指纹：大小 + mtime + 头部 8MB sha256。
    弱但够区分不同文件，避免全量读取大视频计算 hash。无法读则每次不同（不缓存）。"""
    import hashlib
    try:
        st = app.os.stat(src)
        h = hashlib.sha256()
        h.update(f"{st.st_size}:{st.st_mtime:.3f}".encode("utf-8"))
        try:
            with open(src, "rb") as fh:
                h.update(fh.read(8 * 1024 * 1024))
        except Exception:  # noqa: BLE001
            pass
        return h.hexdigest()[:32]
    except Exception:  # noqa: BLE001
        return app.uuid.uuid4().hex


def _run_preview_transcode(preview_id: str, src: str) -> None:
    """后台把任意格式视频转码为 WebKit 通用格式：H.264 main@L4.0 + yuv420p + AAC + faststart。

    macOS WKWebView 的 `<video>` 对 HEVC / H.264 high@L4 / 10-bit / yuv444 等编码直接黑屏
    （2026-08-29 实测）。优化策略（2026-08-29）：
      1) 若源已是 WebKit 可直接播放的格式 → 仅流拷贝（瞬时，不重编码）；
      2) 否则优先 VideoToolbox 硬件编码（macOS 近实时），无硬编环境回退 libx264 软编。
    任选路径失败都会回退到 libx264 软编，保证鲁棒。
    """
    job = app.DW_JOBS.get(preview_id)
    if not job:
        return
    cache_dir = app.DW_DIR / "preview_cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    key = _preview_cache_key(src)
    cached = cache_dir / f"prev_{key}.mp4"
    try:
        # 0) 缓存命中：同文件（指纹相同）直接复用，省去转码
        if cached.exists() and cached.stat().st_size > 0:
            app.logger.info("dw preview %s cache HIT (%s)", preview_id, key)
            try:
                app.Path(src).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
            job["status"] = "completed"
            job["out_path"] = str(cached)
            return
        out_path = app.DW_DIR / f"dw_prev_{preview_id}.mp4"
        info = _probe_streams(src)
        if _webkit_playable(info):
            # 已兼容：流拷贝，仅把 moov 前置以支持边下边播（瞬时）
            cmd = [app.FFMPEG_BIN, "-y", "-i", str(src),
                   "-c", "copy", "-movflags", "+faststart", str(out_path)]
            tried = "copy"
        elif _videotoolbox_available():
            cmd = _preview_encode_cmd(src, out_path, True, info)
            tried = "videotoolbox"
        else:
            cmd = _preview_encode_cmd(src, out_path, False, info)
            tried = "libx264"
        proc = _subprocess.run(cmd, capture_output=True, timeout=1800)
        if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            # 首选路径失败 → 回退 libx264 软编（最稳妥）
            app.logger.warning("dw preview %s 首选[%s]失败，回退 libx264", preview_id, tried)
            cmd = _preview_encode_cmd(src, out_path, False, info)
            proc = _subprocess.run(cmd, capture_output=True, timeout=1800)
            if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
                err_tail = proc.stderr.decode("utf-8", "ignore")[-300:] if proc.stderr else "unknown"
                raise RuntimeError(f"转码失败：{err_tail or 'unknown'}")
        # 写入缓存供同文件复用（避免重复转码）
        try:
            app.shutil.copyfile(out_path, cached)
        except Exception:  # noqa: BLE001
            pass
        # 转完删除上传的原件（预览只需转码产物）
        try:
            app.Path(src).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        job["status"] = "completed"
        job["out_path"] = str(out_path)
        app.logger.info("dw video preview %s done -> %s (cache %s)", preview_id, out_path.name, key)
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        app.logger.warning("dw video preview %s failed: %s", preview_id, e)


@router.post("/api/dw/video/preview")
def create_dw_video_preview(
    file: app.UploadFile = app._FastAPIFile(...),
    request: app.Request = None,
) -> dict:
    """上传视频并后台转码为 WebKit 通用格式，用于前端 `<video>` 稳定播放。

    返回 preview_id；前端轮询 /api/dw/video/preview/{id}/status，
    completed 后把 <video src> 指向 /api/dw/video/preview/{id} 即可播放并拖动进度条。
    """
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.mp4").suffix.lower()
    if suffix not in DW_VIDEO_EXTS:
        raise app.HTTPException(status_code=409, detail="请上传视频文件（mp4/mov/mkv/webm/avi 等）")
    save_path = _save_upload(file, "dw_prev_up")
    preview_id = app.uuid.uuid4().hex[:12]
    with app.DW_LOCK:
        app.DW_JOBS[preview_id] = {
            "status": "running", "out_path": "", "error": "", "filename": "",
            "kind": "preview",
        }
    app.executor.submit(_run_preview_transcode, preview_id, str(save_path))
    return {"preview_id": preview_id, "status": "running"}


@router.get("/api/dw/video/preview/{preview_id}/status")
def dw_video_preview_status(preview_id: str) -> dict:
    job = app.DW_JOBS.get(preview_id)
    if not job or job.get("kind") != "preview":
        raise app.HTTPException(status_code=404, detail="转码任务不存在")
    return {"status": job["status"], "error": job.get("error", "")}


@router.get("/api/dw/video/preview/{preview_id}")
def dw_video_preview_file(preview_id: str) -> app.FileResponse:
    """返回转码后的 MP4。Starlette FileResponse 原生支持 Range 请求（可拖进度条）。"""
    job = app.DW_JOBS.get(preview_id)
    if not job or job.get("kind") != "preview":
        raise app.HTTPException(status_code=404, detail="转码任务不存在")
    if job["status"] != "completed":
        raise app.HTTPException(status_code=409, detail="转码尚未完成")
    out = app.Path(job["out_path"])
    if not out.exists():
        raise app.HTTPException(status_code=410, detail="结果文件已清理")
    return app.FileResponse(path=str(out), filename=out.name, media_type="video/mp4")


@router.post("/api/dw/video/preview/{preview_id}/open")
def dw_video_preview_open(preview_id: str) -> dict:
    """调 macOS 系统默认播放器（QuickTime / IINA / VLC…）打开转码后的 MP4。

    背景：app 端 WKWebView <video> 元素即使 controls=false 也会渲染 right edge PiP
    控件（macOS Safari 设计，CSS / hidden 属性都拦不住），无法在 app 内静默预览。
    退而求其次：用户在 app 内「点击播放」→ 调系统播放器跳出 app 看视频（macOS 标准
    行为），回到 app 继续框选。

    仅在 macOS 下生效；其他平台返回 501 让前端降级到 window.open。
    """
    job = app.DW_JOBS.get(preview_id)
    if not job or job.get("kind") != "preview":
        raise app.HTTPException(status_code=404, detail="转码任务不存在")
    if job["status"] != "completed":
        raise app.HTTPException(status_code=409, detail="转码尚未完成")
    out = app.Path(job["out_path"])
    if not out.exists():
        raise app.HTTPException(status_code=410, detail="结果文件已清理")
    if app.sys.platform != "darwin":
        raise app.HTTPException(status_code=501, detail="仅 macOS 支持此端点")
    try:
        # `open <file>` 用系统默认 app 打开（QuickTime 默认；用户装了 IINA/VLC 会接管）
        # 不阻塞 — Popen 后立即返回，让前端不卡
        _subprocess.Popen(
            ["open", str(out.resolve())],
            stdout=_subprocess.DEVNULL,
            stderr=_subprocess.DEVNULL,
            start_new_session=True,  # 独立进程组，关闭 app 不影响播放器
        )
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"打开失败：{e}")
    return {"opened": True, "path": str(out.resolve()), "platform": "darwin"}