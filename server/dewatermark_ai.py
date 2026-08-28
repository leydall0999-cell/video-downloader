"""server/dewatermark_ai.py — AI 图片去水印（LaMa ONNX，免 torch）。

技术路线：
- 用 onnxruntime 直接加载 LaMa 的 ONNX 权重（Carve/LaMa-ONNX 的 lama_fp32.onnx，
  Apache 2.0，固定 512x512 输入，opset 17），不走 cv2.dnn（需 OpenCV 5.0），
  也不依赖 torch（CPU 即可跑，~200ms~2s / 512 片）。
- 为什么是 LaMa：傅里叶卷积对大掩码（水印常占图 20%+ 面积）效果远超 OpenCV TELEA/NS、
  MAT、SD Inpaint，近无痕、边缘自然，是 Cleanup.pictures 等网页去水印工具的事实标准。
- 掩码：复用 dewatermark_core._build_region_mask（来自前端多选区 regions，0/255）。
- 大图处理：以 512 为瓦片、64px 羽化重叠分块推理后按余弦羽化权重拼接，
  LaMa 的傅里叶全局感受野让瓦片边界基本无痕。

依赖（onnxruntime）缺失或模型未下载时：
- available() 返回 False，上层路由据此回退 OpenCV / 报友好错误，不阻塞进程启动。
"""
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("vdl.dewatermark_ai")

try:
    import cv2 as _cv2
except Exception:  # noqa: BLE001
    _cv2 = None

try:
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None


# LaMa ONNX 权重（fp32，固定 512x512 输入；Carve-Photos/lama 导出，Apache 2.0）
LAMA_ONNX_URL = "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx"
LAMA_ONNX_NAME = "lama_fp32.onnx"

# 推理瓦片大小与羽化重叠（LaMa 推荐 512 / 32~64 重叠）
TILE = 512
OVERLAP = 64

_SESSION = None
_LOCK = threading.Lock()


def _model_dir() -> Path:
    """模型缓存目录：优先 VDL_MODELS_DIR，其次 Railway 持久卷，最后回退本地 models/。"""
    raw = os.environ.get("VDL_MODELS_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if raw:
        return Path(raw) / "vdl_models"
    return Path(__file__).resolve().parent / "models"


def _ensure_model() -> Path:
    """确保模型文件存在，缺失则从 HuggingFace 下载（首次较慢，约 100MB）。"""
    d = _model_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / LAMA_ONNX_NAME
    if p.exists() and p.stat().st_size > 1_000_000:
        return p
    import urllib.request

    logger.info("ai_dewatermark: 下载 LaMa ONNX %s -> %s", LAMA_ONNX_URL, p)
    tmp = p.with_suffix(".tmp")
    urllib.request.urlretrieve(LAMA_ONNX_URL, tmp)
    tmp.replace(p)
    logger.info("ai_dewatermark: 模型就绪 %s (%d bytes)", p, p.stat().st_size)
    return p


def _get_session():
    """懒加载 onnxruntime InferenceSession（进程内缓存，线程安全）。"""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    with _LOCK:
        if _SESSION is not None:
            return _SESSION
        import onnxruntime as ort  # 延迟导入，缺失时不阻塞启动

        p = _ensure_model()
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # 限线程：避免 Railway 免费实例 CPU 尖峰 / 内存膨胀（单图推理本就快，无需多线程）
        so.intra_op_num_threads = 1
        so.inter_op_num_threads = 1
        _SESSION = ort.InferenceSession(str(p), sess_options=so, providers=["CPUExecutionProvider"])
    return _SESSION


def available() -> bool:
    """AI 去水印是否可用（需要 cv2 + numpy + onnxruntime）。"""
    if _cv2 is None or _np is None:
        return False
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def _infer_tile(sess, img_tile, mask_tile):
    """对单张 512x512 瓦片推理，返回 (512,512,3) 浮点 [0,1] 修复结果。

    img_tile / mask_tile：float32，[0,1]，shape (512,512,3) / (512,512,1)。
    LaMa 输入 image 为 RGB [0,1]、mask 为 [0,1]，输出 [0,1]。
    """
    inp_img = img_tile.transpose(2, 0, 1)[None].astype(_np.float32)
    inp_mask = mask_tile.transpose(2, 0, 1)[None].astype(_np.float32)
    names = [i.name for i in sess.get_inputs()]
    feeds = {names[0]: inp_img, names[1]: inp_mask}
    out = sess.run(None, feeds)[0][0]  # (3,512,512)
    return _np.clip(out.transpose(1, 2, 0), 0, 1)


def _tile_weight(th: int, tw: int, y0: int, x0: int, h: int, w: int, overlap: int):
    """生成瓦片羽化权重 (th,tw)：仅在与其他瓦片重叠的边界做余弦羽化，内部为 1。

    边缘瓦片（贴图边界、无邻居重叠）对应侧权重恒为 1，避免图边缘被压暗。
    """
    def _ramp(n, is_top, interior):
        # n: 该轴瓦片实际长度；is_top: 是否为靠近重叠侧的边（前 overlap 行/列）；
        # interior: 该侧是否还有邻居（即非贴图边界）
        arr = _np.ones(n, _np.float32)
        if interior and overlap > 0 and n > overlap:
            edge = _np.linspace(0.0, 1.0, overlap)
            # 余弦羽化：0->1 平滑过渡
            edge = (1 - _np.cos(edge * _np.pi)) / 2
            if is_top:
                arr[:overlap] = edge
            else:
                arr[n - overlap:] = edge[::-1]
        return arr

    vr = _ramp(th, is_top=True, interior=(y0 > 0))
    vb = _ramp(th, is_top=False, interior=(y0 + th < h))
    hr = _ramp(tw, is_top=True, interior=(x0 > 0))
    hb = _ramp(tw, is_top=False, interior=(x0 + tw < w))
    return _np.outer(vr * vb, hr * hb)[..., None]


def ai_image_inpaint(src_path, dst_path, regions, tile: int = TILE, overlap: int = OVERLAP) -> Path:
    """AI 图片去水印：LaMa ONNX 按区域 mask 推理，结果写入 dst_path。

    regions：归一化区域列表 [{"x","y","w","h","op"}]（来自 dewatermark_core.normalize_regions）。
    至少需要一个有效 add 区域；缺失或全部为减去区域则报错。
    """
    if not available():
        raise RuntimeError("AI 去水印不可用（缺少 onnxruntime 依赖或模型未下载）")
    import dewatermark_core as dwc

    img = _cv2.imread(str(src_path), _cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("无法读取图片，可能是损坏或格式不支持")
    h, w = img.shape[:2]
    mask = dwc._build_region_mask(regions, w, h)  # (h,w) 0/255
    if not mask.any():
        raise ValueError("未框选有效加选区域（请先框选水印，减选需依附加选）")

    img_f = img[..., ::-1].astype(_np.float32) / 255.0  # BGR->RGB, [0,1]
    mask_f = (mask.astype(_np.float32) / 255.0)[..., None]  # (h,w,1) [0,1]

    sess = _get_session()
    acc = _np.zeros((h, w, 3), _np.float32)
    wsum = _np.zeros((h, w, 1), _np.float32)

    ys = list(range(0, max(1, h - tile), tile - overlap))
    xs = list(range(0, max(1, w - tile), tile - overlap))
    if h > tile:
        ys.append(max(0, h - tile))
    if w > tile:
        xs.append(max(0, w - tile))
    if not ys:
        ys = [0]
    if not xs:
        xs = [0]

    for y0 in ys:
        for x0 in xs:
            y1 = min(y0 + tile, h)
            x1 = min(x0 + tile, w)
            th, tw = y1 - y0, x1 - x0
            # 拼到 512 瓦片
            pimg = _np.zeros((tile, tile, 3), _np.float32)
            pmask = _np.zeros((tile, tile, 1), _np.float32)
            pimg[:th, :tw] = img_f[y0:y1, x0:x1]
            pmask[:th, :tw] = mask_f[y0:y1, x0:x1]
            out = _infer_tile(sess, pimg, pmask)[:th, :tw]
            wt = _tile_weight(th, tw, y0, x0, h, w, overlap)
            acc[y0:y1, x0:x1] += out * wt
            wsum[y0:y1, x0:x1] += wt

    # 加权平均（重叠区融合），无覆盖区保持原图（理论上 mask 区必被覆盖）
    cov = wsum > 0
    result = _np.where(cov, acc / _np.where(wsum == 0, 1, wsum), img_f)
    out_bgr = (result * 255).clip(0, 255).astype(_np.uint8)[..., ::-1]  # RGB->BGR
    ok = _cv2.imwrite(str(dst_path), out_bgr)
    if not ok:
        raise RuntimeError("AI 去水印结果写入失败")
    return Path(dst_path)
