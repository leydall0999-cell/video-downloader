"""原字幕带探测（与解说管线同款算法）。

前端「原字幕羽化」面板从预览视频抽几帧发上来，这里判断「烧进画面的原字幕」
落在画面底部哪一条横带上，返回**占画面高的比例**，供两处使用：

1. 预览窗口用 Canvas 把这条带画出来（用户可拖拽 / 输入微调）；
2. 用户调好后把同一个比例随任务回传，管线按它渲染 —— 预览看到什么，成片就烧什么。

为什么要在这里复刻一份而不是 import 管线：管线是独立仓 + 独立解释器
（打包后以子进程跑 `process.py`），App 侧无法 import；而探测只要 ~40 行纯 PIL，
比拉起一个管线进程便宜得多（后者要加载 whisper/torch 等重依赖）。

⚠️ 与管线 `scripts/edit_ffmpeg.py::_prepare_feather / _band_rows` **同源**：
   判据（只认白字 >=205、允许 2 行间隙、双行合并 max(6 行, 4.5%)、亮场景溢出守卫、
   丢 >25% 的异常帧、中位数聚合、上下各扩 8% 字幕高且至少 0.3% 画面）
   必须与那边保持一致，改任一处都要对照另一处，
   否则会出现「预览和成片位置不一致」——这正是本功能要消灭的问题。
"""
from __future__ import annotations


def _band_rows(gray, w0: int, h0: int):
    """在图像底部 30% 区域内找「白字字幕行」的行范围，找不到返回 None。

    判据：一行里近白像素（白字笔画，>=205）占比落在合理区间，连续这样的行拼成
    候选条；允许最多 2 行间隙（描边/抗锯齿会打断笔画，双行字幕两行间也常有空隙）。

    只认白字、不认黑像素：原视频常是「黑底白字」硬字幕条，若把黑像素也算进文字，
    整条黑底会被误判成字幕带（band 撑到 30%+），这已被历史教训证伪。
    返回 (top, bottom) 行号（原图坐标），已合并相近的多行字幕块。
    """
    y0 = int(h0 * 0.70)
    px = gray.load()
    lo_w = max(8, int(w0 * 0.015))       # 最少白字像素（≥8 避免抗锯齿/噪声把边缘零星白点算进命中）
    hi_w = int(w0 * 0.45)                 # 最多白字像素（比例过高多半是整屏白块）

    rows = []
    for y in range(y0, h0):
        nw = 0
        for x in range(0, w0, 2):        # 隔列采样
            if px[x, y] >= 205:
                nw += 1
        nw *= 2
        rows.append(lo_w <= nw <= hi_w)

    # 收集所有连续命中段，允许最多 2 行间隙
    segments = []
    cur = None
    gap = 0
    for i, hit in enumerate(rows):
        if hit:
            cur = (i, i) if cur is None else (cur[0], i)
            gap = 0
        elif cur is not None:
            gap += 1
            if gap > 2:
                segments.append(cur)
                cur = None
                gap = 0
    if cur is not None:
        segments.append(cur)

    if not segments:
        return None

    # 合并相近的段：双行字幕的两行之间常有几行空隙，合并后才得到完整字幕带，
    # 否则只会羽化其中一行、另一行原字幕残留。阈值取 max(6 行, 画面高 4.5%)：
    # 2026-09-16 实测（合成双行帧，行距 8.3% 画面高）3% 阈值只够吞单倍行距，
    # 稍宽的行距就差 1 行没并上、只羽化到第二行；4.5% 能覆盖主流双行排版的间隙。
    # ⚠️ 与管线 _band_rows 同步（那边 v9 已是 4.5%，App 侧 2026-09-18 补齐）。
    merged = []
    for seg in sorted(segments, key=lambda s: s[0]):
        if not merged or seg[0] - merged[-1][1] > max(6, int(h0 * 0.045)):
            merged.append(list(seg))
        else:
            merged[-1][1] = seg[1]

    best = max(merged, key=lambda s: s[1] - s[0])
    top, bot = y0 + best[0], y0 + best[1]
    # 亮场景溢出守卫（2026-09-18）：白窗/天空/白墙的高光常从扫描区顶（0.70h）
    # 一路亮到底，拼出的「带」贴着扫描区顶且很高 —— 真字幕几乎不会顶到 0.75h
    # 还超过画面高 15%。这种点判为亮场景溢出而非字幕，整帧弃用（其余帧会补上
    # 信号）；不弃的话 4 帧里 3 帧亮场景会把中位数撑到带高 20%+，糊掉大片画面。
    # 实测（少帅 45min，24 帧统计）：该守卫砍掉全部 9 个高光误报点，
    # 中位带高从 ~0.17 回落到 0.06（与真实字幕吻合）。
    if (top - y0) <= max(2, int(h0 * 0.05)) and (bot - top) > h0 * 0.15:
        return None
    if bot - top < max(2, int(h0 * 0.01)):   # 太薄，多半是画面噪点
        return None
    return top, bot


def _band_cols(gray, w0: int, top: int, bot: int):
    """在已探测出的带行范围内扫白像素的横向范围，返回 (minx, maxx) 或 None。

    与 _band_rows 同判据（>=205 近白），隔列采样。供「羽化宽度随原字幕长短」用：
    管线擦除矩形按 max(新字幕宽, 原字幕宽)+1字 收窄，预览同口径绘制。
    """
    px = gray.load()
    minx, maxx = None, None
    for y in range(top, bot + 1):
        for x in range(0, w0, 2):
            if px[x, y] >= 205:
                if minx is None or x < minx:
                    minx = x
                if maxx is None or x > maxx:
                    maxx = x
    if minx is None or maxx is None:
        return None
    return minx, maxx


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _percentile(xs, q):
    """线性插值分位数（q∈[0,1]）。用于「鲁棒并集」：p10 顶 / p90 底 能覆盖同片内的
    位置漂移，又不会被单个极端点（抗锯齿拉长、偶发误检）撑大。"""
    xs = sorted(xs)
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


# 「鲁棒并集」相对中位数带允许的最大增长（占画面高）。
# 🔴 2026-09-18 定案，**管线侧必须用同一个数**（edit_ffmpeg.py::_prepare_feather 与
#    自适应那处）。取 6% 的理由：足够覆盖「同片内字幕位置有变化」的常见幅度
#    （实测两个位置相差 8% 画高时，±6% 的中位带即可同时盖住），又不至于因为个别
#    噪声点把带撑成大糊块（历史上的 min/max 拼块就是这么出问题的）。
_GROW_CAP = 0.06


def detect_band_ratio(images) -> dict:
    """从若干帧图聚合出原字幕带（比例）。images 为 PIL.Image 序列。

    返回 {"found", "band_y_ratio", "band_h_ratio", "hits", "total", "note"}。
    found=False 时 note 说明原因，前端应把羽化带退回「底部默认」并提示手动拖。

    为什么用「中心中位数 + 高度中位数」而不是 min/max 拼大块：min/max 会被个别
    探测点的边缘噪声（抗锯齿把白字上下各拉长几行）撑大，导致羽化带过高、糊住带外画面。
    """
    total = len(images)
    bands = []
    cols = None          # 所有命中帧里最宽的横向范围（比例），union 防残字
    for im in images:
        try:
            gray = im.convert("L")
            b = _band_rows(gray, gray.width, gray.height)
        except Exception:
            continue
        if not b:
            continue
        # 单帧 band 高度 > 25% 视为「演职员表/大字幕墙/广告条」，丢弃该点避免污染最终 band
        rel_h = (b[1] - b[0]) / max(1, gray.height)
        if rel_h > 0.25:
            continue
        bands.append((b[0] / gray.height, b[1] / gray.height))
        # 横向范围：取各帧的并集（最宽帧）——窄了会漏出原字幕，宽了只是多擦一点
        try:
            c = _band_cols(gray, gray.width, b[0], b[1])
        except Exception:
            c = None
        if c:
            w0 = gray.width
            r0, r1 = c[0] / w0, c[1] / w0
            if cols is None or (r1 - r0) > (cols[1] - cols[0]):
                cols = (r0, r1)

    if len(bands) < max(2, -(-total * 3 // 10)):
        # 前端抽 10 帧（2026-09-18 从 4 帧加密度：硬字幕是间歇出现的，4 个固定
        # 时刻有概率大半落在无字幕镜头上 →「命中帧太少」误报）。门槛随帧数走：
        # max(2, 30% 帧数) —— 10 帧要求 ≥3，4 帧仍要求 ≥2，与管线口径同源。
        return {"found": False, "band_y_ratio": 0.0, "band_h_ratio": 0.0,
                "band_x_ratio": 0.0, "band_w_ratio": 0.0,
                "hits": len(bands), "total": total,
                "note": ("未探测到原字幕（画面较干净，或字幕不是白色）"
                         if not bands else "命中帧太少，无法确认原字幕位置")}

    centers = [(b[0] + b[1]) / 2 for b in bands]
    heights = [b[1] - b[0] for b in bands]
    mc, mh = _median(centers), _median(heights)
    med_top, med_bot = mc - mh / 2, mc + mh / 2
    # 🔴 2026-09-18（第二次修正）：「鲁棒并集」取代纯中位数。
    #   背景：用户截图反馈「有部分字预览没有擦除」——真机复现 + 合成实验定量：
    #     同一片内字幕位置在变（两处交替 / 缓慢漂移 / 单行与双行混排）时，
    #     **只取中心中位数**会把带放在"两头都不沾"的位置：实测字幕在 620/560 两处
    #     交替时，中位带只盖住字高的 42%~47%（露 28~30 行），双行镜头甚至整行露在带外。
    #   做法：顶取 p10、底取 p90（丢单个极端点），相对中位带**最多再长 _GROW_CAP**；
    #     若并集总高仍超过 25% 画高（与 _band_rows 的弃帧口径一致）则退回中位数，
    #     保证不会退化成"糊住带外一大片"。
    #   ⚠️ 管线 `edit_ffmpeg.py` 的 `_prepare_feather` 与自适应那处必须同步改，否则
    #      预览与成片位置漂移（见文件头说明）。
    tops = [b[0] for b in bands]
    bots = [b[1] for b in bands]
    if len(bands) >= 3:
        u_top, u_bot = _percentile(tops, 0.10), _percentile(bots, 0.90)
    else:
        u_top, u_bot = med_top, med_bot
    u_top = max(u_top, med_top - _GROW_CAP)
    u_bot = min(u_bot, med_bot + _GROW_CAP)
    if u_bot - u_top > 0.25:
        u_top, u_bot = med_top, med_bot
    span = max(0.0, u_bot - u_top)
    # 羽化带比原字幕略大：上下各扩 8% 字幕高度（至少 0.3% 画面），
    # 包住描边/辉光/抗锯齿外溢，避免「原字幕羽化不够、残字漏出」。
    pad_rel = max(0.003, span * 0.08)
    top_rel = max(0.0, u_top - pad_rel)
    bot_rel = min(1.0, u_bot + pad_rel)
    out = {"found": True,
           "band_y_ratio": round(top_rel, 6),
           "band_h_ratio": round(bot_rel - top_rel, 6),
           "hits": len(bands), "total": total, "note": ""}
    # 横向范围（可选字段）：探测到才带，供预览把擦除效果画成「随原字幕长短」的窄矩形。
    # 太宽/太窄都不可信（噪声/漏检），宽度须落在画面 5%~95% 内才下发。
    if cols:
        cw_r = cols[1] - cols[0]
        if 0.05 <= cw_r <= 0.95:
            out["band_x_ratio"] = round(max(0.0, cols[0]), 6)
            out["band_w_ratio"] = round(min(1.0, cw_r), 6)
    return out
