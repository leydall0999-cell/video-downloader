"""server/dewatermark_core.py — 需求文档模块二：PDF / 图片去水印核心逻辑。

技术路线（与视频去水印 ffmpeg delogo / E2FGVI 无关，独立实现）：
- 图片：OpenCV inpainting（TELEA / NS）做轻量修复，需用户框选水印区域。
- PDF：
  * 注释型水印：PyMuPDF 遍历页面注释，删除 Watermark 型注释（无损、保留文字可选中性）。
  * 栅格化模式：页面栅格化 → 图片 inpaint → 重排合成（适用于扫描件 / 内容流内嵌水印）。

依赖为原生 C 扩展，安装失败（如无对应 wheel / 离线环境）时整功能优雅降级：
- `_cv2` / `_np` / `_fitz` 为 None → available()/pdf_available() 返回 False，
  上层路由据此返回 503，不影响进程启动（与 libtorrent 模式一致）。
"""
import logging
from pathlib import Path

logger = logging.getLogger("vdl.dewatermark")

try:
    import cv2 as _cv2
except Exception:  # noqa: BLE001
    _cv2 = None

try:
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

try:
    import fitz as _fitz
except Exception:  # noqa: BLE001
    _fitz = None


def available() -> bool:
    """图片去水印是否可用（需要 cv2 + numpy）。"""
    return _cv2 is not None and _np is not None


def pdf_available() -> bool:
    """PDF 去水印是否可用（需要 fitz）。"""
    return _fitz is not None


# ------------------------------------------------------------------ 纯逻辑（不依赖原生库，可独立单测）

def normalize_region(region) -> dict:
    """把前端传来的区域（可能含字符串/越界值）收敛为 0..1 的浮点字典。

    返回 {"x","y","w","h"}，坐标被裁剪到 [0,1] 且保证 x+w<=1, y+h<=1。
    region 为 None / 空 / 非法时返回 None（调用方据此报 400）。
    """
    if not region:
        return None
    try:
        x = float(region.get("x", 0))
        y = float(region.get("y", 0))
        w = float(region.get("w", 0))
        h = float(region.get("h", 0))
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    # 先夹到 [0,1]
    x = min(max(x, 0.0), 1.0)
    y = min(max(y, 0.0), 1.0)
    w = min(max(w, 0.0), 1.0)
    h = min(max(h, 0.0), 1.0)
    # 防止越界
    if x + w > 1.0:
        w = 1.0 - x
    if y + h > 1.0:
        h = 1.0 - y
    if w <= 0 or h <= 0:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def _region_to_px(region: dict, w: int, h: int):
    """把归一化区域换算为像素矩形 (x, y, rw, rh)，并夹到图像边界内。"""
    x = max(0, int(round(region["x"] * w)))
    y = max(0, int(round(region["y"] * h)))
    rw = max(0, int(round(region["w"] * w)))
    rh = max(0, int(round(region["h"] * h)))
    if x + rw > w:
        rw = w - x
    if y + rh > h:
        rh = h - y
    return x, y, rw, rh


def _inpaint_array(img, region: dict, w: int, h: int, method: str, radius: float):
    """对一张 BGR numpy 数组做区域 inpaint，返回同形状数组。"""
    x, y, rw, rh = _region_to_px(region, w, h)
    if rw <= 0 or rh <= 0:
        raise ValueError("水印区域无效")
    mask = _np.zeros((h, w), dtype=_np.uint8)
    mask[y:y + rh, x:x + rw] = 255
    flag = _cv2.INPAINT_TELEA if method == "telea" else _cv2.INPAINT_NS
    return _cv2.inpaint(img, mask, float(radius), flag)


# ------------------------------------------------------------------ 图片去水印

def image_inpaint(src_path, dst_path, region: dict, method: str = "telea", radius: int = 3) -> Path:
    """对上传图片做区域 inpaint，结果写入 dst_path（保留原扩展名）。

    区域 region 为归一化字典 {"x","y","w","h"}（0..1），缺失则报错。
    method: telea | ns；radius: inpaint 半径（建议 1..10）。
    """
    if not available():
        raise RuntimeError("OpenCV/numpy 未安装，图片去水印不可用")
    if not region:
        raise ValueError("图片去水印需要框选水印区域")
    img = _cv2.imread(str(src_path), _cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("无法读取图片，可能是损坏或格式不支持")
    h, w = img.shape[:2]
    out = _inpaint_array(img, region, w, h, method, radius)
    ok = _cv2.imwrite(str(dst_path), out)
    if not ok:
        raise RuntimeError("去水印结果写入失败")
    return Path(dst_path)


# ------------------------------------------------------------------ PDF 去水印

def pdf_remove_annotations(src_path, dst_path) -> int:
    """删除 PDF 中的 Watermark 型注释（无损，保留文字与矢量内容）。返回删除数量。"""
    if not pdf_available():
        raise RuntimeError("PyMuPDF 未安装，PDF 去水印不可用")
    doc = _fitz.open(str(src_path))
    try:
        removed = 0
        for page in doc:
            for annot in list(page.annots() or []):
                # annot.type 为 (subtype_int, subtype_name)
                if annot.type[0] == _fitz.PDF_ANNOT_WATERMARK:
                    page.delete_annot(annot)
                    removed += 1
        doc.save(str(dst_path), incremental=False, deflate=True)
    finally:
        doc.close()
    return removed


def pdf_raster_remove(src_path, dst_path, region: dict, method: str = "telea",
                      radius: int = 3, dpi: int = 150) -> Path:
    """栅格化去水印：每页渲染为图片 → 区域 inpaint → 重排合成新 PDF。

    适用于扫描件或水印内嵌在内容流中的 PDF。会丢失文字可选中性（按图片重排）。
    region 为归一化字典；dpi 控制栅格化清晰度。
    """
    if not (pdf_available() and available()):
        raise RuntimeError("PyMuPDF / OpenCV 未安装，PDF 栅格化去水印不可用")
    if not region:
        raise ValueError("栅格化去水印需要框选水印区域")
    doc = _fitz.open(str(src_path))
    try:
        new_doc = _fitz.open()
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            img = _np.frombuffer(pix.samples, dtype=_np.uint8).reshape(pix.height, pix.width, pix.n)
            # fitz pixmap 为 RGB(A)，转 BGR 供 OpenCV 处理
            if pix.n == 4:
                img = _cv2.cvtColor(img, _cv2.COLOR_RGBA2BGR)
            else:
                img = _cv2.cvtColor(img, _cv2.COLOR_RGB2BGR)
            out = _inpaint_array(img, region, pix.width, pix.height, method, radius)
            img_rgb = _cv2.cvtColor(out, _cv2.COLOR_BGR2RGB)
            out_pix = _fitz.Pixmap(_fitz.csRGB, pix.width, pix.height, img_rgb.tobytes())
            new_page = new_doc.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(page.rect, pixmap=out_pix)
        new_doc.save(str(dst_path))
    finally:
        doc.close()
        new_doc.close()
    return Path(dst_path)
