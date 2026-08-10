"""剪辑选项模型：解说类型 / 高光来源 / 开头高光 / 跳过片头片尾 / 保留时长百分比。

链路上三个环节共用这一张表，避免同一个选项在各处被解释成不同东西：
    process.py（命令行入参） → llm_script.py（写什么解说词） → edit_ffmpeg.py（怎么渲染）

【默认必须项，不做成开关】
    单行字幕（同一时刻只一行）、字幕与原字幕同行、只羽化原字幕不羽化画面、
    解说时压低原声、解说词生成后可人工审核。

【固定选择】
    skip_intro_outro  是否跳过片头片尾
    retain_pct        保留全片时长百分比（手动输入 10~100，None/100 = 不裁）

【三个选择方案】
    1. 高光来源 highlight_source: ai(AI 自动挑) / manual(人工在审核面板选)
    2. 解说类型 commentary_type:  deep_hl / normal_hl / full_normal / full_deep
    3. 开头高光 intro_highlight:  True(片头插入精彩片段) / False(不插入)
"""

# commentary_type -> 渲染家族(family) + 解说深浅(depth)
COMMENTARY_TYPES = {
    "deep_hl": {"family": "highlights", "depth": "deep",
                "label": "高光处叠加深度解说"},
    "normal_hl": {"family": "highlights", "depth": "normal",
                  "label": "高光部分普通解说"},
    "full_normal": {"family": "full", "depth": "normal",
                    "label": "全片普通解说"},
    "full_deep": {"family": "full", "depth": "deep",
                  "label": "全片深入解说"},
}

DEFAULT_TYPE = "deep_hl"
HIGHLIGHT_SOURCES = ("ai", "manual")

# 旧版 mode 字段（三选一）→ 新选项，保证老脚本/老调用方不炸
_LEGACY_MODE = {
    "highlights": ("deep_hl", False, False),
    "highlights_intro": ("deep_hl", True, False),
    "full_web": ("full_deep", False, True),
    "full": ("deep_hl", False, False),  # 更早的 full 模式已移除
}


def normalize_type(value):
    """把任意输入规整成合法的 commentary_type；无法识别时返回 None。"""
    v = (value or "").strip()
    if v in COMMENTARY_TYPES:
        return v
    return None


def legacy_mode(commentary_type, intro_highlight=False):
    """反向映射回旧版 mode 字段，供仍按 mode 走的接口/worker 使用。"""
    fam = COMMENTARY_TYPES.get(commentary_type, COMMENTARY_TYPES[DEFAULT_TYPE])["family"]
    if fam == "full":
        return "full_web"
    return "highlights_intro" if intro_highlight else "highlights"


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_pct(value):
    """保留时长百分比：合法范围 10~100，None/非法/100 一律视为不裁。"""
    if value in (None, ""):
        return None
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    if pct <= 0 or pct >= 100:
        return None
    return max(10.0, min(99.0, pct))


def resolve(script=None, **overrides):
    """合并「显式入参 > 脚本内 options > 脚本旧 mode 字段 > 默认值」，返回选项字典。

    script: 已读出的 script.json 内容（dict）或 None。
    overrides: commentary_type / highlight_source / intro_highlight /
               skip_intro_outro / retain_pct / web / mode。
    """
    script = script or {}
    saved = script.get("options") or {}

    # 1) 解说类型
    ctype = (normalize_type(overrides.get("commentary_type"))
             or normalize_type(saved.get("commentary_type")))
    intro = overrides.get("intro_highlight")
    if intro is None:
        intro = saved.get("intro_highlight")
    web = overrides.get("web")
    if web is None:
        web = saved.get("web")

    # 2) 旧 mode 兜底（显式 mode > 脚本自带 mode）
    if ctype is None:
        legacy = _LEGACY_MODE.get((overrides.get("mode") or script.get("mode") or "").strip())
        if legacy:
            ctype, l_intro, l_web = legacy
            intro = l_intro if intro is None else intro
            web = l_web if web is None else web
    if ctype is None:
        ctype = DEFAULT_TYPE

    src = (overrides.get("highlight_source") or saved.get("highlight_source") or "ai").strip()
    if src not in HIGHLIGHT_SOURCES:
        src = "ai"

    skip = overrides.get("skip_intro_outro")
    if skip is None:
        skip = saved.get("skip_intro_outro")
    pct = overrides.get("retain_pct")
    if pct is None:
        pct = saved.get("retain_pct")

    meta = COMMENTARY_TYPES[ctype]
    return {
        "commentary_type": ctype,
        "family": meta["family"],
        "depth": meta["depth"],
        "label": meta["label"],
        "highlight_source": src,
        "intro_highlight": _as_bool(intro, False),
        "skip_intro_outro": _as_bool(skip, False),
        "retain_pct": _as_pct(pct),
        "web": _as_bool(web, False),
    }


def one_click():
    """一键生成：AI 高度自由发挥 + 联网找资料 + 全片深入解说 + 片头插精彩片段。

    单行字幕/同行/只羽化原字幕/弱化原声属默认铁律，渲染层强制生效，这里不重复声明。
    """
    return resolve(commentary_type="full_deep", highlight_source="ai",
                   intro_highlight=True, skip_intro_outro=False,
                   retain_pct=None, web=True)
