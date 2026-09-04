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

# 🎯 通用主体定位 prompt（抠图用）：同时支持人像/物品/文字主体。
_SUBJECT_PROMPT = (
    "你是一个图像主体定位助手。请分析这张图，找出用户最想一键抠出的主体。\n\n"
    "主体通常是图中最显眼、最突出的元素，例如：\n"
    "  - 人像（含全身/半身/大头照，包括头发、四肢外缘）\n"
    "  - 物品/产品（如商品、logo、图标、装饰元素）\n"
    "  - 文字标题（如海报主标题、一行醒目标语）\n\n"
    "请只返回一个 JSON 对象，不要任何额外文字、不要 markdown 代码块：\n"
    "{\n"
    '  "subject": {"label": "<人像/物品/文字/主体>", "box": [x1, y1, x2, y2]}\n'
    "}\n\n"
    "bbox 是边界框，坐标为归一化值 0~1，格式 [左上x, 左上y, 右下x, 右下y]。\n"
    "**紧贴外缘**：bbox 必须紧贴主体的最外缘像素，不要包含大面积背景空白。"
    "例如人像要框到头发梢、指尖，但不要多留背景；文字要框到笔画外缘。\n"
    "**返回唯一主体**：只返回一个最主要的主体（面积最大或视觉上最突出的那个）。"
)

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


def _grounding_prompt(user_prompt: str) -> str:
    """把用户的自然语言描述（「说扣什么」）转成 VLM 定位指令。

    让 VLM 不找「主主体」，而是**按描述定位那个特定对象**——这是豆包式
    「说扣什么就抠什么」的本地等价实现（云端 VLM 当作轻量 Grounding 用）。
    """
    p = (user_prompt or "").strip()
    if not p:
        return _SUBJECT_PROMPT
    return (
        "你是一个精确的图像主体定位助手。用户想从这张图里抠出的对象是：\n"
        f"「{p}」\n\n"
        "请按以下步骤定位：\n"
        "1. 先理解描述里的**方位词**（左下角/右上角/中间/左边等）——它们指画面的对应区域，"
        "目标一定在那个区域里；\n"
        "2. 在该区域里找到最符合描述的**较大完整物体**（排除小星星、圆点、装饰碎屑）；\n"
        "3. 返回它紧贴最外缘的边界框。\n\n"
        "请只返回一个 JSON 对象，不要任何额外文字、不要 markdown 代码块：\n"
        "{\n"
        '  "reason": "<一句话：目标在画面哪个区域、旁边有什么参照物>",\n'
        '  "subject": {"label": "<用一句话描述你框住的对象，含用户意图>", "box": [x1, y1, x2, y2]}\n'
        "}\n\n"
        "bbox 是边界框，坐标为归一化值 0~1，格式 [左上x, 左上y, 右下x, 右下y]。\n"
        "**紧贴外缘**：bbox 必须紧贴该对象的像素外缘，不要包含大面积背景空白；"
        "对象含多个人时只框用户描述的那一个。若图中找不到该对象，返回 "
        '{"subject": null}。'
    )


def _verify_prompt(user_prompt: str) -> str:
    """验证红框内容是否就是用户描述的对象（整图+红框标注，保留空间上下文）。"""
    return (
        f"用户想从这张图里抠出的对象是：「{user_prompt}」。\n"
        "图中用**红色矩形**标出了一个定位框。请判断：红框里的内容是否就是用户描述的对象？\n"
        "注意：① 描述里的方位词（左下角/右上角等）必须与红框在画面中的实际位置吻合；\n"
        "② 红框内的内容必须是描述的那个物体本体，而不是小星星/装饰图案。\n"
        "只返回 JSON，不要额外文字：\n"
        '{"match": true} 或 {"match": false, "reason": "<一句话说明红框里实际是什么、位置在哪>"}'
    )


# 方位词 → 归一化区域约束（重试时显式注入，绕开 VLM 的方位理解短板）
_REGION_HINTS: list[tuple[tuple[str, ...], tuple[float, float, float, float]]] = [
    (("左下", "左下角"), (0.0, 0.5, 0.45, 1.0)),
    (("右下", "右下角"), (0.55, 0.5, 1.0, 1.0)),
    (("左上", "左上角"), (0.0, 0.0, 0.45, 0.5)),
    (("右上", "右上角"), (0.55, 0.0, 1.0, 0.5)),
    (("左", ), (0.0, 0.0, 0.4, 1.0)),
    (("右", ), (0.6, 0.0, 1.0, 1.0)),
    (("顶部", "上方", "上边"), (0.0, 0.0, 1.0, 0.4)),
    (("底部", "下方", "下边"), (0.0, 0.6, 1.0, 1.0)),
    (("中间", "中央", "中心"), (0.25, 0.25, 0.75, 0.75)),
]


def _region_hint(prompt: str) -> tuple[float, float, float, float] | None:
    """从用户描述解析方位词，返回归一化区域 [x1,y1,x2,y2]；无方位词返回 None。"""
    p = prompt or ""
    for keys, region in _REGION_HINTS:
        if any(k in p for k in keys):
            return region
    return None


def detect_subject(image_path: str, prompt: str | None = None, max_side: int = 1024, timeout: int = 60) -> dict:
    """调 VLM 定位主体，返回 {subject, all, model, provider}。失败抛 RuntimeError。

    prompt：用户自然语言描述（「说扣什么」）。给定时 VLM 按描述定位特定对象，
        而非默认的主主体；为空则定位画面主主体。
    """
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
    user_text = _grounding_prompt(prompt) if (prompt or "").strip() else _SUBJECT_PROMPT
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
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
    #   {"subject": {"label","box"}}    ← 新主体定位格式（detect_subject 主格式）
    #   {"blocks": [{"label","bbox"}]}  ← 旧文字/元素定位格式（兼容兜底）
    #   {"blocks": [{"label","box"}]}   ← 老格式兜底
    #   [{"label","bbox"}]              ← 直接数组
    #   {"label","bbox"}                ← 单个对象（裸返回）
    if isinstance(parsed, dict) and "subject" in parsed:
        # 新格式：直接提取 subject，并把它放进 all 列表
        subj = parsed["subject"]
        if not subj or not isinstance(subj, dict):
            # 描述性定位（「说扣什么」）时 VLM 可能返回 {"subject": null} 表示没找到该对象
            raise RuntimeError(
                f"视觉模型未找到你描述的对象「{(prompt or '')[:40]}」｜原始回复: {content[:200]}"
            )
        raw_box = (
            subj.get("box") or subj.get("bbox") or subj.get("bbox_2d")
            or subj.get("bounding_box") or subj.get("boundingBox")
            or subj.get("region") or subj.get("rect")
            or subj.get("coordinates") or subj.get("location")
            or subj.get("area")
        )
        if raw_box:
            try:
                _vals = [float(v) for v in raw_box[:4]]
            except Exception:
                _vals = []
            if _vals:
                if max(_vals) > 1.5 and _imgW and _imgH:
                    _vals = [_vals[0] / _imgW, _vals[1] / _imgH, _vals[2] / _imgW, _vals[3] / _imgH]
                box = _parse_box(_vals)
                if box:
                    label = str(subj.get("label", "主体"))
                    return {"subject": {"label": label, "box": box},
                            "all": [{"label": label, "box": box}],
                            "model": model, "provider": provider}
        blocks_raw = []
    elif isinstance(parsed, list):
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


def detect_subject_checked(image_path: str, prompt: str | None = None,
                           max_side: int = 1024, timeout: int = 60,
                           max_retries: int = 1) -> dict:
    """带验证的「说扣什么」定位：定位 → 裁片验证 → 不匹配则强调方位词重试。

    背景：qwen-vl 对「左下角的画架」这类**方位词+特定物体**描述会框错位置
    （实测把左下角画架框到了中部红星星）。本函数在定位后把框内裁片再喂给
    VLM 自检「这是不是用户要的对象」，不匹配则带更强方位提示重试一次。
    裁片验证调用很小（512px），成本可忽略；定位失败时行为与 detect_subject 一致。
    """
    import base64 as _b64mod
    import io as _io
    from PIL import Image as _PIL

    det = detect_subject(image_path, prompt, max_side=max_side, timeout=timeout)
    p = (prompt or "").strip()
    if not p:
        return det

    for attempt in range(max_retries + 1):
        box = det["subject"]["box"]
        # 验证方式：整图缩到 768px + 红框标注（保留空间上下文，VLM 才能判断
        # 方位词是否吻合；裸裁片没有位置信息，VLM 会胡乱放行）
        try:
            with _PIL.open(image_path) as im:
                im.load()
                W, H = im.size
                x1, y1, x2, y2 = box
                vis = im.convert("RGB").copy()
                vis.thumbnail((768, 768))
                from PIL import ImageDraw as _ID
                sc = vis.width / W
                _ID.Draw(vis).rectangle(
                    [int(x1 * W * sc), int(y1 * H * sc), int(x2 * W * sc), int(y2 * H * sc)],
                    outline=(255, 0, 0), width=max(3, vis.width // 200),
                )
        except Exception:
            return det
        buf = _io.BytesIO()
        vis.save(buf, format="PNG")
        cb64 = _b64mod.b64encode(buf.getvalue()).decode()

        cfg = get_vision_config()
        url = (cfg.get("base_url") or "").strip().rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {(cfg.get('api_key') or '').strip()}"}
        payload = {
            "model": (cfg.get("model") or "").strip(),
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": _verify_prompt(p)},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{cb64}"}},
                ]},
            ],
            "temperature": 0.0,
        }
        matched = True
        reason = ""
        try:
            resp = _post_json(url, headers, payload, timeout=timeout)
            content = resp["choices"][0]["message"]["content"]
            parsed = _extract_json(content)
            if isinstance(parsed, dict) and parsed.get("match") is False:
                matched = False
                reason = str(parsed.get("reason", ""))[:120]
        except Exception:
            matched = True  # 验证失败不阻塞主流程

        if matched:
            return det
        if attempt < max_retries:
            # 重试：把方位词解析成显式坐标范围注入（绕开 VLM 方位理解短板）
            region = _region_hint(p)
            extra = f"（注意：「{p}」中的方位词指画面对应区域；目标是该区域里较大的完整物体，不是小星星/圆点等装饰。上一次定位框错了对象：{reason}）"
            if region:
                extra += (f" 显式约束：目标边界框必须完全落在归一化区域 "
                          f"x∈[{region[0]:.2f},{region[2]:.2f}]、y∈[{region[1]:.2f},{region[3]:.2f}] 内，"
                          "区域之外的一切物体一律忽略。")
            try:
                det = detect_subject(image_path, p + extra, max_side=max_side, timeout=timeout)
            except Exception:
                return det
    return det


def tight_box_on_composite(sheet_png_b64: str, user_prompt: str,
                           timeout: int = 60) -> list[float] | None:
    """二次紧贴框：在白底抠图成品上给出「只含用户描述对象本体」的归一化框。

    背景：横幅/装饰可能与目标物理相连（连通域拆不开），矩形定位框也无法排除
    框内邻居。此步让 VLM 在干净的成品图上做纯语义+几何判断。
    失败返回 None（调用方保留原结果）。
    """
    try:
        cfg = get_vision_config()
        url = (cfg.get("base_url") or "").strip().rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {(cfg.get('api_key') or '').strip()}"}
        text = (
            f"用户想抠出的对象是：「{user_prompt}」。\n"
            "这张白底图是抠图结果，里面除了目标本体，可能还混有相邻的其他元素"
            "（如黑底小标题横幅、星星装饰等）。\n"
            "请给出**只属于目标本体**（含其描边、外轮廓、投影）的紧贴边界框，"
            "排除其他一切元素。坐标为归一化 0~1，[左上x, 左上y, 右下x, 右下y]。\n"
            '只返回 JSON：{"box": [x1,y1,x2,y2]}；找不到则 {"box": null}'
        )
        payload = {
            "model": (cfg.get("model") or "").strip(),
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{sheet_png_b64}"}},
                ]},
            ],
            "temperature": 0.0,
        }
        resp = _post_json(url, headers, payload, timeout=timeout)
        content = resp["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        if isinstance(parsed, dict) and parsed.get("box"):
            vals = [float(v) for v in list(parsed["box"])[:4]]
            if all(0.0 <= v <= 1.0 for v in vals) and vals[2] > vals[0] and vals[3] > vals[1]:
                return vals
    except Exception:
        return None
    return None


def select_matching_components(sheet_png_b64: str, user_prompt: str, count: int,
                               timeout: int = 60) -> list[int]:
    """部件选择：给编号部件拼图，让 VLM 选出匹配用户描述的部件编号。

    背景：矩形定位框+云端抠图都无法区分「大标题 vs 紧挨的小标题横幅」——
    框内它们是连片前景。按连通域拆部件后让 VLM 做部件级选择。
    任何异常返回 []（调用方保留原结果，不引入回归）。
    """
    if count < 2:
        return []
    try:
        cfg = get_vision_config()
        url = (cfg.get("base_url") or "").strip().rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {(cfg.get('api_key') or '').strip()}"}
        text = (
            f"用户想抠出的对象是：「{user_prompt}」。\n"
            f"这张拼图里有 {count} 个从定位框内拆出的独立部件，每个左上角有红色编号 1~{count}。\n"
            "请判断哪些编号的部件属于用户要抠的对象（语义和范围都要符合，"
            "比如「文字大标题」不包括黑色小标题横幅条、不包括星星装饰）。\n"
            "只返回 JSON，不要额外文字：\n"
            '{"keep": [编号, ...]}'
        )
        payload = {
            "model": (cfg.get("model") or "").strip(),
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{sheet_png_b64}"}},
                ]},
            ],
            "temperature": 0.0,
        }
        resp = _post_json(url, headers, payload, timeout=timeout)
        content = resp["choices"][0]["message"]["content"]
        parsed = _extract_json(content)
        if isinstance(parsed, dict) and isinstance(parsed.get("keep"), list):
            out = []
            for v in parsed["keep"]:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if 1 <= iv <= count:
                    out.append(iv)
            return out
    except Exception:
        return []
    return []


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

