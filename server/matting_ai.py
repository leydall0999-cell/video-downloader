"""server/matting_ai.py — 一键抠图（图片去背景）AI 引擎。

用 BiRefNet ONNX 做显著性抠图，输出带 alpha 通道的透明 PNG。

为什么不用 rembg 库：
    rembg 会连带引入 scipy / scikit-image / numba / pooch 等重型科学计算栈，
    打进桌面包会让安装包暴涨数百 MB，且 numba 首次启动有 JIT 编译开销。
    BiRefNet 的前/后处理本身不到 40 行，这里直接用 App 已打包的
    onnxruntime + numpy + Pillow 实现，效果等价、依赖零增长。

算法（基于 rembg 2.x 的 BiRefNetSessionGeneral，但修补了 alpha 质量）：
    前处理 RGB → LANCZOS 缩到 1024×1024 → 除以全图最大像素值 →
    按 ImageNet mean/std 归一化 → HWC→CHW → 扩 batch 维 → float32
    推理   ort.InferenceSession.run(None, {input_name: tensor})
    后处理 取 out[0][:, 0, :, :] → sigmoid → **软阈值 [0.05, 0.5] 替代 min-max**
              → 中间置信保留为软 alpha（光晕/浅描边/3D 阴影关键）→
              轻 GaussianBlur (r=0.8) 羽化边缘 → ×255 uint8 → LANCZOS 放大
              回原图尺寸 → 作为 alpha 通道合成 RGBA

    为何弃用 min-max 归一化：
        原 rembg 把整个 sigmoid 概率图硬拉伸到 [0,1]（(x-min)/(max-min)）。
        对于贴纸/标题风格图像，模型对"白色光晕"的置信大约在 0.2~0.5，
        min-max 后会被压到 ≈ 0.1，肉眼几乎不可见——表现为"光晕丢了"。
        改为软阈值：[lo, hi] 内按原始比例映射、之外直接 0/1，
        让光晕以约 0.4~0.6 alpha 保留在结果里。

模型：
    默认 birefnet-general-lite（213 MB，质量接近完整版、内存/速度友好）；
    可切 birefnet-general（927 MB，质量最高）。首次使用时惰性下载到
    ~/.vdl_models（或 VDL_MODELS_DIR），下载进度可通过 download_progress() 轮询。

License 注意：
    BiRefNet 权重多为「非商业用途」授权，个人自用 / 内部分发一般可接受；
    若需对外商业分发，请改用 MIT 友好的模型。前端说明区已告知用户。
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- 模型注册表

# size_mb 用于前端「首次下载约 xxx MB」提示；input_size 是 ONNX 输入分辨率。
MODELS: dict[str, dict] = {
    "birefnet-general-lite": {
        "filename": "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
        "size_mb": 213,
        "input_size": (1024, 1024),
        "md5": "4fab47adc4ff364be1713e97b7e66334",
        "desc": "轻量版 · 213MB · 推荐",
        # 国内/国外多个源依次尝试，覆盖 GitHub release 在某些网络下不可达的情况
        "urls": [
            "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
            "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
            "https://ghfast.top/https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
            "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
            "https://mirror.ghproxy.com/https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
            "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
            "https://gh-proxy.com/https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
            "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
            "https://github.moeyy.dev/https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
            "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
        ],
    },
    "birefnet-general": {
        "filename": "BiRefNet-general-epoch_244.onnx",
        "size_mb": 927,
        "input_size": (1024, 1024),
        "md5": "7a35a0141cbbc80de11d9c9a28f52697",
        "desc": "完整版 · 927MB · 质量最高",
        "urls": [
            "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
            "BiRefNet-general-epoch_244.onnx",
            "https://ghfast.top/https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
            "BiRefNet-general-epoch_244.onnx",
            "https://mirror.ghproxy.com/https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
            "BiRefNet-general-epoch_244.onnx",
            "https://gh-proxy.com/https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
            "BiRefNet-general-epoch_244.onnx",
            "https://github.moeyy.dev/https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
            "BiRefNet-general-epoch_244.onnx",
        ],
    },
}

DEFAULT_MODEL = "birefnet-general-lite"

# 归一化常量（ImageNet，与 rembg 一致）
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

# ---------------------------------------------------------------- 运行时状态

_MODEL_NAME = os.environ.get("VDL_MAT_MODEL") or DEFAULT_MODEL
_SESSION = None
_SESSION_NAME = ""
_LOCK = threading.Lock()

# 下载进度快照（供 /api/matting/models 轮询，前端显示「首次下载 xx%」）
_DL: dict = {"active": False, "model": "", "done": 0, "total": 0, "pct": 0.0, "error": ""}
_DL_LOCK = threading.Lock()


# ---------------------------------------------------------------- 基础查询


def available() -> bool:
    """一键抠图是否可用（需要 onnxruntime + numpy + Pillow，均在 App 内已打包）。"""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def current_model() -> str:
    return _MODEL_NAME


def set_model(name: str) -> None:
    """切换模型（清空已缓存 session，下次推理重建）。未知名字回退默认。"""
    global _MODEL_NAME, _SESSION, _SESSION_NAME
    with _LOCK:
        _MODEL_NAME = name if name in MODELS else DEFAULT_MODEL
        if _SESSION_NAME != _MODEL_NAME:
            _SESSION = None
            _SESSION_NAME = ""


def list_models() -> list[dict]:
    """返回模型元信息（含已下载标记），供前端下拉/提示使用。"""
    out = []
    for name, meta in MODELS.items():
        p = _local_path(name)
        out.append(
            {
                "name": name,
                "desc": meta["desc"],
                "size_mb": meta["size_mb"],
                "downloaded": _valid_cached(name),
                "active": name == _MODEL_NAME,
            }
        )
    return out


def download_progress() -> dict:
    """当前下载进度快照（非活跃时 active=False）。"""
    with _DL_LOCK:
        return dict(_DL)


# ---------------------------------------------------------------- 模型下载


def _model_dir() -> Path:
    """模型缓存目录：VDL_MODELS_DIR 优先，其次 ~/.vdl_models（与 dewatermark_ai 一致）。"""
    raw = os.environ.get("VDL_MODELS_DIR")
    base = Path(raw) if raw else Path.home()
    d = base / ".vdl_models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _legacy_rembg_path(name: str) -> Path:
    """rembg 2.x 的缓存位置；若用户机器上已下载过，直接复用，避免重复下 213MB/927MB。"""
    meta = MODELS.get(name)
    if not meta:
        return Path("/nonexistent")
    return Path.home() / ".rembg" / "models" / name / meta["filename"]


def _local_path(name: str) -> Path:
    return _model_dir() / MODELS[name]["filename"]


# 缓存有效的最小体积。只要正式名文件存在且超过它就直接用——
# 因为只有下载完整后才会从 .part 原子 rename 成正式名，半截文件不会顶着正式名存在。
# 不用「模型体积的百分比」当阈值：那样对换来源/换版本的小体积模型会误判为损坏并反复重下。
_MIN_CACHE_BYTES = 1024 * 1024  # 1 MB


def _valid_cached(name: str) -> bool:
    """本地是否已有可直接使用的模型缓存。"""
    p = _local_path(name)
    return p.exists() and p.stat().st_size > _MIN_CACHE_BYTES


def _set_dl(**kw) -> None:
    with _DL_LOCK:
        _DL.update(kw)


def _download(name: str) -> Path:
    """下载模型到 ~/.vdl_models，返回本地路径。带进度回显 + 断点续传 + 原子落盘。"""
    meta = MODELS[name]
    dest = _local_path(name)

    # 已有可用缓存 → 直接命中
    if _valid_cached(name):
        return dest

    # 复用 rembg 旧缓存
    legacy = _legacy_rembg_path(name)
    if legacy.exists() and legacy.stat().st_size > 0:
        try:
            dest.hardlink_to(legacy)
            return dest
        except Exception:  # noqa: BLE001
            try:
                import shutil

                shutil.copy2(legacy, dest)
                return dest
            except Exception:  # noqa: BLE001
                pass

    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err = ""
    for url in meta["urls"]:
        try:
            _set_dl(active=True, model=name, done=0, total=0, pct=0.0, error="")
            _http_get(url, tmp, name)
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise RuntimeError("下载结果为空")
            tmp.replace(dest)  # 原子落盘
            _set_dl(active=False, pct=100.0, done=tmp.stat().st_size if tmp.exists() else 0)
            return dest
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:300]
            tmp.unlink(missing_ok=True)
            _set_dl(active=False, error=last_err)
            continue

    raise RuntimeError(
        f"抠图模型下载失败（已尝试 {len(meta['urls'])} 个源）：{last_err or '未知错误'}。"
        f"请在浏览器打开下方任一链接手动下载 {meta['filename']}，再从本应用上传到 {_model_dir()}。"
    )


def _http_get(url: str, tmp: Path, name: str) -> None:
    """带进度的流式下载，支持 .part 续传。"""
    headers = {"User-Agent": "VDL/1.0"}
    resume = tmp.stat().st_size if tmp.exists() else 0
    if resume > 0:
        headers["Range"] = f"bytes={resume}-"

    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener()
    # 跟随 github release 的 objects.githubusercontent.com 跳转
    with opener.open(req, timeout=60) as resp:
        if resume > 0 and resp.status != 206:
            resume = 0  # 服务端不支持续传，从头下
        mode = "ab" if resume > 0 else "wb"
        total = int(resp.headers.get("Content-Length") or 0)
        if resume > 0:
            total += resume
        _set_dl(total=total, done=resume)

        chunk = 1024 * 256
        done = resume
        last_emit = 0.0
        with tmp.open(mode) as fh:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                fh.write(buf)
                done += len(buf)
                now = time.time()
                # 节流：最多每 0.4s 更新一次进度，避免刷爆轮询
                if now - last_emit > 0.4 or done >= total:
                    last_emit = now
                    pct = round(done / total * 100, 1) if total else 0.0
                    _set_dl(done=done, total=total, pct=pct)


# ---------------------------------------------------------------- 推理


def _get_session():
    """懒加载 ONNX session（首次调用会触发模型下载）。"""
    global _SESSION, _SESSION_NAME
    with _LOCK:
        if _SESSION is not None and _SESSION_NAME == _MODEL_NAME:
            return _SESSION

        import onnxruntime as ort

        path = _download(_MODEL_NAME)
        so = ort.SessionOptions()
        # 抠图是单张串行任务，图优化已足够；不开并行避免与下载线程抢核
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # CPU 是唯一稳定 EP（BiRefNet 含傅里叶类 op，CoreML EP 会 OOM，与 LaMa 同理）
        _SESSION = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
        _SESSION_NAME = _MODEL_NAME
        return _SESSION


def warmup() -> None:
    """预热：确认依赖齐备 + 触发模型下载 + 建 session + 跑一次空推理。"""
    if not available():
        raise RuntimeError("一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")
    sess = _get_session()
    # 空跑一次 1×1，避免首次真实请求时还要付 ONNX 初始化开销
    import numpy as np

    name = sess.get_inputs()[0].name
    sess.run(None, {name: np.zeros((1, 3, 1024, 1024), dtype=np.float32)})


def _lanczos():
    """兼容 Pillow 新旧版本的 LANCZOS 枚举名。"""
    from PIL import Image

    return getattr(Image, "Resampling", Image).LANCZOS


def _preprocess(img, size: tuple[int, int]):
    """RGB → 归一化 CHW float32 张量（对齐 rembg BaseSession.normalize）。"""
    import numpy as np

    im = img.convert("RGB").resize(size, _lanczos())
    ary = np.array(im).astype(np.float32)
    ary = ary / max(float(np.max(ary)), 1e-6)

    tmp = np.zeros((ary.shape[0], ary.shape[1], 3), dtype=np.float32)
    tmp[:, :, 0] = (ary[:, :, 0] - _MEAN[0]) / _STD[0]
    tmp[:, :, 1] = (ary[:, :, 1] - _MEAN[1]) / _STD[1]
    tmp[:, :, 2] = (ary[:, :, 2] - _MEAN[2]) / _STD[2]

    return np.expand_dims(tmp.transpose((2, 0, 1)), 0)


def _sigmoid(x):
    import numpy as np

    return 1.0 / (1.0 + np.exp(-x))


def predict_mask(img):
    """对 PIL 图像推理，返回原图尺寸的 L 模式 alpha 蒙版。

    关键改进：
    - **软阈值替代 min-max 归一化**：原 rembg 流程把 sigmoid 概率硬拉伸到 [0,1]，
      会把"小白光晕/浅描边/3D 阴影"等软 alpha 区间（softmax 0.2~0.5）压缩到接近 0，
      抠图结果就是"光晕没了、只剩硬切的字符"。
      改为软阈值 [0.05, 0.5]：之外直接 0/1，之内保留原始梯度，
      光晕以约 0.4~0.6 alpha 出现在结果里（贴纸风抠图的视觉预期）。
    - **轻 GaussianBlur (r=0.8)**：alpha 边缘羽化，避免硬阶梯。
    - **LANCZOS 缩回原图尺寸**：保持平滑过渡。

    边界情况：单色图（hi<lo）→ 退化回原 sigmoid 输出，避免除零。
    """
    import numpy as np
    from PIL import Image, ImageFilter

    sess = _get_session()
    meta = MODELS[_MODEL_NAME]
    feed = _preprocess(img, meta["input_size"])

    input_name = sess.get_inputs()[0].name
    outs = sess.run(None, {input_name: feed})

    pred = _sigmoid(outs[0][:, 0, :, :])
    pred = np.squeeze(pred)

    # 软阈值替换 min-max：保留中间置信为软 alpha
    threshold_lo, threshold_hi = 0.05, 0.5
    span = threshold_hi - threshold_lo
    if span > 1e-8:
        mask = np.where(
            pred >= threshold_hi,
            1.0,
            np.where(
                pred <= threshold_lo,
                0.0,
                (pred - threshold_lo) / span,
            ),
        )
    else:
        mask = pred  # 退化：原 sigmoid

    # 轻 GaussianBlur：mask 边缘羽化（让光晕边缘更自然）
    mask_arr = np.clip(mask * 255.0, 0, 255).astype("uint8")
    mask_img = Image.fromarray(mask_arr, mode="L")
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=0.8))

    if mask_img.size != img.size:
        mask_img = mask_img.resize(img.size, _lanczos())

    return mask_img


def matting_image(src: str | Path, out: str | Path, box: tuple | list | None = None) -> None:
    """对单张图片做一键抠图，输出 RGBA 透明 PNG 到 out。

    src/out 为路径（str 或 Path）。透明 PNG 可直接用于合成 / 换背景。

    box（可选）：用户手动框选的主体区域，归一化 (x, y, w, h)，取值 0~1。
      给定时**只把框内区域送进模型推理**（而不是全图推理后再裁），
      这样模型会把框内内容当成整张图来找显著主体——
      照片里有多个人/物时，框谁就抠谁，比全图推理精确得多。
      框外一律透明。
    """
    from PIL import Image

    if not available():
        raise RuntimeError("一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")

    with Image.open(src) as im:
        im.load()
        rgb = im.convert("RGB")
        W, H = rgb.size

        box_px = _normalize_box(box, W, H)
        if box_px is None:
            # 未框选：整图推理
            mask = predict_mask(rgb)
        else:
            x0, y0, x1, y1 = box_px
            bw, bh = x1 - x0, y1 - y0
            # **推理上下文外扩 10%**：用户框选通常只罩住主体（如字符本体），
            # 但软元素（白色光晕 / 3D 阴影 / 描边）紧贴主体外侧。
            # 推理时多看 10% 上下文，模型就能把这些软元素纳入 alpha，
            # 输出仍严格按用户原框裁切——视觉效果是"框内 + 自动扩展的软边缘"。
            # 不外扩会出现"光晕/阴影丢失"问题（用户最常见的反馈）。
            pad_w = max(int(bw * 0.10), 4)
            pad_h = max(int(bh * 0.10), 4)
            x0p = max(0, x0 - pad_w)
            y0p = max(0, y0 - pad_h)
            x1p = min(W, x1 + pad_w)
            y1p = min(H, y1 + pad_h)
            crop = rgb.crop((x0p, y0p, x1p, y1p))
            crop_mask = predict_mask(crop)
            # 输出 mask 严格按用户原框；外扩区是被模型"看到"但被舍弃的上下文。
            # 取交集：把外扩裁切的 mask 中属于原框的部分贴回原图坐标。
            import numpy as _np
            out_arr = _np.zeros((H, W), dtype=_np.uint8)
            crop_w_p, crop_h_p = crop_mask.size
            # 用户原框在外扩 crop 内的位置（裁切不超出 crop 范围）
            sx0 = max(0, x0 - x0p)
            sy0 = max(0, y0 - y0p)
            sx1 = min(crop_w_p, x1 - x0p)
            sy1 = min(crop_h_p, y1 - y0p)
            if sx1 > sx0 and sy1 > sy0:
                region = _np.array(crop_mask, dtype=_np.uint8)[sy0:sy1, sx0:sx1]
                paste_w, paste_h = sx1 - sx0, sy1 - sy0
                out_arr[y0:y0 + paste_h, x0:x0 + paste_w] = region
            mask = Image.fromarray(out_arr, mode="L")

        rgba = rgb.convert("RGBA")
        rgba.putalpha(mask)

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(out_path, "PNG")


def _normalize_box(box, W: int, H: int):
    """把归一化 box (x,y,w,h) 转成像素坐标 (x0,y0,x1,y1)，并夹紧到图像范围内。

    返回 None 表示「未框选 / 框无效」（面积过小或越界），走整图推理。
    """
    if not box:
        return None
    try:
        bx, by, bw, bh = [float(v) for v in box[:4]]
    except Exception:  # noqa: BLE001
        return None
    if not (0.0 <= bx < 1.0 and 0.0 <= by < 1.0):
        return None
    if bw <= 0 or bh <= 0:
        return None
    # 太小的框（可能是误点）视为无效，避免用户随手一点就变成抠一小块
    if bw * bh < 0.001:
        return None

    x0 = max(0, min(W, int(round(bx * W))))
    y0 = max(0, min(H, int(round(by * H))))
    x1 = max(0, min(W, int(round((bx + bw) * W))))
    y1 = max(0, min(H, int(round((by + bh) * H))))
    if x1 - x0 < 8 or y1 - y0 < 8:  # 小于 8px 模型也糊，直接放弃框选
        return None
    return (x0, y0, x1, y1)
