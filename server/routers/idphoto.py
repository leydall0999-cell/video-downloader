"""server/routers/idphoto.py — 证件照制作（2026-10-10 竞品对标「水印云」certified 模块）。

链路：上传生活照 → 复用抠图引擎（matting_ai）分离人像 → 换标准底色 →
      按规格裁剪构图 → （可选）冲印排版 → 300dpi PNG 下载。

handler 通过 `app.<name>` 访问共享内核（与 routers/vision.py 同一约定）。
"""
import re

from PIL import Image

import app
import matting_ai as mat
from fastapi import APIRouter

router = APIRouter()

# ───────────────────────────── 常量 ─────────────────────────────
# 规格表只作展示用真源：前端持有一份用于下拉框，后端按 w/h 数值处理，
# 因此这里只定义**合法底色**（与竞品色板对齐：透明/白/黑/灰/蓝系/红系）。
ID_BG_COLORS = {
    "transparent": None,
    "white": (255, 255, 255),
    "black": (0, 0, 0),
    "gray": (153, 153, 153),
    "blue": (62, 84, 185),       # 标准证件蓝
    "lightblue": (147, 206, 250),
    "red": (254, 43, 34),        # 标准证件红
    "darkred": (139, 27, 27),
}
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ALLOWED_LAYOUT = (1, 4, 8)
# 任务表：与 DW_JOBS 同构（内存态，重启即清；结果文件落在 app.DW_DIR）
IDPHOTO_JOBS: dict[str, dict] = {}


def _bg_rgba(bg: str):
    """底色字符串 → RGBA 元组；`transparent` / 非法值回退透明。"""
    bg = (bg or "").strip()
    if bg in ID_BG_COLORS:
        rgb = ID_BG_COLORS[bg]
        return (0, 0, 0, 0) if rgb is None else rgb + (255,)
    if _HEX_RE.match(bg):
        return (int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16), 255)
    return (0, 0, 0, 0)


def compose_id_photo(src: str, out: str, w: int, h: int, bg: str, layout: int = 1) -> dict:
    """把抠好的 RGBA 人像合成到规格画布上。

    构图规则（对标常见证件照）：主体等比缩放至「宽 ≤78%、高 ≤86%」，
    水平居中、底部对齐（下边距约 3%），顶部自然留白——与竞品「人像占画布
    八成、可拖动微调」的默认位置一致，避免头顶切边。

    layout>1 时按 2 列网格拼到白底整版（冲印排版），单张仍保留原尺寸。
    返回 {"width","height","layout"} 供前端展示。
    """
    im = Image.open(src).convert("RGBA")
    bbox = im.split()[-1].getbbox()
    # bbox 为 None = 整张全透明（抠图没抠出任何主体）。此时若继续走合成，
    # 会静默产出一张纯底色废图——用户看起来像「做好了其实是空的」，必须显式报错。
    if bbox is None:
        raise RuntimeError("抠图结果为空，请换一张人像清晰、光线均匀的照片")
    im = im.crop(bbox)
    iw, ih = im.size
    if iw <= 0 or ih <= 0:
        raise RuntimeError("抠图结果为空，请换一张人像清晰、光线均匀的照片")
    scale = min(w * 0.78 / iw, h * 0.86 / ih)
    nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
    im = im.resize((nw, nh), Image.LANCZOS)

    single = Image.new("RGBA", (w, h), _bg_rgba(bg))
    x = (w - nw) // 2
    y = max(int(h * 0.03), int(h * 0.97) - nh)
    single.paste(im, (x, y), im)

    if layout <= 1:
        sheet = single
    else:
        cols = 2
        rows = layout // cols
        gap = max(8, int(w * 0.06))
        sheet = Image.new("RGB", (cols * w + (cols + 1) * gap, rows * h + (rows + 1) * gap), (255, 255, 255))
        flat = Image.new("RGB", single.size, (255, 255, 255))
        flat.paste(single, (0, 0), single)
        for i in range(rows * cols):
            r, c = divmod(i, cols)
            sheet.paste(flat, (gap + c * (w + gap), gap + r * (h + gap)))
    # 300dpi：冲印/打印按物理尺寸出图（25×35mm 等规格依赖 dpi 元数据）
    sheet.save(out, format="PNG", dpi=(300, 300))
    return {"width": sheet.width, "height": sheet.height, "layout": layout}


def _run(job_id: str, src: str, w: int, h: int, bg: str, layout: int, label: str) -> None:
    job = IDPHOTO_JOBS[job_id]
    rgba_path = app.DW_DIR / f"idp_src_{job_id}.png"
    try:
        job.update(phase="抠图", progress="正在分离人像与背景…")
        mat.matting_image(app.Path(src), rgba_path, meta=job)
        if not rgba_path.exists() or rgba_path.stat().st_size == 0:
            raise RuntimeError("抠图未产出有效结果")
        job.update(phase="合成", progress="正在换底色并排版…")
        out_path = app.DW_DIR / f"idp_{job_id}.png"
        info = compose_id_photo(str(rgba_path), str(out_path), w, h, bg, layout)
        job.update(status="completed", phase="", progress="", out_path=str(out_path),
                   filename=f"证件照_{label}_{w}x{h}.png", detail=info)
    except Exception as e:  # noqa: BLE001
        job.update(status="failed", phase="", progress="", error=str(e)[:500])
        app.logger.warning("idphoto %s failed: %s", job_id, e)
    finally:
        # 中间产物（抠图 RGBA）用完即删：结果图已独立落盘
        try:
            rgba_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


@router.post("/api/idphoto/make")
def create_id_photo(
    file: app.UploadFile = app._FastAPIFile(...),
    request: app.Request = None,
    w: int = app.Form(295),
    h: int = app.Form(413),
    bg: str = app.Form("blue"),
    layout: int = app.Form(1),
    label: str = app.Form("一寸"),
) -> dict:
    """上传生活照 → 抠图换底 → 按规格出证件照（异步任务）。"""
    app._check_rate_limit(request)
    suffix = app.Path(file.filename or "upload.png").suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        raise app.HTTPException(status_code=409, detail="请上传图片文件（png/jpg/webp/bmp）")
    if not (50 <= int(w) <= 3000) or not (50 <= int(h) <= 3000):
        raise app.HTTPException(status_code=400, detail="证件照尺寸需在 50~3000 像素之间")
    if int(layout) not in _ALLOWED_LAYOUT:
        raise app.HTTPException(status_code=400, detail="排版张数仅支持 1 / 4 / 8")
    save_path = app.DW_DIR / f"idp_up_{app.uuid.uuid4().hex[:12]}{suffix}"
    try:
        with save_path.open("wb") as f:
            f.write(file.file.read())
    except Exception as e:  # noqa: BLE001
        raise app.HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    job_id = app.uuid.uuid4().hex[:12]
    IDPHOTO_JOBS[job_id] = {
        "status": "running", "out_path": "", "error": "", "filename": "",
        "phase": "", "progress": "", "detail": {},
    }
    app.executor.submit(_run, job_id, str(save_path), int(w), int(h), bg, int(layout),
                        (label or "一寸")[:40])
    return {"job_id": job_id, "status": "running"}


@router.get("/api/idphoto/{job_id}")
def id_photo_status(job_id: str) -> dict:
    job = IDPHOTO_JOBS.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="证件照任务不存在")
    return {"status": job["status"], "error": job.get("error", ""),
            "filename": job.get("filename", ""), "detail": job.get("detail") or {},
            "phase": job.get("phase", ""), "progress": job.get("progress", "")}


@router.get("/api/idphoto/{job_id}/file")
def id_photo_file(job_id: str) -> app.FileResponse:
    job = IDPHOTO_JOBS.get(job_id)
    if not job:
        raise app.HTTPException(status_code=404, detail="证件照任务不存在")
    if job["status"] != "completed":
        raise app.HTTPException(status_code=409, detail="处理尚未完成")
    out = app.Path(job["out_path"])
    if not out.exists():
        raise app.HTTPException(status_code=410, detail="结果文件已清理")
    return app.FileResponse(path=str(out), filename=job.get("filename") or out.name,
                            media_type="application/octet-stream")
