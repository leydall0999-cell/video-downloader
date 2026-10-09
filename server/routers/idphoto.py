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

try:  # numpy / cv2 均随 App 打包（抠图链路已依赖）；缺失时本模块退化为纯 PIL 构图
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None
try:
    import cv2 as _cv2
except Exception:  # noqa: BLE001
    _cv2 = None

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


# ── 构图参数（2026-10-10 修「人物偏小 / 位置偏下」） ──
# 背景：原来只把「主体 bbox 整体」缩到 78%宽 / 86%高，没有区分头与身体。
# 半身生活照里身体占大头 ⇒ 脸被缩得很小；且抠图残留（原图水印 / 签名）会把
# bbox 撑大，人像在框内占比进一步变小、位置整体偏下。
# 现在：先清洗掩码 → 再按人像结构（头顶 / 脖子）走「头主导」构图，
# 结构不像人像时回退到放宽后的整体缩放。
_ALPHA_STRONG = 24       # 主体核心阈值（低于此视为抠图软边 / 背景残留）
_ALPHA_SOFT = 2          # 实际保留阈值（软边留一点点，避免头发被切硬）
_HEAD_W_TARGET = 0.62    # 头主导：头宽目标占画布宽（证件照标准 58%~69%）
_HEAD_H_TARGET = 0.68    # 头主导：头高（发顶→颈）目标占画布高（标准 63%~75%）
_HEAD_TOP_PAD = 0.085    # 头主导：头顶留白占画布高（标准 6%~12%）
_FALLBACK_W = 0.88       # 回退：主体宽上限
_FALLBACK_H = 0.90       # 回退：主体高上限


def _mask_region(alpha):
    """从 alpha 通道分离「主体区域」，滤掉抠图残留（原图水印 / 签名 / 孤立噪点）。

    返回 (strong, sel) 两张 bool 图：
      strong —— 主体核心区：阈值 + 形态学闭 + 只留最大连通域。用于量 bbox 与
                估头肩结构（必须"干净"，否则水印会把 bbox 撑大、把人像挤小）。
      sel    —— 实际保留的像素：核心区外扩几像素后与低阈值相交。用外扩是为了
                保住头发 / 衣角的**软边**（直接二值化会把边缘切硬）。
    无 numpy 时退化为「整体保留」，绝不因缺依赖而报错。
    """
    if _np is None:
        b = alpha > _ALPHA_SOFT
        return b, b
    strong = alpha > _ALPHA_STRONG
    if not strong.any():
        return strong, strong
    if _cv2 is not None:
        m = strong.astype(_np.uint8)
        closed = _cv2.morphologyEx(m, _cv2.MORPH_CLOSE, _np.ones((5, 5), _np.uint8),
                                   iterations=2)
        n, lab, stats, _ = _cv2.connectedComponentsWithStats(closed, 8)
        if n > 1:
            main = 1 + int(_np.argmax(stats[1:, 4]))     # 面积最大的前景连通域
            keep = lab == main
            strong = strong & keep                        # 相交，去掉闭运算外扩
            grown = _cv2.dilate(keep.astype(_np.uint8), _np.ones((3, 3), _np.uint8),
                                iterations=4).astype(bool)
            return strong, (alpha > _ALPHA_SOFT) & grown
    return strong, (alpha > _ALPHA_SOFT) & strong


def _find_head_rows(m) -> tuple | None:
    """用「逐行前景宽度」估人像的 (头顶行, 脖子行)。

    人像掩码的行宽天然呈「发顶窄 → 面部变宽 → 脖子收窄 → 肩部骤宽」，
    所以在主体下半段找行宽的局部极小值、并要求它明显窄于头部最宽处，即为脖子。
    结构不像人像（矩形 / 商品 / 无收窄 / 比例离谱）时返回 None，交给回退构图。
    """
    if _np is None or getattr(m, "ndim", 0) != 2 or m.size == 0:
        return None
    h, w = m.shape
    if h < 40 or w < 20:
        return None
    row = m.sum(axis=1).astype(_np.float64)
    if row.max() <= 0:
        return None
    ys = _np.nonzero(row > max(2.0, row.max() * 0.02))[0]
    if len(ys) < 20:
        return None
    y0, y1 = int(ys[0]), int(ys[-1])
    span = y1 - y0 + 1
    if span < 40:
        return None
    k = max(3, span // 120)   # 平滑核要小：核太大把「脖子最窄处」往后拖，头会被算高、脸变小
    sm = _np.convolve(row, _np.ones(k) / k, mode="same")
    lo, hi = int(y0 + span * 0.28), int(y0 + span * 0.92)
    if hi - lo < 8:
        return None
    yneck = lo + int(_np.argmin(sm[lo:hi]))
    if yneck <= y0:
        return None
    head_max = float(sm[y0:yneck + 1].max())
    if head_max <= 0 or float(sm[yneck]) > head_max * 0.70:
        return None              # 没明显收窄 ⇒ 不是脖子（衣服 / 矩形主体的起伏）
    head_h = yneck - y0
    if not (0.16 * span <= head_h <= 0.78 * span):
        return None              # 头占主体高的比例离谱 ⇒ 不是人像结构
    return y0, yneck


def compose_id_photo(src: str, out: str, w: int, h: int, bg: str, layout: int = 1) -> dict:
    """把抠好的 RGBA 人像合成到规格画布上。

    构图规则（对标证件照标准，2026-10-10 修「人物偏小 / 位置偏下」）：
      1) 先清洗掩码——抠图残留（原图水印 / 签名 / 孤立噪点）清零并排除出 bbox，
         否则残留会把主体框撑大，人像在框里占比变小、整体下移；
      2) 能识别出人像结构（头顶 / 脖子）时走「头主导」：按头宽/头高定尺度，
         头顶留白 8.5%、头宽约 60% 画布宽（证件照标准），肩以下自然裁切——
         这正是证件照「头肩特写」的样子，脸明显更大；
      3) 结构不像人像（商品 / 矩形主体）时回退整体缩放：宽 ≤88%、高 ≤90%，居中。

    layout>1 时按 2 列网格拼到白底整版（冲印排版），单张仍保留原尺寸。
    返回 {"width","height","layout"} 供前端展示。
    """
    im = Image.open(src).convert("RGBA")
    mask_crop = None
    if _np is not None:
        arr = _np.array(im)
        alpha = arr[..., 3]
        bbox = None
        if int(alpha.max()) > 0:
            strong, sel = _mask_region(alpha)
            if strong.any():
                arr[..., 3] = _np.where(sel, alpha, 0)   # 主体之外的残留一律清零
                im = Image.fromarray(arr, "RGBA")
                bbox = Image.fromarray((strong.astype(_np.uint8) * 255), "L").getbbox()
                mask_crop = strong if bbox else None
    else:
        bbox = im.split()[-1].getbbox()
    # bbox 为空 = 整张全透明（抠图没抠出任何主体）。此时若继续走合成，
    # 会静默产出一张纯底色废图——用户看起来像「做好了其实是空的」，必须显式报错。
    if not bbox:
        raise RuntimeError("抠图结果为空，请换一张人像清晰、光线均匀的照片")
    im = im.crop(bbox)
    if mask_crop is not None:
        mask_crop = mask_crop[bbox[1]:bbox[3], bbox[0]:bbox[2]]
    iw, ih = im.size
    if iw <= 0 or ih <= 0:
        raise RuntimeError("抠图结果为空，请换一张人像清晰、光线均匀的照片")

    head = _find_head_rows(mask_crop) if mask_crop is not None else None
    if head:
        # ── 头主导：脸大、头顶留白标准、肩以下裁切（证件照「头肩特写」）
        y_top, y_neck = head
        head_h = max(1, y_neck - y_top)
        head_w = max(1, int(mask_crop[y_top:y_neck + 1].sum(axis=1).max()))
        scale = min(w * _HEAD_W_TARGET / head_w, h * _HEAD_H_TARGET / head_h, 6.0)
        nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
        im = im.resize((nw, nh), Image.LANCZOS)
        # 水平按头部重心居中（半身照里人常偏一侧），但最多偏离画面中心 10% 宽
        colsum = mask_crop[y_top:y_neck + 1].sum(axis=0).astype(_np.float64)
        cx = float((_np.arange(iw) * colsum).sum() / colsum.sum()) if colsum.sum() > 0 else iw / 2.0
        x0, lim = (w - nw) // 2, int(w * 0.10)
        x = max(min(int(round(w / 2.0 - cx * scale)), x0 + lim), x0 - lim)
        y = max(0, int(round(h * _HEAD_TOP_PAD - y_top * scale)))
    else:
        # ── 回退：整体等比缩放（放宽到 88%/90%，与原 78%/86% 相比人明显更大）
        scale = min(w * _FALLBACK_W / iw, h * _FALLBACK_H / ih)
        nw, nh = max(1, int(round(iw * scale))), max(1, int(round(ih * scale)))
        im = im.resize((nw, nh), Image.LANCZOS)
        x = (w - nw) // 2
        y = int(h * 0.97) - nh
        y = max(y, int(h * 0.05))          # 顶部至少留白 5%，不切头顶
        y = min(y, max(0, h - nh))         # 底部不溢出（除非主体比画布还大）

    single = Image.new("RGBA", (w, h), _bg_rgba(bg))
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
        # 明确声明「人像」并关掉 AI 图片类型识别：证件照的输入必然是人，走自动模式会
        # 先调 VLM 判类型（vision_client.classify_image，timeout=45s）——白等 45 秒，
        # 且万一被判成海报/商品就会走错抠图通道。声明后直接进人像专用链路（更快更准）。
        mat.matting_image(app.Path(src), rgba_path, vision_label="人像", auto_vlm=False,
                          meta=job)
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
