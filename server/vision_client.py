"""server/vision_client.py — 轻量多模态视觉模型客户端（OpenAI 兼容）。

专为「一键抠图 · AI 视觉定位」场景设计：把图发给 VLM（DashScope / Gemini /
Ollama / 自建兼容端点均可，配置复用 vision_config），让模型"看懂"图、定位
主要文字/图形主体，返回归一化边界框 (x1,y1,x2,y2) ∈ [0,1]。

仅依赖标准库 + urllib + Pillow（App 已打包），不引入 requests / opencv 等重依赖。

主入口 detect_subject(image_path) -> dict：
    {
      "subject": {"label": <str>, "box": [x1, y1, x2, y2]},   # 面积最大的主体
      "all":     [ {"label":..., "box":[...]}, ... ],
      "model":   <str>,
      "provider":<str>
    }
失败抛 RuntimeError（调用方据此回退到基础抠图）。
"""
from __future__ import annotations

import base64
import io
import json
import re
import urllib.request

from vision_config import get_vision_config

_PROMPT = (
    "你是一个精确的图像要素定位助手。请分析这张图，识别其中主要的文字或图形主体"
    "（通常是视觉上最突出的标题、logo、主要角色/物体）。\n\n"
    "请只返回一个 JSON 对象，不要任何额外文字、不要 markdown 代码块：\n"
    "{\n"
    '  "blocks": [\n'
    '    {"label": "<该要素的简短描述，如 主标题/副标题/logo/角色>", "bbox": [x1, y1, x2, y2]}\n'
    "  ]\n"
    "}\n"
    "bbox 是边界框，坐标为归一化值 0~1，格式 [左上x, 左上y, 右下x, 右下y]。\n"
    "请尽可能列出图中所有显著文字/图形要素（主标题、副标题、logo 等各一个 bbox）。"
)

_SYSTEM = "你是一个严谨的图像分析助手，只输出要求的 JSON，不做任何额外解释。"


def _compress_to_b64(image_path: str, max_side: int = 1024) -> str:
    """读图、等比缩到 max_side 以内、转 PNG base64（减小 VLM 请求体积、加速推理）。"""
    from PIL import Image

    with Image.open(image_path) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")


def _post_json(url: str, headers: dict, payload: dict, timeout: int = 60) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def _extract_json(text: str) -> dict:
    """从 VLM 回复容错提取 JSON（兼容 ```json 代码块 / JSON 对象 / JSON 数组）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("空回复")

    # 1) 直接 json.loads
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass

    # 2) 去 markdown 代码块
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except Exception:  # noqa: BLE001
                pass

    # 3) 截取第一个 { 到最后一个 }
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:  # noqa: BLE001
            pass

    # 4) 尝试数组 [ ... ]
    s, e = text.find("["), text.rfind("]")
    if s != -1 and e != -1 and e > s:
        try:
            return {"blocks": json.loads(text[s:e + 1])}
        except Exception:  # noqa: BLE001
            pass

    raise ValueError("无法从 VLM 回复解析 JSON")


def _parse_box(b) -> list[float] | None:
    """规整 VLM 返回的 box 为 [x1, y1, x2, y2]，夹紧 0~1、允许反序、过小丢弃。"""
    if not isinstance(b, (list, tuple)) or len(b) < 4:
        return None
    try:
        vals = [float(v) for v in b[:4]]
    except Exception:  # noqa: BLE001
        return None
    x1, y1, x2, y2 = vals
    if x1 > x2:
        x1, x2 = x2, x1
    if y1 > y2:
        y1, y2 = y2, y1
    x1, y1 = max(0.0, min(1.0, x1)), max(0.0, min(1.0, y1))
    x2, y2 = max(0.0, min(1.0, x2)), max(0.0, min(1.0, y2))
    if x2 - x1 < 0.001 or y2 - y1 < 0.001:
        return None
    return [x1, y1, x2, y2]


def detect_subject(image_path: str, max_side: int = 1024, timeout: int = 60) -> dict:
    """调 VLM 定位主体，返回 {subject, all, model, provider}。失败抛 RuntimeError。"""
    cfg = get_vision_config()
    key = (cfg.get("api_key") or "").strip()
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    model = (cfg.get("model") or "").strip()
    provider = cfg.get("provider", "auto")

    if not key or not base_url or not model:
        raise RuntimeError(
            "视觉模型未配置（请在「设置 → 视觉模型」里选择 DashScope 并填入 API Key）"
        )

    b64 = _compress_to_b64(image_path, max_side)
    url = base_url + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            },
        ],
        "temperature": 0.1,
    }

    try:
        resp = _post_json(url, headers, payload, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"视觉模型调用失败：{e}") from e

    content = ""
    try:
        content = resp["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"视觉模型返回格式异常：{str(resp)[:200]}")

    parsed = _extract_json(content)
    # 兼容多种 VLM 输出结构：
    #   {"blocks": [{"label","bbox"}]}  ← 主格式（prompt 要求）
    #   {"blocks": [{"label","box"}]}   ← 老格式兜底
    #   [{"label","bbox"}]              ← 直接数组
    #   {"label","bbox"}                ← 单个对象（裸返回）
    if isinstance(parsed, list):
        blocks_raw = parsed
    else:
        blocks_raw = parsed.get("blocks") or []
        if not blocks_raw and "bbox" in parsed:
            blocks_raw = [parsed]
    blocks = []
    for b in blocks_raw:
        if not isinstance(b, dict):
            continue
        # 兼容多种 bbox 字段名：box / bbox / bbox_2d / bounding_box（qwen-vl-max 习惯用 bbox_2d）
        raw_box = b.get("box") or b.get("bbox") or b.get("bbox_2d") or b.get("bounding_box")
        if raw_box is None and isinstance(b.get("boxes"), (list, tuple)) and len(b["boxes"]) >= 4:
            raw_box = list(b["boxes"])[:4]
        box = _parse_box(raw_box)
        if not box:
            continue
        # 拿到的 bbox 字段名（用于诊断）
        blocks.append({"label": str(b.get("label", "")), "box": box})

    if not blocks:
        raise RuntimeError("视觉模型未识别出任何主体")

    def _area(b):
        x1, y1, x2, y2 = b["box"]
        return (x2 - x1) * (y2 - y1)

    blocks_sorted = sorted(blocks, key=_area, reverse=True)
    return {
        "subject": blocks_sorted[0],
        "all": blocks_sorted,
        "model": model,
        "provider": provider,
    }
