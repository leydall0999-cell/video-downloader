"""server/matting_ai.py — 一键抠图（图片去背景）AI 引擎。

用 BiRefNet ONNX 做显著性抠图，输出带 alpha 通道的透明 PNG。

为什么不用 rembg 库：
    rembg 会连带引入 scipy / scikit-image / numba / pooch 等重型科学计算栈，
    打进桌面包会让安装包暴涨数百 MB，且 numba 首次启动有 JIT 编译开销。
    BiRefNet 的前/后处理本身不到 40 行，这里直接用 App 已打包的
    onnxruntime + numpy + Pillow 实现，效果等价、依赖零增长。

算法（对齐 rembg 2.x 的 BiRefNetSessionGeneral，保证输出一致）：
    前处理 RGB → LANCZOS 缩到 1024×1024 → 除以全图最大像素值 →
    按 ImageNet mean/std 归一化 → HWC→CHW → 扩 batch 维 → float32
    推理   ort.InferenceSession.run(None, {input_name: tensor})
    后处理 取 out[0][:, 0, :, :] → sigmoid → min-max 拉伸到 [0,1] →
    ×255 转 uint8 灰度 → LANCZOS 放大回原图尺寸 → 作为 alpha 通道合成 RGBA

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
        "urls": [
            "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
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
        f"抠图模型下载失败：{last_err or '未知错误'}。"
        f"可手动下载 {meta['filename']} 后放到 {_model_dir()}"
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
    """对 PIL 图像推理，返回原图尺寸的 L 模式 alpha 蒙版。"""
    import numpy as np
    from PIL import Image

    sess = _get_session()
    meta = MODELS[_MODEL_NAME]
    feed = _preprocess(img, meta["input_size"])

    input_name = sess.get_inputs()[0].name
    outs = sess.run(None, {input_name: feed})

    pred = _sigmoid(outs[0][:, 0, :, :])
    ma = float(np.max(pred))
    mi = float(np.min(pred))
    if ma - mi > 1e-8:
        pred = (pred - mi) / (ma - mi)
    pred = np.squeeze(pred)

    mask = Image.fromarray((np.clip(pred, 0.0, 1.0) * 255).astype("uint8"), mode="L")
    if mask.size != img.size:
        mask = mask.resize(img.size, _lanczos())
    return mask


def matting_image(src: str | Path, out: str | Path) -> None:
    """对单张图片做一键抠图，输出 RGBA 透明 PNG 到 out。

    src/out 为路径（str 或 Path）。透明 PNG 可直接用于合成 / 换背景。
    """
    from PIL import Image

    if not available():
        raise RuntimeError("一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")

    with Image.open(src) as im:
        im.load()
        rgb = im.convert("RGB")
        mask = predict_mask(rgb)
        rgba = rgb.convert("RGBA")
        rgba.putalpha(mask)

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(out_path, "PNG")
