"""云端通用抠图（火山 AI MediaKit · 智能抠图 / 豆包级软 alpha）。

这是「随便给一张图都能完美抠出主体」的高质量路径：
- 字节专门的图像 matting 服务，对任意主体（人/物/商品）返回 **软透明边缘** PNG，
  质量对标豆包，远优于 cv 服务的 GeneralSegment（后者是硬分割，仅 1px 抗锯齿）。
- 鉴权：独立的 **Bearer API Key**（火山控制台 → AI MediaKit → API Key 管理），
  与 cv 服务的 SigV4 AK/SK 不是同一个。
- 流程：① 申请上传地址拿 mediakit:// file_id → ② PUT 上传本地图 →
         ③ remove-image-background（scene=general/human/product）→ ④ 下载透明 PNG。
- 全部用标准库（urllib/ssl/base64/json）实现，不引入第三方 SDK。

本地无网/无 Key 时，matting_ai 会自动回退到 cv GeneralSegment 或本地 BiRefNet/MODNet。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import ssl
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_MEDIAKIT_HOST = "mediakit.cn-beijing.volces.com"
_REQ_UPLOAD_URL = f"https://{_MEDIAKIT_HOST}/api/v1/tools-sync/request-media-upload-url"
_REMOVE_URL = f"https://{_MEDIAKIT_HOST}/api/v1/tools-sync/remove-image-background"
_ENHANCE_URL = f"https://{_MEDIAKIT_HOST}/api/v1/tools-sync/enhance-image"

# 不校验证书（桌面端内置证书可能与系统不完全一致，避免无谓的 SSL 失败）
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _read_cfg() -> dict:
    from cloud_matting_config import get_cloud_matting_config

    return get_cloud_matting_config()


def is_mediakit_ready() -> bool:
    """是否已配置 MediaKit Bearer Key。"""
    key = (_read_cfg().get("mediakit_api_key") or "").strip()
    return bool(key)


def _post_json(url: str, api_key: str, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        body = r.read().decode("utf-8")
    return json.loads(body)


def _request_upload(api_key: str, timeout: int) -> dict:
    """返回 {file_id, method, upload_url, upload_headers}。"""
    resp = _post_json(_REQ_UPLOAD_URL, api_key, {}, timeout)
    if not resp.get("success"):
        raise RuntimeError(f"MediaKit 申请上传地址失败: {resp.get('error') or resp}")
    return resp["result"]


def _put_upload(upload_url: str, img_bytes: bytes, content_type: str, timeout: int) -> None:
    req = urllib.request.Request(upload_url, data=img_bytes, method="PUT")
    req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        if r.status >= 300:
            raise RuntimeError(f"MediaKit 上传返回 HTTP {r.status}")


def _download(url: str, timeout: int) -> Image.Image:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        data = r.read()
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _refine_alpha(a: np.ndarray) -> np.ndarray:
    """清理 alpha 通道噪声、平滑过渡带，再做受控的边缘锐化。

    云端返回的 alpha 在背景处可能带极低值噪点，在高 alpha 主体内部
    可能有细小孔洞。本函数：
    - α < 0.03 的像素强制置 0（彻底清背景）；
    - α > 0.97 的像素强制置 1（填实主体）；
    - 对 0.03~0.97 过渡区做轻微高斯平滑，使发丝/边缘更自然；
    - 再用 Unsharp Mask 在过渡带轻微锐化（σ=1.5, amount=0.40），让单根发丝更分明。
    整个操作很轻，避免吞掉真正的半透明发丝。
    """
    import cv2

    a = a.copy()
    a[a < 0.03] = 0.0
    a[a > 0.97] = 1.0

    edge = (a > 0.03) & (a < 0.97)
    if np.any(edge):
        a_pil = Image.fromarray((a * 255.0).astype(np.uint8), mode="L")
        # 平滑：让发丝过渡更自然（仅 20% 混合，保留模型细节）
        a_blur = np.array(a_pil.filter(ImageFilter.GaussianBlur(radius=0.5))).astype(np.float32) / 255.0
        a[edge] = 0.80 * a[edge] + 0.20 * a_blur[edge]

        # 锐化（Unsharp Mask）：让过渡带的发丝边缘更分明
        # σ=1.5 增强细发丝且不放大过多噪声；amount=0.40 保守
        a_blur2 = cv2.GaussianBlur(a, (0, 0), 1.5)
        a_sharp = np.clip(a + 0.40 * (a - a_blur2), 0.0, 1.0)
        a[edge] = a_sharp[edge]

    return np.clip(a, 0.0, 1.0)


def _decontaminate_edge_spill(rgb: Image.Image, rgba: Image.Image,
                              var_threshold: float = 0.015) -> Image.Image:
    """对纯色背景场景的云端结果做边缘去溢色。

    MediaKit 等云端抠图返回的 RGB 仍是原图颜色（I = αF + (1-α)B）。
    半透明边缘像素会残留背景色（如橙幕棚拍时头发边缘发橙）。
    本函数从原图边缘 + alpha≈0 区域估计背景色 B，若判定为纯色背景
    则对半透明像素反推真实前景色 F = (I - (1-α)B) / α。
    复杂背景（方差大）时跳过，避免误伤。
    """
    src = np.array(rgb.convert("RGB")).astype(np.float32) / 255.0
    out = np.array(rgba.convert("RGBA")).astype(np.float32) / 255.0
    rgb_arr, a = out[..., :3], out[..., 3]
    h, w = src.shape[:2]

    # 1) 背景样本：MediaKit 已判为背景（alpha<0.03）且位于原图边缘 18% 区域
    border_mask = np.zeros((h, w), dtype=bool)
    bh = max(1, int(h * 0.18))
    bw = max(1, int(w * 0.18))
    border_mask[:bh, :] = True
    border_mask[-bh:, :] = True
    border_mask[:, :bw] = True
    border_mask[:, -bw:] = True
    bg_mask = (a < 0.03) & border_mask

    # 兜底：若主体占满画面导致边缘无 alpha=0，则直接取最外围条带
    if np.sum(bg_mask) < 100:
        border_mask = np.zeros((h, w), dtype=bool)
        border_mask[:max(1, h // 12), :] = True
        border_mask[-max(1, h // 12):, :] = True
        border_mask[:, :max(1, w // 12)] = True
        border_mask[:, -max(1, w // 12):] = True
        bg_mask = border_mask

    candidates = src[bg_mask]
    if len(candidates) == 0:
        return rgba

    B = np.median(candidates, axis=0)
    var = float(np.mean(np.var(candidates, axis=0)))
    if var > var_threshold:
        # 复杂背景：不执行去溢色
        return rgba

    # 2) 对更宽的半透明边缘（0.02 < α < 0.98）反推前景色
    edge = (a > 0.02) & (a < 0.98)
    eps = 1e-6
    F = rgb_arr.copy()
    F[edge] = (rgb_arr[edge] - (1.0 - a[edge, None]) * B[None, :]) / np.maximum(a[edge, None], eps)
    F = np.clip(F, 0.0, 1.0)

    # 极低 alpha 区直接置为背景色，避免残留杂点
    near_transparent = a < 0.06
    F[near_transparent] = B

    out[..., :3] = F
    out = (np.clip(out, 0.0, 1.0) * 255.0).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


# ---------------------------------------------------------------------------
# 输出侧超分辨率（对齐豆包：输出比输入更高清）
# ---------------------------------------------------------------------------
# 抠完图之后，对主体跑 ONNX 版 Real-ESRGAN（RRDBNet，BSD-3-Clause）整体放大 2x
# 并重建发丝/皮肤细节，从而输出比输入分辨率更高、更清晰的结果。
# 模型来源：HuggingFace SceneWorks/real-esrgan-onnx（与 xinntao/Real-ESRGAN 权重 1:1 导出）。
_SR_SCALE = 2  # 输出放大倍数（2 = 输入 2x；4 亦可但文件/耗时显著增大）
_SR_MODEL_FILES = {2: "real_esrgan_x2.onnx", 4: "real_esrgan_x4.onnx"}
_SR_BASE_URLS = [
    "https://huggingface.co/SceneWorks/real-esrgan-onnx/resolve/main/",
    "https://hf-mirror.com/SceneWorks/real-esrgan-onnx/resolve/main/",
]
_SR_CACHE_DIR = Path(os.path.expanduser("~/.video-downloader/models/sr"))
_SESSIONS: dict[int, Any] = {}


def _ensure_sr_model(scale: int) -> Path | None:
    """下载并返回 Real-ESRGAN ONNX 模型路径；失败返回 None（不阻塞主流程）。"""
    fname = _SR_MODEL_FILES.get(scale)
    if not fname:
        return None
    path = _SR_CACHE_DIR / fname
    if path.exists() and path.stat().st_size > 5_000_000:
        return path
    _SR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for base in _SR_BASE_URLS:
        url = base + fname
        try:
            with urllib.request.urlopen(url, timeout=180, context=_SSL_CTX) as r:
                data = r.read()
            path.write_bytes(data)
            if path.stat().st_size > 5_000_000:
                logger.info("SR 模型已下载: %s (%d bytes)", path, path.stat().st_size)
                return path
        except Exception as exc:  # noqa: BLE001
            logger.warning("SR 模型下载失败 %s: %s", url, exc)
    return None


def _get_sr_session(scale: int):
    """加载（并缓存）Real-ESRGAN 推理会话；优先 CoreML(苹果加速)，回退 CPU。"""
    if scale in _SESSIONS:
        return _SESSIONS[scale]
    path = _ensure_sr_model(scale)
    if not path:
        return None
    import onnxruntime as ort

    sess: Any = None
    for provs in (["CoreMLExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"]):
        try:
            sess = ort.InferenceSession(str(path), providers=provs)
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("SR 会话创建失败 %s: %s", provs, exc)
    if sess is None:
        return None
    _SESSIONS[scale] = sess
    return sess


def _sr_once(rgb: np.ndarray, sess, scale: int) -> np.ndarray:
    inp = rgb.transpose(2, 0, 1)[None].astype(np.float32)
    out = sess.run(None, {sess.get_inputs()[0].name: inp})[0][0]
    return np.clip(out, 0.0, 1.0).transpose(1, 2, 0)


def _sr_rgb(rgb: np.ndarray, sess, scale: int, tile: int = 512, overlap: int = 16) -> np.ndarray:
    """对 RGB 跑 Real-ESRGAN；大图分块推理后拼接，避免一次占满内存。"""
    h, w = rgb.shape[:2]
    if h <= tile and w <= tile:
        return _sr_once(rgb, sess, scale)
    out_h, out_w = h * scale, w * scale
    out = np.zeros((out_h, out_w, 3), dtype=np.float32)
    weight = np.zeros((out_h, out_w, 1), dtype=np.float32)
    for y in range(0, h, tile - overlap):
        for x in range(0, w, tile - overlap):
            y2 = min(y + tile, h)
            x2 = min(x + tile, w)
            ph, pw = y2 - y, x2 - x
            if ph < tile or pw < tile:
                pad = np.zeros((tile, tile, 3), dtype=np.float32)
                pad[:ph, :pw] = rgb[y:y2, x:x2]
                tiled = _sr_once(pad, sess, scale)[: ph * scale, : pw * scale]
            else:
                tiled = _sr_once(rgb[y:y2, x:x2], sess, scale)
            oy, ox = y * scale, x * scale
            oh, ow = tiled.shape[:2]
            out[oy:oy + oh, ox:ox + ow] += tiled
            weight[oy:oy + oh, ox:ox + ow] += 1.0
    out /= np.maximum(weight, 1e-6)
    return out


def _color_guided_alpha(I: np.ndarray, p: np.ndarray,
                        r: int = 8, eps: float = 1e-5, step: int = 512) -> np.ndarray:
    """He et al. guided filter——Levin closed-form matting 的 O(N) 快解。

    closed-form matting（Levin 2007）假设前景/背景颜色在局部窗口内线性，
    据此重估 alpha；guided filter 论文证明其局部线性模型与 matting Laplacian
    同构，用 box filter 即可 O(N) 求解，无需解稀疏线性系统。
    I：引导图 = 带背景的原图 RGB（真实前景/背景颜色，局部模型的关键）；
    p：待精修 alpha（云端输出上采样后的软边缘）。
    只在过渡带使用（调用方控制），逐行分块解 3x3 线性系统以控制内存。
    """
    def bf(x: np.ndarray) -> np.ndarray:
        return cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1),
                             normalize=True, borderType=cv2.BORDER_REPLICATE)

    I = I.astype(np.float32)
    p = p.astype(np.float32)
    mean_I = bf(I)
    mean_p = bf(p)
    prods = {}
    for i in range(3):
        prods[(i, i)] = bf(I[..., i] * I[..., i])
    for i, j in ((0, 1), (0, 2), (1, 2)):
        prods[(i, j)] = bf(I[..., i] * I[..., j])
    Ip = [bf(I[..., k] * p) for k in range(3)]

    h, w = p.shape
    a_coef = np.empty((h, w, 3), np.float32)
    b_coef = np.empty((h, w), np.float32)
    eps_i = (eps * np.eye(3, dtype=np.float32))
    for y0 in range(0, h, step):
        y1 = min(h, y0 + step)
        sl = slice(y0, y1)
        var = np.empty((y1 - y0, w, 3, 3), np.float32)
        for i in range(3):
            for j in range(3):
                ii, jj = min(i, j), max(i, j)
                v = prods[(ii, jj)][sl] - mean_I[sl, :, i] * mean_I[sl, :, j]
                var[..., i, j] = v
                var[..., j, i] = v
        cov = np.stack([Ip[k][sl] - mean_I[sl, :, k] * mean_p[sl] for k in range(3)], axis=-1)
        sol = np.linalg.solve(var + eps_i, cov[..., None])[..., 0]
        a_coef[sl] = sol
        b_coef[sl] = mean_p[sl] - np.sum(sol * mean_I[sl], axis=-1)

    mean_a = np.stack([bf(a_coef[..., k]) for k in range(3)], axis=-1)
    mean_b = bf(b_coef)
    return np.clip(np.sum(mean_a * I, axis=-1) + mean_b, 0.0, 1.0)


def _super_resolve(rgba: Image.Image, scale: int = _SR_SCALE,
                   guide_rgb: Image.Image | None = None,
                   use_sr: bool = True,
                   suppress_bg: bool = True) -> Image.Image:
    """输出侧超分辨率 + 全套精修。

    use_sr=True：对 RGB 跑 Real-ESRGAN 放大 scale 倍（alpha 用 LANCZOS 同步）。
    use_sr=False：输入已是 AI 增强分辨率，跳过本地 SR，直接在当前尺寸跑全套
    精修（GF closed-form / 背景色压制 / 低α去斑驳 / 前景色传播 / 细节层增强）。
    suppress_bg=False：跳过背景色压制+去斑驳（裁剪定位模式用——裁片背景色与
    浅色目标可能极近，压制会洗白目标；框外清洁由调用方的羽化框遮罩负责）。
    模型缺失/推理失败均安全跳过，不引入回归。
    """
    if use_sr and scale <= 1:
        return rgba
    if use_sr:
        sess = _get_sr_session(scale)
        if sess is None:
            logger.warning("SR 模型不可用，跳过超分（输出保持原分辨率）")
            return rgba
        arr = np.array(rgba.convert("RGBA")).astype(np.float32) / 255.0
        rgb, a = arr[..., :3], arr[..., 3]
        try:
            sr_rgb = _sr_rgb(rgb, sess, scale)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SR 推理失败，跳过: %s", exc)
            return rgba
        h, w = sr_rgb.shape[:2]
        a_pil = Image.fromarray((a * 255.0).astype(np.uint8), mode="L")
        a_up = np.array(a_pil.resize((w, h), Image.LANCZOS)).astype(np.float32) / 255.0
        out = np.dstack([np.clip(sr_rgb, 0.0, 1.0), a_up])
    else:
        out = np.array(rgba.convert("RGBA")).astype(np.float32) / 255.0
    h, w = out.shape[:2]

    # closed-form matting 精修（color guided filter）：LANCZOS 上采样会把 alpha
    # 边缘磨软，这里以原图真实颜色为引导，在过渡带重估 alpha，让发丝边缘
    # 重新贴合真实颜色边界。失败则跳过，不影响主流程。
    if guide_rgb is not None:
        guide = None
        try:
            guide = np.array(guide_rgb.convert("RGB").resize((w, h), Image.LANCZOS)).astype(np.float32) / 255.0
            a_ref = _color_guided_alpha(guide, out[..., 3])
            band = (out[..., 3] > 0.02) & (out[..., 3] < 0.98)
            out[..., 3][band] = a_ref[band]
        except Exception as exc:  # noqa: BLE001
            logger.warning("closed-form 精修失败，跳过: %s", exc)

        # 背景色压制：模型对部分纯背景像素会高估 alpha（此类像素 I≈背景色 B，
        # despill 公式 F=(I-(1-α)B)/α 只会还原出 B 本身，形成彩色光晕，无解）。
        # 用原图颜色判断「这像素本来就是背景」，按颜色距离只降不升地衰减 alpha。
        # 两个关键约束（离线调参实测）：
        #   ① 只压低 α 区（α<0.55）——深色头发 α0.7~0.9 的像素若被压会啃出碎边；
        #   ② ramp 场先做 σ=2 高斯平滑——逐像素噪声 ramp 会把边缘打碎（连通域 49→89）。
        # suppress_bg=False（裁剪定位模式）时整段跳过：裁片背景色与浅色目标
        # （如米色纸上的浅木画架）可能极近，压制会洗白目标；框外清洁由调用方的
        # 羽化框遮罩负责，压制在裁剪模式下冗余且有害。
        if suppress_bg:
            try:
                known = out[..., 3] < 0.01  # MediaKit 高置信背景（含 refine 后）
                wgt = known.astype(np.float32)
                k = 121  # 大核把背景色场传播到主体周边（兼容渐变背景）
                Bf = cv2.blur(guide * wgt[..., None], (k, k)) / (cv2.blur(wgt, (k, k))[..., None] + 1e-6)
                dist = np.linalg.norm(guide - Bf, axis=-1)
                ramp = np.clip((dist - 0.07) / 0.09, 0.0, 1.0)
                ramp = cv2.GaussianBlur(ramp, (0, 0), 2.0)
                zone = out[..., 3] < 0.55
                out[..., 3][zone] = out[..., 3][zone] * ramp[zone]

                # 低 α 去斑驳：压制+GF 后低 α 区有孤立坑洞（深底上呈斑点）。
                # 中值滤波专治此类小斑块（高斯平滑只能摊开、消不掉），实测轮廓带
                # 斑点 586→0，且细梢锥形完整保留。
                med = cv2.medianBlur((out[..., 3] * 255.0).astype(np.uint8), 5).astype(np.float32) / 255.0
                z2 = (out[..., 3] > 0.02) & (out[..., 3] < 0.55)
                out[..., 3][z2] = 0.25 * out[..., 3][z2] + 0.75 * med[z2]
            except Exception as exc:  # noqa: BLE001
                logger.warning("背景色压制失败，跳过: %s", exc)

    # 前景色传播（foreground estimation）：半透明细梢的 RGB 来自 despill 反推，
    # α 越低噪声放大越狠（×1/α）。把实心区(α>0.85)的发色按就近尺度传播过来，
    # 梢部呈现顺滑的真实发色而非颗粒状混色。优先取最小覆盖尺度（多色主体不混色）。
    try:
        a_f = out[..., 3]
        solid = (a_f > 0.85).astype(np.float32)
        if solid.mean() > 0.02:
            rgb_f = out[..., :3]
            prop = np.zeros_like(rgb_f)
            have = np.zeros(a_f.shape, dtype=bool)
            for k in (24, 64, 144):
                num = cv2.blur(rgb_f * solid[..., None], (k, k))
                den = cv2.blur(solid, (k, k))
                ok = (den > 1e-4) & (~have)
                pr = num / np.maximum(den, 1e-6)[..., None]
                prop[ok] = pr[ok]
                have |= ok
            wt = np.clip((0.85 - a_f) / 0.85, 0.0, 1.0)
            band = a_f > 0.02
            Fnew = rgb_f * (1 - wt[..., None]) + prop * wt[..., None]
            out[..., :3] = np.where(band[..., None], Fnew, rgb_f)
    except Exception as exc:  # noqa: BLE001
        logger.warning("前景色传播失败，跳过: %s", exc)

    # 细节层增强：原图发丝本身偏软（对照豆包确认为其生成增强所致），
    # 用保边分解把中频（波纹/发丝组）与高频（细梢）对比拉起来，观感更精致。
    try:
        rgb_e = out[..., :3]
        base = cv2.GaussianBlur(rgb_e, (0, 0), 6.0)
        mid = cv2.GaussianBlur(rgb_e, (0, 0), 1.2)
        out[..., :3] = np.clip(rgb_e + 0.9 * (mid - base) + 0.6 * (rgb_e - mid), 0.0, 1.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("细节层增强失败，跳过: %s", exc)
    return Image.fromarray((out * 255.0).astype(np.uint8), mode="RGBA")


def _finalize_output(rgba: Image.Image) -> Image.Image:
    """抠图收尾清理（保守，只做两件事）：

    ① 剔除与主体**完全不连通**、且无任何实心像素(α≥0.5)的孤立低 α 碎屑
       （云端偶发的飞点）。贴着主体轮廓的细发梢与主体属同一连通域，不受影响。
    ② 全透明区(α<0.01)的 RGB 置白。alpha=0 本身不可见，但部分查看器/编辑器
       忽略 alpha 直读 RGB，会把透明区存的原始背景色（如橙幕的橙棕）整个
       显示出来，让用户误以为背景没抠掉。
    """
    arr = np.array(rgba.convert("RGBA"))
    a = arr[..., 3].astype(np.float32) / 255.0
    mask = (a > 0.02).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n > 2:
        main = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        for i in range(1, n):
            if i == main:
                continue
            comp = labels == i
            if a[comp].max() < 0.5 and stats[i, cv2.CC_STAT_AREA] < 0.02 * a.size:
                arr[..., 3][comp] = 0
    arr[..., :3][a < 0.01] = 255
    return Image.fromarray(arr, mode="RGBA")


def _enhance_image(rgb: Image.Image, version: str = 'professional',
                   multiple: float = 2.0, mode: str | None = None,
                   timeout: int = 120) -> Image.Image:
    """AI MediaKit 图像画质增强（豆包同源生成式增强）。

    version: standard / professional(发丝级) / max(大模型生成式)。
    multiple: 输出放大倍数（professional/max 支持 1~30）。
    mode: generative_first(生成度优先，默认) / fidelity_first(保真度优先)。
    返回增强后的 RGB 图（通常为 multiple 倍分辨率）。失败抛异常由调用方回退。
    """
    cfg = _read_cfg()
    api_key = (cfg.get("mediakit_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("云端画质增强未配置 AI MediaKit API Key")

    buf = io.BytesIO()
    rgb.save(buf, format="PNG")
    up = _request_upload(api_key, timeout)
    upload_url = up.get("upload_url")
    if not upload_url:
        raise RuntimeError(f"MediaKit 增强上传地址缺失: {up}")
    _put_upload(upload_url, buf.getvalue(), "image/png", timeout)

    payload: dict = {"image_url": up.get("file_id"), "tool_version": version,
                     "multiple": multiple}
    if mode:
        payload["generative_enhance_mode"] = mode
    resp = _post_json(_ENHANCE_URL, api_key, payload, timeout)
    if not resp.get("success"):
        raise RuntimeError(f"MediaKit 画质增强失败: {resp.get('error') or resp}")
    out_url = (resp.get("result") or {}).get("image_url")
    if not out_url:
        raise RuntimeError(f"MediaKit 画质增强未返回结果图: {resp}")
    return _download(out_url, timeout).convert("RGB")


def mediakit_remove_bg(rgb: Image.Image, scene: str = "general",
                       timeout: int = 120, upscale: int | None = None,
                       suppress_bg: bool = True) -> Image.Image:
    """对任意图做软 alpha 抠图，返回与输入同尺寸的 RGBA。

    scene: general（未知主体，自动检测）/ human（人像，发丝更精）/ product（商品，自动裁背景）。
    upscale: 输入放大倍数。None 时按场景自动：human→2（补发丝细节），其他→1。
             放大后调用云端模型，再把 alpha 缩回原尺寸，既保留发丝又不让输出变糊。
    suppress_bg=False：跳过背景色压制+去斑驳（裁剪定位模式专用——裁片背景色与
             浅色目标可能极近，压制会洗白目标；框外清洁由调用方的羽化框遮罩负责）。
    任一环节失败都抛异常，由调用方回退本地链路。
    """
    cfg = _read_cfg()
    api_key = (cfg.get("mediakit_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("云端通用抠图未配置 AI MediaKit API Key")

    if scene not in ("general", "human", "product"):
        scene = "general"

    # ── AI 画质增强（豆包同源）：先增强后抠图 ──
    # 配置键 enhance_version：off=关；standard / professional / max=指定版本。
    # 未配置时 auto：人像默认 professional(发丝级)，其他场景不增强（控费）。
    ver = str(cfg.get("enhance_version") or "").strip().lower()
    if ver in ("", "auto"):
        ver = "professional" if scene == "human" else "off"

    enhanced = None
    if ver in ("standard", "professional", "max"):
        try:
            enhanced = _enhance_image(rgb, version=ver, multiple=2.0, timeout=timeout)
            logger.info("AI 画质增强完成(%s): %s", ver, enhanced.size)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI 画质增强失败，回退原始链路: %s", exc)

    use_sr = enhanced is None
    if enhanced is not None:
        # 增强图已是 2x AI 细节：跳过本地 LANCZOS 上采样与 Real-ESRGAN，
        # 全程在增强分辨率上抠图与精修（AI 增强取代本地 SR）。
        upscale = 1
        work_rgb = enhanced
        orig_size = work_rgb.size
    else:
        if upscale is None:
            # 人像场景默认 2x 放大，让云端模型看到更多发丝细节
            upscale = 2 if scene == "human" else 1
        upscale = max(1, min(upscale, 3))
        work_rgb = rgb
        if upscale > 1:
            work_rgb = rgb.resize((rgb.width * upscale, rgb.height * upscale), Image.LANCZOS)
        orig_size = rgb.size

    # 本地图编码为 PNG 二进制（无损），上传走 mediakit:// 客户端直传
    buf = io.BytesIO()
    work_rgb.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    # ① 申请上传地址
    up = _request_upload(api_key, timeout)
    file_id = up.get("file_id")
    upload_url = up.get("upload_url")
    if not file_id or not upload_url:
        raise RuntimeError(f"MediaKit 上传地址字段缺失: {up}")

    # ② PUT 上传
    _put_upload(upload_url, img_bytes, "image/png", timeout)

    # ③ 提交抠图
    payload = {"image_url": file_id, "scene": scene}
    resp = _post_json(_REMOVE_URL, api_key, payload, timeout)
    if not resp.get("success"):
        raise RuntimeError(f"MediaKit 抠图失败: {resp.get('error') or resp}")
    result = resp.get("result") or {}
    out_url = result.get("image_url")
    if not out_url:
        raise RuntimeError(f"MediaKit 未返回结果图: {resp}")

    # ④ 下载透明 PNG
    out = _download(out_url, timeout)

    # ⑤ 若做了放大，只把 alpha 缩回原尺寸再贴回原图 RGB，保持原图清晰度
    if upscale > 1:
        if out.size != work_rgb.size:
            out = out.resize(work_rgb.size, Image.LANCZOS)
        alpha = out.split()[3]
        alpha = alpha.resize(orig_size, Image.LANCZOS)
        a_arr = np.array(alpha).astype(np.float32) / 255.0
        a_arr = _refine_alpha(a_arr)
        # 用原图 RGB + 优化后的 alpha 合成
        rgb_arr = np.array(rgb.convert("RGB")).astype(np.float32) / 255.0
        composed = np.dstack([rgb_arr, a_arr])
        out = Image.fromarray((np.clip(composed, 0.0, 1.0) * 255.0).astype(np.uint8), mode="RGBA")
    else:
        if out.size != orig_size:
            out = out.resize(orig_size, Image.LANCZOS)
        # 1x 路径同样精炼 alpha
        arr = np.array(out.convert("RGBA")).astype(np.float32) / 255.0
        arr[..., 3] = _refine_alpha(arr[..., 3])
        out = Image.fromarray((np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8), mode="RGBA")

    # ⑥ 纯色背景时做一次边缘去溢色（如橙/绿幕棚拍），消除半透明边缘彩边
    out = _decontaminate_edge_spill(work_rgb if enhanced is not None else rgb, out)

    # ⑦ 输出侧超分辨率：抠完之后整体放大 2x 并重建发丝/皮肤细节，
    #    再以原图真实颜色为引导做 closed-form matting 精修（color guided filter），
    #    让输出比输入分辨率更高、发丝边缘更贴真实边界。模型缺失时自动跳过。
    out = _super_resolve(out, scale=(_SR_SCALE if use_sr else 1),
                         guide_rgb=(work_rgb if enhanced is not None else rgb),
                         use_sr=use_sr, suppress_bg=suppress_bg)

    # ⑧ 收尾清理：剔孤立碎屑 + 透明区 RGB 置白（防忽略 alpha 的查看器露底色）
    out = _finalize_output(out)
    return out
