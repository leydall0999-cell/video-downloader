"""LLM 解说词生成：把转写稿提炼成口播脚本。

用法（被 process.py 的 auto_script 自动调用，不单独使用）：
    from llm_script import llm_script
    script = llm_script(transcript_path, script_path, title=..., voice=...)

环境变量：
    LLM_API_KEY     - 必填，兼容 OpenAI API 的 Key
    LLM_BASE_URL    - 可选，默认 https://api.openai.com/v1（DeepSeek/通义千问/Ollama 改这里）
    LLM_MODEL       - 可选，默认 gpt-4o-mini（推荐 deepseek-chat 便宜好用）
    LLM_MAX_TOKENS  - 可选，默认 4096
"""
import json
import os
import re
import urllib.request
import urllib.error
import urllib.parse

import commentary_options as copts


# 解说口吻风格表：每个风格对应「系统提示里的风格指令」「LLM 温度」「默认联动音色」。
# 新增风格只需在此加一项，并在前端 index.html 的 comStyle radio、app.js 的 STYLE_VOICE 同步。
STYLE_CONFIG = {
    "none": {
        "label": "默认",
        "temperature": 0.7,
        "voice": "zh-CN-XiaoxiaoNeural",
        "prompt": "",
    },
    "funny": {
        "label": "搞笑",
        "temperature": 0.4,
        "voice": "zh-CN-YunxiaNeural",  # 青年男声：年轻活泼
        "prompt": (
            "【解说风格·搞笑】用幽默调侃的口吻解说：玩梗、夸张比喻、制造反差、自嘲式吐槽，"
            "让观众会心一笑。笑点必须建立在对内容真实解读之上，不许为了搞笑编造剧情或歪曲事实。"
        ),
    },
    "serious": {
        "label": "严肃",
        "temperature": 0.3,
        "voice": "zh-CN-YunyangNeural",  # 新闻腔男声
        "prompt": (
            "【解说风格·严肃】用冷静、克制、客观的口吻解说：重事实、讲逻辑、少情绪渲染，"
            "把事情讲清楚、讲准确，不玩梗、不煽情。"
        ),
    },
    "domineering": {
        "label": "霸道",
        "temperature": 0.35,
        "voice": "zh-CN-YunjianNeural",  # 沉稳男声：低沉笃定
        "prompt": (
            "【解说风格·霸道】用强势笃定的口气解说：多用肯定句下判断、给结论，节奏干脆利落，"
            "像在替观众做主、把观点砸实，不模棱两可。"
        ),
    },
    "angry": {
        "label": "愤青",
        "temperature": 0.45,
        "voice": "zh-CN-YunyangNeural",  # 新闻腔男声
        "prompt": (
            "【解说风格·愤青】用代入感强、带情绪锋芒的口吻解说：为不公鸣不平、为观众抱不平，"
            "敢吐槽、敢批点，但立场要站得住、指向要明确。"
        ),
    },
    "suspense": {
        "label": "悬疑",
        "temperature": 0.6,
        "voice": "zh-CN-YunjianNeural",  # 沉稳男声：低沉神秘
        "prompt": (
            "【解说风格·悬疑】用层层递进、留钩子、放慢节奏的口吻解说：先抛谜题、压低信息、再逐步揭晓，"
            "制造「接下来会发生什么」的紧张感与期待感。"
        ),
    },
    "healing": {
        "label": "治愈",
        "temperature": 0.4,
        "voice": "zh-CN-XiaoxiaoNeural",  # 温柔女声
        "prompt": (
            "【解说风格·治愈】用温柔、松弛、有陪伴感的口吻解说：语速舒缓、多共情、像在轻声分享，"
            "让观众感到被理解、被安抚，不急不躁。"
        ),
    },
    "sarcastic": {
        "label": "毒舌",
        "temperature": 0.4,
        "voice": "zh-CN-YunyangNeural",  # 新闻腔男声：犀利冷幽默
        "prompt": (
            "【解说风格·毒舌】用犀利、一针见血、带冷幽默反讽的口吻解说：精准戳破套路与荒谬，"
            "金句频出、嘴不留情，但讽刺要有理有据、不人身攻击。"
        ),
    },
}


def _style_prompt(style: str) -> str:
    """返回某风格的口吻指令（none/未知返回空串）。"""
    cfg = STYLE_CONFIG.get(style)
    if not cfg:
        return ""
    return cfg.get("prompt", "")


def _web_search(query: str, n: int = 5):
    """尽力而为的联网搜索：优先 SerpAPI，其次 Bing，再次 DuckDuckGo lite。
    全程异常吞掉，失败返回空列表（调用方会退化到仅用模型自身知识）。"""
    results = []
    try:
        key = (os.environ.get("SERPAPI_KEY") or "").strip()
        if key:
            url = ("https://serpapi.com/search.json?engine=google&num=%d"
                   "&api_key=%s&q=%s" % (n, urllib.parse.quote(key), urllib.parse.quote(query)))
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            for o in data.get("organic_results", [])[:n]:
                results.append((o.get("title", ""), o.get("snippet", "")))
            return results
        bing = (os.environ.get("BING_SUBSCRIPTION_KEY") or "").strip()
        if bing:
            url = ("https://api.bing.microsoft.com/v7.0/search?q=%s&count=%d"
                   % (urllib.parse.quote(query), n))
            req = urllib.request.Request(url, headers={"Ocp-Apim-Subscription-Key": bing})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            for o in data.get("webPages", {}).get("value", [])[:n]:
                results.append((o.get("name", ""), o.get("snippet", "")))
            return results
        # DuckDuckGo lite（无需 key，但可能被限流/改版）
        url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", "ignore")
        snippets = re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', html, re.S)
        titles = re.findall(r'class="result-link"[^>]*>(.*?)</a>', html, re.S)
        for i in range(min(n, len(snippets))):
            t = re.sub(r"<.*?>", "", titles[i]) if i < len(titles) else ""
            s = re.sub(r"<.*?>", "", snippets[i])
            results.append((t.strip(), s.strip()))
    except Exception as e:
        print(f"  [警告] 联网搜索失败（将仅用模型自身知识）: {e}")
    return results


def _call_llm(system_prompt: str, user_prompt: str, temperature: float = 0.7) -> str:
    """调 OpenAI 兼容 API，返回 assistant 文本。"""
    api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("LLM_APIKEY") or "").strip()
    if not api_key:
        raise RuntimeError("LLM_API_KEY 未设置")

    base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini").strip()
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "4096") or 4096)

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"LLM API 返回 {e.code}: {body_text}")
    except Exception as e:
        raise RuntimeError(f"LLM API 调用失败: {e}")


def _build_system_prompt(family, depth, src, intro, style="none"):
    """按（解说家族, 深浅, 高光来源, 是否片头高光）组合出 system prompt。

    family: "highlights"（只挑高光叠加解说） / "full"（全片连贯解说）
    depth:  "deep"（深入/自由发挥） / "normal"（普通简洁）
    src:    "ai"（AI 自动挑高光） / "manual"（人工在审核面板挑，多给候选）
    intro:  是否把最精彩一段剪到片头当钩子
    """
    if family == "highlights":
        base = """你是一个资深的短视频「高光解说」写手。你的任务不是复述全片，而是**只挑选视频里最精彩、最值得讲的高光时刻**，为它们写出解说词。

**挑选高光的标准（通读全文后判断）：**
- 戏剧冲突、转折、反转、高潮
- 金句、名场面、情绪高点、爆笑/震撼瞬间
- 关键信息、硬核知识点、出人意料的结论
- 观众最可能反复观看、最想听你"讲透"的片段

**写解说词的要求：**
1. 不止复述画面，要**补充背景、分析因果、点出深意、给出你的观点/吐槽/钩子**，把这一段讲清楚。
2. 语气有观点、有节奏、有网感——可以犀利、可以玩梗，但信息要准。
3. 高光之间**允许大段空白**——平淡、铺垫、重复的片段**不要**写解说。
4. 旁白最终会**按单行字幕在画面上轮播**（过长会自动切短句），所以你只需保证内容精彩，不用刻意压成一句话。"""
        if depth == "normal":
            base += "\n5. 解说要**普通、简洁、点到为止**，把事情说清楚即可，不必过度延伸或注水。"
        else:
            base += "\n5. 解说要**深入发挥**：单段可较长（建议 15~40 秒），补充背景与分析，把这一段讲透。"
        if src == "manual":
            base += ("\n\n**高光来源=人工：** 高光由**人工在审核面板后续挑选**，因此请为时间轴上**尽量多的精彩候选点**都写出解说（覆盖更全、候选更多），"
                     "不要只保留一两处；用户会决定保留哪些。")
        else:
            base += "\n\n**高光来源=AI：** segments 里**只放真正的高光时刻**，不要为全片均匀铺满。"
        base += """

**输出格式（严格的 JSON）：**
```json
{
  "title": "视频标题",
  "voice": "zh-CN-XiaoxiaoNeural",
  "style": "高光解说",
  "segments": [
    {"start": 12.0, "end": 18.0, "narration": "（解说词：背景+分析+观点）", "note": "高光"},
    {"start": 95.0, "end": 103.0, "narration": "（另一处高光的解说词）", "note": "高光"}
  ]
}
```
**重要：**
- start/end 必须从转写稿的时间戳里选择，确保解说能对上画面（可略放宽到该时刻前后）。
- 旁白文本要写完整句子，不要省略。"""
        if intro:
            base += "\n- 在挑出的高光里，把**最精彩、最能勾人的那一段**单独标为 note=\"开场钩子\"，成片会把它剪到**最开头**当钩子；其余高光 note 仍为 \"高光\"。"
        style_block = _style_prompt(style)
        if style_block:
            base += "\n\n" + style_block + "\n（以上为本条解说须贯穿全文的风格基调，请全程保持此口吻。）"
    else:  # full
        base = """你是一个风格鲜明的「全片解说」写手，对全片做连贯解说（开场钩子 + 主体 + 结尾引导），不要有空白冷场。

**要求：**
1. 通读转写稿，对**全片**做连贯解说。
2. **字幕铁律：每一段旁白在画面上只能显示「一行」字幕，绝不允许同一时刻出现两行或更多字幕。** 旁白文本请控制在一句话内（中文约 20~30 字），不要写成长段落。
3. 字幕位置与原视频字幕保持「同一行」（底部居中）；原字幕出现时会被羽化处理，不会与你抢版面。"""
        if depth == "normal":
            base += "\n4. 普通连贯解说，把事情讲清楚、节奏顺畅即可，不必刻意玩梗或过度延伸。"
        else:
            base += "\n4. 高度自由发挥：可以补充背景、延伸观点、玩梗吐槽、制造悬念，**不要干巴巴念稿**。"
        if src == "manual":
            base += "\n- 高光来源=人工：尽量为全片每个有意义的段落都写解说，方便人工在审核面板增删取舍。"
        base += """

**输出格式（严格的 JSON）：**
```json
{
  "title": "视频标题",
  "voice": "zh-CN-XiaoxiaoNeural",
  "style": "全片解说",
  "segments": [
    {"start": 0.0, "end": 5.0, "narration": "一句话钩子旁白", "note": "开场"},
    {"start": 5.0, "end": 12.0, "narration": "一句话主体旁白", "note": "核心"}
  ]
}
```
**重要：** start/end 取自转写稿时间戳；narration 必须是**单行短句**（一行字幕装得下）。"""
        if intro:
            base += "\n- 把**最精彩、最能勾人的那一段**标为 note=\"开场钩子\"，成片会把它剪到**最开头**当钩子。"
        style_block = _style_prompt(style)
        if style_block:
            base += "\n\n" + style_block + "\n（以上为本条解说须贯穿全文的风格基调，请全程保持此口吻。）"
    return base


def llm_script(transcript_path: str, script_path: str, title: str = "",
               voice: str = "", mode: str = None, commentary_type: str = None,
               highlight_source: str = None, intro_highlight: bool = None,
               web: bool = None, retain_pct=None, skip_intro_outro: bool = None,
               style: str = "none") -> str:
    """核心：读转写稿 → 调 LLM 生成解说脚本 JSON → 写回。

    剪辑选项（与 process.py / edit_ffmpeg.py 共用 commentary_options 模型）：
      commentary_type：deep_hl / normal_hl / full_normal / full_deep
      highlight_source：ai(AI 自动挑高光) / manual(人工在审核面板挑)
      intro_highlight：是否把最精彩一段剪到片头当钩子
      web：是否联网搜索资料辅助发挥（独立开关，任何类型都可开）
      retain_pct / skip_intro_outro：保留时长百分比 / 跳过片头片尾
      mode：旧版三选一字段，仅用于兼容老调用方（会被新选项覆盖）
    返回 script_path。

    生成的 script.json 会同时写入「新 options（渲染层优先采用）」与「旧 mode（兼容老 worker）」，
    这样用户在审核面板改完选项后，build() 读回脚本仍能拿到当时的选择。
    """
    segs = json.load(open(transcript_path, encoding="utf-8"))
    opts = copts.resolve(script=None, mode=mode, commentary_type=commentary_type,
                         highlight_source=highlight_source, intro_highlight=intro_highlight,
                         web=web, retain_pct=retain_pct, skip_intro_outro=skip_intro_outro)
    ctype = opts["commentary_type"]
    family = opts["family"]
    depth = opts["depth"]
    web_on = opts["web"]
    intro = opts["intro_highlight"]
    src = opts["highlight_source"]

    # 把转写稿拼成纯文本（带时间戳），方便 LLM 理解
    transcript_text = "\n".join(
        f"[{s['start']:.1f}s-{s['end']:.1f}s] {s.get('text', '').strip()}"
        for s in segs if s.get("text", "").strip()
    )
    if not transcript_text.strip():
        raise RuntimeError("转写稿为空，无法生成解说词")

    total_duration = segs[-1]["end"] if segs else 0
    title_hint = f'视频标题：{title}\n' if title else ""

    system_prompt = _build_system_prompt(family, depth, src, intro, style=style)
    mode_label = copts.COMMENTARY_TYPES[ctype]["label"]

    user_prompt = f"""{title_hint}视频总时长：{total_duration:.0f}秒
解说模式：{mode_label}
"""
    if web_on:
        refs = []
        for q in [title or "本视频主题", f"{title or ''} 解说 解析", f"{title or ''} 讲了什么 亮点"]:
            if not q.strip():
                continue
            refs += _web_search(q, 3)
            if len(refs) >= 6:
                break
        ref_block = "\n".join(f"- {t}：{s}" for t, s in refs[:8]) if refs \
            else "（联网搜索暂不可用，请基于你的知识自由发挥）"
        user_prompt += f"\n联网搜索到的相关资料（仅供参考，可自由发挥）：\n{ref_block}\n"
    user_prompt += f"""
转写稿（带时间戳）：
{transcript_text}

请根据上面的转写稿，生成这个视频的解说脚本 JSON。"""

    print(f"  [LLM] 调用 {os.environ.get('LLM_MODEL', 'gpt-4o-mini')} 生成解说词"
          f"（{ctype} · 风格={STYLE_CONFIG.get(style, {}).get('label', style)}{' · 联网' if web_on else ''}）…")
    style_temp = STYLE_CONFIG.get(style, {}).get("temperature", 0.7)
    result = _call_llm(system_prompt, user_prompt, temperature=style_temp)

    # 解析 JSON（LLM 可能用 ```json ... ``` 包着，去掉）
    result = result.strip()
    if result.startswith("```"):
        result = result.split("\n", 1)[-1] if "\n" in result else result[3:]
        if result.endswith("```"):
            result = result[:-3]
        result = result.strip()

    script = json.loads(result)

    # 保证输出格式与 pipeline 期望一致
    if "title" not in script:
        script["title"] = title or os.path.splitext(os.path.basename(transcript_path))[0]
    if voice:
        script["voice"] = voice
    elif style in STYLE_CONFIG and STYLE_CONFIG[style].get("voice"):
        # 风格联动音色：未显式指定 voice 时，默认套用该风格的推荐音色
        script["voice"] = STYLE_CONFIG[style]["voice"]
    if "voice" not in script:
        script["voice"] = os.environ.get("VOICE", "zh-CN-XiaoxiaoNeural")

    # 新选项（渲染层优先采用），与旧 mode（兼容老 worker）一并落盘
    script["options"] = {
        "commentary_type": ctype,
        "highlight_source": src,
        "intro_highlight": intro,
        "web": web_on,
        "retain_pct": opts["retain_pct"],
        "skip_intro_outro": opts["skip_intro_outro"],
    }
    script["mode"] = copts.legacy_mode(ctype, intro)

    default_note = "高光" if family == "highlights" else "全片"
    for s in script.get("segments", []):
        if "note" not in s or not s.get("note"):
            s["note"] = default_note

    json.dump(script, open(script_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    seg_count = len(script.get("segments", []))
    style = script.get("style", "?")
    print(f"  [LLM] 解说词生成完成：{seg_count} 段，风格={style} → {script_path}")
    return script_path
