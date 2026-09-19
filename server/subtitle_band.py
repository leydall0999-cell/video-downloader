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
   丢 >25% 的异常帧、中位数聚合、上下各扩 _PAD_RATIO(5%) 字幕高且至少 0.3% 画面）
   必须与那边保持一致，改任一处都要对照另一处，
   否则会出现「预览和成片位置不一致」——这正是本功能要消灭的问题。
"""
from __future__ import annotations


def _band_rows(gray, w0: int, h0: int, min_runs: int = 3, avg_run_max: float | None = None):
    """在图像底部 30% 区域内找「白字字幕行」的行范围，找不到返回 None。

    判据：一行里近白像素（白字笔画，>=205）占比落在合理区间，连续这样的行拼成
    候选条；允许最多 2 行间隙（描边/抗锯齿会打断笔画，双行字幕两行间也常有空隙）。

    只认白字、不认黑像素：原视频常是「黑底白字」硬字幕条，若把黑像素也算进文字，
    整条黑底会被误判成字幕带（band 撑到 30%+），这已被历史教训证伪。
    返回 (top, bottom) 行号（原图坐标），已合并相近的多行字幕块。

    `avg_run_max`：第四判据「游程平均宽」的上限（像素）。None=用默认值
    （`max(4, 1.25% 宽)`）；传 0 或负数=关闭该判据（仅供「命中点不足时放宽重试」用）。
    """
    y0 = int(h0 * 0.70)
    px = gray.load()
    lo_w = max(8, int(w0 * 0.015))       # 最少白字像素（≥8 避免抗锯齿/噪声把边缘零星白点算进命中）
    hi_w = int(w0 * 0.45)                 # 最多白字像素（比例过高多半是整屏白块）
    # 单行最长连续白游程上限（真实像素）：白字笔画在 480 宽探测图上单条游程实测
    # ≤22px（2026-09-19 少帅第8集多帧校准），而白桌布/白碗/白墙等大面积白物单条
    # 游程 38~118px——这是区分「文字行」与「白物行」的关键判据。
    # ⚠️ 与管线 scripts/edit_ffmpeg.py::_band_rows 同步（2026-09-19 同日同改）。
    _max_run = max(24, int(w0 * 0.05))
    _min_runs = max(1, int(min_runs))
    # 第四判据「游程形状」（2026-09-19 三次根治，⚠️ 与管线 scripts/edit_ffmpeg.py
    # ::_band_rows 同值）：真字幕笔画细，字间/笔画间的空隙把它切成许多条**窄**游程；
    # 画面里的亮块（土黄军装的高光肩章/铜扣、白瓷器、金属反光、大面积高光）则是
    # 「少数几条宽游程」。用「游程平均宽」与「游程条数」两个量一起看：
    #
    #   命中条件：avg_run ≤ _AVG_RUN_MAX   **或**   n_runs ≥ _N_RUNS_STRONG
    #
    # 为什么是「或」而不是「且」：只卡平均宽会误杀**粗体大字幕**（笔画本身就有
    # 6~10px），也会误杀本仓合成测试里 10px 宽的白条 —— 而字幕是「许多条」笔画，
    # 条数天然很多；亮块是「少数几条」宽游程，条数天然很少。两者取并集既保住灵敏度，
    # 又能把亮块挡住（必须同时是「宽游程」且「条数少」才判非文字）。
    #
    # 实测（480 宽探测图，用户真实片源 24 帧逐行打印）：
    #   真字幕行    avg_run 2.0~6.2（中位 3.3），n_runs 10~22
    #   军装高光块  avg_run 6.4~12.5（中位 8.6），n_runs 4~10 ← 两个条件都不满足
    # 前三条判据挡不住军装块：白像素总数恰好落在 ① 区间内，单条最宽游程 22~24 刚好
    # 卡在 ② 的 24px 门槛下，n_runs 也够 ③ 的 3 条 —— 于是整块「军装」被当成字幕段，
    # 且行数比真字幕还多而被「取最长段」选中 ⇒ 用户截图里虚线框压在军装上、真字幕
    # 反而在框外（2026-09-19 用户第 2 张图；帧 2334.9s 实测带位 0.700~0.789，
    # 真字幕在 0.837~0.900；加第四条后回到 0.837~0.900，肉眼复核框已落在字上）。
    #
    # 阈值取 1.25% 画宽（480 宽 → 6.0px）与 2.5% 画宽（480 宽 → 12 条）：
    # 真字幕 avg_run 上限 6.2 与之相邻（那一行属双行字幕的一行、邻行仍命中，段不断）；
    # 军装块 n_runs 上限 10 < 12、avg_run 中位 8.6 > 6，两条都够不着。
    # ⚠️ 残留边界：条纹衣物（许多条 10px 宽条纹）仍可能骗过第四条，靠帧间一致性兜底
    #    + 面板手动 dy/dh 覆盖；这是「纯像素启发式」的能力边界，已如实记录。
    _avg_run_max = max(4.0, w0 * 0.0125) if avg_run_max is None else float(avg_run_max)
    _n_runs_strong = _N_RUNS_STRONG

    rows = []
    for y in range(y0, h0):
        nw = 0
        run = 0
        max_run = 0
        n_runs = 0
        for x in range(0, w0, 2):        # 隔列采样
            if px[x, y] >= 205:
                nw += 1
                run += 2                 # 隔列采样，每个命中像素代表 ~2px 真实宽度
                if run > max_run:
                    max_run = run
            else:
                if run:
                    n_runs += 1
                run = 0
        if run:
            n_runs += 1
        nw *= 2
        # 文字行四判据（第三条 2026-09-19 追加、第四条同日三次根治，
        # ⚠️ 与管线 scripts/edit_ffmpeg.py::_band_rows 同值）：
        #   ① 白像素总数落在区间内；
        #   ② 没有「一整条」超长白游程（白桌布/白墙那种大面积白物）；
        #   ③ 有足够多条**笔画游程**——文字由多个字组成，被字间/笔画间空隙切成许多条
        #      短游程（480 宽图上真字幕行隔列采样实测 8~15 条）；而画面底部的小白块
        #      （金属反光 / 白瓷器 / 亮边）单行只有 1~2 条连续游程，恰好同时躲过 ①
        #      （总数落在 1.5%~45%）与 ②（游程 19~21px ≤ 24px），被当成文字行混进并集
        #      ⇒ 带位被下拉、带高被撑大、报「这一段字幕没擦干净而且下面多糊一条」。
        #      唯一可能被误伤的形态是「只有 1~2 个字的超短字幕」，故 min_runs 可关
        #      （detect_band_ratio 在命中帧不足时会用 min_runs=0 放宽重试一次）。
        #   ④ 游程**形状**——「平均宽 ≤ _avg_run_max」或「条数 ≥ _n_runs_strong」至少
        #      满足一个（见上）。③ 只数条数，数不出「少数几条宽游程」的亮块；只有
        #      「宽游程 + 条数少」同时成立才判非文字。
        avg_run = (nw / n_runs) if n_runs else float("inf")
        rows.append(lo_w <= nw <= hi_w and max_run <= _max_run and n_runs >= _min_runs
                    and (_avg_run_max <= 0
                         or avg_run <= _avg_run_max
                         or n_runs >= _n_runs_strong))

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
    # ⚠️ 2026-09-19 同步管线 P2-12：亮场景溢出守卫由 0.15h 放宽到 0.18h。
    #    0.15h 偏严，会把字号极大的真实底部字幕（带高 >15%）误杀；而白窗/天空/白墙类
    #    整片亮溢出通常 >20%，仍拦得住。两侧必须同值，否则预览判「非字幕」、
    #    渲染却擦掉 —— 用户看到的就是「预览和成片对不上」。
    if (top - y0) <= max(2, int(h0 * 0.05)) and (bot - top) > h0 * 0.18:
        return None
    if bot - top < max(2, int(h0 * 0.01)):   # 太薄，多半是画面噪点
        return None
    # 近底守卫（2026-09-19 收尾，⚠️ 与管线 scripts/edit_ffmpeg.py::_band_rows 同值）：
    # 硬字幕几乎从不下探到画面最底部 7%（必留安全边距）。实测少帅第8集真实字幕带底
    # 恒在 ~0.90h，而亮场景/白物污染的「伪字幕带」常探到 0.94h~0.99h（t165/166/625.5/
    # 626s 等），往往比真字幕更高更靠下 ⇒ 被「取最长段」选中 → 带位被拉低、带高被撑大。
    # 带底超过 0.93h 一律判非字幕（静默弃用该帧），由其余干净帧补信号。
    # ⚠️ 2026-09-19 同步管线 P2-4：阈值由 0.93h 放宽到 0.96h —— 少数片子字幕确实压在
    #    画面最底部（>0.93h 但 <0.96h），旧阈值会把它们整帧弃用 ⇒ 预览侧「探测不到」
    #    而渲染侧照擦（管线已是 0.96），两侧结论打架就是「预览不理想」的来源。
    #    仅 0.96h~0.99h 的极端污染仍被拦截。
    if bot > h0 * 0.96:
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


# 第四判据「游程条数」的强阈值（2026-09-19，⚠️ 与管线同值）：**固定条数，不随分辨率缩放**。
# 理由：游程条数≈一行字的笔画段数，是**字形本身**的属性 —— 分辨率升高只会让原本
# 粘连的细笔画分开（条数不降），故固定下限是保守的；按宽度等比例放大反而会在高分辨率
# 下把真实字幕判掉（本仓 test_band_ratio_independent_of_resolution 就是这么暴露的：
# 同一份比例内容在 1280 宽下条数仍是 14 条，按 2.5%×1280=32 条去要求必然误杀）。
# 取值须 > 实测「亮块」条数上限（480 宽下军装高光块 4~10 条）且 ≤ 真实字幕下限（10~22 条）：
# 12 同时满足（且高于本仓合成测试的 14 条白条）。
_N_RUNS_STRONG = 12

# 帧间位置离群判据（2026-09-19 三次根治第二层，⚠️ 与管线 `_prepare_feather` /
# `_segment_feather` 同值同理）：
#   「原字幕」在同一支片里的位置是**稳定**的（这也是全片共用一条带的依据）；
#   而亮块误报（军装高光 / 白瓷器 / 金属反光 / 片头片尾演职员表）位置**随机**。
#   于是把各帧 band 的**中心**拿来投票：中心偏离中位数超过容差的帧判为离群、丢弃。
#   容差 = max(_POS_TOL_FLOOR, 3×MAD)，用 MAD（中位绝对偏差）而不是标准差，因为
#   MAD 本身抗离群；**双峰分布**（字幕确实在两处交替，2026-09-18 实测 620/560 两处、
#   相差约 8% 画高）时 MAD 会变大 ⇒ 容差自动放宽到能同时容纳两处 ⇒ **退化为「不丢」**，
#   不会误伤已知的合法漂移。只有「一个主峰 + 个别散点」才真正动手。
_POS_TOL_FLOOR = 0.06


def _band_center(b) -> float:
    return (b[0] + b[1]) / 2.0


def _keep_by_position(bands, floor_tol: float = _POS_TOL_FLOOR):
    """按帧间位置投票挑出「同一处」的命中帧，返回 (keep_indices, med, mad, tol)。"""
    n = len(bands)
    if n < 3:
        return list(range(n)), 0.0, 0.0, float(floor_tol)
    centers = [_band_center(b) for b in bands]
    med = _median(centers)
    mad = _median([abs(c - med) for c in centers])
    tol = max(float(floor_tol), 3.0 * mad)
    keep = [i for i, c in enumerate(centers) if abs(c - med) <= tol]
    return keep, med, mad, tol


# 「鲁棒并集」相对中位数带允许的最大增长（占画面高）。
# 🔴 2026-09-18 定案，**管线侧必须用同一个数**（edit_ffmpeg.py::_prepare_feather 与
#    自适应那处）。取 6% 的理由：足够覆盖「同片内字幕位置有变化」的常见幅度
#    （实测两个位置相差 8% 画高时，±6% 的中位带即可同时盖住），又不至于因为个别
#    噪声点把带撑成大糊块（历史上的 min/max 拼块就是这么出问题的）。
_GROW_CAP = 0.06

# 羽化带相对原字幕的**上下外扩**（占字幕高的比例，非画面高）。
# 🔴 2026-09-19 用户反馈「默认带高太大，想 9 左右」：原为 0.08，实拍字幕高约 8.2% 画高时
#    返回的带高 = span*(1+2*0.08) ≈ 9.5% 画高，看着比字幕胖一圈 ⇒ 收到 0.05 后 ≈ 9.0%。
#    保留这个外扩是为了包住描边/辉光/抗锯齿外溢（降到 0 会「残字漏出」）；
#    ⚠️ 管线 `edit_ffmpeg.py::_prepare_feather` 与自适应那处必须同值。
_PAD_RATIO = 0.05


def detect_band_ratio(images) -> dict:
    """从若干帧图聚合出原字幕带（比例）。images 为 PIL.Image 序列。

    返回 {"found", "band_y_ratio", "band_h_ratio", "hits", "total", "note"}。
    found=False 时 note 说明原因，前端应把羽化带退回「底部默认」并提示手动拖。

    为什么用「中心中位数 + 高度中位数」而不是 min/max 拼大块：min/max 会被个别
    探测点的边缘噪声（抗锯齿把白字上下各拉长几行）撑大，导致羽化带过高、糊住带外画面。
    """
    total = len(images)

    def _collect(min_runs):
        """用给定的笔画游程下限跑一遍全部帧，返回 (bands, per_frame, cols_list, idx_list)。

        `idx_list`：每个 band 对应的**帧下标**（bands 是稀疏的，只收命中帧）——
        帧间位置投票要据此把离群帧的 per_frame 一并作废（否则预览「跟幕」会跳到亮块上）。
        `cols_list`：与 bands 一一对应的横向范围（比例），过滤后重新取并集。
        """
        b_acc = []
        # 🔴 2026-09-18：逐帧结果。与 images **索引一一对应**，前端拿着它 + 自己记录的抽帧时间点
        #   就能让预览「跟幕」——播放头走到哪儿，框就跳到那一片时段探测出的带上。
        pf_acc = []
        idx_acc = []             # 每个 band 命中的帧下标
        cols_list = []           # 每个 band 的横向范围（比例）或 None
        for k, im in enumerate(images):
            try:
                gray = im.convert("L")
                b = _band_rows(gray, gray.width, gray.height, min_runs=min_runs,
                               avg_run_max=None if min_runs else 0)
            except Exception:
                b = None
            if not b:
                pf_acc.append({"found": False})
                continue
            # 单帧 band 高度 > 25% 视为「演职员表/大字幕墙/广告条」，丢弃该点避免污染最终 band
            rel_h = (b[1] - b[0]) / max(1, gray.height)
            if rel_h > 0.25:
                pf_acc.append({"found": False, "note": "oversized"})
                continue
            b_acc.append((b[0] / gray.height, b[1] / gray.height))
            idx_acc.append(k)
            pf_acc.append({"found": True,
                           "band_y_ratio": b[0] / gray.height,
                           "band_h_ratio": (b[1] - b[0]) / gray.height})
            # 横向范围：取各帧的并集（最宽帧）——窄了会漏出原字幕，宽了只是多擦一点
            try:
                c = _band_cols(gray, gray.width, b[0], b[1])
            except Exception:
                c = None
            if c:
                cols_list.append((c[0] / gray.width, c[1] / gray.width))
            else:
                cols_list.append(None)
        return b_acc, pf_acc, cols_list, idx_acc

    bands, per_frame, cols_list, band_idx = _collect(3)
    _need = max(2, -(-total * 3 // 10))
    if len(bands) < _need:
        # 放宽重试（2026-09-19，与管线 _prepare_feather 同口径同原因）：笔画游程判据
        # （_band_rows 第三判据）可能误杀「只有 1~2 个字的超短字幕」帧；万一因此命中帧
        # 不够门槛，前端会退回"底部默认带"，用户看到的就是"原字幕根本没擦干净"。
        # 丢掉游程判据重判一遍（图像已在内存，零额外抽帧开销）。
        # 放宽时连第四判据（游程形状）一起关：它同样是"像不像文字"的形态判据，
        # 只松第三、不松第四，等于没松。帧间投票仍会兜住误报（见下）。
        rb, rp, rl, ri = _collect(0)
        if len(rb) > len(bands):
            bands, per_frame, cols_list, band_idx = rb, rp, rl, ri

    if len(bands) < max(2, -(-total * 3 // 10)):
        # 前端抽 10 帧（2026-09-18 从 4 帧加密度：硬字幕是间歇出现的，4 个固定
        # 时刻有概率大半落在无字幕镜头上 →「命中帧太少」误报）。门槛随帧数走：
        # max(2, 30% 帧数) —— 10 帧要求 ≥3，4 帧仍要求 ≥2，与管线口径同源。
        return {"found": False, "band_y_ratio": 0.0, "band_h_ratio": 0.0,
                "band_x_ratio": 0.0, "band_w_ratio": 0.0,
                "hits": len(bands), "total": total,
                # 未达门槛＝整片结论不可信 ⇒ 逐帧值也不往外给（免得预览拿零星噪声去跟幕）
                "per_frame": [],
                "note": ("未探测到原字幕（画面较干净，或字幕不是白色）"
                         if not bands else "命中帧太少，无法确认原字幕位置")}

    # ---- 第二层：帧间位置投票，丢掉「位置离群」的命中帧（2026-09-19 三次根治）----
    # 动因：单帧判据再严，也总有某个镜头（大片高光/片头片尾字幕墙）能凑出一段
    # 「像文字」的带；而真字幕在**全片**的位置稳定，误报位置随机 —— 用帧间一致性
    # 把它们分开，比继续给单帧加判据更可靠（单帧判据越加越容易误杀真字幕）。
    keep, _pmed, _pmad, _ptol = _keep_by_position(bands)
    if len(keep) < len(bands):
        dropped = [band_idx[i] for i in range(len(bands)) if i not in set(keep)]
        for k in dropped:
            per_frame[k] = {"found": False, "note": "position-outlier"}
        bands = [bands[i] for i in keep]
        cols_list = [cols_list[i] for i in keep]
        print(f"[探测] 帧间位置投票剔除 {len(dropped)} 帧离群带"
              f"（中位中心 {_pmed:.3f}，容差 {_ptol:.3f}）：帧 {dropped}")
    if len(bands) < max(2, -(-total * 3 // 10)):
        # 剔除后剩下的帧连门槛都不到 ⇒ 这些「命中」不是同一个稳定元素
        # （典型：片头片尾的演职员表 + 各种亮块各说各话）。此时给位置＝赌博，
        # fail-open 让用户手动指定更安全（绝不因探测去糊一片画面）。
        return {"found": False, "band_y_ratio": 0.0, "band_h_ratio": 0.0,
                "band_x_ratio": 0.0, "band_w_ratio": 0.0,
                "hits": len(bands), "total": total, "per_frame": [],
                "note": "命中帧位置不集中（画面无稳定白字字幕可信），已跳过自动探测"}
    # 横向范围：过滤后重新取并集（被剔除帧的 cols 不能算进来）
    cols = None
    for c in cols_list:
        if c and (cols is None or (c[1] - c[0]) > (cols[1] - cols[0])):
            cols = c

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
    # 羽化带比原字幕略大：上下各扩 _PAD_RATIO 字幕高度（至少 0.3% 画面），
    # 包住描边/辉光/抗锯齿外溢，避免「原字幕羽化不够、残字漏出」。
    # ⚠️ 2026-09-19 由 0.08 收到 0.05（用户：默认带高太大，9.5 → 9 左右），
    #    管线两处必须同步，见 _PAD_RATIO 注释。
    pad_rel = max(0.003, span * _PAD_RATIO)
    top_rel = max(0.0, u_top - pad_rel)
    bot_rel = min(1.0, u_bot + pad_rel)
    # 逐帧值补**同样的外扩**（与 top_rel/bot_rel 同口径），前端拿去直接当"该帧的带"照画，
    # 不必自己再算 pad —— 少一处口径不一致的机会。
    for pf in per_frame:
        if not pf.get("found"):
            continue
        t0 = max(0.0, pf["band_y_ratio"] - pad_rel)
        b0 = min(1.0, pf["band_y_ratio"] + pf["band_h_ratio"] + pad_rel)
        pf["band_y_ratio"] = round(t0, 6)
        pf["band_h_ratio"] = round(b0 - t0, 6)
    out = {"found": True,
           "band_y_ratio": round(top_rel, 6),
           "band_h_ratio": round(bot_rel - top_rel, 6),
           "hits": len(bands), "total": total, "note": "",
           "per_frame": per_frame}
    # 横向范围（可选字段）：探测到才带，供预览把擦除效果画成「随原字幕长短」的窄矩形。
    # 太宽/太窄都不可信（噪声/漏检），宽度须落在画面 5%~95% 内才下发。
    if cols:
        cw_r = cols[1] - cols[0]
        if 0.05 <= cw_r <= 0.95:
            out["band_x_ratio"] = round(max(0.0, cols[0]), 6)
            out["band_w_ratio"] = round(min(1.0, cw_r), 6)
    return out
