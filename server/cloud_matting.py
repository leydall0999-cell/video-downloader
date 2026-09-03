"""云端抠图后端（火山引擎视觉智能 Visual Service）。

提供「说扣什么就抠什么」的像素级抠图能力，质量对齐豆包（火山即豆包视觉后端）。

设计要点：
  - 签名：火山 OpenAPI 用 AWS SigV4 变体（algorithm=HMAC-SHA256），纯标准库实现，
    无需 volcengine SDK，不给瘦身包加依赖。
  - 主接口 _volc_general_segment(rgb) 调 GeneralSegment，返回 RGBA 透明 PNG。
  - 「说扣什么就抠哪」技巧：VLM 给出描述对象的 bbox 时，先把图 crop 到该框（带 padding），
    对 crop 调 GeneralSegment（crop 内主主体=描述对象），结果贴回原图。
    这样只用文档最清楚的 GeneralSegment，无需 EntitySegment 的多实体解析，鲁棒且可测。
  - 云端优先、本地兜底：matting_ai 在可用时优先调用；任何异常按类型决定回退还是抛出。
  - 全程用 urllib（stdlib），不引入 requests。
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import io
import json
import urllib.error
import urllib.request

from PIL import Image

from cloud_matting_config import get_cloud_matting_config

# 火山视觉智能固定参数
_HOST = "visual.volcengineapi.com"
_REGION = "cn-north-1"
_SERVICE = "cv"
_ENDPOINT = "https://visual.volcengineapi.com"
_VERSION = "2020-08-26"

# 图片最大边（base64 后需 < 5MB；1280 边 PNG 通常 < 1MB，留足余量）
_MAX_SIDE = 1280


# ───────────────────────────── 签名 ─────────────────────────────
def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sign_headers(ak: str, sk: str, method: str, body_bytes: bytes,
                  content_type: str, query: str) -> dict[str, str]:
    """生成火山 SigV4 签名所需的请求头（Authorization / X-Date / X-Content-Sha256）。"""
    t = datetime.datetime.utcnow()
    x_date = t.strftime("%Y%m%dT%H%M%SZ")
    short_date = t.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body_bytes).hexdigest()

    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{_HOST}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    canonical_request = "\n".join([
        method, "/", query, canonical_headers, signed_headers, payload_hash,
    ])
    credential_scope = f"{short_date}/{_REGION}/{_SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k_date = _hmac(sk.encode("utf-8"), short_date)
    k_region = _hmac(k_date, _REGION)
    k_service = _hmac(k_region, _SERVICE)
    k_signing = _hmac(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth = (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Host": _HOST,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Authorization": auth,
        "Content-Type": content_type,
    }


# ───────────────────────────── 调用 ─────────────────────────────
def _volc_post(action: str, body_bytes: bytes, content_type: str,
               ak: str, sk: str, timeout: int = 60) -> dict:
    """调火山视觉智能接口，返回解析后的 JSON。4xx 抛 RuntimeError（含明细），不静默。"""
    query = f"Action={action}&Version={_VERSION}"
    url = f"{_ENDPOINT}/?{query}"
    headers = _sign_headers(ak, sk, "POST", body_bytes, content_type, query)
    req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:  # noqa: BLE001
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"火山抠图接口 {action} HTTP {e.code}: {detail[:300]}") from e
    except Exception as e:  # noqa: BLE001
        # 网络/超时等瞬态错误：调用方据此回退本地
        raise
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"火山抠图返回非 JSON: {raw[:200]}") from e
    if parsed.get("code") not in (0, None, 10000):
        raise RuntimeError(f"火山抠图接口 {action} 业务错误 code={parsed.get('code')} msg={parsed.get('message')}")
    return parsed


def _to_b64_png(rgb: Image.Image) -> str:
    """RGB 转 PNG base64；过大则等比缩到 _MAX_SIDE 以内。"""
    w, h = rgb.size
    scale = min(1.0, _MAX_SIDE / max(w, h))
    if scale < 1.0:
        rgb = rgb.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    rgb.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _decode_foreground(parsed: dict) -> Image.Image:
    """从 GeneralSegment 响应解析透明前景图（优先 foreground_image，否则用 mask 合成）。"""
    data = parsed.get("data") or {}
    fg_b64 = data.get("foreground_image") or data.get("foreground")
    if fg_b64:
        img = Image.open(io.BytesIO(base64.b64decode(fg_b64)))
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        return img
    mask_b64 = data.get("mask")
    if mask_b64:
        mask = Image.open(io.BytesIO(base64.b64decode(mask_b64))).convert("L")
        # mask 是灰度图 → 作为 alpha，原图作 RGB
        rgb = Image.new("RGB", mask.size, (255, 255, 255))
        return Image.merge("RGBA", (*rgb.split(), mask))
    raise RuntimeError(f"火山抠图响应缺少 foreground/mask 字段: {str(data)[:200]}")


def _general_segment(rgb: Image.Image, ak: str, sk: str, timeout: int = 60) -> Image.Image:
    """对单张 RGB 调 GeneralSegment，返回 RGBA 透明前景图。"""
    b64 = _to_b64_png(rgb)
    # form-urlencoded：value 整体 percent-encode，避免 base64 的 + / = 被误解析
    import urllib.parse
    body = ("image_base64=" + urllib.parse.quote(b64, safe="")
            + "&return_foreground_image=1&refine=1").encode("utf-8")
    parsed = _volc_post("GeneralSegment", body, "application/x-www-form-urlencoded", ak, sk, timeout)
    return _decode_foreground(parsed)


# ───────────────────────────── 对外入口 ─────────────────────────────
def cloud_matting_rgba(rgb: Image.Image, box: list | None = None,
                       timeout: int = 60) -> Image.Image:
    """云端抠图，返回与 rgb 同尺寸的 RGBA。

    box: 归一化 [x1,y1,x2,y2]（VLM 定位到的描述对象）。给定时 crop 到 box 再抠，
         结果贴回 → 实现「说扣什么就抠哪」；为 None 时抠全图主主体。
    """
    cfg = get_cloud_matting_config()
    ak = (cfg.get("access_key") or "").strip()
    sk = (cfg.get("secret_key") or "").strip()
    if not ak or not sk:
        raise RuntimeError("云端抠图未配置火山 AK/SK（设置→视觉模型栏→云端抠图）")

    if box and len(box) == 4:
        x1, y1, x2, y2 = [float(v) for v in box[:4]]
        if x1 > x2:
            x1, x2 = x2, x1
        if y1 > y2:
            y1, y2 = y2, y1
        W, H = rgb.size
        bw, bh = x2 - x1, y2 - y1
        pad = 0.10
        cx0 = max(0, int(round(x1 * W - bw * W * pad)))
        cy0 = max(0, int(round(y1 * H - bh * H * pad)))
        cx1 = min(W, int(round(x2 * W + bw * W * pad)))
        cy1 = min(H, int(round(y2 * H + bh * H * pad)))
        if cx1 > cx0 and cy1 > cy0:
            crop = rgb.crop((cx0, cy0, cx1, cy1))
            crop_rgba = _general_segment(crop, ak, sk, timeout)
            full = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            full.paste(crop_rgba, (cx0, cy0))
            return full
    # 无 box 或 crop 非法 → 全图主主体
    return _general_segment(rgb, ak, sk, timeout)
