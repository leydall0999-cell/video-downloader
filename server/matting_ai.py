"""server/matting_ai.py — 一键抠图（图片去背景）AI 引擎。

用 RMBG-2.0 / BiRefNet 等显著性抠图 ONNX 模型，输出带 alpha 通道的透明 PNG。

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
    默认 birefnet-general（**MIT 许可，可商用**，927 MB，质量最高）。
    备选 birefnet-general-lite（213 MB，MIT）。
    可选 rmbg-2.0（**CC BY-NC 4.0，仅限非商业用途**，977 MB，图形/文字边界最佳）；
    因授权限制**不做默认**，仅作「非商用」场景的显式 opt-in。
    首次使用时惰性下载到 ~/.vdl_models（或 VDL_MODELS_DIR），进度可轮询。

License 注意（重要）：
    - **BiRefNet（birefnet-general / -lite）= MIT 许可**：可商用 / 修改 / 再分发，
      只需保留版权声明 → 作为产品默认引擎安全。
    - **RMBG-2.0（rmbg-2.0）= CC BY-NC 4.0**：权重仅限非商业用途开放，
      商业用途须与 BRIA 签商业授权协议。**绝不可作为商业分发产品的默认模型**；
      前端下拉已标注「⚠️ 仅非商用」，仅提供给做个人 / 非商用研究的用户显式选择。
"""
from __future__ import annotations

import os
import threading
import time
import urllib.request
from pathlib import Path

from cloud_matting_config import is_cloud_matting_ready, is_cloud_matting_mediakit_ready

# ---------------------------------------------------------------- 模型注册表

# size_mb 用于前端「首次下载约 xxx MB」提示；input_size 是 ONNX 输入分辨率。
MODELS: dict[str, dict] = {
    "auto": {
        # 🤖 智能自动路由：根据背景类型自动选最优引擎，用户无需判断背景纯不纯。
        #   - 纯色/近似纯色背景（绿幕/橙幕/摄影棚纯色）→ 色度键（颜色距离遮罩 + 强去溢色，硬边干净、零 ML）；
        #   - 复杂/非纯色背景（花纹/渐变/真实场景）→ BiRefNet 通用语义分割（软边羽化）。
        # 作为默认引擎，开箱即用、通吃两类背景——纯色不再受 ML 模型 OOD 之苦，
        # 复杂不再受色度键局限。
        "algorithm": True,
        "size_mb": 0,
        "desc": "智能自动 · 自动识别纯色/复杂背景（默认推荐）",
        "license": "内置算法 + MIT",
        "commercial": "yes",
    },
    "rmbg-2.0": {
        # 真实文件名是 bria-rmbg-2.0.onnx（rembg v0.0.0 release 的命名约定：
        # 「bria」前缀 + 模型正式名）。官方 SHA256 (rembg 2.0.81 bria_rmbg.py 注释)：
        #   sha256:5b486f08200f513f460da46dd702db5fbb47d79b4be4b708a19444bcd4e79958
        "filename": "bria-rmbg-2.0.onnx",
        # RMBG-2.0 FP32 版官方 977 MB（briaai/RMBG-2.0 onnx/model.onnx）。
        # 下载前若用户知情：约 1 GB，会花 30s~3min，耐心等。FP16 量化版仅 514 MB，
        # 但 rembg v0.0.0 release 没托管此版本，要换下载源就先不动。
        "size_mb": 977,
        "input_size": (1024, 1024),
        "norm": "255",  # RMBG-2.0 标准前处理：÷255 后 ImageNet 归一化
        # ⚠️ 授权：CC BY-NC 4.0，仅限非商业用途。commercial="non-commercial"
        # 让前端明确打标，且本模型不作为默认引擎。
        "license": "CC BY-NC 4.0",
        "commercial": "non-commercial",
        "desc": "RMBG-2.0 · 977MB · 图形/文字最佳（⚠️ 仅非商用）",
        # 国内/国外多个源依次尝试，覆盖 GitHub release 在某些网络下不可达的情况
        "urls": [
            "https://github.com/danielgatis/rembg/releases/download/v0.0.0/bria-rmbg-2.0.onnx",
            "https://gh-proxy.com/https://github.com/danielgatis/rembg/releases/download/v0.0.0/bria-rmbg-2.0.onnx",
            "https://ghfast.top/https://github.com/danielgatis/rembg/releases/download/v0.0.0/bria-rmbg-2.0.onnx",
            "https://mirror.ghproxy.com/https://github.com/danielgatis/rembg/releases/download/v0.0.0/bria-rmbg-2.0.onnx",
            "https://github.moeyy.dev/https://github.com/danielgatis/rembg/releases/download/v0.0.0/bria-rmbg-2.0.onnx",
        ],
    },
    "birefnet-general-lite": {
        "filename": "BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx",
        "size_mb": 213,
        "input_size": (1024, 1024),
        "norm": "max",  # BiRefNet（rembg 风格）：÷全图最大像素值后 ImageNet 归一化
        "md5": "4fab47adc4ff364be1713e97b7e66334",
        # MIT 许可，可商用
        "license": "MIT",
        "commercial": "yes",
        "desc": "BiRefNet 轻量版 · 213MB · MIT 可商用",
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
        "norm": "max",
        "md5": "7a35a0141cbbc80de11d9c9a28f52697",
        # MIT 许可，可商用 → 作为默认引擎
        "license": "MIT",
        "commercial": "yes",
        "desc": "BiRefNet 完整版 · 927MB · 质量最高（推荐·MIT可商用）",
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
    "modnet-photographic": {
        "filename": "modnet_photographic_portrait_matting.onnx",
        "size_mb": 25,
        # MODNet ONNX 是动态 shape；768×768 比官方 512×512 更能保留发丝细节，
        # 实测单帧耗时仍 < 1s，可接受。
        "input_size": (768, 768),
        "norm": "255",
        # MODNet 原始仓库权重为 MIT；此 ONNX 来自 HivisionIDPhotos 发布的官方权重导出，
        # 与 HuggingFace DavG25/modnet-pretrained-models 的 Apache-2.0 版本同源，均允许商用。
        "license": "MIT / Apache-2.0",
        "commercial": "yes",
        "desc": "MODNet 人像抠图 · 25MB · 发丝级连续 alpha（MIT/Apache-2.0 可商用）",
        "urls": [
            "https://github.com/Zeyi-Lin/HivisionIDPhotos/releases/download/pretrained-model/"
            "modnet_photographic_portrait_matting.onnx",
            "https://ghfast.top/https://github.com/Zeyi-Lin/HivisionIDPhotos/releases/download/pretrained-model/"
            "modnet_photographic_portrait_matting.onnx",
            "https://mirror.ghproxy.com/https://github.com/Zeyi-Lin/HivisionIDPhotos/releases/download/pretrained-model/"
            "modnet_photographic_portrait_matting.onnx",
            "https://gh-proxy.com/https://github.com/Zeyi-Lin/HivisionIDPhotos/releases/download/pretrained-model/"
            "modnet_photographic_portrait_matting.onnx",
            "https://github.moeyy.dev/https://github.com/Zeyi-Lin/HivisionIDPhotos/releases/download/pretrained-model/"
            "modnet_photographic_portrait_matting.onnx",
        ],
    },
    "chroma-key": {
        # 纯色背景色度键（chroma key）：无 ML 权重，纯算法（边缘洪泛 + 去溢色 despill）。
        # 对纯色/近似纯色背景（绿幕/橙幕/摄影棚纯色底）是「正解」——这类输入对 BiRefNet/
        # MODNet 等自然照片训练的人像模型是 OOD，会反复给出高 alpha 把背景留住；而色度键
        # 从画面边缘洪泛定位背景、直接透明，远比 ML 模型稳。algorithm=True 表示无需下载权重。
        "algorithm": True,
        "size_mb": 0,
        "desc": "纯色背景色度键 · 算法内置 · 绿幕/橙幕/纯色底专用（推荐）",
        "license": "内置算法",
        "commercial": "yes",
    },
    "sam-matting": {
        # 🪄 SAM 像素级分割 + 导向滤波软抠像（连续 alpha，发丝级）。
        # 思路：MobileSAM 给像素级二值 mask（边界精确到像素）→ 腐蚀/膨胀生成
        # 三值 trimap（确定前景 / 未知带 / 确定背景）→ 用原图颜色作引导的导向滤波
        # 在未知带内解出连续 alpha（发丝/婚纱呈半透明）。免 scipy，依赖仅 numpy/opencv，
        # 不增加安装包体积。相比 BiRefNet 默认路径：边缘是连续半透明、不发丝硬切。
        "algorithm": True,
        "size_mb": 0,
        "desc": "SAM 软抠像 · 像素级 + 连续 alpha 发丝（推荐·人像/物体）",
        "license": "MIT（MobileSAM）+ 内置算法",
        "commercial": "yes",
    },
}

DEFAULT_MODEL = "auto"

# 归一化常量（ImageNet，与 rembg 一致）
_MEAN = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)

# ---------------------------------------------------------------- 运行时状态

_MODEL_NAME = os.environ.get("VDL_MAT_MODEL") or DEFAULT_MODEL
# 按模型名缓存 session（切模型不必丢弃已建 session，来回切换不重复初始化）
_SESSIONS: dict = {}
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
    """切换默认模型（仅改全局默认；已缓存的各模型 session 保留）。未知名字回退默认。"""
    global _MODEL_NAME
    with _LOCK:
        _MODEL_NAME = name if name in MODELS else DEFAULT_MODEL


def list_models() -> list[dict]:
    """返回模型元信息（含已下载标记 + 推荐标记），供前端下拉/提示使用。"""
    out = []
    for name, meta in MODELS.items():
        is_algo = meta.get("algorithm", False)
        out.append(
            {
                "name": name,
                "desc": meta["desc"],
                "size_mb": meta.get("size_mb", 0),
                "downloaded": True if is_algo else _valid_cached(name),
                "active": name == _MODEL_NAME,
                "recommended": name == DEFAULT_MODEL,
                "license": meta.get("license", ""),
                "commercial": meta.get("commercial", ""),
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
    if MODELS.get(name, {}).get("algorithm"):
        return True  # 算法内置模型无需下载
    p = _local_path(name)
    return p.exists() and p.stat().st_size > _MIN_CACHE_BYTES


def _set_dl(**kw) -> None:
    with _DL_LOCK:
        _DL.update(kw)


def _download(name: str) -> Path:
    """下载模型到 ~/.vdl_models，返回本地路径。带进度回显 + 断点续传 + 原子落盘。"""
    meta = MODELS[name]
    dest = _local_path(name)

    # 算法内置模型（无权重）→ 无需下载，直接返回占位路径
    if meta.get("algorithm"):
        return _model_dir() / "__algorithm__"

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


def _get_session(name: str | None = None):
    """懒加载 ONNX session（首次调用会触发模型下载）。

    name 缺省用全局 _MODEL_NAME；每个模型独立缓存，切换模型不丢弃已建 session。
    """
    name = name or _MODEL_NAME
    with _LOCK:
        sess = _SESSIONS.get(name)
        if sess is not None:
            return sess

        import onnxruntime as ort

        path = _download(name)
        so = ort.SessionOptions()
        # 抠图是单张串行任务，图优化已足够；不开并行避免与下载线程抢核
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # CPU 是唯一稳定 EP（BiRefNet 含傅里叶类 op，CoreML EP 会 OOM，与 LaMa 同理）
        sess = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
        _SESSIONS[name] = sess
        return sess


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


def _preprocess(img, size: tuple[int, int], norm: str = "max"):
    """RGB → 归一化 CHW float32 张量（对齐各模型官方前处理）。

    norm="max"：÷全图最大像素值（BiRefNet / rembg 风格）；
    norm="255"：÷255（RMBG-2.0 标准 ImageNet 前处理）。
    """
    import numpy as np

    im = img.convert("RGB").resize(size, _lanczos())
    ary = np.array(im).astype(np.float32)
    if norm == "255":
        ary = ary / 255.0
    else:
        ary = ary / max(float(np.max(ary)), 1e-6)

    tmp = np.zeros((ary.shape[0], ary.shape[1], 3), dtype=np.float32)
    tmp[:, :, 0] = (ary[:, :, 0] - _MEAN[0]) / _STD[0]
    tmp[:, :, 1] = (ary[:, :, 1] - _MEAN[1]) / _STD[1]
    tmp[:, :, 2] = (ary[:, :, 2] - _MEAN[2]) / _STD[2]

    return np.expand_dims(tmp.transpose((2, 0, 1)), 0)


def _sigmoid(x):
    import numpy as np

    return 1.0 / (1.0 + np.exp(-x))


def _keep_connected_to_core(
    mask,
    core_thresh: float = 0.4,
    ground_thresh: float = 0.10,
):
    """BFS 连通性筛选：保留与「主体核心区」连通的 mask 区域，孤立小块强制 0。

    为什么需要这个：
        主体（含 3D 阴影、光晕、描边等软元素）的 mask 在显著性图上
        是「连通的一片」——所有非主体像素的置信度都在 0.10~0.45 区间。
        但「远端黑色 slogan 这种不该抠出来的元素」其实置信度也在 0.10~0.35
        ——和阴影/光晕几乎落在同一区间，单靠阈值无法区分。
        区分它们的唯一可靠特征是「**与主体是否相邻**」：阴影紧贴主体（连通），
        slogan 远离主体（孤立）。本函数用 BFS 从核心主体扩散，挑出所有与
        核心连通的 mask 区，剩下的孤立块全部强制 0。

    参数：
        mask: numpy HxW float32 in [0, 1]，已经是过完软阈值 + power + 抗噪的版本
        core_thresh: 核心区阈值（mask>此值的视为「确定属于主体」），默认 0.4
        ground_thresh: 地面阈值（BFS 可走的最低 mask 值），默认 0.10
            ——主体边缘、阴影底纹通常 mask > 0.10，孤立的小碎块可能也 > 0.10
            但与核心不连通。

    返回：与 core 连通的 mask 区域保留；其余置 0。

    复杂度：O(N)，N = mask 像素数。对 1024x1024 图实测 < 50ms。
        不引入 scipy/numpy 之外的依赖（纯 numpy + collections.deque），
        运行时进桌面包不会膨胀安装包。
    """
    import numpy as np
    from collections import deque

    core = mask > core_thresh
    if not core.any():
        return mask  # 没有可识别的核心（极端情况）→ 原样返回

    ground = mask > ground_thresh
    H, W = mask.shape
    visited = np.zeros((H, W), dtype=bool)
    q = deque()
    # 入队：所有核心区像素
    ys, xs = np.where(core)
    for i in range(len(ys)):
        y, x = int(ys[i]), int(xs[i])
        visited[y, x] = True
        q.append((y, x))
    # BFS：4-邻接扩散，只走 ground 区
    while q:
        y, x = q.popleft()
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < H and 0 <= nx < W and not visited[ny, nx] and ground[ny, nx]:
                visited[ny, nx] = True
                q.append((ny, nx))
    return np.where(visited, mask, 0.0)


def predict_mask(img, model: str | None = None):
    """对 PIL 图像推理，返回原图尺寸的 L 模式 alpha 蒙版。

    model：指定用哪个模型（缺省用全局 _MODEL_NAME）。

    关键改进：
    - **输出 sigmoid 自动探测**：BiRefNet 的 ONNX 输出是原始 logits（需 sigmoid）；
      RMBG-2.0 多数导出已含 sigmoid（输出直接是 [0,1]）。用「最大值 > 1」判定：
      是 logits 就补 sigmoid，否则直接用——同一套后处理兼容两种模型，不挑导出格式。
    - **软阈值 [0.20, 0.75] + power(1.8) + < 0.04 强制透 + 连通性筛选**：
      「远端黑色 slogan 这种 0.10~0.35 置信区被误判为前景」的核心难题是——该区段
      也包括主体阴影、光晕、软描边，**单靠阈值无法区分**。本流程分三段：
        1. 软阈值收紧到 [0.20, 0.75]，主体高置信区满 alpha，背景直接透；
        2. power(1.8) 非线性锐化中间区，让 < 0.30 概率加速跌落到 0；
        3. mask<0.04 强制透，去微小孤立噪点；
        4. **BFS 连通性筛选**：从核心区（mask>0.4）4-邻接扩散，只走 mask>0.10 区
           ——与核心连通的阴影/光晕/描边保留，**远离主体的孤立小块（slogan）**干掉。
      阴影和 slogan 在阈值上几乎无法区分，但在「**是否与核心主体相邻**」上是
      显然不同的两类——这是唯一能稳定辨别它们的特征。
    - **轻 GaussianBlur (r=0.8)**：alpha 边缘羽化，避免硬阶梯。
    - **LANCZOS 缩回原图尺寸**：保持平滑过渡。
    """
    import numpy as np
    from PIL import Image, ImageFilter

    # 🤖 auto 是路由标签而非真实引擎：解析为默认实际引擎，避免下游 MODELS[auto] 缺 input_size 等字段崩溃。
    if (model or _MODEL_NAME) == "auto":
        model = "birefnet-general"

    sess = _get_session(model)
    meta = MODELS[model or _MODEL_NAME]
    feed = _preprocess(img, meta["input_size"], meta.get("norm", "max"))

    input_name = sess.get_inputs()[0].name
    outs = sess.run(None, {input_name: feed})

    # 取单通道 alpha 并展平为 [H, W]
    raw = np.squeeze(outs[0][:, 0, :, :])
    # sigmoid 自动探测：BiRefNet 输出 logits（max 可远超 1）→ 需 sigmoid；
    # RMBG-2.0 多数导出已含 sigmoid（输出已在 [0,1]）→ 直接当概率用。
    if float(np.max(raw)) > 1.0:
        pred = _sigmoid(raw)
    else:
        pred = raw
    pred = np.clip(pred, 0.0, 1.0)

    # MODNet 是 trimap-free 人像 matting 模型，输出本身就是连续 alpha；
    # 不需要 BiRefNet 那套阈值/power/BFS/高斯羽化，直接缩回原图尺寸返回。
    if (model or _MODEL_NAME).startswith("modnet"):
        from PIL import Image

        mod_mask = (pred * 255.0).astype("uint8")
        mask_img = Image.fromarray(mod_mask, mode="L")
        if mask_img.size != img.size:
            mask_img = mask_img.resize(img.size, Image.BILINEAR)
        return mask_img

    # 软阈值 + 非线性锐化（v2）：
    # 阈值改陡 [0.20, 0.75]：主体高置信 (>0.75) 满 alpha；远端小字/小斑块（<0.20）
    # 直接透；中间区按原始比例给软 alpha（保留光晕/描边/阴影）。
    # 非线性锐化 power(1.8)：让 mask 中段（≈0.3~0.5 概率）加速跌落到 0——
    # 解决「远处黑色 slogan 这种 0.10~0.35 置信区被映射成 30~70% 半透明」
    # 这类「远端半透碎块没抠掉」的问题。
    # mask < 0.04 强制 0：消除微小孤立 alpha 噪点（远离主体的细小颗粒、
    # 噪声纹理，跟主图风格"贴纸抠图"毫不相关的细节）。
    threshold_lo, threshold_hi = 0.20, 0.75
    span = threshold_hi - threshold_lo
    if span > 1e-8:
        mask_linear = np.where(
            pred >= threshold_hi,
            1.0,
            np.where(
                pred <= threshold_lo,
                0.0,
                (pred - threshold_lo) / span,
            ),
        )
    else:
        mask_linear = pred  # 退化：原 sigmoid
    # **V9 连通性筛选（早做，raw 概率图）**：BFS 在 power 锐化之前对 soft-thresholded
    # mask 做连通性筛选——只在「与主体核心区 4-邻接连通的非零区域」上保留 alpha，
    # 其余孤立小块（远端 slogan、背景小噪点）整体强制 0。
    #   阈值用 mask_linear（未锐化）的概率图，ground_thresh=0.05 更宽松，
    #   让 0.30~0.45 中置信的阴影/光晕「地面」可被 BFS 走进；但孤立 slogan 区
    #   不与核心连通，整片被判定为「非连通」并清 0。
    #   这一步是「远端黑色没抠掉」与「主体阴影完整」的关键区分——同样置信区间
    #   的两类像素，靠「是否与主体相邻」这一拓扑特征唯一可靠地区分。
    mask_linear = _keep_connected_to_core(
        mask_linear, core_thresh=0.3, ground_thresh=0.05
    )
    # 非线性锐化：中间区加速跌落
    mask = np.power(np.clip(mask_linear, 0.0, 1.0), 1.8)
    # 抗噪：极低概率噪点强制透
    mask = np.where(mask < 0.04, 0.0, mask)

    # 边缘羽化：高斯模糊把 0/1 硬边界展宽为连续 alpha 过渡带（发丝/阴影边缘更自然）。
    # 注：BiRefNet/RMBG-2.0 在 1024 固定输入下输出本质是 0/1 粗 mask，亚像素连续
    # alpha 无法由后处理凭空生成；高斯羽化是目前在不换模型前提下最稳定的软边手段。
    # 之前试过基于颜色的引导滤波（_guided_soften）——实测反而把边界推得更锐（硬边率升高），
    # 故不采用。
    mask_arr = np.clip(mask * 255.0, 0, 255).astype("uint8")
    mask_img = Image.fromarray(mask_arr, mode="L")
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=0.8))

    if mask_img.size != img.size:
        mask_img = mask_img.resize(img.size, _lanczos())

    return mask_img


def _save_out(rgba, out) -> None:
    """原子写透明 PNG（RGBA → out 路径）。"""
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(out_path, "PNG")


def _largest_fg_bbox(mask, W: int, H: int):
    """从显著性 mask 找最大连通前景块的 bbox（归一化 [x1,y1,x2,y2]），无显著返回 None。"""
    import numpy as np
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return None
    arr = np.array(mask)
    binm = (arr > 45).astype(np.uint8)
    cnts, _ = cv2.findContours(binm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    big = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(big) < 0.003 * H * W:
        return None  # 太小不视为主体（可能全是背景噪声）
    x, y, w, h = cv2.boundingRect(big)
    return [max(0.0, x / W), max(0.0, y / H), min(1.0, (x + w) / W), min(1.0, (y + h) / H)]


def _fg_component_count(mask) -> int:
    """前景连通块数量（alpha>45 的 4-邻接连通域个数）。"""
    import numpy as np
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return 1
    arr = np.array(mask)
    binm = (arr > 45).astype(np.uint8)
    n, _ = cv2.connectedComponents(binm)
    return max(0, n - 1)


def _sam_refine_or_keep(rgb, mask, prompt, W: int, H: int, model=None):
    """🪄 SAM 像素级精修（引导式）：SAM 可用且成功 → SAM mask；否则原样返回 BiRefNet mask。

    **关键启发式**：SAM 是「单连通主体」语义分割——对整行装饰字（6 个分离字块）这类
    **多连通主体**，它只会挑其中 1 块（实测只抠出 321px 的一小截笔画，主体全丢）。
    因此：前景连通块 > 1 时**跳过 SAM**，保留 BiRefNet（它能把框内所有字块都抠出来）；
    单连通主体（人物 / 单个物件）——SAM 边缘明显更贴，才交给 SAM 精修。
    """
    if _fg_component_count(mask) > 1:
        return mask  # 多主体（文字行/多元素）→ 不用 SAM，避免只抠一块
    try:
        p = dict(prompt)
        if p.get("type") == "box":
            x1, y1, x2, y2 = p["norm"]
            bw, bh = x2 - x1, y2 - y1
            p["norm"] = [
                max(0.0, x1 - 0.12 * bw), max(0.0, y1 - 0.12 * bh),
                min(1.0, x2 + 0.12 * bw), min(1.0, y2 + 0.12 * bh),
            ]
        sm = sam_refine_mask(rgb, p, W, H)
        if sm is not None:
            return sm
    except Exception:  # noqa: BLE001  SAM 权重未下/推理失败 → 用 BiRefNet 结果
        pass
    return mask


def _refine_alpha_by_background(rgb, alpha, bg_sample_frac=0.05, strength=0.85):
    """颜色/色度键精修 alpha：纯色背景人像中，用前景-背景颜色距离重新校正过渡区。

    对摄影棚纯色背景（橙/绿/蓝底），MODNet 在硬边处经常给出 0.7~0.9 的中间 alpha，
    导致身体轮廓残留背景色。本函数把问题反建模为经典合成方程 I = alpha*F + (1-alpha)*B：
      - 从 alpha 最低/最高的像素分别稳健估计背景色 B 与前景色 F；
      - 对每个像素反解 alpha_lin = ((I-B)·(F-B)) / |F-B|^2；
      - 在过渡区（0.05 < alpha < 0.95）用 alpha_lin 主导替换原 alpha，
        使身体硬边直接拉高到接近 1，发丝半透明区按颜色比例自然过渡；
      - 最后对仍明显带背景色的残留像素再温和压低。

    高置信前景（alpha>0.95）与几乎透明（alpha<0.05）区域基本不动，避免主体变虚或
    发丝被过度削掉。

    strength：修正强度，默认 0.85（较强）；取值 0~1，越大对"像背景"的像素压得越狠。
    """
    import numpy as np

    H, W = alpha.shape
    flat_a = alpha.ravel()
    flat_rgb = rgb.reshape(-1, 3)
    n = len(flat_a)
    k = max(int(n * bg_sample_frac), min(500, n // 20))
    # 背景色 B：取 alpha 最低的 k 个像素中位数
    bg_idx = np.argpartition(flat_a, k)[:k]
    B = np.median(flat_rgb[bg_idx], axis=0)

    # 前景色 F：取 alpha 最高的 k 个像素中位数
    fg_idx = np.argpartition(flat_a, n - k)[-k:]
    F = np.median(flat_rgb[fg_idx], axis=0)

    # 颜色距离
    d_bg = np.linalg.norm(rgb - B, axis=2)

    # tau：过渡区颜色距离 50% 分位数，兜底 0.12
    trans_mask = (alpha > 0.05) & (alpha < 0.95)
    if trans_mask.sum() > 100:
        tau = float(np.percentile(d_bg[trans_mask], 50)) * 0.75
    else:
        tau = 0.12
    tau = max(tau, 0.08)

    # 线性混合反解 alpha（色度键思想）：颜色越像前景 alpha_lin 越接近 1，
    # 颜色越像背景 alpha_lin 越接近 0。对纯色背景人像，能把身体硬边拉到 1。
    diff_BF = F - B
    denom = np.dot(diff_BF, diff_BF) + 1e-6
    alpha_lin = np.clip(((rgb - B) * diff_BF).sum(axis=2) / denom, 0.0, 1.0)

    # 过渡区权重：alpha 0.05~0.20 渐入，0.20~0.80 完全用 alpha_lin，0.80~0.95 渐出。
    # 硬边（alpha~0.85+）会被 alpha_lin 修正到接近 1；发丝（alpha~0.1~0.5）
    # 按颜色比例自然过渡；alpha<0.05 的确定透明区不受噪声干扰。
    blend_low = np.clip((alpha - 0.05) / 0.15, 0.0, 1.0)
    blend_high = np.clip((0.95 - alpha) / 0.15, 0.0, 1.0)
    blend = blend_low * blend_high

    alpha_refined = alpha * (1.0 - blend) + alpha_lin * blend

    # 额外压低：过渡区里仍明显带背景色的像素（如发丝尖端粘的橙粉）再压一点
    bg_conf = np.exp(-(d_bg ** 2) / (2.0 * tau ** 2))
    alpha_refined = alpha_refined * (1.0 - strength * bg_conf * blend)

    return np.clip(alpha_refined, 0.0, 1.0)


def _decontaminate_modnet_fg(rgb, alpha, bg_thr=0.10, min_bg_frac=0.02, radius=5):
    """MODNet 前景去污染：在透明过渡带用背景色恢复真实前景色，消除绿/蓝底渗色。

    MODNet 输出连续 alpha，但 RGB 仍是原图，导致半透明边缘会把原背景色带出来（如
    绿底人像头发边缘发绿）。按合成公式 I = α·F + (1−α)·B，已知 I 和 α 后反推 F：
        F = (I − (1−α)·B_est) / max(α, eps)
    其中 B_est 用已知背景（alpha<bg_thr）像素估计。

    策略：
      - 纯色/均匀背景：用已知背景的中位数颜色作为全局 B_est，最快且最稳；
      - 非均匀背景/主体占满画面：降级为 cv2.inpaint（缩 4x 提速）从边界背景像素
        向内填充得到空间变化 B_est。
    """
    import cv2
    import numpy as np

    H, W, _ = rgb.shape
    alpha3 = alpha[..., None]
    bg_mask = alpha < bg_thr
    n_bg = int(bg_mask.sum())

    if n_bg >= min_bg_frac * H * W:
        # 纯色/均匀背景：全局中位数背景估计
        bg_pixels = rgb[bg_mask] if n_bg > 0 else rgb.reshape(-1, 3)
        B = np.broadcast_to(np.median(bg_pixels, axis=0), (H, W, 3)).copy()
    else:
        # 非均匀背景：从已知背景向内 inpaint，缩 4x 加速
        holes = (255.0 * (~bg_mask)).astype(np.uint8)
        s = 4
        small = cv2.resize(rgb, (W // s, H // s), interpolation=cv2.INTER_AREA)
        sm_holes = cv2.resize(holes, (W // s, H // s), interpolation=cv2.INTER_NEAREST)
        Bs = np.zeros_like(small)
        for c in range(3):
            src = (small[:, :, c] * 255.0).astype(np.uint8)
            Bs[:, :, c] = cv2.inpaint(src, sm_holes, inpaintRadius=radius,
                                       flags=cv2.INPAINT_TELEA).astype(np.float64) / 255.0
        B = cv2.resize(Bs, (W, H), interpolation=cv2.INTER_CUBIC)

    # 前景锚定：从 solid-foreground（alpha>0.95）取最亮的 top-10% 像素作为
    # “真实前景色”参考（如白发、白衣服）。在低 alpha 发丝/边缘区，单纯反推
    # F = (I-(1-α)B)/α 会因 α 过小、信号弱而偏暗/偏色；用 F_anchor 做 soft
    # anchor 可让半透明边缘保持前景应有的亮度，而不是残留背景色。
    fg_mask = alpha > 0.95
    n_fg = int(fg_mask.sum())
    use_anchor = False
    F_anchor = np.array([1.0, 1.0, 1.0])
    if n_fg >= 100:
        fg_pixels = rgb[fg_mask]
        lum = fg_pixels.mean(axis=1)
        if lum.size:
            top_thresh = np.percentile(lum, 90)
            top_pixels = fg_pixels[lum >= top_thresh]
            if len(top_pixels):
                cand = top_pixels.mean(axis=0)
                if cand.mean() >= 0.45:  # 仅对亮前景启用 anchor（人像白发/白衣）
                    F_anchor = cand
                    use_anchor = True

    # 反推去污染：F = I + (I - B) * (1-α)/α。
    # 低 alpha 区 (I-B) 很小，cap 从 2.0 提到 4.0 才充分去除绿底渗色；
    # 但 cap 过高会让白边/浅色背景在边缘留下过亮条纹，反而显粗糙。
    # 降到 3.0 保留去污染能力同时抑制过曝，配合 F_anchor 避免结果偏暗。
    eps = 0.01
    scale = (1.0 - alpha) / np.maximum(alpha, eps)
    scale = np.minimum(scale, 3.0)
    scale3 = scale[..., None]
    F_decon = rgb + (rgb - B) * scale3
    F_decon = np.clip(F_decon, 0.0, 1.0)

    if use_anchor:
        # α<0.03 完全用 anchor；α>0.33 完全用去污染结果；中间平滑过渡。
        blend = np.clip((alpha - 0.03) / 0.30, 0.0, 1.0)
        blend3 = blend[..., None]
        F = blend3 * F_decon + (1.0 - blend3) * F_anchor
        F = np.clip(F, 0.0, 1.0)
    else:
        F = F_decon

    # alpha=0 时 RGB 不可见，统一置为背景色避免存储异常值
    F = np.where(alpha3 == 0.0, B, F)
    return F


def _detect_solid_background(rgb, band_frac: float = 0.06, tol: float = 0.14,
                              min_cover: float = 0.25, min_border_frac: float = 0.55):
    """判断图像是否为「纯色/近似纯色背景」（如绿幕/橙幕/摄影棚纯色底）。

    改进（2026-09-04）：主体常贴边（人像头顶/头发触顶、肩膀触侧边），旧实现按「四边各自
    中位数」比对会因某边是深色主体而误判非纯色，导致纯色背景走了 ML 模型（OOD、留橙边）。
    新版更稳健：
      1. 取四边色带（band_frac 宽）像素，中位数估计背景主色 key（对 <50% 主体污染稳健）；
      2. 若边缘色带中 >= min_border_frac 比例像素与 key 同色 → 边缘基本纯色；
      3. flood-fill 从四边洪泛定位与 key 同色且连通边缘的区块=真实背景，
         覆盖面积 >= min_cover 才认定纯色（避免四角同色但中间花哨的误判）。

    返回 (is_solid, key_color)，key_color 为归一化 [0,1] RGB。
    纯色背景对 BiRefNet/MODNet 等自然照片训练模型是 OOD，应改走色度键而非 matting。
    """
    import numpy as np

    arr = np.array(rgb, dtype=np.float64) / 255.0
    H, W = arr.shape[:2]
    e = max(4, int(min(H, W) * band_frac))

    border = np.concatenate([
        arr[:e].reshape(-1, 3),
        arr[H - e:].reshape(-1, 3),
        arr[:, :e].reshape(-1, 3),
        arr[:, W - e:].reshape(-1, 3),
    ], axis=0)
    # 中位数对 <50% 主体贴边污染稳健（主体占少数时 key 仍是背景色）
    key = np.median(border, axis=0)
    bd = np.linalg.norm(border - key, axis=1)
    border_frac = float(np.mean(bd < tol))
    if border_frac < min_border_frac:
        return False, key

    # 二次确认：flood-fill 覆盖面积足够大才认纯色背景
    try:
        import cv2
        d = np.linalg.norm(arr - key, axis=2)
        passable = (d < tol).astype(np.uint8)
        num, labels = cv2.connectedComponents(passable, connectivity=8)
        if num <= 1:
            return False, key
        bmask = np.zeros((H, W), dtype=bool)
        bmask[0, :] = bmask[-1, :] = True
        bmask[:, 0] = bmask[:, -1] = True
        touching = np.unique(labels[bmask & (labels > 0)])
        bg = np.isin(labels, touching)
        if float(bg.mean()) < min_cover:
            return False, key
    except Exception:  # noqa: BLE001
        pass
    return True, key


def _estimate_bg_color(rgb):
    """从四边边缘像素估计背景主色（归一化 [0,1] RGB），供去溢色使用。"""
    import numpy as np

    arr = np.array(rgb, dtype=np.float64) / 255.0
    H, W = arr.shape[:2]
    e = max(4, int(min(H, W) * 0.06))
    border = np.concatenate([
        arr[:e].reshape(-1, 3),
        arr[H - e:].reshape(-1, 3),
        arr[:, :e].reshape(-1, 3),
        arr[:, W - e:].reshape(-1, 3),
    ], axis=0)
    return np.median(border, axis=0)


def _despill_strong(rgb_norm, alpha, B, bg_close: float = 0.10, max_alpha_kill: float = 0.55):
    """强去溢色（纯色背景专用）：按合成方程 I=αF+(1-α)B 反推真实前景 F，
    并消除半透明过渡带里仍贴近背景色的残留像素（这些本质是背景溢出，应透明）。

    rgb_norm: 归一化 [0,1] HxWx3；alpha: [0,1] HxW；B: 背景主色。
    max_alpha_kill: 仅对 alpha 低于此阈值的像素执行 kill。默认 0.55，避免误杀
        肤色等「颜色≈背景」的实体前景（其 alpha 通常>0.55），只清除真正的半透明边缘溢色。
    返回 (F, alpha2)：alpha2 在「反推后 F 仍极近背景色」处强制 0，彻底清除彩边。
    """
    import numpy as np

    a = alpha.astype(np.float64)
    a0 = np.maximum(a, 0.15)
    F = (rgb_norm - (1.0 - a0)[..., None] * B[None, None, :]) / a0[..., None]
    F = np.clip(F, 0.0, 1.0)
    # 反推后若 F 仍极近背景色 → 该像素本就是背景溢出 → alpha 归零（清除彩边）
    diff = np.linalg.norm(F - B[None, None, :], axis=2)
    kill = (diff < bg_close) & (a < max_alpha_kill)
    a2 = np.where(kill, 0.0, a)
    return F, a2


def _matting_chroma_key(rgb, W: int, H: int, box=None, polygon=None, vision_box=None):
    """纯色/近似纯色背景抠图（色度键 chroma key）——MODNet/BiRefNet 的「正解」替代方案。

    为什么比人像 matting 模型更合适：
        纯色背景（绿幕/橙幕/摄影棚纯色）对人像 matting 模型是 OOD 输入——模型训练于
        自然照片，会把整片纯色背景也输出成高 alpha 而「去不掉」。这类问题本质是色度键
        问题，用经典算法即可稳健解决，无需 ML。

    算法：
        1. 背景色 key：取画面边缘像素中位数（纯色背景下边缘即背景）。
        2. 每像素到 key 的颜色距离 d。
        3. 背景「可通过」图：d < tol 的像素视为背景候选。
        4. 颜色距离遮罩（关键改进）：直接按「颜色与 key 的相似度」判定背景——
           纯色背景像素颜色都≈key，主体（人/物）颜色明显不同；这比连通域洪泛稳健：
           主体贴边、把背景切断成不连通块时，洪泛会漏掉橙色，颜色阈值不会。
        5. 软边：颜色过渡带内 alpha 从 0 渐入到 1（轻高斯羽化，沿颜色梯度跟随轮廓）。
        6. 去溢色 despill：按合成公式 I = αF + (1−α)B 反推 F，去除边缘残留的橙色渗色。
        7. 应用用户选区（套索/框/AI 框）作为硬边界。

    局限：若主体衣物与背景同色且连到画面边缘，会被一并键掉（所有色度键的通病，绿幕同理）。
    """
    import numpy as np
    import cv2
    from PIL import Image, ImageDraw

    img = np.array(rgb, dtype=np.float64) / 255.0  # HxWx3 in [0,1]

    # 1) 背景色估计：边缘像素中位数
    e = max(4, int(min(H, W) * 0.06))
    border = np.concatenate([
        img[:e].reshape(-1, 3),
        img[H - e:].reshape(-1, 3),
        img[:, :e].reshape(-1, 3),
        img[:, W - e:].reshape(-1, 3),
    ], axis=0)
    key = np.median(border, axis=0)

    # 2) 颜色距离 → 直接按「与背景色的相似度」判定背景，不依赖连通域/洪泛。
    #    纯色背景的核心特征就是「背景像素颜色都≈key」；主体（人/物）颜色与 key 明显不同。
    #    主体贴边、把背景切断成不连通块时，洪泛会漏掉橙色 → 颜色阈值对贴边稳健得多。
    d = np.linalg.norm(img - key, axis=2)
    tol = 0.12           # 背景阈值：d<tol 视为纯背景 → alpha=0
    tol_hi = 0.30        # 过渡带上限：d>tol_hi 视为纯前景 → alpha=1
    alpha = np.clip((d - tol) / (tol_hi - tol), 0.0, 1.0)
    # 轻微高斯羽化（沿颜色梯度，自然跟随主体轮廓，不靠洪水填充）
    edge_px = max(3, int(min(H, W) * 0.004))
    alpha = cv2.GaussianBlur(alpha, (0, 0), sigmaX=max(0.6, edge_px * 0.4))

    # 3) 强去溢色（按合成方程 I=αF+(1-α)B 反推前景 + 清除仍贴近背景色的残留）
    F, alpha = _despill_strong(img, alpha, key)

    # 7) 用户选区作为硬边界
    if polygon and len(polygon) >= 3:
        sel = Image.new("L", (W, H), 0)
        pts = [(float(p[0]) * W, float(p[1]) * H) for p in polygon if len(p) >= 2]
        if len(pts) >= 3:
            ImageDraw.Draw(sel).polygon(pts, fill=255)
            s = np.array(sel, dtype=np.float32) / 255.0
            alpha = alpha * s
    elif box is not None:
        bp = _normalize_box(box, W, H)
        if bp:
            x0, y0, x1, y1 = bp
            g = np.zeros((H, W), dtype=np.float32)
            g[y0:y1, x0:x1] = 1.0
            alpha = alpha * g
    elif vision_box is not None:
        vx0, vy0, vx1, vy1 = [float(v) for v in vision_box[:4]]
        g = np.zeros((H, W), dtype=np.float32)
        g[int(vy0 * H):int(vy1 * H), int(vx0 * W):int(vx1 * W)] = 1.0
        alpha = alpha * g

    out = np.dstack([F, alpha])
    out = (np.clip(out, 0.0, 1.0) * 255.0).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


def _matting_modnet(rgb, W: int, H: int, box=None, polygon=None, vision_box=None, model: str | None = None):
    """MODNet 人像抠图（自动路由入口）。

    纯色背景（橙/绿/蓝幕）对 MODNet 是 OOD，直接跑会留背景色溢出。故检测到纯色背景时
    改走 _matting_solid_person_hybrid（色度键保干净轮廓 + MODNet 保发丝半透 + 强去溢色）；
    复杂背景走 _matting_modnet_core。
    """
    try:
        solid, _ = _detect_solid_background(rgb)
        if solid:
            return _matting_solid_person_hybrid(
                rgb, W, H, box=box, polygon=polygon, vision_box=vision_box, model=model)
    except Exception:  # noqa: BLE001
        pass  # 检测异常则继续原 MODNet 流程
    return _matting_modnet_core(rgb, W, H, box=box, polygon=polygon, vision_box=vision_box, model=model)


def _matting_modnet_core(rgb, W: int, H: int, box=None, polygon=None, vision_box=None,
                         model: str | None = None, skip_gate: bool = False):
    """MODNet 人像抠图核心（不含纯色背景检测）。

    MODNet 是 trimap-free 人像 matting，对人物/发丝给出自然半透明 alpha。
    非人像场景效果差，因此作为可选引擎由用户显式选择。

    处理逻辑：
      - 无选区：整图跑 MODNet；
      - 矩形框/套索/VLM 框：以该框为裁切区域跑 MODNet（保留上下文），
        再把框/套索外区域强制透明；
      - 最后做前景色去污染，消除彩色背景在发丝/边缘的渗色。
    skip_gate：纯色背景混合路径已自带 chroma 门控，无需再跑 BiRefNet 门控（省 927MB 加载）。
    """
    import numpy as np
    from PIL import Image, ImageDraw

    sel = Image.new("L", (W, H), 255)
    crop_px = (0, 0, W, H)
    has_selection = False

    if polygon and len(polygon) >= 3:
        pts = [(float(p[0]) * W, float(p[1]) * H) for p in polygon if len(p) >= 2]
        if len(pts) >= 3:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
            crop_px = (x0, y0, x1, y1)
            ImageDraw.Draw(sel).polygon(pts, fill=255)
            has_selection = True
    elif box is not None:
        bp = _normalize_box(box, W, H)
        if bp:
            crop_px = bp
            x0, y0, x1, y1 = bp
            ImageDraw.Draw(sel).rectangle([x0, y0, x1, y1], fill=255)
            has_selection = True
    elif vision_box is not None:
        vx0, vy0, vx1, vy1 = [float(v) for v in vision_box[:4]]
        crop_px = (int(round(vx0 * W)), int(round(vy0 * H)), int(round(vx1 * W)), int(round(vy1 * H)))
        ImageDraw.Draw(sel).rectangle(crop_px, fill=255)
        has_selection = True

    if crop_px == (0, 0, W, H):
        mask = predict_mask(rgb, model=model)
    else:
        mask = _box_crop_mask(rgb, crop_px, W, H, model=model)

    mod_alpha = np.array(mask, dtype=np.float32) / 255.0

    # 计算手动选区面积比例，用于判断用户是否只是"粗线圈了整图"。
    selection_area_ratio = 1.0
    if has_selection:
        s_arr = np.array(sel, dtype=np.uint8)
        selection_area_ratio = float(s_arr.sum()) / (255.0 * W * H)

    # 🛡️ 稳健化（修复「背景没去掉」）：MODNet 是**人像** matting，遇到非标准照片 /
    # 强色背景（如整片橙底）时会把整片选区都输出 alpha≈1，背景完全没被分离出来。
    # 对「无手动选区」或「手动选区几乎覆盖全图（面积>85%）」的场景，用 BiRefNet
    # 显著性粗 mask 作「全局前景门控」：只保留 BiRefNet 判定为前景的区域，MODNet
    # 的连续软 alpha 仅在前景内生效 → 背景必然透明，同时保留 MODNet 的发丝级软边。
    # ⚠️ 小范围精确选区（box/polygon 面积<85%）时，以用户选区为硬边界，不再用 BiRefNet
    # 门控去裁剪选区内部——否则 BiRefNet 对特殊构图/竖图识别异常时，会误把
    # 用户明确圈出的主体也压掉。
    # BiRefNet 不可用时（未下载/异常/内存不足）回退纯 MODNet。
    gate_enabled = (not has_selection) or (selection_area_ratio > 0.85)
    if gate_enabled and not skip_gate:
        try:
            bi_mask = predict_mask(rgb, model="birefnet-general")
            bi_soft = np.array(bi_mask, dtype=np.float32) / 255.0
            # BiRefNet 本质是 0/1 粗 mask，直接相乘会截断 MODNet 的发丝软边。
            # 对门控 mask 做高斯羽化，把硬边界展宽成连续过渡带；并且采用
            # 「背景硬清零 + 前景完全信任」策略：
            #   - bi_soft < 0.05 视为确定背景 → alpha 强制 0（绝对不漏背景）；
            #   - 0.05 ~ 0.45 为过渡带 → gate 从 0 线性升到 1；
            #   - bi_soft > 0.45 视为确定前景 → gate=1，完整保留 MODNet 软 alpha。
            import cv2

            sigma = max(8.0, min(W, H) / 400.0)
            bi_soft = cv2.GaussianBlur(bi_soft, (0, 0), sigmaX=sigma)
            gate = np.clip((bi_soft - 0.05) / 0.40, 0.0, 1.0)
            gated = mod_alpha * gate
            gated[bi_soft < 0.05] = 0.0
            # 防呆：若门控把前景几乎全抹掉（BiRefNet 异常判空），回退纯 MODNet，避免误清空
            if float(gated.mean()) > 1e-3:
                mod_alpha = gated
        except Exception:  # noqa: BLE001
            pass  # 回退纯 MODNet

    alpha = mod_alpha

    if has_selection:
        s = np.array(sel, dtype=np.float32) / 255.0
        alpha = alpha * s

    # 通道/颜色 alpha 精修：MODNet 在强纯色背景（如橙底）边缘容易把"带背景色的半透明
    # 像素"也给出较高 alpha，导致边缘粗糙、背景色渗出。用颜色距离做局部修正：
    #   - 估计背景色（取 alpha 最低的 5% 像素）；
    #   - 对每个像素计算与背景色的欧氏距离 d；
    #   - 非高置信前景区（alpha < 0.90）中 d 越小（越像背景）的像素，较强地压低 alpha。
    # 这样只处理残留的"背景色半透明带"，高置信主体几乎不受影响。
    rgb_arr = np.array(rgb, dtype=np.float64) / 255.0
    try:
        alpha = _refine_alpha_by_background(rgb_arr, alpha, strength=0.85)
    except Exception:  # noqa: BLE001
        pass

    fg = _decontaminate_modnet_fg(rgb_arr, alpha)
    out = np.dstack([fg, alpha[..., None]])
    out = (np.clip(out, 0.0, 1.0) * 255.0).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


def _matting_solid_person_hybrid(rgb, W: int, H: int, box=None, polygon=None, vision_box=None,
                                 model: str | None = None):
    """纯色背景 + 人像混合抠图：MODNet 保发丝半透，颜色距离背景清零 + 强去溢色。

    为什么不直接用色度键：色度键对纯色背景是「正解」，但会对「与背景同色的发丝」直接
    一刀切掉，丢失细发丝。为什么不直接用 MODNet：MODNet 对纯色背景是 OOD，半透明边缘
    会漏出背景色（橙/绿边）。本函数把两者优点结合：
      - MODNet 给出「发丝级连续 alpha」（保留细发丝 wisps）；
      - 颜色距离背景清零：纯色背景下「颜色≈背景」的像素就是背景 → 直接透明，
        彻底解决 MODNet 把橙/绿底半透明漏出的彩边（不依赖洪水填充，主体贴边也稳）；
      - 过渡带保留 MODNet 软 alpha（它知道这里有发丝）；
      - 按合成方程 I=αF+(1-α)B 强去溢色，仍贴近背景色的残留像素直接透明 → 彻底无彩边。
    """
    import numpy as np
    import cv2
    from PIL import Image

    rgb_arr = np.array(rgb, dtype=np.float64) / 255.0
    B = _estimate_bg_color(rgb)

    # 1) MODNet 软 alpha（发丝级连续 alpha）；本路径自带颜色背景清零，无需 BiRefNet 门控
    try:
        mod_rgba = _matting_modnet_core(
            rgb, W, H, box=box, polygon=polygon, vision_box=vision_box,
            model=model or "modnet-photographic", skip_gate=True)
        mod_a = np.array(mod_rgba.split()[-1], dtype=np.float64) / 255.0
    except Exception:  # noqa: BLE001
        # MODNet 失败：退回纯色度键（颜色距离遮罩 + 去溢色），仍远优于裸 MODNet
        return _matting_chroma_key(rgb, W, H, box=box, polygon=polygon, vision_box=vision_box)

    # 2) 颜色距离驱动 alpha：纯色背景下「颜色与背景 B 的相似度」直接决定透明度，
    #    比 MODNet 的 OOD 软 alpha 干净得多——背景像素(颜色≈B)必然透明，彻底无彩边；
    #    过渡带内用颜色梯度给出平滑边缘，前景内用 MODNet 补发丝级软细节。
    #
    # 关键修复（2026-09-04）：旧实现「d<t_hi 完全由 color_a 接管」会误伤肤色——
    # 橙幕前的人脸皮肤颜色与背景橙相近，d 落在 0.12~0.30 之间，被 color_a 压成半透明，
    # 随后 _despill_strong 又因「反推 F≈B」把皮肤 kill 成透明窟窿。
    # 新版以 MODNet alpha 为基，只在 MODNet 不置信或明确背景处用颜色距离压制：
    #   - d < t_lo：强制背景透明（纯色背景核心区域，不受 MODNet OOD 影响）；
    #   - t_lo <= d < t_hi：MODNet 高置信前景（a>0.85）保持，其余用 min(mod_a, color_a) 抑制；
    #   - d >= t_hi：完全前景，保留 MODNet alpha（发丝 wisps 不丢）。
    d = np.linalg.norm(rgb_arr - B[None, None, :], axis=2)
    t_lo, t_hi = 0.10, 0.28
    color_a = np.clip((d - t_lo) / (t_hi - t_lo), 0.0, 1.0)

    final_a = mod_a.copy()
    # 明确背景色域：强制透明
    final_a[d < t_lo] = 0.0
    # 过渡带：保护 MODNet 高置信前景（a>0.85），其余按颜色距离收紧（消除 OOD 背景/彩边）。
    transition = (d >= t_lo) & (d < t_hi)
    confident_fg = mod_a > 0.85
    uncertain = transition & ~confident_fg
    final_a[uncertain] = np.minimum(final_a[uncertain], color_a[uncertain])

    # 3) 强去溢色：只清理真正的半透明边缘溢色，不杀高 alpha 实体前景（防止皮肤被抠）
    F, final_a = _despill_strong(rgb_arr, final_a, B, max_alpha_kill=0.55)
    out = np.dstack([F, final_a[..., None]])
    out = (np.clip(out, 0.0, 1.0) * 255.0).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


def _is_person_label(label: str | None) -> bool:
    """判断 VLM 返回的主体标签是否为人像（用于路由到 MODNet 真连续 alpha）。"""
    if not label:
        return False
    k = (label or "").lower()
    keys = ("人", "人物", "人像", "肖像", "person", "people", "portrait", "man",
            "woman", "child", "baby", "boy", "girl", "face", "头", "脸", "男", "女",
            "小孩", "婴儿", "老")
    return any(x in k for x in keys)


def _norm_box_from_inputs(vision_box, polygon, box):
    """从 VLM 框 / 套索 / 矩形框 推导归一化 [x1,y1,x2,y2]，供云端抠图定位用。

    优先级：VLM 视觉框（说扣什么目标）> 套索多边形 > 矩形框。都无则返回 None（抠全图主主体）。
    """
    if vision_box and len(vision_box) == 4:
        x1, y1, x2, y2 = [float(v) for v in vision_box[:4]]
        return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
    if polygon and len(polygon) >= 3:
        xs = [float(p[0]) for p in polygon]
        ys = [float(p[1]) for p in polygon]
        return [min(xs), min(ys), max(xs), max(ys)]
    if box and len(box) == 4:
        x, y, w, h = [float(v) for v in box[:4]]
        return [x, y, x + w, y + h]
    return None


def matting_image(src: str | Path, out: str | Path, box: tuple | list | None = None, model: str | None = None, vision_box: tuple | list | None = None, polygon: list | None = None, click: list | None = None, blocks: list | None = None, sam_refine: bool = False, vision_label: str = "", meta: dict | None = None) -> None:
    """对单张图片做一键抠图，输出 RGBA 透明 PNG 到 out。

    src/out 为路径（str 或 Path）。透明 PNG 可直接用于合成 / 换背景。

    sam_refine（可选，默认 False）：🔬 精细边缘——用户给了语义选区
      （套索 polygon / 矩形 box / AI 视觉定位 vision_box / 点选 click）且开启时，
      改用 MobileSAM（像素级语义分割）直接出主体 mask，边缘远优于显著性模型；
      无选区（自动整图）或 blocks 多块时不适用，保持 BiRefNet。任何失败自动回退。

    blocks（可选）：**🧲 智能魔棒多选**——用户 hover 高亮智能元素块、点击选中多个
      不相邻的块（如主标题 + 右上角图案），值 = 每个选中块的归一化轮廓点列表
      [[[x,y]...], ...]（analyze_blocks 返回的 contour）。后端把它们作并集一次抠出。
    click（可选）：**[x,y] 归一化 0~1**，点图抠图——用户直接在预览图上
      单击想要的主体（主标题/图案/按钮等），后端跑全图显著性，
      从点击点 BFS 扩散找到"点击处所属的那个元素"（连通显著块）只抠它。
      用户点哪抠哪——"要抠的东西不固定"由用户随手决定。

    box（可选）：用户手动框选的主体区域，归一化 (x, y, w, h)，取值 0~1。
      给定时**只把框内区域送进模型推理**（而不是全图推理后再裁），
      这样模型会把框内内容当成整张图来找显著主体——
      照片里有多个人/物时，框谁就抠谁，比全图推理精确得多。
      框外一律透明。
    polygon（可选）：用户**套索工具**自由圈出的多边形，归一化点列表
      [[x1,y1], [x2,y2], ...]（≥3 点）。比矩形框精确得多——能贴合不规则主体
      （如只圈主标题文字、避开紧贴的副标题/装饰）。优先级最高：给了 polygon
      就以它为准（套索内保留、套索外强制透明），box/vision_box 作为上下文提示。
    vision_box（可选）：VLM 视觉定位给出的主体边界框，归一化 [x1,y1,x2,y2] ∈ [0,1]。
      给定时用「AI 视觉定位」引导抠图（见 vision_guided_mask）——VLM 看懂图、
      明确框出主体（如主标题），框外（副标题/装饰/背景）强制透明，专治"同色系
      包围下主字 vs 副字分不清"的难题。与 box 同给时取交集（手动框为硬边界）。
    """
    from PIL import Image

    if not available():
        raise RuntimeError("一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")

    with Image.open(src) as im:
        im.load()
        rgb = im.convert("RGB")
        W, H = rgb.size

        # ☁️ 云端抠图优先（火山，豆包级像素质量）；失败回退下方本地链路。
        # 仅对「默认/智能/像素级」意图启用云端优先；用户显式选了本地特殊引擎
        # （色度键=纯色专用、modnet=本地人像）时尊重本地选择，不覆盖。
        # 优先级：① AI MediaKit 智能抠图（通用软 alpha，任意主体，豆包级）>
        #         ② cv 视觉智能 GeneralSegment/HumanSegment（硬分割，物体可用）>
        #         ③ 本地 BiRefNet/MODNet/色度键 兜底。
        if meta is not None:
            meta.setdefault("cloud_used", False)
        _cloud_models = ("auto", "birefnet-general", "sam-matting")
        _is_person = (
            _is_person_label(vision_label)
            or _is_person_label((meta or {}).get("prompt", ""))
        )
        if (model or _MODEL_NAME) in _cloud_models:
            _cb = _norm_box_from_inputs(vision_box, polygon, box)
            # ① MediaKit 通用软 alpha 抠图（豆包级，任意图）
            if is_cloud_matting_mediakit_ready():
                try:
                    from cloud_matting_mediakit import mediakit_remove_bg
                    _scene = "human" if _is_person else "general"
                    rgba = mediakit_remove_bg(rgb, scene=_scene, timeout=90)
                    if meta is not None:
                        meta["cloud_used"] = True
                        meta["cloud_provider"] = "volcengine-mediakit"
                    _save_out(rgba, out)
                    return
                except Exception as _ce:  # noqa: BLE001
                    import logging as _lg
                    _lg.getLogger("matting_ai").warning("MediaKit 抠图失败，回退: %s", _ce)
                    if meta is not None:
                        meta["cloud_error"] = str(_ce)[:200]
            # ② cv 视觉智能（SigV4 AK/SK）：物体走 GeneralSegment，人像走 HumanSegment
            if is_cloud_matting_ready():
                try:
                    from cloud_matting import cloud_matting_rgba
                    rgba = cloud_matting_rgba(rgb, box=_cb, person=_is_person, timeout=60)
                    if meta is not None:
                        meta["cloud_used"] = True
                        meta["cloud_provider"] = "volcengine"
                    _save_out(rgba, out)
                    return
                except Exception as _ce:  # noqa: BLE001
                    import logging as _lg
                    _lg.getLogger("matting_ai").warning("云端抠图失败，回退本地: %s", _ce)
                    if meta is not None:
                        meta["cloud_error"] = str(_ce)[:200]

        # 🤖 智能自动路由（auto / 默认引擎）：根据背景类型自动选最优引擎，用户无需判断背景纯不纯。
        #   - 纯色/近似纯色背景（绿幕/橙幕/摄影棚纯色）→ 色度键（颜色距离遮罩 + 强去溢色，硬边干净、零 ML 权重），
        #     远比硬刚 MODNet/BiRefNet 稳（纯色对自然照片训练模型是 OOD，会反复给背景高 alpha）；
        #   - 复杂/非纯色背景（花纹/渐变/真实场景）→ BiRefNet 通用语义分割（软边羽化）。
        # 这样「无论背景纯不纯」都通吃：纯色不再受 ML 模型 OOD 之苦，复杂不再受色度键局限。
        #   · 检测到纯色时（无论有无用户选区）整图/带选区都走色度键（内部会按选区做硬边界）；
        #   · 复杂背景时把底层引擎强制为 BiRefNet，继续走下方完整选区逻辑（blocks/click/套索/框/AI 框）。
        if (model or _MODEL_NAME) == "auto":
            is_solid = False
            try:
                is_solid, _ = _detect_solid_background(rgb)
            except Exception:  # noqa: BLE001
                is_solid = False
            if is_solid:
                # 纯色背景 + 人像：色度键保干净轮廓 + MODNet 保发丝半透 + 强去溢色，
                # 远优于二者单独使用（色度键丢发丝、MODNet 漏橙/绿边）。
                _person = _is_person_label(vision_label) or _is_person_label(
                    (meta or {}).get("prompt", ""))
                if _person:
                    try:
                        rgba = _matting_solid_person_hybrid(
                            rgb, W, H, box=box, polygon=polygon, vision_box=vision_box,
                            model="modnet-photographic",
                        )
                        _save_out(rgba, out)
                        return
                    except Exception as _e:  # noqa: BLE001
                        import logging as _lg

                        _lg.getLogger("matting_ai").warning(
                            "auto 纯色人像混合路由失败回退色度键: %s", _e)
                rgba = _matting_chroma_key(
                    rgb, W, H, box=box, polygon=polygon, vision_box=vision_box
                )
                _save_out(rgba, out)
                return
            # 复杂/非纯色背景 + 人像 → MODNet 真连续 alpha（发丝级半透），优于 BiRefNet 硬分割
            if _is_person_label(vision_label) or _is_person_label((meta or {}).get("prompt", "")):
                try:
                    rgba = _matting_modnet(
                        rgb, W, H, box=box, polygon=polygon, vision_box=vision_box,
                        model="modnet-photographic",
                    )
                    _save_out(rgba, out)
                    return
                except Exception as _e:  # noqa: BLE001
                    import logging as _lg

                    _lg.getLogger("matting_ai").warning("auto 人像路由 MODNet 失败回退: %s", _e)
            # 复杂/非纯色背景 → 强制底层引擎为 BiRefNet 通用分割，复用下方选区逻辑。
            model = "birefnet-general"

        # 🎨 纯色背景色度键（chroma key）引擎：用户显式选择时强制走，不依赖 ML 模型。
        # 对纯色/近似纯色背景（绿幕/橙幕/摄影棚纯色）是「正解」，远稳于人像 matting。
        if (model or _MODEL_NAME) == "chroma-key":
            rgba = _matting_chroma_key(rgb, W, H, box=box, polygon=polygon, vision_box=vision_box)
            _save_out(rgba, out)
            return

        # 🪄 SAM 软抠像：像素级分割 + 导向滤波连续 alpha（发丝/婚纱半透，到像素级）。
        # 不依赖 scipy，全分辨率实时；任何失败自动回退下方 BiRefNet 分支。
        if (model or _MODEL_NAME) == "sam-matting":
            try:
                # 人像 → MODNet 真连续 alpha（发丝半透），比 SAM 软抠像更自然
                if _is_person_label(vision_label):
                    rgba = _matting_modnet(
                        rgb, W, H, box=box, polygon=polygon, vision_box=vision_box,
                        model="modnet-photographic",
                    )
                    _save_out(rgba, out)
                    return
                rgba = _matting_sam_trimap(
                    rgb, W, H, box=box, polygon=polygon, vision_box=vision_box, click=click
                )
                _save_out(rgba, out)
                return
            except Exception as _e:  # noqa: BLE001
                import logging as _lg

                _lg.getLogger("matting_ai").warning("sam-matting 回退 BiRefNet: %s", _e)
                # 落到下方默认 BiRefNet 流程继续处理（重置 model 避免后续 _get_session('sam-matting') 崩）
                model = "birefnet-general"

        # 🔬 SAM 精细抠图前置：用户勾了「精细边缘」且给了语义 prompt（框/
    # AI视觉定位/点选，blocks 多块除外）→ 优先走 SAM 像素级分割。
    # SAM 靠 prompt 切主体、边缘远精于显著性；失败自动回退下方 BiRefNet 分支。
    #
    # 注意：**套索(polygon)不走 SAM**。SAM 对 polygon 只取外接矩形当 box prompt，
    # 输出是粗粒度二值 mask——会吞掉细笔画（如「醒」右侧）且完全丢失半透明区域
    # （橙色立体阴影、发丝）。而 BiRefNet 套索路径（_polygon_mask）已带中心连通保留
    # + 主色聚类去杂质 + 橙色阴影补全 + 6px 羽化，对文字/阴影/毛发类主体远优于
    # SAM 的二值剪影。故套索一律走 BiRefNet，SAM 仅保留给框选/自动/点选等实心物体。
    if sam_refine and not blocks and not polygon:
        try:
            sprompt = None
            if click:
                sprompt = {"type": "point", "pt": [float(click[0]), float(click[1])]}
            elif vision_box is not None:
                sprompt = {"type": "box", "norm": [float(v) for v in vision_box[:4]]}
            elif box:
                from PIL import Image as _Im
                bp = _normalize_box(box, W, H)
                if bp is not None:
                    sprompt = {"type": "box", "norm": [bp[0] / W, bp[1] / H, bp[2] / W, bp[3] / H]}
            if sprompt:
                sam_mask = sam_refine_mask(rgb, sprompt, W, H)
                if sam_mask is not None:
                    rgba = rgb.convert("RGBA")
                    rgba.putalpha(_edge_soften_mask(sam_mask))
                    _save_out(rgba, out)
                    return
        except Exception as _e:  # noqa: BLE001  任何异常 → 回退显著性流程
            import logging as _lg
            _lg.getLogger("matting_ai").warning("sam_refine 回退 BiRefNet: %s", _e)

    # 🎭 MODNet 人像 matting：连续 alpha / 发丝级精修（可选引擎）。
    # 跳过 BiRefNet 的阈值/power/BFS/橙色阴影补全/边缘羽化，直接保留模型原生 alpha，
    # 并做前景色去污染消除背景色渗色。输出已是 RGBA，无需再 putalpha。
    # 点选/魔棒是多选专用 BiRefNet 能力，MODNet 不支持 → 此场景回退 BiRefNet 路径。
    if (model or _MODEL_NAME).startswith("modnet") and not blocks and not click:
        rgba = _matting_modnet(rgb, W, H, box=box, polygon=polygon, vision_box=vision_box, model=model)
        _save_out(rgba, out)
        return

    if blocks:
        # 🧲 智能魔棒多选：用户 hover 高亮 + 点击选中的多个元素块（并集）一起抠
        mask = _blocks_mask(rgb, blocks, W, H, model=model)
    elif click:
        # 👆 点图抠图：用户点哪抠哪（点击处所在连通显著块）
        mask = _click_mask(rgb, click, W, H, model=model)
    elif polygon:
        if vision_box is not None:
            # **套索 + AI 视觉定位 同时勾选** → 交集模式：
            # AI 先按 VLM box 整图抠主体（主标题，box 外强制透明），
            # 再乘套索 polygon（用户圈的范围边界）取交集。
            # 语义：用户粗圈整张海报也没关系，AI 会在圈内锁定主标题抠，
            # 圈外的副标题/装饰/卡片全透明。VLM 定位异常时回退纯套索。
            mask = _polygon_vision_intersect(rgb, polygon, vision_box, W, H, model=model)
            if mask is None:
                mask = _polygon_mask(rgb, polygon, W, H, model=model)
        else:
            # 套索（最精确的用户意图）→ 优先级最高（无 AI 时）
            mask = _polygon_mask(rgb, polygon, W, H, model=model)
    elif vision_box and box is not None:
        # **手动框选 + AI 视觉定位 同时给出**：手动框是用户的明确意图，必须作为
        # 硬边界（「框住什么就是什么」），VLM 只在用户框**内部**再做语义精修。
        # 两者取交集：既尊重用户框选，又用 VLM 剔除框内夹带的副标题/装饰。
        # 若 VLM box 与用户框无重叠（VLM 理解偏差），退化为用户框，绝不越界。
        box_px = _normalize_box(box, W, H)
        if box_px is None:
            # 用户框无效（过小/越界）→ 只用 VLM（局部裁切推理，修复装饰字盲区）
            _vx0, _vy0, _vx1, _vy1 = [float(v) for v in vision_box[:4]]
            _vbw, _vbh = _vx1 - _vx0, _vy1 - _vy0
            _ex0, _ey0 = max(0.0, _vx0 - _vbw * 0.12), max(0.0, _vy0 - _vbh * 0.12)
            _ex1, _ey1 = min(1.0, _vx1 + _vbw * 0.12), min(1.0, _vy1 + _vbh * 0.12)
            _vbpx = (int(round(_ex0 * W)), int(round(_ey0 * H)), int(round(_ex1 * W)), int(round(_ey1 * H)))
            mask = _box_crop_mask(rgb, _vbpx, W, H, model=model)
        else:
            ux1, uy1 = box_px[0] / W, box_px[1] / H
            ux2, uy2 = box_px[2] / W, box_px[3] / H
            vx1, vy1, vx2, vy2 = [float(v) for v in vision_box[:4]]
            ix1, iy1 = max(ux1, vx1), max(uy1, vy1)
            ix2, iy2 = min(ux2, vx2), min(uy2, vy2)
            if ix2 - ix1 > 1e-4 and iy2 - iy1 > 1e-4:
                # 局部裁切推理（修复整图 BiRefNet 对装饰风字盲区）；
                # 框外强制透明由下方「硬夹紧到用户框」再保证一次。
                _ix_px = (int(round(ix1 * W)), int(round(iy1 * H)), int(round(ix2 * W)), int(round(iy2 * H)))
                mask = _box_crop_mask(rgb, _ix_px, W, H, model=model)
                # 硬夹紧到用户框：vision_guided_mask 内部还有 12% 外扩（保留光晕/
                # 阴影软元素），可能让 alpha 略微溢出用户框。这里再与用户框相乘，
                # 保证「框住什么就是什么」——用户框外一律 0，绝不越界。
                import numpy as _np
                from PIL import Image as _Image

                _arr = _np.array(mask).astype(_np.float32)
                _ux0, _uy0 = int(round(ux1 * W)), int(round(uy1 * H))
                _ux1, _uy1 = int(round(ux2 * W)), int(round(uy2 * H))
                _g = _np.zeros_like(_arr)
                if _ux1 > _ux0 and _uy1 > _uy0:
                    _g[_uy0:_uy1, _ux0:_ux1] = 1.0
                mask = _Image.fromarray(
                    _np.clip(_arr * _g, 0, 255).astype(_np.uint8), mode="L"
                )
            else:
                # 无交集（VLM 框到了框外）→ 退化为用户框，用框内裁切推理
                mask = _box_crop_mask(rgb, box_px, W, H, model=model)
    elif vision_box:
        # 🪄 VLM 视觉定位引导抠图（**局部裁切推理**，非整图推理再裁）：
        # 装饰风字体（白字+描边+半透明投影）在整图显著性图上几乎全黑（BiRefNet 盲区），
        # 但把 VLM 框定的主体区域**局部裁切**后单独送模型，弱信号文字会在局部显成显著主体——
        # 这是「VLM 框住主标题 → 抠出整行清晰字形」的关键修复。
        # 先将 VLM box 外扩 12% 涵盖主体外侧光晕/阴影（与旧 vision_guided_mask 的 expand 一致），
        # 再交 _box_crop_mask（内部 10% 上下文 + 严格限制回原框，框外强制透明）。
        vx0, vy0, vx1, vy1 = [float(v) for v in vision_box[:4]]
        _bw, _bh = vx1 - vx0, vy1 - vy0
        _ex0, _ey0 = max(0.0, vx0 - _bw * 0.12), max(0.0, vy0 - _bh * 0.12)
        _ex1, _ey1 = min(1.0, vx1 + _bw * 0.12), min(1.0, vy1 + _bh * 0.12)
        _vb_px = (int(round(_ex0 * W)), int(round(_ey0 * H)), int(round(_ex1 * W)), int(round(_ey1 * H)))
        mask = _box_crop_mask(rgb, _vb_px, W, H, model=model)
        # 🪄 智能精修：VLM 框内若是单连通主体（人物/物件）→ SAM 像素级贴边；
        # 多连通（整行文字）保持局部裁切的 BiRefNet 结果（SAM 只切一块会丢字）。
        mask = _sam_refine_or_keep(rgb, mask, {"type": "box", "norm": [vx0, vy0, vx1, vy1]}, W, H, model)
    elif box is None:
        # 未框选：🪄 智能自动——显著性整图 → 取最大连通主体块 bbox → SAM 像素级精修
        mask = predict_mask(rgb, model=model)
        pb = _largest_fg_bbox(mask, W, H)
        if pb is not None:
            mask = _sam_refine_or_keep(rgb, mask, {"type": "box", "norm": pb}, W, H, model)
    else:
        box_px = _normalize_box(box, W, H)
        if box_px is None:
            # 框选无效（过小/越界）：退回整图推理
            mask = predict_mask(rgb, model=model)
        else:
            # 矩形框选 = 4 点多边形，复用套索同款后处理（中心连通保留、
            # 主色聚类去黑条、橙色阴影补全）。单纯 _box_crop_mask 局部裁切
            # 不剔除框内杂质，框稍大就会连带黑条/背景一起抠出。
            _bx0, _by0, _bx1, _by1 = box_px
            poly_box = [
                [_bx0 / W, _by0 / H],
                [_bx1 / W, _by0 / H],
                [_bx1 / W, _by1 / H],
                [_bx0 / W, _by1 / H],
            ]
            mask = _polygon_mask(rgb, poly_box, W, H, model=model)

    rgba = rgb.convert("RGBA")
    # 🔬 全局边缘软化（像素级）：所有路径（自动/点选/框选/套索/魔棒多块）统一经过。
    # BiRefNet 输出经阈值+power 后基本是 0/255 二值，边缘锯齿硬切；此处只在
    # 前景/背景交界 2px 环带内对 alpha 做高斯平滑，让头发丝/衣边/文字边缘自然过渡。
    mask = _edge_soften_mask(mask)
    rgba.putalpha(mask)

    _save_out(rgba, out)


def _edge_soften_mask(mask, radius: int = 2, sigma: float = 1.2):
    """🔬 alpha 边缘软化：只在前景/背景交界 ±radius 环带内平滑，其余保持。

    BiRefNet/后处理常把 alpha 推到 0/255 两极 → 输出边缘是一像素级硬切（毛糙/锯齿）。
    这里用形态学找到边缘环带（dilate - erode），带内 alpha 用高斯模糊版本替换：
     - 硬边变成 2-3px 自然半透明过渡（视觉=柔和发丝/衣边）
     - 前景内部与背景深处原样保留（不影响主体细节）
    mask: PIL L 模式 0-255。返回同尺寸 PIL L。
    """
    import numpy as np
    from PIL import Image as _PIL_Img
    try:
        import cv2
    except Exception:  # noqa: BLE001
        return mask
    arr = np.array(mask, dtype=np.uint8)
    fg = (arr > 127).astype(np.uint8)
    struct = np.ones((2 * radius + 1, 2 * radius + 1), dtype=np.uint8)
    dil = cv2.dilate(fg, struct, iterations=1)
    ero = cv2.erode(fg, struct, iterations=1)
    band = (dil - ero).astype(bool)  # 边界环带
    if not band.any():
        return mask
    blur = cv2.GaussianBlur(arr.astype(np.float32), (7, 7), sigma)
    out = arr.copy().astype(np.float32)
    out[band] = blur[band]
    return _PIL_Img.fromarray(np.clip(out, 0, 255).astype(np.uint8), mode="L")


# ===== 🪄 SAM 软抠像（像素级分割 + 导向滤波连续 alpha）=====
def _guided_filter_matting(rgb_pil, p, radius: int = 10, eps: float = 1e-3):
    """导向滤波软抠像：把二值/近二值 trimap `p` 在未知带内解成连续 alpha。

    用原图颜色作引导（guided filter），让 alpha 的过渡沿颜色边缘对齐——
    发丝/婚纱这类半透明区域会按「更像前景还是背景」得到 0~1 之间的连续透明度。
    免 scipy，只需 numpy + opencv（boxFilter），O(N) 全分辨率实时可跑。

    rgb_pil: PIL RGB 原图；p: float32 [H,W] 0~1（确定前景=1 / 确定背景=0 / 未知=初值）。
    返回 float32 [H,W] 0~1 连续 alpha。
    """
    import numpy as np

    try:
        import cv2
    except Exception:  # noqa: BLE001
        # 没 opencv 就退化成高斯模糊软边，至少不崩
        from PIL import Image, ImageFilter

        tmp = Image.fromarray(np.clip(p * 255.0, 0, 255).astype(np.uint8), mode="L")
        return np.asarray(tmp.filter(ImageFilter.GaussianBlur(radius=2)), dtype=np.float32) / 255.0

    I = np.asarray(rgb_pil, dtype=np.float64) / 255.0  # [H,W,3]
    H, W, _ = I.shape
    p = p.astype(np.float64)

    def _box(x):
        k = 2 * radius + 1
        if x.ndim == 2:
            return cv2.boxFilter(x, -1, (k, k), borderType=cv2.BORDER_REFLECT)
        return cv2.boxFilter(x, -1, (k, k), borderType=cv2.BORDER_REFLECT)

    mean_I = _box(I)                       # [H,W,3]
    mean_p = _box(p)                       # [H,W]
    mean_Ip = np.stack([_box(I[:, :, c] * p) for c in range(3)], axis=2)  # [H,W,3]
    cov_Ip = mean_Ip - mean_I * mean_p[:, :, None]                        # [H,W,3]
    mean_II = np.stack([_box(I[:, :, c] * I[:, :, c]) for c in range(3)], axis=2)  # [H,W,3]
    var_I = mean_II - mean_I * mean_I                                   # 对角协方差 [H,W,3]
    # 对角协方差近似的 color guided filter（快速、稳）
    a = cov_Ip / (var_I + eps)           # [H,W,3]
    b = mean_p - np.sum(a * mean_I, axis=2)  # [H,W]
    mean_a = _box(a)                     # [H,W,3]
    mean_b = _box(b)                     # [H,W]
    q = np.clip(np.sum(mean_a * I, axis=2) + mean_b, 0.0, 1.0)  # [H,W]
    return q.astype(np.float32)


def _matting_sam_trimap(rgb, W: int, H: int, box=None, polygon=None,
                        vision_box=None, click=None, model=None):
    """SAM 像素级分割 → trimap → 导向滤波连续 alpha 的软抠像引擎。

    兼容所有选区模式：
      - click：点选 → SAM point prompt
      - vision_box：AI 视觉定位框 → SAM box prompt
      - box：用户矩形框 → SAM box prompt（框外强制透明由 sam_refine_mask 保证）
      - polygon：套索 → SAM 取 bbox 当 prompt，输出乘多边形硬边界
      - 无选区（auto）：BiRefNet 最大主体 bbox → SAM box prompt
    任何一步失败都回退到 BiRefNet 基础抠图，绝不崩。
    """
    import numpy as np
    from PIL import Image

    try:
        import cv2
    except Exception:  # noqa: BLE001
        cv2 = None

    # ---------- 1) 决定 SAM prompt ----------
    sprompt = None
    if click:
        sprompt = {"type": "point", "pt": [float(click[0]), float(click[1])]}
    elif vision_box is not None:
        sprompt = {"type": "box", "norm": [float(v) for v in vision_box[:4]]}
    elif box:
        bp = _normalize_box(box, W, H)
        if bp is not None:
            sprompt = {"type": "box", "norm": [bp[0] / W, bp[1] / H, bp[2] / W, bp[3] / H]}
    elif polygon:
        sprompt = {"type": "poly", "poly": [[float(p[0]), float(p[1])] for p in polygon]}

    if sprompt is None:
        # auto：BiRefNet 最大主体 bbox 当 SAM 提示
        try:
            base = predict_mask(rgb, model="birefnet-general")
            pb = _largest_fg_bbox(base, W, H)
            if pb is not None:
                sprompt = {"type": "box", "norm": pb}
        except Exception:  # noqa: BLE001
            sprompt = None

    # ---------- 2) SAM 像素级 mask ----------
    if sprompt is None:
        # 彻底没提示：退化 BiRefNet 整图
        base = predict_mask(rgb, model="birefnet-general")
        rgba = rgb.convert("RGBA")
        rgba.putalpha(_edge_soften_mask(base))
        return rgba

    sam = sam_refine_mask(rgb, sprompt, W, H)  # L 0~255
    m = np.asarray(sam, dtype=np.float32) / 255.0

    # ---------- 3) trimap：确定前景/未知带/确定背景 ----------
    fg = (m > 0.85).astype(np.uint8)
    bg = (m < 0.15).astype(np.uint8)
    if cv2 is not None:
        fge = cv2.erode(fg, np.ones((7, 7), np.uint8), iterations=1)   # k_fg=3
        bge = cv2.dilate(bg, np.ones((9, 9), np.uint8), iterations=1)  # k_bg=4
    else:
        # cv2 缺失（极端情况）退化：直接以 0.85/0.15 阈值当确定区，导向滤波仍能软化
        fge, bge = fg, bg
    trimap = np.zeros((H, W), np.float32)
    trimap[fge > 0] = 1.0
    trimap[bge > 0] = 0.0
    unk = (fge == 0) & (bge == 0)
    trimap[unk] = m[unk]  # 未知带用 SAM 软值作初值

    # ---------- 4) 导向滤波软抠像 ----------
    alpha = _guided_filter_matting(rgb, trimap, radius=10, eps=1e-3)
    # 强制确定像素（导向滤波可能轻微越界）
    if fge is not None:
        alpha[fge > 0] = 1.0
        alpha[bge > 0] = 0.0

    # ---------- 5) 前景去污染（消除背景色渗色）----------
    rgb_arr = np.asarray(rgb, dtype=np.float64) / 255.0
    try:
        F = _decontaminate_modnet_fg(rgb_arr, alpha)
    except Exception:  # noqa: BLE001
        F = rgb_arr
    out = np.zeros((H, W, 4), np.float64)
    out[:, :, :3] = F
    out[:, :, 3] = alpha
    return Image.fromarray((np.clip(out, 0, 1) * 255.0).astype(np.uint8), mode="RGBA")


# ===== 🔬 SAM（MobileSAM-Tiny ONNX，MIT）像素级精修引擎 =====
# 权重来自 huggingface.co/Acly/MobileSAM（MIT，可商用——项目「内置 AI 默认须 MIT/Apache/BSD」
# 铁律满足）。两个 onnx：
#   - mobile_sam_image_encoder.onnx：输入 input_image [H,W,3] float32 **raw 0-255**，
#     图内自带 resize(长边1024等比)+ImageNet归一化+pad1024 前处理；
#     输出 image_embeddings [1,256,64,64]。
#   - sam_mask_decoder_single.onnx：标准 SAM 六输入（embeddings/points/labels/
#     mask_input/has_mask/orig_im_size）→ 单 mask [1,1,1024,1024]（1024 空间）。
# SAM 靠 prompt（box/point）分割，无 prompt 无意义 → 只在用户给了选区/AI定位时启用。
_SAM_FILES = {
    "enc": ("mobilesam_encoder.onnx",
            ["https://huggingface.co/Acly/MobileSAM/resolve/main/mobile_sam_image_encoder.onnx"]),
    "dec": ("mobilesam_decoder_single.onnx",
            ["https://huggingface.co/Acly/MobileSAM/resolve/main/sam_mask_decoder_single.onnx"]),
}
_SAM_SESSIONS: dict = {}
_SAM_SESS_LOCK = threading.Lock()


def _sam_download(kind: str) -> Path:
    """下载 SAM 权重到 ~/.vdl_models（复用断点续传/进度/原子落盘）。"""
    fname, urls = _SAM_FILES[kind]
    dest = _model_dir() / fname
    if dest.exists() and dest.stat().st_size > _MIN_CACHE_BYTES:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err = ""
    for url in urls:
        try:
            _set_dl(active=True, model="mobilesam-" + kind, done=0, total=0, pct=0.0, error="")
            _http_get(url, tmp, "mobilesam-" + kind)
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise RuntimeError("下载结果为空")
            tmp.replace(dest)
            _set_dl(active=False, pct=100.0)
            return dest
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:300]
            tmp.unlink(missing_ok=True)
            _set_dl(active=False, error=last_err)
            continue
    raise RuntimeError(f"MobileSAM 模型下载失败：{last_err}")


def _sam_session(kind: str):
    """懒加载 SAM encoder/decoder onnx session。kind: 'enc' | 'dec'"""
    with _SAM_SESS_LOCK:
        sess = _SAM_SESSIONS.get(kind)
        if sess is not None:
            return sess
        import onnxruntime as ort

        path = _sam_download(kind)
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
        _SAM_SESSIONS[kind] = sess
        return sess


def _sam_mask_cropped(rgb, norm_box, W: int, H: int):
    """SAM 精度升级：把框选/AI 定位的主体区域 crop 出来（带 padding）单独编码到 1024，
    让主体占满编码分辨率 → 边缘像素级更细。返回原图尺寸 PIL L 掩码；不适用时返回 None。

    适用：主体占据画面较小（如 30%）时，整图缩到 1024 主体仅 ~300px 很粗；crop 后
    主体占满 1024 → 发丝/细物边缘明显更精细。框外区域强制为 0（不在 crop 内）。
    """
    import numpy as np
    from PIL import Image, ImageDraw

    x1, y1, x2, y2 = [float(v) for v in norm_box[:4]]
    px0, py0, px1, py1 = x1 * W, y1 * H, x2 * W, y2 * H
    bw, bh = px1 - px0, py1 - py0
    if bw <= 0 or bh <= 0:
        return None
    pad = 0.12
    cx0 = max(0, int(round(px0 - bw * pad)))
    cy0 = max(0, int(round(py0 - bh * pad)))
    cx1 = min(W, int(round(px1 + bw * pad)))
    cy1 = min(H, int(round(py1 + bh * pad)))
    cw, ch = cx1 - cx0, cy1 - cy0
    # crop 太小或几乎等于全图 → 用整图路径更稳
    if cw < W * 0.35 or ch < H * 0.35 or (cw * ch) > (W * H * 0.92):
        return None

    crop = rgb.crop((cx0, cy0, cx1, cy1))
    cwi, chi = crop.size
    scale = 1024.0 / max(cwi, chi)
    nw, nh = max(1, int(round(cwi * scale))), max(1, int(round(chi * scale)))
    simg = crop.resize((nw, nh), Image.LANCZOS)
    arr = np.asarray(simg, dtype=np.float32)
    enc = _sam_session("enc")
    dec = _sam_session("dec")
    in_name = enc.get_inputs()[0].name
    embed = enc.run(None, {in_name: arr})[0]

    # 整张 crop 当前景 → box prompt 覆盖 crop 全部（归一化 [0,0,1,1]）
    pts = [[0.0, 0.0], [1.0, 1.0]]
    labels = [2.0, 3.0]
    ppts = [[v * dim * scale for v, dim in zip(p, (cwi, chi))] for p in pts]
    feed = {
        "image_embeddings": embed,
        "point_coords": np.array(ppts, dtype=np.float32)[None, :, :],
        "point_labels": np.array(labels, dtype=np.float32)[None, :],
        "mask_input": np.zeros((1, 1, 256, 256), dtype=np.float32),
        "has_mask_input": np.array([0.0], dtype=np.float32),
        "orig_im_size": np.array([chi, cwi], dtype=np.int64),
    }
    out = dec.run(None, feed)
    raw = out[0]
    m = np.squeeze(raw, axis=0)
    if m.ndim == 3:
        m = m[0]
    elif m.ndim != 2:
        m = m.reshape(-1)[:1024 * 1024].reshape(1024, 1024)
    if float(m.max()) > 2.0:
        m = 1.0 / (1.0 + np.exp(-m))
    m = np.clip(m, 0.0, 1.0)
    pimg = Image.fromarray((m * 255.0).astype(np.uint8), mode="L")
    pimg = pimg.crop((0, 0, nw, nh)).resize((cwi, chi), Image.LANCZOS)
    # 贴回全图（crop 外强制 0）
    full = Image.new("L", (W, H), 0)
    full.paste(pimg, (cx0, cy0))
    return full


def sam_refine_mask(rgb, prompt: dict, W: int, H: int):
    """🔬 SAM 像素级精修：prompt 给 box/point 语义，返回原图尺寸 L 模式 alpha。

    prompt:
      {"type": "box",  "norm": [x1,y1,x2,y2]}      —— 手动框 / AI视觉定位框
      {"type": "poly", "poly": [[x,y],...]}        —— 套索（取其 bbox 作 prompt，
                                                    输出再乘用户多边形硬边界）
      {"type": "point","pt":  [x,y]}               —— 点图抠图
    任何一步失败抛 RuntimeError（调用方回退 BiRefNet，绝不崩）。
    """
    # 框选/AI 定位 → 优先用 crop 到 1024 的高精度路径（主体占满编码分辨率，边缘更细）
    if prompt.get("type") == "box":
        try:
            cm = _sam_mask_cropped(rgb, prompt["norm"], W, H)
            if cm is not None:
                return cm
        except Exception:  # noqa: BLE001
            pass
    return _sam_mask_whole(rgb, prompt, W, H)


def _sam_mask_whole(rgb, prompt: dict, W: int, H: int):
    """🔬 SAM 整图像素级精修（crop 高精度路径的回退）：prompt 给 box/point 语义，返回原图尺寸 L。"""
    import numpy as np
    from PIL import Image, ImageDraw

    enc = _sam_session("enc")
    dec = _sam_session("dec")

    # ---------- 1) encoder：resize 长边 1024 等比 → raw 0-255 HWC → embed ----------
    w0, h0 = rgb.size
    scale = 1024.0 / max(w0, h0)
    nw, nh = max(1, int(round(w0 * scale))), max(1, int(round(h0 * scale)))
    simg = rgb.resize((nw, nh), Image.LANCZOS)
    arr = np.asarray(simg, dtype=np.float32)  # [nh, nw, 3] 0-255
    in_name = enc.get_inputs()[0].name
    embed = enc.run(None, {in_name: arr})[0]  # [1,256,64,64]

    # ---------- 2) prompt 坐标 → 1024 空间（scale=1024/max(orig)）----------
    def _px(v: float, dim: int) -> float:
        return v * dim * scale  # 归一化 → 原图像素 → 1024 空间

    pts = []
    labels = []
    if prompt.get("type") in ("box", "poly"):
        if prompt.get("type") == "box":
            x1, y1, x2, y2 = prompt["norm"]
        else:
            xs = [p[0] for p in prompt["poly"]]
            ys = [p[1] for p in prompt["poly"]]
            x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        # box → 左上(2) 右下(3)
        pts = [[_px(x1, w0), _px(y1, h0)], [_px(x2, w0), _px(y2, h0)]]
        labels = [2.0, 3.0]
    else:  # point
        px, py = prompt["pt"]
        pts = [[_px(px, w0), _px(py, h0)]]
        labels = [1.0]

    # ---------- 3) decoder ----------
    feeds = {}
    for i in dec.get_inputs():
        feeds[i.name] = i  # 记录（宽松匹配名字）
    in_names = {i.name: i for i in dec.get_inputs()}
    feed = {
        "image_embeddings": embed,
        "point_coords": np.array(pts, dtype=np.float32)[None, :, :],
        "point_labels": np.array(labels, dtype=np.float32)[None, :],
        "mask_input": np.zeros((1, 1, 256, 256), dtype=np.float32),
        "has_mask_input": np.array([0.0], dtype=np.float32),
        "orig_im_size": np.array([h0, w0], dtype=np.int64),
    }
    out = dec.run(None, feed)
    raw = out[0]  # [1,1,1024,1024]（single）或 [1,4,...]（若模型为 multi 兼容）
    m = np.squeeze(raw, axis=0)
    if m.ndim == 3:
        m = m[0]
    elif m.ndim != 2:
        m = m.reshape(-1)[:1024 * 1024].reshape(1024, 1024)
    if float(m.max()) > 2.0:  # logits → sigmoid（与 predict_mask 同法探测）
        m = 1.0 / (1.0 + np.exp(-m))
    m = np.clip(m, 0.0, 1.0)
    # 1024 空间 → 原图（等比缩放；SAM 内部 pad 区在边缘，故用原图坐标裁剪映射）
    pimg = Image.fromarray((m * 255.0).astype(np.uint8), mode="L")
    # 对应到 (nw,nh) 内的区域（pad 在右下）
    pimg = pimg.crop((0, 0, nw, nh)).resize((w0, h0), Image.LANCZOS)
    out_mask = np.asarray(pimg, dtype=np.float32) / 255.0

    # ---------- 4) prompt 硬边界（套索多边形/用户框外强制透明，绝不越界）----------
    # SAM box prompt 语义是"分割框内主体"，但主体紧贴延伸物（如标题下方装饰）会随
    # mask 溢出框外——必须按用户框硬裁，守住「框住什么就是什么」。
    if prompt.get("type") in ("poly", "box"):
        poly_img = Image.new("L", (w0, h0), 0)
        dr = ImageDraw.Draw(poly_img)
        if prompt.get("type") == "poly":
            dr.polygon([(p[0] * w0, p[1] * h0) for p in prompt["poly"]], fill=255)
        else:
            x1, y1, x2, y2 = prompt["norm"]
            dr.rectangle([x1 * w0, y1 * h0, x2 * w0, y2 * h0], fill=255)
        out_mask = out_mask * (np.asarray(poly_img, dtype=np.float32) / 255.0)
    return Image.fromarray(np.clip(out_mask * 255.0, 0, 255).astype(np.uint8), mode="L")



def analyze_blocks(rgb, model: str | None = None, min_area_ratio: float = 0.0006, max_blocks: int = 60):
    """🧲 智能元素块分析：跑全图显著性，把图切分为独立的"元素块"。

    返回块清单（供前端 hover 高亮 + 点击选块）：
      [{"id": int, "bbox": [x1,y1,x2,y2] 归一化, "contour": [[x,y]...] 归一化轮廓点, "area": 像素数}]
    典型：海报 → 标题一块、颜料盘一块、铅笔一块、按钮一块…各自独立成块。
    """
    import numpy as np
    import cv2

    mask_img = predict_mask(rgb, model=model)
    arr = np.array(mask_img)
    H, W = arr.shape
    binm = (arr > 45).astype(np.uint8)  # alpha > ~0.18
    contours, _ = cv2.findContours(binm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = min_area_ratio * H * W
    blocks = []
    for i, c in enumerate(contours):
        area = float(cv2.contourArea(c))
        if area < min_area:
            continue
        # 轮廓简化（Douglas-Peucker），控制前端绘制量
        epsilon = 0.006 * cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, epsilon, True).reshape(-1, 2)
        if len(approx) < 3:
            continue
        norm = [[round(float(x) / W, 4), round(float(y) / H, 4)] for x, y in approx]
        xs = [p[0] for p in norm]
        ys = [p[1] for p in norm]
        blocks.append({
            "id": i,
            "bbox": [min(xs), min(ys), max(xs), max(ys)],
            "contour": norm,
            "area": int(area),
        })
        if len(blocks) >= max_blocks:
            break
    # 按面积降序（大块优先命中）
    blocks.sort(key=lambda b: b["area"], reverse=True)
    return blocks


def split_block(rgb, block, model: str | None = None, k: int = 3):
    """🧲 块细分：把 hover/选中的一块（可能含主标题+小标题等连片内容）
    按颜色聚类进一步拆成若干子块，返回与 analyze_blocks 同构的块清单。

    连片问题：BiRefNet 常把紧贴的主标题+小标题连成一大块；但两者颜色差异大
    （橙底白字 vs 黑底黄字），块内 k-means 颜色聚类能把它们分开。
    """
    import numpy as np
    import cv2

    W, H = rgb.size
    contour = block.get("contour") if isinstance(block, dict) else block
    if not contour or len(contour) < 3:
        return []
    poly = np.array([[float(p[0]) * W, float(p[1]) * H] for p in contour], dtype=np.float32).astype(np.int32)
    msk = np.zeros((H, W), dtype=np.uint8)
    cv2.fillPoly(msk, [poly], 255)
    rgb_arr = np.array(rgb).astype(np.float32) / 255.0
    ys, xs = np.where(msk > 0)
    if len(ys) < 64:
        return []
    pix = rgb_arr[ys, xs]
    kk = max(2, min(k, len(pix) // 50))
    rng = np.random.default_rng(7)
    try:
        centers = pix[rng.choice(len(pix), size=kk, replace=False)].copy()
    except Exception:  # noqa: BLE001
        return []
    for _ in range(10):
        d = np.linalg.norm(pix[:, None, :] - centers[None, :, :], axis=2)
        lab = d.argmin(axis=1)
        for c in range(kk):
            s = pix[lab == c]
            if len(s):
                centers[c] = s.mean(axis=0)

    sub_blocks = []
    for c in range(kk):
        sub = np.zeros((H, W), dtype=np.uint8)
        sel = lab == c
        sub[ys[sel], xs[sel]] = 255
        sub_contours, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for sc in sub_contours:
            if cv2.contourArea(sc) < 60:
                continue
            ep = 0.006 * cv2.arcLength(sc, True)
            ap = cv2.approxPolyDP(sc, ep, True).reshape(-1, 2)
            if len(ap) < 3:
                continue
            norm = [[round(float(x) / W, 4), round(float(y) / H, 4)] for x, y in ap]
            xs2 = [p[0] for p in norm]
            ys2 = [p[1] for p in norm]
            sub_blocks.append({
                "id": f"sub{c}_{len(sub_blocks)}",
                "bbox": [min(xs2), min(ys2), max(xs2), max(ys2)],
                "contour": norm,
                "area": int(cv2.contourArea(sc)),
            })
    sub_blocks.sort(key=lambda b: b["area"], reverse=True)
    return sub_blocks


def _blocks_mask(rgb, blocks, W: int, H: int, model: str | None = None):
    """🧲 智能魔棒多选抠图：把用户 hover+点击选中的多个元素块（并集）一起抠。

    blocks: 每个元素可为归一化轮廓 [[x,y]...] 或 dict {"contour":[...], "tag": ...}
      tag='text' → 📝 VLM 文字块（如装饰风主标题）。BiRefNet 显著性对这类字整图
      几乎无响应（零星 alpha<45 不成块），若按普通块乘显著性会抠空。改为对文字块
      bbox 做**局部裁切推理**（_box_crop_mask，模型在局部把字当显著主体），
      单块实测 alpha>160 像素从全图的 63 提升到 6846——字被真正挖出来。
      tag='auto'/其它 → 普通显著性块（原逻辑：并集 bbox 裁切推理 × union mask）。
    实现：
      1. 各块轮廓 union 二值 mask（=用户选中范围，块间隙透明）
      2. tag=auto 块：并集 bbox(+10% pad) 裁切推理一次，alpha × union
      3. tag=text 块：各自 bbox 局部裁切推理，直接取 alpha（块内即其轮廓范围）
      4. 各路 alpha 取逐像素 max 合成，整体 × union 保证块间隙/块外透明
    """
    import numpy as np
    from PIL import Image, ImageDraw

    if not blocks:
        return None
    items = []  # {"contour": norm, "tag": "auto"|"text"}
    for blk in blocks:
        tag = "auto"
        if isinstance(blk, dict):
            contour = blk.get("contour") or blk.get("polygon") or blk
            tag = str(blk.get("tag") or "auto")
        else:
            contour = blk
        if isinstance(contour, (list, tuple)) and len(contour) >= 3:
            clean = [[float(p[0]), float(p[1])] for p in contour
                     if isinstance(p, (list, tuple)) and len(p) >= 2]
            if len(clean) >= 3:
                items.append({"contour": clean, "tag": tag})
    if not items:
        return None

    norm_all = [it["contour"] for it in items]
    text_items = [it for it in items if it["tag"] == "text"]
    auto_items = [it for it in items if it["tag"] != "text"]

    out = np.zeros((H, W), dtype=np.float32)

    # ---- tag=auto 块：并集 bbox 裁切推理 × union（原逻辑） ----
    if auto_items:
        all_pts = [p for poly in (it["contour"] for it in auto_items) for p in poly]
        xs = [p[0] * W for p in all_pts]
        ys = [p[1] * H for p in all_pts]
        x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
        x1, y1 = min(W, int(max(xs))), min(H, int(max(ys)))
        if x1 - x0 >= 8 and y1 - y0 >= 8:
            bw, bh = x1 - x0, y1 - y0
            pad_w = max(int(bw * 0.10), 4)
            pad_h = max(int(bh * 0.10), 4)
            x0p, y0p = max(0, x0 - pad_w), max(0, y0 - pad_h)
            x1p, y1p = min(W, x1 + pad_w), min(H, y1 + pad_h)
            crop = rgb.crop((x0p, y0p, x1p, y1p))
            crop_mask = predict_mask(crop, model=model)
            cw, ch = crop_mask.size
            sx0 = max(0, x0 - x0p)
            sy0 = max(0, y0 - y0p)
            sx1 = min(cw, x1 - x0p)
            sy1 = min(ch, y1 - y0p)
            if sx1 > sx0 and sy1 > sy0:
                region = np.array(crop_mask, dtype=np.uint8)[sy0:sy1, sx0:sx1]
                m = region.astype(np.float32) / 255.0
                out[y0:y0 + sy1 - sy0, x0:x0 + sx1 - sx0] = np.maximum(
                    out[y0:y0 + sy1 - sy0, x0:x0 + sx1 - sx0], m)

    # ---- tag=text 块：各自 bbox 局部裁切推理（把弱显著文字在局部显出来） ----
    for it in text_items:
        poly = it["contour"]
        xs = [p[0] * W for p in poly]
        ys = [p[1] * H for p in poly]
        x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
        x1, y1 = min(W, int(max(xs))), min(H, int(max(ys)))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        try:
            lm = _box_crop_mask(rgb, (x0, y0, x1, y1), W, H, model=model)
            la = np.array(lm).astype(np.float32) / 255.0
            out = np.maximum(out, la)
        except Exception:  # noqa: BLE001
            continue

    # ---- text 块 mask 阈值清理：仅在 text bbox 内 alpha<0.40 强制 0 ----
    # 阻止"字外被 VLM bbox 包进去的白底/卡片底/装饰"被当前景带出——这些像素 BiRefNet
    # 通常给 0.0~0.3 弱 alpha，没到 0.40 的位置清透，让前景仅保留真正的"字像素"。
    # text bbox 之外不走阈值（可能 VLM 框比字紧一点 + 软边缘越界）。
    if text_items:
        text_bbox_union = np.zeros((H, W), dtype=bool)
        for ti in text_items:
            xs = [p[0] * W for p in ti["contour"]]; ys = [p[1] * H for p in ti["contour"]]
            tx0, ty0 = max(0, int(min(xs))), max(0, int(min(ys)))
            tx1, ty1 = min(W, int(max(xs))), min(H, int(max(ys)))
            if tx1 > tx0 and ty1 > ty0:
                text_bbox_union[ty0:ty1, tx0:tx1] = True
        out = np.where(text_bbox_union & (out < 0.40), 0.0, out)

    # ---- union：全部块二值多边形 → GaussianBlur 软化（5x5 sigma=1.0）——
    # 硬 0/1 矩形乘 mask 会产生"卡片硬边"，软化后 alpha 边缘自然过渡，像素级。
    # 中心区域被 *1.3 重映射确保仍是接近 1（不丢失前景不透明度）。
    union = Image.new("L", (W, H), 0)
    ud = ImageDraw.Draw(union)
    for poly in norm_all:
        ud.polygon([(float(p[0]) * W, float(p[1]) * H) for p in poly], fill=255)
    try:
        import cv2 as _cv2_feather
        u_raw = np.array(union, dtype=np.float32)
        u_soft = _cv2_feather.GaussianBlur(u_raw, (5, 5), 1.0)
        u = np.clip(u_soft / 255.0 * 1.3, 0.0, 1.0)
    except Exception:  # noqa: BLE001
        u = np.array(union).astype(np.float32) / 255.0
    out = out * u
    return Image.fromarray(np.clip(out * 255.0, 0, 255).astype(np.uint8), mode="L")


def _click_mask(rgb, click, W: int, H: int, model: str | None = None):
    """点图抠图（👆 用户点哪抠哪）。

    click: 归一化 [x, y] ∈ [0,1]——用户直接在预览图上单击想要的主体
    （主标题 / 图案 / 按钮 / logo 等，"抠什么"由用户随手决定，不写死）。
    实现：
      1. 全图 BiRefNet 得 alpha（含 predict_mask 的软阈值/连通/羽化后处理）
      2. 点击处 alpha < 0.20 → 点到了背景 → raise 提示（用户可改点别的元素）
      3. 从点击点 4-邻接 BFS，只走 alpha > 0.20 的区域 → 得到"点击处所属元素"
      4. 输出该元素连通块（保留原 alpha 值）
    典型：海报上主标题/颜料盘/铅笔各自独立成显著块，点谁抠谁。
    """
    import numpy as np
    from collections import deque
    from PIL import Image

    cx = int(np.clip(round(float(click[0]) * W), 0, W - 1))
    cy = int(np.clip(round(float(click[1]) * H), 0, H - 1))

    mask_img = predict_mask(rgb, model=model)  # 原图尺寸 L 模式 0-255
    m = np.array(mask_img).astype(np.float32) / 255.0
    if m[cy, cx] < 0.20:
        raise ValueError("点到背景了，请在画面上的元素（标题/图案/按钮）上单击")

    # 两阶段连通：核心（高置信）+ 1 步邻接弱边缘。
    # 避免单纯 ground>0.20 BFS 把"与点击元素相邻的其他显著主体"一起带走
    # （典型：主标题与小标题紧贴时连成一片，传统 BFS 跨过去连带抠出小标题）。
    # 阶段1: alpha > 0.45 的高置信核心——BFS 严格在用户点的元素内；
    #         同时加 **m 梯度跌落判定**：邻居 alpha 比当前低 > 0.25 → 视为元素边界（即使中间没
    #         明显的 alpha gap，跨过主标题边缘到小标题这类急剧变化也会停止）。
    # 阶段2: 核心紧邻 1 步的 alpha > 0.15 区——保留软光晕/描边
    core = np.zeros((H, W), dtype=bool)
    q = deque([(cy, cx)])
    core[cy, cx] = True
    core_ground = m > 0.45
    while q:
        y, x = q.popleft()
        cur_m = m[y, x]
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < H and 0 <= nx < W and not core[ny, nx] and core_ground[ny, nx]:
                # 防跨边界：邻居 alpha 急剧下降 → 视为元素边界（不连）
                if m[ny, nx] < cur_m - 0.25:
                    continue
                core[ny, nx] = True
                q.append((ny, nx))

    # 核心紧邻 1 步的弱边缘（向量化）
    core_dilated = np.zeros_like(core)
    core_dilated[1:-1, 1:-1] = (
        core[1:-1, 1:-1] | core[:-2, 1:-1] | core[2:, 1:-1]
        | core[1:-1, :-2] | core[1:-1, 2:]
    )
    weak_edge = core_dilated & ~core & (m > 0.15)

    kept = core | weak_edge
    out = m.copy()
    out[~kept] = 0.0
    return Image.fromarray(np.clip(out * 255.0, 0, 255).astype(np.uint8), mode="L")


def _polygon_vision_intersect(rgb, polygon, vision_box, W: int, H: int, model: str | None = None):
    """套索 + AI 视觉定位 交集抠图。

    背景：单独用套索时，用户必须画得足够贴合（圈太大则抠出圈内所有内容）；
    单独用 AI 视觉定位时，VLM 可能把范围框大/框偏。两者**同时勾选**时
    取交集——AI 按 VLM box 整图抠出主体（主标题），再乘套索 polygon
    （用户圈的范围边界）。粗圈整张海报也能用：AI 在圈内锁定主标题抠，
    圈外的副标题/装饰/卡片全透明。

    失败（VLM 异常等）返回 None → 调用方回退纯套索 _polygon_mask。
    """
    try:
        import numpy as np
        from PIL import Image, ImageDraw

        # 局部裁切推理（修复整图 BiRefNet 对装饰风字盲区）：VLM 框外扩 12% 涵盖
        # 光晕/阴影，再 _box_crop_mask（框外强制透明）。之后与套索 polygon 取交集。
        _vx0, _vy0, _vx1, _vy1 = [float(v) for v in vision_box[:4]]
        _vbw, _vbh = _vx1 - _vx0, _vy1 - _vy0
        _ex0, _ey0 = max(0.0, _vx0 - _vbw * 0.12), max(0.0, _vy0 - _vbh * 0.12)
        _ex1, _ey1 = min(1.0, _vx1 + _vbw * 0.12), min(1.0, _vy1 + _vbh * 0.12)
        _vbpx = (int(round(_ex0 * W)), int(round(_ey0 * H)), int(round(_ex1 * W)), int(round(_ey1 * H)))
        guided = _box_crop_mask(rgb, _vbpx, W, H, model=model)  # 原图尺寸 L 模式
        g = np.array(guided).astype(np.float32) / 255.0
        pts = [(float(p[0]) * W, float(p[1]) * H) for p in polygon if len(p) >= 2]
        if len(pts) < 3:
            return None
        # 套索二值图（用户圈的范围 = 硬边界）
        poly_img = Image.new("L", (W, H), 0)
        ImageDraw.Draw(poly_img).polygon(pts, fill=255)
        p = np.array(poly_img).astype(np.float32) / 255.0
        m = g * p  # AI 主体 ∩ 用户圈选
        # 与套索同款后处理：中心连通 + 主色聚类（防 AI box 边缘杂质）
        m = _polygon_center_keep(m)
        rgb_arr = np.array(rgb).astype(np.float32) / 255.0
        m, _ = _polygon_dominant_filter(rgb_arr, m, pts, W, H)
        return Image.fromarray(np.clip(m * 255.0, 0, 255).astype(np.uint8), mode="L")
    except Exception:  # noqa: BLE001
        return None


def _polygon_mask(rgb, polygon, W: int, H: int, model: str | None = None):
    """套索抠图：用户自由圈出的多边形区域，套索内保留、套索外强制透明。

    polygon: 归一化点列表 [[x, y], ...]（≥3 点），取值 0~1。

    为什么套索比矩形框精准得多：
        矩形框必然把"框内但不属于主体"的部分（紧贴的副标题、装饰、笔触）一起框进来，
        模型只会把这些也当前景保留。套索能贴合主体的真实轮廓——比如沿着主标题的
        橙色描边画一圈，把下方紧贴的黑色笔触副标题留在外面，抠出来就是干净的标题。

    实现：
        1. 归一化点 → 像素点；算出多边形 bbox
        2. 按 bbox + 10% 外扩裁切送进模型（保留光晕/阴影等软元素的上下文）
        3. 构造多边形 mask（PIL ImageDraw.polygon 填充）
        4. 推理 mask × 多边形 mask → 套索外一律 0
    """
    import numpy as _np
    from PIL import Image, ImageDraw

    if not polygon or len(polygon) < 3:
        return None  # 无效套索，调用方回退

    try:
        pts = [(float(p[0]) * W, float(p[1]) * H) for p in polygon if len(p) >= 2]
    except Exception:  # noqa: BLE001
        return None
    if len(pts) < 3:
        return None

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, y0 = max(0, int(min(xs))), max(0, int(min(ys)))
    x1, y1 = min(W, int(max(xs))), min(H, int(max(ys)))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None  # 太小，模型也糊

    # 推理上下文外扩 10%（同矩形框选，保留光晕/描边/阴影软元素）
    bw, bh = x1 - x0, y1 - y0
    pad_w = max(int(bw * 0.10), 4)
    pad_h = max(int(bh * 0.10), 4)
    x0p, y0p = max(0, x0 - pad_w), max(0, y0 - pad_h)
    x1p, y1p = min(W, x1 + pad_w), min(H, y1 + pad_h)

    crop = rgb.crop((x0p, y0p, x1p, y1p))
    crop_mask = predict_mask(crop, model=model)

    # 把裁切推理结果贴回原图坐标（只取多边形 bbox 范围）
    out_arr = _np.zeros((H, W), dtype=_np.uint8)
    crop_w_p, crop_h_p = crop_mask.size
    sx0 = max(0, x0 - x0p)
    sy0 = max(0, y0 - y0p)
    sx1 = min(crop_w_p, x1 - x0p)
    sy1 = min(crop_h_p, y1 - y0p)
    if sx1 > sx0 and sy1 > sy0:
        region = _np.array(crop_mask, dtype=_np.uint8)[sy0:sy1, sx0:sx1]
        paste_w, paste_h = sx1 - sx0, sy1 - sy0
        out_arr[y0:y0 + paste_h, x0:x0 + paste_w] = region
    mask = Image.fromarray(out_arr, mode="L")

    # 构造多边形 mask，与推理 mask 相乘 → 套索外强制透明
    poly_img = Image.new("L", (W, H), 0)
    ImageDraw.Draw(poly_img).polygon(pts, fill=255)
    m = _np.array(mask).astype(_np.float32) / 255.0
    p = _np.array(poly_img).astype(_np.float32) / 255.0
    m = m * p
    # 弱 alpha 杂点清理：仅清理"圈进 poly 但与主体 BFS 不连通"的弱 alpha 杂点。
    # 主体（高 alpha / BFS 连通区域内的低 alpha 如字/描边/阴影）一律保留。
    # 此前硬性 alpha<0.40 全清会把装饰风字体（橙描边+白字普遍 alpha<0.40）误杀。
    core_alpha = _polygon_center_keep(m)
    m = _np.where((m < 0.40) & (core_alpha < 0.05), 0.0, m)
    # 中心连通性过滤：从 mask 最高点（=主标题最显眼字符的核心）4-邻接 BFS，
    # 只走 alpha > 0.20 的像素。主标题与下方副标题/远端装饰之间是白色背景，
    # mask 几乎为 0，BFS 自然跨不过去，整条副标题条会被一次性剔掉。
    # （这一刀是 dominant filter 之前的"硬骨架"——dominant 处理"近处小杂簇"，
    #  center-keep 处理"远处异色连体区"。）
    m = _polygon_center_keep(m)
    # 套索圈内主色聚类提纯：BiRefNet 按显著性抠图会把圈内「紧贴主体的黑色笔触/
    # 深色装饰」当同一显著性物体保留。聚类按颜色把前景分组，剔除与主体主色
    # 差异大且整体偏暗的杂质簇（不会误伤橙底白字这类多色主体的浅色部分）。
    rgb_arr = _np.array(rgb).astype(_np.float32) / 255.0
    m, drop_extra = _polygon_dominant_filter(rgb_arr, m, pts, W, H)
    # 轻度收尾（小幅修整，不过度）：
    # 1) 开运算（5x5）去掉细小挂絮/低 alpha 孤点——剔黑条后边界上残留的
    #    碎屑与撕裂细丝；主体笔画宽度远大于 5px，不受影响。
    # 2) 多边形边界 6px 线性渐变羽化——套索边界硬切（如切过星星/装饰一半）
    #    变成柔和过渡，视觉上不再生硬。
    import cv2 as _cv2_feather
    _opened = _cv2_feather.morphologyEx(
        (m > 0.08).astype(_np.uint8), _cv2_feather.MORPH_OPEN,
        _np.ones((5, 5), _np.uint8),
    )
    m = m * _opened
    # 3) 橙色立体阴影补全：标题的半透明深橙阴影层模型响应弱（alpha 斑驳
    #    0.2~0.4、部分成块缺失），视觉上"阴影不完整"。仅对满足以下全部条件
    #    的像素补实到 0.65：贴纸主体 ~15px 邻域内 + 颜色属橙棕阴影家族
    #    （奶油背景/白字/黑条/黄蛋均不满足该色域）+ 不在黑条剔除域 + 圈内。
    #    参数已小幅收紧（半径 41->31、亮度上限 0.82->0.72、绿色通道上限
    #    0.70->0.62），避免浅色奶油/描边白边被误补成阴影外延。
    _main_bin = (m > 0.45).astype(_np.uint8)
    _near = _cv2_feather.dilate(_main_bin, _np.ones((31, 31), _np.uint8)) > 0
    _r, _g, _b = rgb_arr[..., 0], rgb_arr[..., 1], rgb_arr[..., 2]
    _lum = 0.299 * _r + 0.587 * _g + 0.114 * _b
    _fam = (
        (_r > 0.60) & ((_r - _b) > 0.22) & (_g > 0.30) & (_g < 0.62)
        & (rgb_arr.max(axis=2) < 0.72) & (_lum < 0.72)
    )
    _poly_in = _np.array(poly_img) > 127
    _boost = _near & _fam & (m < 0.55) & (~drop_extra) & _poly_in
    m[_boost] = _np.maximum(m[_boost], 0.65)
    _dist = _cv2_feather.distanceTransform(
        (_np.array(poly_img) > 127).astype(_np.uint8), _cv2_feather.DIST_L2, 3
    )
    m = m * _np.clip(_dist / 6.0, 0.0, 1.0)
    return Image.fromarray(_np.clip(m * 255.0, 0, 255).astype(_np.uint8), mode="L")


def _polygon_center_keep(mask_arr, ground_thresh: float = 0.20):
    """套索圈内同行连通性过滤：保留最大前景块 + 与其同一水平带的块，剔除其余。

    为什么需要它：
        dominant filter 按颜色聚类，但「与主体颜色相近但空间上离主体很远」
        的整片区域（如下方整条副标题条"转发集赞｜好友同行｜报名砸金蛋"）
        会被 kmeans 归入主色簇保留下来——用户根本不想要它。
        用水平带做硬切：只保留与最大前景块垂直方向重叠的连通块（同一行文字），
        下方副标题条与主标题之间是白色背景且不在同一行，被一次性剔除。

    为什么不能用「从 mask 最高点 BFS 只留单一连通块」（旧实现）：
        整行标题 = 6 个互不相连的字符块，字符间隙的背景 mask≈0，BFS 跨不过去；
        且 argmax（行优先第一个 1.0 像素）常落在标题上方的细碎装饰（彩带/星点）
        上——结果只留一颗碎屑、整行标题全被杀（用户截图：抠图结果全空）。

    保留策略：
        1. 对 mask > ground_thresh 做 4-邻接连通域标记（cv2，毫秒级）；
        2. 最大面积块 = 主体核心，必留；
        3. 其余块：与任一已保留块的垂直重叠 ≥ 较矮者高度的 35% → 同一行，保留
           （传递扩散，整行字符全保）；无重叠 → 不同行（副标题条/远处装饰），剔除；
        4. 面积 < 64px 的孤立小碎块不参与保留判定（直接随非保留块剔除，
           但整体前景过小时回退原 mask，避免误杀）。

    参数：
        mask_arr: 已乘 polygon 后的 float [0,1] 数组
        ground_thresh: 前景判定最低 mask 值（默认 0.20）

    返回：仅保留主体同行块的 mask；其余置 0。
    """
    import numpy as np
    import cv2

    H, W = mask_arr.shape
    if mask_arr.max() < 0.30:
        return mask_arr  # mask 太低，说明主体没检出，不做切除

    ground = (mask_arr > ground_thresh).astype(np.uint8)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(ground, connectivity=4)
    if num <= 2:  # 只有背景（+至多一块前景），无需过滤
        return mask_arr

    areas = stats[1:, cv2.CC_STAT_AREA]
    if int(areas.max()) < 64:
        return mask_arr  # 前景过小，不判切除

    main = 1 + int(np.argmax(areas))
    keep = np.zeros(num, dtype=bool)
    keep[main] = True

    tops = stats[:, cv2.CC_STAT_TOP].astype(np.int64)
    hts = stats[:, cv2.CC_STAT_HEIGHT].astype(np.int64)
    changed = True
    while changed:
        changed = False
        for c in range(1, num):
            if keep[c] or stats[c, cv2.CC_STAT_AREA] < 64:
                continue
            # 与任一已保留块垂直重叠 ≥ 较矮者 35% → 同一水平行
            for k in range(1, num):
                if not keep[k]:
                    continue
                ov = min(tops[c] + hts[c], tops[k] + hts[k]) - max(tops[c], tops[k])
                if ov >= 0.35 * min(hts[c], hts[k]):
                    keep[c] = True
                    changed = True
                    break

    out = mask_arr.copy()
    out[~keep[labels]] = 0.0
    return out


def _polygon_dominant_filter(rgb_arr, mask_arr, polygon_pts, W: int, H: int):
    """套索圈内主色聚类提纯：把圈内模型前景按颜色聚成多簇，
    剔除「与主体主色差异大且整体偏暗」的杂质簇（黑色笔触/墨渍/深色装饰）。

    为什么需要它：BiRefNet 按显著性抠图，会把圈内「紧贴主体的黑色笔触/副标题」
    与主体当同一个显著性物体整体保留（显著性无法区分"相连的不同颜色区域"）。
    但这类杂质有个稳定特征：**颜色离主体主色远，且自身亮度极低（黑色/深棕）**。
    聚类后整簇判定，比像素级判定稳定得多，且不会误伤主体内的浅色多色
    （如"橙底+白字"：白字亮度高，不会被当暗杂质剔除）。

    实现：
        1. 取 polygon 内 mask>0.3 的像素，k-means(k=4) 按 RGB 聚类；
        2. 面积最大簇 = 主体主色；
        3. 某簇「距最大簇色距 > 0.45 且簇内平均亮度 < 0.30」→ 整簇判为暗杂质，
           对应像素 alpha 强制 0；
        4. 其余保留（含白字等浅色簇）。

    安全：聚类/计算失败时回退原 mask。
    """
    import numpy as np
    from PIL import Image, ImageDraw

    # polygon 内有效像素
    poly_img = Image.new("L", (W, H), 0)
    ImageDraw.Draw(poly_img).polygon(polygon_pts, fill=255)
    poly_arr = np.array(poly_img).astype(bool)

    reliable = (mask_arr > 0.30) & poly_arr
    n_fg = int(reliable.sum())
    if n_fg < 64:
        return mask_arr, np.zeros_like(mask_arr)  # 前景太少，跳过

    pix = rgb_arr[reliable]
    k = 5 if n_fg >= 4000 else 4  # k=5 让浅色装饰能自成簇与白字区分

    # k-means（纯 numpy 向量化）
    try:
        rng = np.random.default_rng(1)
        centers = pix[rng.choice(n_fg, size=k, replace=False)].copy()
        for _ in range(12):
            d = np.linalg.norm(pix[:, None, :] - centers[None, :, :], axis=2)
            lab = d.argmin(axis=1)
            for c in range(k):
                sel_c = pix[lab == c]
                if len(sel_c):
                    centers[c] = sel_c.mean(axis=0)
    except Exception:  # noqa: BLE001
        return mask_arr, np.zeros_like(mask_arr)

    sizes = np.bincount(lab, minlength=k)
    big = int(np.argmax(sizes))
    main_color = centers[big]

    # 构造每簇二值图 + 主簇图，用于"连通性豁免"判定
    # （主簇 = 主标题主体；与主簇 4-邻接连通的异色簇视为"主体的一部分"
    #  ——典型：主标题内的黑色飞白/描边/阴影，应保留）
    ys, xs = np.where(reliable)
    cluster_masks = np.zeros((k, H, W), dtype=bool)
    for c in range(k):
        cluster_masks[c] = np.zeros((H, W), dtype=bool)
        cluster_masks[c][ys, xs] = (lab == c)
    main_mask = cluster_masks[big]

    # 判定各簇是否需要剔除：
    #   条件 1（暗异色大杂质）：色距 > 0.40 且簇亮度 < 0.30 — 黑色笔触/墨渍
    #   条件 2（异色小杂簇）：色距 > 0.40 且亮度 < 0.85 且面积 < 主簇 18%
    #      ——主标题旁的浅色小三角/斜线笔触/零星点等装饰碎块
    #   保护（不被误剔）：
    #     - **与主簇 4-邻接连通**的异色簇豁免（视为"主体的一部分"——飞白/描边）
    #     - 高亮异色簇（lum >= 0.85，如白字/亮黄装饰）即使面积小也保留
    #     - 异色但面积较大的簇（>= 主簇 18%）保留——可能是主标题的多色描边
    drop = np.zeros(k, dtype=bool)
    drop_extra = np.zeros((H, W), dtype=bool)  # 暗簇大块 bbox 域像素级剔除
    dark_rects = []  # 暗簇大块的 bbox（用于孤岛清理）
    for c in range(k):
        if c == big:
            continue
        dist = float(np.linalg.norm(centers[c] - main_color))
        lum = float(centers[c].max())
        mc = cluster_masks[c]
        # 暗异色簇（黑色笔触/墨渍/贴条）：**不再因邻接豁免**——紧贴主体的大块
        # 暗色元素（如标题正下方的黑色副标题条，与主体经光晕连体）也要剔除。
        # 按连通子块判定：只剔面积 ≥ max(2000, 主簇 1.5%) 的大块；
        # 主体内的小块黑飞白/描边/阴影（面积小）保留。
        # 大块整体 bbox 矩形域内的**非主簇**像素一并剔除（黑条上贴的白字/黄字
        # 与暗色像素反锯齿相连、不构成封闭孔洞，只能按 bbox 域连字一起剔），
        # 主簇像素保护豁免。
        if dist > 0.40 and lum < 0.30:
            import cv2
            n2, lab2, st2, _ = cv2.connectedComponentsWithStats(
                mc.astype(np.uint8), connectivity=4
            )
            min_area = max(2000, int(sizes[big] * 0.015))
            big_rect = None
            for c2 in range(1, n2):
                if st2[c2, cv2.CC_STAT_AREA] < min_area:
                    continue
                rx, ry = st2[c2, cv2.CC_STAT_LEFT], st2[c2, cv2.CC_STAT_TOP]
                rw, rh = st2[c2, cv2.CC_STAT_WIDTH], st2[c2, cv2.CC_STAT_HEIGHT]
                rect = np.zeros((H, W), dtype=bool)
                rect[ry:ry + rh, rx:rx + rw] = True
                drop_extra |= rect & (~main_mask)
                dark_rects.append((rx, ry, rw, rh))
                if big_rect is None:
                    big_rect = (rx, ry, rw, rh)
            # 黑条贴纸的配套物：散落在主暗块周围的小黑装饰短线/撕裂碎块
            # （面积 < min_area 逃过上面的剔除，视觉上"底部还带黑色"）。
            # 与最大暗块 bbox 外扩域相交的暗簇小块（面积 ≥60px）一并剔除；
            # 标题内部的黑飞白/阴影远离该域，不受影响。
            if big_rect is not None:
                bx, by, bw2, bh2 = big_rect
                mx = max(int(bw2 * 0.03), 30)
                my = max(int(bh2 * 0.25), 40)
                ex0, ey0 = max(0, bx - mx), max(0, by - my)
                ex1, ey1 = min(W, bx + bw2 + mx), min(H, by + bh2 + my)
                for c2 in range(1, n2):
                    a2 = int(st2[c2, cv2.CC_STAT_AREA])
                    if a2 < 60 or a2 >= min_area:
                        continue
                    x2_, y2_ = st2[c2, cv2.CC_STAT_LEFT], st2[c2, cv2.CC_STAT_TOP]
                    w2_, h2_ = st2[c2, cv2.CC_STAT_WIDTH], st2[c2, cv2.CC_STAT_HEIGHT]
                    if x2_ < ex1 and x2_ + w2_ > ex0 and y2_ < ey1 and y2_ + h2_ > ey0:
                        drop_extra |= lab2 == c2
            continue
        # 连通性豁免：簇 c 是否与主簇 4-邻接连通？
        # 4-邻接检查：c 像素的上/下/左/右 4 个邻居中是否有 main_mask
        adj_to_main = (
            (mc[1:, :] & main_mask[:-1, :]).any() or    # c 上邻接 main
            (mc[:-1, :] & main_mask[1:, :]).any() or   # c 下邻接 main
            (mc[:, 1:] & main_mask[:, :-1]).any() or   # c 左邻接 main
            (mc[:, :-1] & main_mask[:, 1:]).any()      # c 右邻接 main
        )
        if adj_to_main:
            # 与主体贴着的浅色/彩色异色簇 = 飞白/描边/阴影，应保留
            continue
        # 异色小杂簇（浅色装饰）：色距 + 不够亮 + 面积小
        if dist > 0.40 and lum < 0.85 and sizes[c] < sizes[big] * 0.18:
            drop[c] = True

    if not drop.any() and not dark_rects:
        return mask_arr, drop_extra

    # 被判杂质的簇像素在原图位置 alpha 置 0，其余保持原样
    out = mask_arr.copy()
    ys, xs = np.where(reliable)
    drop_px = drop[lab]
    out[ys[drop_px], xs[drop_px]] = 0.0
    out[drop_extra] = 0.0

    # 暗块 bbox 内的残留孤岛清理：bbox 域剔除后，黑条上的白字/黄字若与主簇
    # 同色系（被 main_mask 保护）会成为孤立小块——凡不触及暗块顶行（即不与
    # 上方主体连通）的残留组件一律剔净。
    if dark_rects:
        import cv2
        for (rx, ry, rw, rh) in dark_rects:
            sub = (out[ry:ry + rh, rx:rx + rw] > 0.2).astype(np.uint8)
            n3, lab3, _, _ = cv2.connectedComponentsWithStats(sub, connectivity=4)
            for c3 in range(1, n3):
                if (lab3[0, :] == c3).any():
                    continue  # 触及顶行 = 与上方主体连通，保留
                out[ry:ry + rh, rx:rx + rw][lab3 == c3] = 0.0
    return out, drop_extra


def _box_crop_mask(rgb, box_px, W: int, H: int, model: str | None = None):
    """手动框选抠图：只把框内（+外扩上下文）送进模型，输出严格按原框，框外全透明。

    box_px: 像素坐标 (x0, y0, x1, y1)（已由 _normalize_box 归一化 + 夹紧）。
    保持「框住什么就是什么」语义——输出 alpha 只在用户原框内可能非零。
    """
    import numpy as _np
    from PIL import Image

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
    crop_mask = predict_mask(crop, model=model)
    # 输出 mask 严格按用户原框；外扩区是被模型"看到"但被舍弃的上下文。
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
    return Image.fromarray(out_arr, mode="L")


def vision_guided_mask(img, vision_box, model: str | None = None, expand: float = 0.12):
    """用 VLM 给出的主体 box 引导 BiRefNet 抠图，专治"主字 vs 紧贴副标题/装饰"难题。

    vision_box: 归一化 [x1, y1, x2, y2] ∈ [0,1]（VLM 定位的主体边界框）。
    返回原图尺寸 L 模式 alpha 蒙版，box 外强制透明。

    为什么需要 VLM 引导：
        单靠 BiRefNet 显著性图 + 阈值 + BFS 连通性，永远分不清「同色系包围」下
        的主字与副字——它们显著性置信度几乎一致。但 VLM 能"看懂"图，明确知道
        主标题是哪些字、副标题是哪些字，给出准确的语义边界框。
        本函数：
          1. 整图跑 BiRefNet 得基础 mask（保留光晕/阴影等软元素的细节）
          2. 把 VLM box 适度外扩（默认 12%，涵盖光晕/阴影外延，避免硬切框边）
          3. box 外强制透明（guide 乘法）——这是"副标题/装饰被精准剔除"的关键
          4. box 内再做 BFS 连通核心，避免 box 内夹带的孤立小碎块残留
    """
    import numpy as np
    from PIL import Image

    base = predict_mask(img, model=model)  # 原图尺寸 L 模式
    W, H = base.size
    x1, y1, x2, y2 = vision_box
    # 适度外扩，涵盖主体外侧的光晕/阴影/描边
    bw, bh = x2 - x1, y2 - y1
    x1 = max(0.0, x1 - bw * expand)
    x2 = min(1.0, x2 + bw * expand)
    y1 = max(0.0, y1 - bh * expand)
    y2 = min(1.0, y2 + bh * expand)
    base_arr = np.array(base).astype(np.float32) / 255.0
    # 构造引导图：box 内为 1，外为 0
    guide = np.zeros_like(base_arr)
    gx0, gy0 = int(round(x1 * W)), int(round(y1 * H))
    gx1, gy1 = int(round(x2 * W)), int(round(y2 * H))
    if gx1 > gx0 and gy1 > gy0:
        guide[gy0:gy1, gx0:gx1] = 1.0
    guided = base_arr * guide  # box 外强制透明
    # box 内再做 BFS 连通核心：剔除 box 内"与主体不连通"的孤立小碎块
    guided = _keep_connected_to_core(guided, core_thresh=0.3, ground_thresh=0.05)
    out_arr = np.clip(guided * 255.0, 0, 255).astype("uint8")
    return Image.fromarray(out_arr, mode="L")


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
