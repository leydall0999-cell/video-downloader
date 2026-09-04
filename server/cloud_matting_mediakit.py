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

from PIL import Image

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


def mediakit_remove_bg(rgb: Image.Image, scene: str = "general",
                       timeout: int = 90) -> Image.Image:
    """对任意图做软 alpha 抠图，返回与输入同尺寸的 RGBA。

    scene: general（未知主体，自动检测）/ human（人像，发丝更精）/ product（商品，自动裁背景）。
    任一环节失败都抛异常，由调用方回退本地链路。
    """
    cfg = _read_cfg()
    api_key = (cfg.get("mediakit_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("云端通用抠图未配置 AI MediaKit API Key")

    if scene not in ("general", "human", "product"):
        scene = "general"

    # 本地图编码为 PNG 二进制（无损），上传走 mediakit:// 客户端直传
    buf = io.BytesIO()
    rgb.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    # ① 申请上传地址
    up = _request_upload(api_key, timeout)
    file_id = up.get("file_id")
    upload_url = up.get("upload_url")
    if not file_id or not upload_url:
        raise RuntimeError(f"MediaKit 上传地址字段缺失: {up}")
    upload_headers = up.get("upload_headers") or []

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

    # ④ 下载透明 PNG，并 resize 回原图尺寸（防止 API 缩放）
    out = _download(out_url, timeout)
    if out.size != rgb.size:
        out = out.resize(rgb.size, Image.LANCZOS)
    return out
