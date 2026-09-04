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
import ssl
import urllib.request

from PIL import Image, ImageFilter

import numpy as np

_MEDIAKIT_HOST = "mediakit.cn-beijing.volces.com"
_REQ_UPLOAD_URL = f"https://{_MEDIAKIT_HOST}/api/v1/tools-sync/request-media-upload-url"
_REMOVE_URL = f"https://{_MEDIAKIT_HOST}/api/v1/tools-sync/remove-image-background"

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
    """清理 alpha 通道噪声并轻微平滑边缘。

    云端返回的 alpha 在背景处可能带极低值噪点，在高 alpha 主体内部
    可能有细小孔洞。本函数：
    - α < 0.03 的像素强制置 0（彻底清背景）；
    - α > 0.97 的像素强制置 1（填实主体）；
    - 对 0.03~0.97 过渡区做轻微高斯平滑，使发丝/边缘更自然。
    整个操作很轻，避免吞掉真正的半透明发丝。
    """
    a = a.copy()
    a[a < 0.03] = 0.0
    a[a > 0.97] = 1.0

    edge = (a > 0.03) & (a < 0.97)
    if np.any(edge):
        a_pil = Image.fromarray((a * 255.0).astype(np.uint8), mode="L")
        a_blur = np.array(a_pil.filter(ImageFilter.GaussianBlur(radius=0.5))).astype(np.float32) / 255.0
        # 只在过渡区混合少量平滑结果，保留模型原本细节
        a[edge] = 0.80 * a[edge] + 0.20 * a_blur[edge]

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


def mediakit_remove_bg(rgb: Image.Image, scene: str = "general",
                       timeout: int = 120, upscale: int | None = None) -> Image.Image:
    """对任意图做软 alpha 抠图，返回与输入同尺寸的 RGBA。

    scene: general（未知主体，自动检测）/ human（人像，发丝更精）/ product（商品，自动裁背景）。
    upscale: 输入放大倍数。None 时按场景自动：human→2（补发丝细节），其他→1。
             放大后调用云端模型，再把 alpha 缩回原尺寸，既保留发丝又不让输出变糊。
    任一环节失败都抛异常，由调用方回退本地链路。
    """
    cfg = _read_cfg()
    api_key = (cfg.get("mediakit_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("云端通用抠图未配置 AI MediaKit API Key")

    if scene not in ("general", "human", "product"):
        scene = "general"

    if upscale is None:
        # 人像场景默认 2x 放大，让云端模型看到更多发丝细节
        upscale = 2 if scene == "human" else 1
    upscale = max(1, min(upscale, 3))

    orig_size = rgb.size
    work_rgb = rgb
    if upscale > 1:
        work_rgb = rgb.resize((rgb.width * upscale, rgb.height * upscale), Image.LANCZOS)

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
    out = _decontaminate_edge_spill(rgb, out)
    return out
