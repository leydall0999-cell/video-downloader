"""server/routers/sr.py — 图片高清修复（快速档 / AI 档）。2026-09-12 新增，桌面端优先。

两档**完全本地**，不接任何云端 API、不需要 API Key、不产生按次费用：

- **快速档**：Pillow ``LANCZOS`` 放大 + ``UnsharpMask`` 锐化。秒级完成，任何 Mac 都流畅。
- **AI 档**：Real-ESRGAN ONNX（BSD-3-Clause，合规）分块推理，CoreML 优先，真实细节重建。

设计依据（均为 2026-09-12 本机实测，非估算）：

- M1 CoreML 上 x2plus 约 2.4 s / 512×512、13.7 s / 720p；**纯 CPU（Intel Mac）再慢约 5.6 倍**。
  ⇒ Intel 上用更小的 tile（256 vs 512）与更低的输入上限（1024 vs 2048）控制内存与耗时。
- ⚠️ CoreML **绝不能**显式指定 ``MLComputeUnits=CPUAndNeuralEngine``：实测比默认配置慢 3 倍
  （ANE 对这类密集卷积支持不全，频繁回退搬运数据）。这里**不传任何 provider_options**。
- **视频 AI 超分不可行**：抽帧 1/4 下 1 分钟片仍需 29 分钟（Intel 1.9 小时），且 x2plus 是
  图片模型、逐帧独立推理无时域一致性会有帧间闪烁；轻量视频模型（animevideov3 / generalv3）
  也无可用 ONNX 权重。故本模块**只做图片**，视频增强走 ffmpeg 传统滤镜（见视频增强模块）。

⚠️ 结果下载必须接三处，缺一处＝点了没反应（WKWebView 不弹 ``<a download>``）：
① 前端 ``wireSaveConvertDownload(el.<列表>)``；② href 拦截正则认得该 URL；
③ 桌面桥 ``save_convert_file_dialog`` 能查到 ``SR_JOBS``。

handler 通过 ``app.<name>`` 访问共享内核（与 compress.py 等路由约定一致）。
"""
from __future__ import annotations

import app
import os
import threading
import time
import urllib.request
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from .core import _device_of
from stats import record_event

# --------------------------------------------------------------------------- #
# 依赖守护：AI 档按需，缺失时快速档仍可正常工作
try:  # pragma: no cover - 缺失属正常运行态
    import numpy as np
except Exception:  # noqa: BLE001
    np = None
try:  # pragma: no cover
    import onnxruntime as ort
except Exception:  # noqa: BLE001
    ort = None
try:  # pragma: no cover
    from PIL import Image, ImageFilter
except Exception:  # noqa: BLE001
    Image = None
    ImageFilter = None

router = APIRouter()

# --------------------------------------------------------------------------- #
# 常量
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MODES = {"fast", "ai"}
SCALES = {2, 4}
_DEFAULT_MODE = "fast"
_DEFAULT_SCALE = 2

SR_JOBS: dict = {}
_LOCK = threading.Lock()

# AI 档输入上限（长边，px）。超出会先等比缩小再超分 —— 直接用大图分块推理会让
# 输出缓冲占用数百 MB（4000×3000 ×2 ⇒ float32 约 576 MB），8GB 机器上有 OOM 风险。
_MAX_SIDE_COREML = 2048
_MAX_SIDE_CPU = 1024
_TILE_COREML = 512
_TILE_CPU = 256

# 模型缓存目录（与 matting_ai / dewatermark_ai 一致）
def _model_dir() -> Path:
    raw = os.environ.get("VDL_MODELS_DIR")
    base = Path(raw) if raw else Path.home()
    d = base / ".vdl_models" / "sr"
    d.mkdir(parents=True, exist_ok=True)
    return d


# Real-ESRGAN ONNX（BSD-3-Clause）。国内镜像在前，失败自动回退官方源。
_SR_MODELS: dict[str, dict] = {
    "x2": {
        "filename": "real_esrgan_x2.onnx",
        "min_bytes": 5_000_000,
        "urls": [
            "https://hf-mirror.com/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x2.onnx",
            "https://huggingface.co/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x2.onnx",
        ],
    },
    "x4": {
        "filename": "real_esrgan_x4.onnx",
        "min_bytes": 5_000_000,
        "urls": [
            "https://hf-mirror.com/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x4.onnx",
            "https://huggingface.co/SceneWorks/real-esrgan-onnx/resolve/main/real_esrgan_x4.onnx",
        ],
    },
}

_DL_LOCK = threading.Lock()
_DL: dict = {"active": False, "model": "", "pct": 0.0, "done": 0, "total": 0, "error": ""}
_SESSIONS: dict[str, object] = {}
_PROVIDER: str = ""


def _set_dl(**kw) -> None:
    with _DL_LOCK:
        _DL.update(kw)


def download_progress() -> dict:
    """供前端展示模型下载进度。"""
    with _DL_LOCK:
        return dict(_DL)


def available() -> bool:
    """AI 档是否可用（依赖齐全即可，模型会按需下载）。"""
    return np is not None and ort is not None and Image is not None


def _local_path(key: str) -> Path:
    return _model_dir() / _SR_MODELS[key]["filename"]


def _valid_cached(key: str) -> bool:
    p = _local_path(key)
    return p.exists() and p.stat().st_size > _SR_MODELS[key]["min_bytes"]


def _ensure_model(key: str, job: dict | None = None) -> Path:
    """确保模型在本地；缺失则依次尝试各镜像下载（带进度 + .part 原子落盘）。"""
    if _valid_cached(key):
        return _local_path(key)
    meta = _SR_MODELS[key]
    dest = _local_path(key)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err = ""
    for url in meta["urls"]:
        try:
            _set_dl(active=True, model=key, done=0, total=0, pct=0.0, error="")
            if job is not None:
                job["stage"] = "下载 AI 模型中"
            req = urllib.request.Request(url, headers={"User-Agent": "VDL/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp, tmp.open("wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                _set_dl(total=total)
                done = 0
                last_emit = 0.0
                while True:
                    buf = resp.read(1024 * 256)
                    if not buf:
                        break
                    fh.write(buf)
                    done += len(buf)
                    now = time.time()
                    if now - last_emit > 0.4 or (total and done >= total):
                        last_emit = now
                        pct = round(done / total * 100, 1) if total else 0.0
                        _set_dl(done=done, pct=pct)
                        if job is not None:
                            job["progress"] = min(30, int(pct * 0.3))
            if not tmp.exists() or tmp.stat().st_size < meta["min_bytes"]:
                raise RuntimeError("下载结果不完整")
            tmp.replace(dest)  # 原子落盘
            _set_dl(active=False, pct=100.0, done=dest.stat().st_size)
            return dest
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:300]
            tmp.unlink(missing_ok=True)
            _set_dl(active=False, error=last_err)
            continue
    raise RuntimeError(
        f"高清修复模型下载失败（已尝试 {len(meta['urls'])} 个源）：{last_err or '未知错误'}"
    )


def _get_session(key: str):
    """懒加载 ONNX session。

    ⚠️ 只按默认参数创建 CoreML —— 显式传 ``MLComputeUnits`` 会显著变慢（见模块 docstring）。
    """
    global _PROVIDER
    if key in _SESSIONS:
        return _SESSIONS[key], _PROVIDER
    path = _ensure_model(key)
    providers = ["CPUExecutionProvider"]
    if ort is not None and "CoreMLExecutionProvider" in ort.get_available_providers():
        providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
    sess = ort.InferenceSession(str(path), providers=providers)
    _PROVIDER = sess.get_providers()[0]
    _SESSIONS[key] = sess
    return sess, _PROVIDER


def _coreml_active() -> bool:
    return _PROVIDER == "CoreMLExecutionProvider"


# --------------------------------------------------------------------------- #
# 两档实现


def _sr_fast(img, scale: int) -> "Image.Image":
    """快速档：Lanczos 放大 + 非锐化掩膜。

    说明：ffmpeg 的 ``cas`` 是视频滤镜；图片这里用 Pillow 的 ``UnsharpMask``
    （等价的传统锐化），无需拉起 ffmpeg 进程，秒级完成。
    """
    w, h = img.size
    out = img.resize((w * scale, h * scale), Image.LANCZOS)
    out = out.filter(ImageFilter.UnsharpMask(radius=2, percent=140, threshold=3))
    return out


def _sr_ai(img, key: str, job: dict) -> "Image.Image":
    """AI 档：Real-ESRGAN 分块推理（overlap 加权混合，避免块间接缝）。"""
    sess, provider = _get_session(key)
    input_name = sess.get_inputs()[0].name
    scale = 2 if key == "x2" else 4
    tile = _TILE_COREML if provider == "CoreMLExecutionProvider" else _TILE_CPU
    max_side = _MAX_SIDE_COREML if provider == "CoreMLExecutionProvider" else _MAX_SIDE_CPU

    rgb = img.convert("RGB")
    w0, h0 = rgb.size
    # 输入上限保护：超出先等比缩小（并在 note 中如实告知）
    if max(w0, h0) > max_side:
        ratio = max_side / float(max(w0, h0))
        nw, nh = max(1, int(w0 * ratio)), max(1, int(h0 * ratio))
        rgb = rgb.resize((nw, nh), Image.LANCZOS)
        job["note"] = (f"输入 {w0}×{h0} 超过 {max_side}px 上限，"
                       f"已先缩放到 {nw}×{nh} 再超分（输出 {nw * scale}×{nh * scale}）")

    src = np.asarray(rgb, dtype=np.float32) / 255.0
    h, w = src.shape[:2]
    H, W = h * scale, w * scale
    out = np.zeros((H, W, 3), dtype=np.float32)
    wmap = np.zeros((H, W, 1), dtype=np.float32)

    step = max(1, tile - 32)  # 32px 重叠，用加权平均消除块间接缝
    ys = list(range(0, h, step))
    xs = list(range(0, w, step))
    total = len(ys) * len(xs)
    done = 0
    t_start = time.time()

    for y in ys:
        for x in xs:
            y1, x1 = y, x
            y2, x2 = min(y + tile, h), min(x + tile, w)
            y1, x1 = max(0, y2 - tile), max(0, x2 - tile)
            chunk = src[y1:y2, x1:x2]
            ch, cw = chunk.shape[:2]
            if ch < tile or cw < tile:
                pad = np.zeros((tile, tile, 3), dtype=np.float32)
                pad[:ch, :cw] = chunk
                chunk = pad
            inp = np.transpose(chunk, (2, 0, 1))[None]
            pred = sess.run(None, {input_name: inp})[0][0]
            pred = np.transpose(pred, (1, 2, 0))
            ph, pw = ch * scale, cw * scale
            pred = pred[:ph, :pw]
            out[y1 * scale:y2 * scale, x1 * scale:x2 * scale] += pred
            wmap[y1 * scale:y2 * scale, x1 * scale:x2 * scale] += 1.0
            done += 1
            job["progress"] = 30 + int(done / total * 65)
            job["elapsed"] = round(time.time() - t_start, 1)
            if done >= 1 and total > 1:
                job["eta"] = round(job["elapsed"] / done * (total - done), 1)
            job["stage"] = f"AI 重建中（{done}/{total} 块）"

    out /= np.maximum(wmap, 1e-6)
    out = np.clip(out, 0.0, 1.0)
    return Image.fromarray((out * 255.0 + 0.5).astype(np.uint8), mode="RGB")


# --------------------------------------------------------------------------- #
# job 表与执行


def _register_job(device_id: str, src_name: str) -> str:
    job_id = app.uuid.uuid4().hex[:12]
    with _LOCK:
        SR_JOBS[job_id] = {
            "status": "running", "stage": "排队中", "progress": 0, "error": "",
            "out_path": "", "filename": "", "src_name": src_name,
            "device_id": device_id, "mode": "", "scale": 0,
            "size_before": 0, "size_after": 0,
            "w_before": 0, "h_before": 0, "w_after": 0, "h_after": 0,
            "note": "", "elapsed": 0.0, "eta": 0.0,
        }
    return job_id


def _run_sr(job_id: str, src: str, mode: str, scale: int, src_is_temp: bool) -> None:
    job = SR_JOBS.get(job_id)
    if not job:
        return
    job["mode"], job["scale"] = mode, scale
    out_path = None
    try:
        if Image is None:
            raise RuntimeError("图片处理库不可用（Pillow 缺失）")
        src_path = app.Path(src)
        job["size_before"] = src_path.stat().st_size

        with Image.open(src_path) as im:
            im.load()
            job["w_before"], job["h_before"] = im.size
            img = im.convert("RGB") if im.mode not in ("RGB", "L") else im.copy()

        if mode == "ai":
            if np is None or ort is None:
                raise RuntimeError("AI 档依赖不可用（onnxruntime / numpy 缺失），请改用快速档")
            job["stage"] = "准备 AI 模型"
            out_img = _sr_ai(img, "x2" if scale == 2 else "x4", job)
        else:
            job["stage"] = "快速放大中"
            job["progress"] = 30
            out_img = _sr_fast(img, scale)
            job["progress"] = 80

        job["w_after"], job["h_after"] = out_img.size
        ext = src_path.suffix.lower() or ".png"
        out_path = app.CONVERT_DIR / f"sr_{job_id}{ext}"
        job["stage"] = "写入文件"
        save_kwargs = {}
        if ext in (".jpg", ".jpeg"):
            save_kwargs = {"quality": 95, "subsampling": 0}
        elif ext == ".webp":
            save_kwargs = {"quality": 92}
        out_img.save(out_path, **save_kwargs)

        job["size_after"] = out_path.stat().st_size
        job["out_path"] = str(out_path)
        job["filename"] = f"{src_path.stem}_{scale}x{out_path.suffix}"
        job["status"] = "completed"
        job["stage"] = "完成"
        job["progress"] = 100
        job["elapsed"] = round(time.time() - job.get("_t0", time.time()), 1)
    except Exception as e:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        job["stage"] = "失败"
    finally:
        if src_is_temp:
            try:
                app.Path(src).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass


def _submit_sr(src: str, mode: str, scale: int, device_id: str,
               src_name: str = "", src_is_temp: bool = False) -> str:
    job_id = _register_job(device_id, src_name or app.Path(src).name)
    with _LOCK:
        SR_JOBS[job_id]["_t0"] = time.time()
    app.executor.submit(_run_sr, job_id, src, mode, scale, src_is_temp)
    return job_id


# --------------------------------------------------------------------------- #
# 校验与路由


def _validate_mode(mode: str) -> str:
    m = (mode or "").lower()
    if m not in MODES:
        return _DEFAULT_MODE
    return m


def _validate_scale(scale: int) -> int:
    try:
        s = int(scale)
    except Exception:  # noqa: BLE001
        return _DEFAULT_SCALE
    return s if s in SCALES else _DEFAULT_SCALE


class LocalSrRequest(BaseModel):
    """桌面端本地文件高清修复请求。"""
    local_path: str
    mode: str = _DEFAULT_MODE     # fast / ai
    scale: int = _DEFAULT_SCALE   # 2 / 4


@router.post("/api/sr/local")
def sr_local(payload: LocalSrRequest, request: app.Request) -> dict:
    """桌面版专用：本机绝对路径直接处理（免上传）。"""
    app._check_rate_limit(request)
    subscribed, free_used, free_daily = app._check_convert_quota(request)
    from .convert import _resolve_safe_local_path
    resolved = _resolve_safe_local_path(payload.local_path)
    if resolved.suffix.lower() not in IMAGE_EXTS:
        raise app.HTTPException(status_code=409, detail="仅支持图片（PNG/JPG/WebP/BMP）")
    mode = _validate_mode(payload.mode)
    scale = _validate_scale(payload.scale)
    job_id = _submit_sr(str(resolved), mode, scale, _device_of(request),
                        src_name=resolved.name, src_is_temp=False)
    record_event("sr_submit", {"mode": mode, "scale": scale, "src": "local"})
    return {"job_id": job_id, "status": "running", "mode": mode, "scale": scale,
            "quota": {"subscribed": subscribed, "free_used": free_used,
                      "free_daily": free_daily}}


@router.get("/api/sr/model/status")
def sr_model_status(request: app.Request) -> dict:
    """AI 档可用性 + 模型缓存状态 + 当前推理后端（前端据此提示预估耗时）。"""
    cached = {k: _valid_cached(k) for k in _SR_MODELS}
    return {"available": available(), "cached": cached,
            "provider": _PROVIDER or ("coreml" if (ort and "CoreMLExecutionProvider"
                                                   in ort.get_available_providers()) else "cpu"),
            "download": download_progress(),
            "max_side": _MAX_SIDE_COREML if _coreml_active() or not _PROVIDER else _MAX_SIDE_CPU}


@router.get("/api/sr/{job_id}")
def sr_status(job_id: str, request: app.Request) -> dict:
    """查询进度。

    ⚠️ 这里**故意不做限流**（与 compress / convert / matting 对齐，2026-09-11 教训）：
    前端每 1.5 s 轮询一次，AI 档一张图可能跑几十秒到几分钟，若把轮询计入 30 次/小时的
    配额，任务跑到一半就会被 429 掐断，进度条永久冻死。**只有提交类端点才限流。**
    """
    with _LOCK:
        job = SR_JOBS.get(job_id)
        if not job:
            raise app.HTTPException(status_code=404, detail="高清修复任务不存在或已过期")
        return dict(job)


@router.get("/api/sr/{job_id}/file")
def sr_file(job_id: str, request: app.Request) -> app.FileResponse:
    """下载结果（同 status：只读取件，不计入限流配额）。"""
    with _LOCK:
        job = SR_JOBS.get(job_id)
    if not job or job.get("status") != "completed" or not job.get("out_path"):
        raise app.HTTPException(status_code=404, detail="高清修复结果不存在")
    p = app.Path(job["out_path"])
    if not p.is_file():
        raise app.HTTPException(status_code=404, detail="结果文件已被清理")
    return app.FileResponse(p, filename=job.get("filename") or p.name,
                            media_type="application/octet-stream")
