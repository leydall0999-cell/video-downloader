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
    "你是一个精确的图像要素定位助手。请分析这张图，识别图中的文字和图形要素。\n\n"
    "请只返回一个 JSON 对象，不要任何额外文字、不要 markdown 代码块：\n"
    "{\n"
    '  "blocks": [\n'
    '    {"label": "<主标题|副标题|logo|装饰|背景>", "bbox": [x1, y1, x2, y2]}\n'
    "  ]\n"
    "}\n"
    "label 命名约定：\n"
    '  - 主标题 = 图中最突出的核心文字标题（通常是字号最大、最显眼的那一行文字）\n'
    '  - 副标题 = 主标题下方/旁边的较小辅助文字、说明性文字\n'
    '  - logo/装饰 = 图形、图标、装饰元素\n'
    '  - 背景 = 大面积背景色块\n'
    "bbox 是边界框，坐标为归一化值 0~1，格式 [左上x, 左上y, 右下x, 右下y]。\n"
    "**重要约束**：主标题的 bbox 必须**只包含主标题文字本身**，不要包含紧贴的副标题、"
    "装饰、笔触、涂鸦、阴影或文字描边。同理副标题/装饰也只框各自本身，不要相互包含。\n"
    "**覆盖完整一行**：如果主标题是一行连续的多个字（如「开学画力觉醒」5 个字），"
    "bbox 必须框住**这一行全部的字**——左边界到第一个字的最左缘、右边界到最后一个字的最右缘，"
    "上/下边界到这一行文字的最高/最低处。**绝不能只框住其中一两个字**。"
    "请先在脑中确认这行标题的首字与末字位置，再给出覆盖整行的 bbox。"
)

_SYSTEM = "你是一个严谨的图像分析助手，只输出要求的 JSON，不做任何额外解释。"

# 📝 文字检测 prompt：找图中所有独立文字元素（含主/副标题、按钮文字、贴纸/卡片内文字）
_TEXT_PROMPT = (
    "你是一个海报/图片文字定位助手。请找出图中**所有独立成组的文字元素**："
    "大字标题、副标题、按钮/贴纸/卡片内的字、徽标字等，逐条列出。\n\n"
    "请只返回一个 JSON 对象，不要任何额外文字、不要 markdown 代码块：\n"
    "{\n"
    '  "blocks": [\n'
    '    {"label": "<该组文字的用途或内容，如 主标题/副标题/立即解锁按钮/卡片标题>", "bbox": [x1, y1, x2, y2]}\n'
    "  ]\n"
    "}\n"
    "bbox 是边界框，坐标为归一化值 0~1，格式 [左上x, 左上y, 右下x, 右下y]，\n"
    "**只框文字外缘**：上下左右边界到笔画外缘 ≤ 3 px；不要包含文字的底色卡、"
    "装饰边框、阴影、贴纸外形或别的文字组。若主标题明显，把它列在 blocks 的第一条。"
    "找不到任何文字时返回 {\"blocks\": []}。"
)



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
    # 读原图尺寸：qwen-vl-max 有时会按原图像素坐标返回 bbox（>1024），
    # 而不是归一化坐标（[0,1]）；检测后按 (W, H) 归一化统一处理。
    try:
        from PIL import Image as _PIL
        with _PIL.open(image_path) as _im:
            _imgW, _imgH = _im.size
    except Exception:
        _imgW, _imgH = None, None
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
        # 兼容多种 bbox 字段名：box / bbox / bbox_2d / bounding_box / boundingBox /
        # region / rect / coordinates / location / area / detections（各家 VLM 命名习惯不同）
        raw_box = (
            b.get("box") or b.get("bbox") or b.get("bbox_2d") or b.get("bounding_box")
            or b.get("boundingBox") or b.get("region") or b.get("rect")
            or b.get("coordinates") or b.get("location") or b.get("area")
        )
        if raw_box is None and isinstance(b.get("boxes"), (list, tuple)) and len(b["boxes"]) >= 4:
            raw_box = list(b["boxes"])[:4]
        # 兜底：单值字段（x1/y1/x2/y2 散在）或 polygon points 取 bbox
        if raw_box is None:
            pts = b.get("points") or b.get("polygon")
            if isinstance(pts, (list, tuple)) and len(pts) >= 2:
                xs, ys = [], []
                for p in pts:
                    if isinstance(p, (list, tuple)) and len(p) >= 2:
                        xs.append(float(p[0])); ys.append(float(p[1]))
                if xs and ys:
                    raw_box = [min(xs), min(ys), max(xs), max(ys)]
        if raw_box is None:
            continue
        # **坐标自动归一化**：qwen-vl-max 有时按原图像素坐标（>1024）返回，
        # 有时按归一化坐标（[0,1]）返回。如果任何坐标 > 1.5 当作像素坐标，
        # 按 (W, H) 归一化到 [0,1]，避免被 _parse_box 当作越界夹紧退化掉。
        try:
            _vals = [float(v) for v in raw_box[:4]]
        except Exception:  # noqa: BLE001
            continue
        if max(_vals) > 1.5 and _imgW and _imgH:
            _vals = [_vals[0] / _imgW, _vals[1] / _imgH, _vals[2] / _imgW, _vals[3] / _imgH]
        box = _parse_box(_vals)
        if not box:
            continue
        blocks.append({"label": str(b.get("label", "")), "box": box})

    if not blocks:
        # 诊断：把 qwen 原始回复写到错误信息里，便于排查字段命名
        snippet = (content or "")[:300].replace("\n", " ")
        raise RuntimeError(f"视觉模型未识别出任何主体｜原始回复: {snippet}")

    def _area(b):
        x1, y1, x2, y2 = b["box"]
        return (x2 - x1) * (y2 - y1)

    blocks_sorted = sorted(blocks, key=_area, reverse=True)

    # **V10.2 主标题优先匹配**：qwen 可能返回多个 blocks（含主/副/装饰），
    # 之前用面积最大的——但"主+副+装饰"组合框面积也很大，反而把副标题/装饰框进去了。
    # 现在优先取 label 含"主标题"/"title"/"main"的 block；只有"装饰/副/logo"标签时才
    # 退回面积最大，避免"qwen 把整个中央区域标成主标题"时把不要的副标题/装饰一起带进来。
    _PRIMARY_KEYS = ("主标题", "main title", "title", "main")
    primary = next(
        (b for b in blocks_sorted
         if any(k in b.get("label", "").lower() for k in _PRIMARY_KEYS)),
        None,
    )
    subject = primary or blocks_sorted[0]
    return {
        "subject": subject,
        "all": blocks_sorted,
        "model": model,
        "provider": provider,
    }


def detect_text_blocks(image_path: str, max_side: int = 1024, timeout: int = 60) -> list[dict]:
    """📝 文字检测：调 VLM 找出图中所有独立文字元素，返回
    [{"label": "...", "box": [x1,y1,x2,y2] 归一化}, ...]（面积降序）。

    用途：🧲 智能选块 hover/选中的文字候选——BiRefNet 显著性对"装饰风字体
    （白字+描边+半透明投影）"天然漏识别（整图 alpha<45 零星不连通、不成块），
    VLM 却能一眼定位"主标题/按钮文字"，补足元素块列表让主标题可选。
    未配置视觉模型 / 调用失败 → 抛 RuntimeError（调用方自行降级，不影响 BiRefNet 块）。
    """
    cfg = get_vision_config()
    key = (cfg.get("api_key") or "").strip()
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    model = (cfg.get("model") or "").strip()

    if not key or not base_url or not model:
        raise RuntimeError("视觉模型未配置")

    b64 = _compress_to_b64(image_path, max_side)
    try:
        from PIL import Image as _PIL
        with _PIL.open(image_path) as _im:
            _imgW, _imgH = _im.size
    except Exception:
        _imgW, _imgH = None, None
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
                    {"type": "text", "text": _TEXT_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            },
        ],
        "temperature": 0.1,
    }
    try:
        resp = _post_json(url, headers, payload, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"文字检测调用失败：{e}") from e
    try:
        content = resp["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"文字检测返回格式异常：{str(resp)[:200]}")

    parsed = _extract_json(content)
    if isinstance(parsed, list):
        blocks_raw = parsed
    else:
        blocks_raw = parsed.get("blocks") or []
    blocks = []
    for b in blocks_raw:
        if not isinstance(b, dict):
            continue
        raw_box = (
            b.get("box") or b.get("bbox") or b.get("bbox_2d") or b.get("bounding_box")
            or b.get("boundingBox") or b.get("region") or b.get("rect")
            or b.get("coordinates") or b.get("location") or b.get("area")
        )
        if raw_box is None and isinstance(b.get("boxes"), (list, tuple)) and len(b["boxes"]) >= 4:
            raw_box = list(b["boxes"])[:4]
        if raw_box is None:
            continue
        try:
            _vals = [float(v) for v in raw_box[:4]]
        except Exception:  # noqa: BLE001
            continue
        if max(_vals) > 1.5 and _imgW and _imgH:
            _vals = [_vals[0] / _imgW, _vals[1] / _imgH, _vals[2] / _imgW, _vals[3] / _imgH]
        box = _parse_box(_vals)
        if not box:
            continue
        blocks.append({"label": str(b.get("label", "")).strip() or "文字", "box": box})

    def _area(b):
        x1, y1, x2, y2 = b["box"]
        return (x2 - x1) * (y2 - y1)

    blocks.sort(key=_area, reverse=True)
    return blocks

