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

# ---------------------------------------------------------------- 模型注册表

# size_mb 用于前端「首次下载约 xxx MB」提示；input_size 是 ONNX 输入分辨率。
MODELS: dict[str, dict] = {
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
}

DEFAULT_MODEL = "birefnet-general"

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
        p = _local_path(name)
        out.append(
            {
                "name": name,
                "desc": meta["desc"],
                "size_mb": meta["size_mb"],
                "downloaded": _valid_cached(name),
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

    # 轻 GaussianBlur：mask 边缘羽化（让光晕边缘更自然）
    mask_arr = np.clip(mask * 255.0, 0, 255).astype("uint8")
    mask_img = Image.fromarray(mask_arr, mode="L")
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=0.8))

    if mask_img.size != img.size:
        mask_img = mask_img.resize(img.size, _lanczos())

    return mask_img


def matting_image(src: str | Path, out: str | Path, box: tuple | list | None = None, model: str | None = None, vision_box: tuple | list | None = None) -> None:
    """对单张图片做一键抠图，输出 RGBA 透明 PNG 到 out。

    src/out 为路径（str 或 Path）。透明 PNG 可直接用于合成 / 换背景。

    box（可选）：用户手动框选的主体区域，归一化 (x, y, w, h)，取值 0~1。
      给定时**只把框内区域送进模型推理**（而不是全图推理后再裁），
      这样模型会把框内内容当成整张图来找显著主体——
      照片里有多个人/物时，框谁就抠谁，比全图推理精确得多。
      框外一律透明。
    vision_box（可选）：VLM 视觉定位给出的主体边界框，归一化 [x1,y1,x2,y2] ∈ [0,1]。
      给定时用「AI 视觉定位」引导抠图（见 vision_guided_mask）——VLM 看懂图、
      明确框出主体（如主标题），框外（副标题/装饰/背景）强制透明，专治"同色系
      包围下主字 vs 副字分不清"的难题。优先级高于手动 box：两者都给时以 VLM 为准。
    """
    from PIL import Image

    if not available():
        raise RuntimeError("一键抠图不可用（缺少 onnxruntime / numpy / Pillow 依赖）")

    with Image.open(src) as im:
        im.load()
        rgb = im.convert("RGB")
        W, H = rgb.size

        if vision_box and box is not None:
            # **手动框选 + AI 视觉定位 同时给出**：手动框是用户的明确意图，必须作为
            # 硬边界（「框住什么就是什么」），VLM 只在用户框**内部**再做语义精修。
            # 两者取交集：既尊重用户框选，又用 VLM 剔除框内夹带的副标题/装饰。
            # 若 VLM box 与用户框无重叠（VLM 理解偏差），退化为用户框，绝不越界。
            box_px = _normalize_box(box, W, H)
            if box_px is None:
                # 用户框无效（过小/越界）→ 只用 VLM
                mask = vision_guided_mask(rgb, vision_box, model=model)
            else:
                ux1, uy1 = box_px[0] / W, box_px[1] / H
                ux2, uy2 = box_px[2] / W, box_px[3] / H
                vx1, vy1, vx2, vy2 = [float(v) for v in vision_box[:4]]
                ix1, iy1 = max(ux1, vx1), max(uy1, vy1)
                ix2, iy2 = min(ux2, vx2), min(uy2, vy2)
                if ix2 - ix1 > 1e-4 and iy2 - iy1 > 1e-4:
                    mask = vision_guided_mask(rgb, [ix1, iy1, ix2, iy2], model=model)
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
            # VLM 视觉定位引导：VLM 已看懂图、框出主体，框外强制透明
            mask = vision_guided_mask(rgb, vision_box, model=model)
        elif box is None:
            # 未框选：整图推理
            mask = predict_mask(rgb, model=model)
        else:
            box_px = _normalize_box(box, W, H)
            if box_px is None:
                # 框选无效（过小/越界）：退回整图推理
                mask = predict_mask(rgb, model=model)
            else:
                mask = _box_crop_mask(rgb, box_px, W, H, model=model)

        rgba = rgb.convert("RGBA")
        rgba.putalpha(mask)

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(out_path, "PNG")


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
