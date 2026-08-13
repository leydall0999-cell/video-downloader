"""ffmpeg 原生渲染模块: 与 edit.py 等价的成片逻辑, 但全程走 ffmpeg C 路径(不走 moviepy 逐帧 Python 合成)。

性能对比: moviepy 的 concatenate_videoclips(method="compose") + TextClip 是主要瓶颈(逐帧 Python 合成 + 全量重编码)。
本模块把每段先各自编码为中间 mp4, 最后用 concat demuxer 拼接(不重编码), 渲染速度通常快 10~40 倍。

并行优化(v2):
- TTS 批量并发: asyncio.gather 全段同时请求 edge_tts(N 句并行, 串行 -> 1/N)
- ffmpeg 切条并发: ThreadPoolExecutor 多段同时渲染 segment mp4
- macOS VideoToolbox 硬件编码: VDL_FFMPEG_HWACCEL=1 启用 h264_videotoolbox

用法(与 edit.py 完全一致的 build 签名, 供 process.py 切换):
    import edit_ffmpeg
    edit_ffmpeg.build(video_path, script_path, out_path, vertical=..., own_voice=..., voice_override=..., original_speed=...)

注意: 依赖 ffmpeg(ffprobe)(打包版走随包二进制, 见 ffmpeg_path.py), 且需中文可渲染字体(与 config.FONT 一致)。
"""
import os
import sys
import json
import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import OUTPUT, WORK, VOICE, FONT, SUBTITLE_SIZE, ORIGINAL_DUCK, ensure_dirs
from ffmpeg_path import ffmpeg_bin, ffprobe_bin
import commentary_options as copts

FFMPEG = ffmpeg_bin()
FFPROBE = ffprobe_bin()
_DARWIN = sys.platform == "darwin"
# 并发度: 环境变量 VDL_TTS_CONCURRENCY 控制 TTS 并发数(edge_tts 网络请求, IO 密集, 默认 8)
_TTS_CONCURRENCY = int(os.environ.get("VDL_TTS_CONCURRENCY", "8") or 8)
# ffmpeg 并发数: 默认 min(CPU 核数, 8)。硬件编码时 CPU 占用极低, 可放心拉高并行段数。
_FFMPEG_CONCURRENCY = int(os.environ.get("VDL_FFMPEG_CONCURRENCY",
                                        str(min(os.cpu_count() or 4, 8))) or 8)


def _pick_hw_encoder():
    """探测最优 H.264 编码器：硬件加速优先(N 卡 nvenc > Intel qsv > AMD amf)，回退 libx264。

    跨平台：macOS 用 VideoToolbox，Windows/Linux 用对应硬件编码器；强制软编码设
    VDL_FFMPEG_HWACCEL=0。返回 (编码器参数列表, 是否硬件编码)。
    """
    prefer = os.environ.get("VDL_FFMPEG_HWACCEL", "auto").lower()
    soft = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
    if prefer in ("0", "false", "off"):
        return soft, False
    try:
        out = subprocess.check_output([FFMPEG, "-hide_banner", "-encoders"],
                                      stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
    except Exception:
        return soft, False
    if _DARWIN:
        if "h264_videotoolbox" in out:
            return ["-c:v", "h264_videotoolbox", "-allow_sw", "1", "-q:v", "60"], True
        return soft, False
    # Windows/Linux：按优先级选硬件编码器（质量参数各异，不可照搬 -crf）
    if "h264_nvenc" in out:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"], True
    if "h264_qsv" in out:
        return ["-c:v", "h264_qsv", "-global_quality", "23"], True
    if "h264_amf" in out:
        return ["-c:v", "h264_amf", "-quality", "balanced", "-qp_i", "23", "-qp_p", "23"], True
    return soft, False


# 模块加载时探测一次（结果恒定），供后续所有渲染命令复用
_VCODEC, _USE_VT = _pick_hw_encoder()


# ==================== 工具函数 ====================

def _run(cmd):
    """以列表形式执行命令(无 shell), 返回 (returncode, stderr)。"""
    p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return p.returncode, p.stderr.decode("utf-8", "ignore")


_DUR_CACHE = {}  # 同路径 ffprobe 时长只查一次（build 内多次 probe 同视频）

def _probe_duration(path):
    if path in _DUR_CACHE:
        return _DUR_CACHE[path]
    d = float(subprocess.check_output(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path]).strip())
    _DUR_CACHE[path] = d
    return d


# ==================== TTS 批量并发 ====================

async def _gen_voice_async(text, out_path, voice):
    """单句 TTS(异步): edge_tts -> 保存 mp3。"""
    import edge_tts
    comm = edge_tts.Communicate(text, voice)
    # 单段硬超时(默认 30s, 可 VDL_TTS_ITEM_TIMEOUT 调):
    # edge_tts 在个别段网络挂起时自身无超时, 会让 asyncio.gather 永久等待整轮卡死;
    # 这里给单段套一层 wait_for, 超时抛 TimeoutError 由上层重试/跳过, 不拖垮整体。
    _TTS_ITEM_TIMEOUT = int(os.environ.get("VDL_TTS_ITEM_TIMEOUT", "30") or 30)
    await asyncio.wait_for(comm.save(out_path), timeout=_TTS_ITEM_TIMEOUT)


# 音色回退链：指定音色（如云希）对部分文本会 NoAudioReceived，自动换稳定音色。
# 收敛到 2 个稳定音色，避免长回退链串行拖慢整体（实测前两个最稳）。
_TTS_FALLBACK_VOICES = [
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
]


async def _ensure_voice_async(text, path, voice, max_retry=3):
    """带重试、校验和音色回退的异步 TTS；成功返回 True，全部失败返回 False。"""
    if await asyncio.to_thread(_valid_audio, path):
        return True

    voices_to_try = [voice] + [v for v in _TTS_FALLBACK_VOICES if v != voice]
    for v in voices_to_try:
        for _ in range(max_retry if v == voice else 2):
            if os.path.exists(path):
                os.remove(path)
            try:
                await _gen_voice_async(text, path, v)
            except Exception:
                pass
            if await asyncio.to_thread(_valid_audio, path):
                return True
    if os.path.exists(path):
        os.remove(path)
    return False


async def _gen_all_voices(tasks, sem):
    """tasks: list of (narration, voice_path, voice)。
    用 asyncio.Semaphore 限制并发数，返回 [ok] 列表。
    """
    async def _one(text, path, voice):
        async with sem:
            return await _ensure_voice_async(text, path, voice)
    return await asyncio.gather(*[_one(t, p, v) for t, p, v in tasks])


# ==================== ffmpeg 切条并行 ====================

def _render_segment(args):
    """独立函数(供 ThreadPoolExecutor 并行)：执行单个 ffmpeg 渲染命令。"""
    cmd, idx = args
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return proc.returncode, proc.stderr.decode("utf-8", "ignore"), idx, cmd[0] if cmd else ""


def _valid_audio(path):
    """音频文件非空且能被 ffprobe 解析, 否则视为 edge_tts 抖动产生的空/损坏文件。"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        subprocess.check_output(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def _valid_video(path):
    """中间片段 mp4 非空且能被 ffprobe 解析时长, 用于断点续作时判定某段已完成。"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    try:
        d = subprocess.check_output(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stderr=subprocess.DEVNULL).strip()
        return float(d) > 0
    except Exception:
        return False


# ==================== 断点续作: 分段进度清单 ====================

def _seg_progress_path(script_path):
    """按视频 id 生成进度清单路径(与 _seg_ff 中间文件同目录, 互不干扰)。"""
    base = os.path.splitext(os.path.basename(script_path))[0]  # 6191ac21e887.script
    vid = base[:-len(".script")] if base.endswith(".script") else base
    return os.path.join(WORK, f"_{vid}_seg_progress.json")


def _seg_progress_load(path, sig):
    """读取进度清单; 若文件不存在或渲染参数签名(sig)变化则视为无效, 返回空 dict。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("sig") != sig:
            return {}  # 编码参数变了(如软/硬编切换), 旧进度作废
        return {int(k) for k in data.get("done", {}).keys()}
    except Exception:
        return {}


def _seg_progress_save(path, sig, done_set):
    """增量持久化已完成段(每完成一段写一次, 进程被杀也不丢进度)。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"sig": sig,
                       "done": {str(k): True for k in done_set}}, f)
    except Exception:
        pass




# ==================== 字幕渲染（默认铁律，不提供开关） ====================
# 1. 同一时刻画面上只能出现「一行」解说字幕：长旁白按标点切成多块、分时段轮播，
#    绝不叠成多行；
# 2. 解说字幕与原字幕保持「同一行」：从预抽的原字幕 .ass 读 MarginV 定位，
#    读不到时退回画面底部 8% 处。

_PUNCT = "，。！？；：、,.!?;:"


def _one_line_chunks(text, max_chars):
    """把旁白切成若干「一行装得下」的短句，保证同一时刻只显示一行。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    chunks, cur = [], ""
    for ch in text:
        cur += ch
        if (ch in _PUNCT and len(cur) >= max_chars * 0.6) or len(cur) >= max_chars:
            piece = cur.strip().strip(_PUNCT)
            if piece:
                chunks.append(piece)
            cur = ""
    tail = cur.strip().strip(_PUNCT)
    if tail:
        chunks.append(tail)
    return chunks or [text[:max_chars]]


def _subtitle_size(h):
    """按画面高度缩放字号（竖屏 854 高时正好等于 SUBTITLE_SIZE）。"""
    return max(20, int(SUBTITLE_SIZE * h / 854.0))


def _render_subtitle_png(text, out_path, w=480, h=854, size=None, y_bottom=None):
    """用 PIL 把「一行」解说文案渲染成透明 PNG(白字黑描边), 交 ffmpeg overlay 烧入。

    规避: 当前 ffmpeg 编译版未必带 libass/drawtext, 故字幕改用离屏渲染成图 + overlay
    (核心滤镜, 各平台通用)。仅生成静态图, 不逐帧处理, 开销极小。

    y_bottom: 文字底边距画面底部的像素数（与原字幕同行时由 .ass 的 MarginV 折算而来）。
    永远只渲染一行：过长时自动缩小字号（切块交由 _one_line_chunks 在上游完成）。
    """
    from PIL import Image, ImageDraw, ImageFont
    size = size or _subtitle_size(h)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    def _variant(px):
        try:
            return ImageFont.truetype(FONT, px) if FONT else ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()

    font = _variant(size)
    if not FONT:
        print("[警告] 未找到支持中文的字体，字幕可能显示为方块（请安装中文字体）")
    used = size
    while used > 16:
        bbox = draw.textbbox((0, 0), text, font=font)
        if (bbox[2] - bbox[0]) <= w - 24:
            break
        used -= 2
        font = _variant(used)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    margin = int(y_bottom) if y_bottom and y_bottom > 0 else int(h * 0.08)
    y = max(0, h - margin - used)
    tx = w // 2 - tw // 2
    ty = y
    # 阴影：半透明黑，向右下偏移，提升在浅色/花背景上的可读性。
    # 此前只有黑描边、无阴影，浅底视频上字容易糊进画面。
    shadow_off = max(2, used // 24)
    draw.text((tx + shadow_off, ty + shadow_off), text, font=font,
              fill=(0, 0, 0, 150))
    # 主字：白字 + 加粗黑描边（描边由原 used//16 加粗到 used//10，更硬朗）
    draw.text((tx, ty), text, font=font,
              fill=(255, 255, 255, 255), stroke_width=max(3, used // 10),
              stroke_fill=(0, 0, 0, 255))
    img.save(out_path)


def _atempo_chain(ratio):
    """atempo 只接受 0.5~2.0, 超出需链式。ratio>0 且为正。"""
    if ratio <= 0:
        ratio = 1.0
    parts = []
    r = ratio
    while r > 2.0:
        parts.append("atempo=2.0")
        r /= 2.0
    while r < 0.5:
        parts.append("atempo=0.5")
        r /= 0.5
    parts.append(f"atempo={r:.4f}")
    return ",".join(parts)


def _build_filler_cmd(video_path, gs, ge, vertical, inter_path, src_has_audio=True):
    """非高光间隙片段：原画面 + 原声音量播放，无字幕、无解说。

    编码参数（分辨率/帧率/像素格式/音频）与高光段保持一致，便于 concat demuxer 拼接。
    源视频无音轨时自动补一条静音轨，保证拼接时音频流一致（不会因个别片段缺音轨导致拼接失败）。
    """
    gd = max(ge - gs, 0.1)
    # 输入定位：只解码本间隙段 [gs, ge]，不再从第 0 帧解码整片 —— 剪辑提速关键
    v_filters = f"[0:v]setpts=PTS-STARTPTS"
    if vertical:
        v_filters += ",scale=480:854:force_original_aspect_ratio=increase,crop=480:854"
    v_filters += "[v]"
    if src_has_audio:
        a_filters = "[0:a]asetpts=PTS-STARTPTS,volume=1.0[a]"
        head = [FFMPEG, "-y", "-ss", f"{gs:.3f}", "-t", f"{gd:.3f}", "-i", video_path]
    else:
        a_filters = "[1:a]asetpts=PTS-STARTPTS,volume=0.0[a]"
        head = [FFMPEG, "-y", "-ss", f"{gs:.3f}", "-t", f"{gd:.3f}", "-i", video_path,
                "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={gd:.3f}"]
    return head + [
        "-filter_complex", v_filters + ";" + a_filters,
        "-map", "[v]", "-map", "[a]",
        *_VCODEC, "-r", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100", "-ac", "2",
        inter_path,
    ]


# ---------------------------------------------------------------------------
# 渲染辅助：高光段 / 全片段 命令生成 + 字幕流/滤镜探测
# ---------------------------------------------------------------------------

_sub_png_counter = 0


def _next_sub_png():
    """生成唯一的字幕 PNG 路径，避免多段并行时互相覆盖（放 WORK 目录，跨平台）。"""
    global _sub_png_counter
    _sub_png_counter += 1
    sub_dir = os.path.join(WORK, "_sub_png")
    os.makedirs(sub_dir, exist_ok=True)
    return os.path.join(sub_dir, f"vdl_sub_{_sub_png_counter}.png")


_FILTER_CACHE = {}


def _has_filter(name):
    """探测当前 ffmpeg 是否支持某滤镜（缓存结果）。"""
    if name in _FILTER_CACHE:
        return _FILTER_CACHE[name]
    try:
        out = subprocess.check_output([FFMPEG, "-hide_banner", "-filters"],
                                      stderr=subprocess.DEVNULL).decode("utf-8", "ignore")
        ok = any(tok.strip() == name for line in out.splitlines() for tok in line.split())
    except Exception:
        ok = False
    _FILTER_CACHE[name] = ok
    return ok


def _has_audio(path):
    """源视频是否带音轨（无音轨时解说段不做原声弱化、也不与原声混流）。"""
    try:
        out = subprocess.check_output(
            [FFPROBE, "-v", "error", "-show_entries", "stream=index",
             "-select_streams", "a", "-of", "csv=p=0", path]).decode("utf-8").strip()
        return bool(out)
    except Exception:
        return False


def _probe_size(path):
    try:
        out = subprocess.check_output([FFPROBE, "-v", "error",
                                       "-show_entries", "stream=width,height",
                                       "-select_streams", "v:0",
                                       "-of", "csv=p=0", path]).decode("utf-8", "ignore").strip()
        w, h = out.split(",")
        return int(w), int(h)
    except Exception:
        return 1920, 1080


def _band_rows(gray_img, w0, h0):
    """在图像底部 30% 区域内找「文字条」的行范围，找不到返回 None。

    判据：一行里近白像素占比落在 1%~45% 之间（纯亮画面/纯暗画面都排除），
    连续这样的行拼成候选条。返回 (top, bottom) 行号（原图坐标）。
    """
    y0 = int(h0 * 0.70)
    px = gray_img.load()
    lo_hits = max(2, int(w0 * 0.01))
    hi_hits = int(w0 * 0.45)
    rows = []
    for y in range(y0, h0):
        n = 0
        for x in range(0, w0, 2):        # 隔列采样，够用且快一倍
            if px[x, y] >= 225:
                n += 1
        n *= 2
        rows.append(lo_hits <= n <= hi_hits)
    best = cur = None
    for i, hit in enumerate(rows):
        if hit:
            cur = (i, i) if cur is None else (cur[0], i)
        elif cur is not None:
            if best is None or (cur[1] - cur[0]) > (best[1] - best[0]):
                best = cur
            cur = None
    if cur is not None and (best is None or (cur[1] - cur[0]) > (best[1] - best[0])):
        best = cur
    if best is None:
        return None
    top, bot = y0 + best[0], y0 + best[1]
    if bot - top < max(2, int(h0 * 0.015)):   # 太薄，多半是画面噪点
        return None
    return top, bot


def _prepare_feather(video_path, vertical, canvas):
    """探测「烧进画面的原字幕」所在横条，供「只羽化原字幕、画面不羽化」使用。

    为什么不走 subtitles 滤镜抽软字幕层：分发版与 Homebrew 版 ffmpeg 都没编 libass
    （`ffmpeg -filters` 里没有 subtitles），而实际素材的原字幕几乎都是烧死在画面里的。
    这里抽 6 帧做文字条探测，只对那一条做高斯模糊，画面其余部分一像素都不动；
    探测不到原字幕就返回 None（干净画面绝不无故糊一条）。

    返回 {"band_y","band_h","margin_v"} 或 None。
    """
    # 竖屏(9:16 Shorts)不再跳过羽化：原视频经 scale=480:854 居中放大裁剪，原字幕仍保留
    # 在底部区域；探测抽帧 scale=480:-2 与竖屏渲染 canvas(480x854) 尺寸一致，坐标可复用。
    if vertical:
        print("[字幕] 竖屏按 480x854 居中裁剪，原字幕保留在底部，照常探测羽化带")
    try:
        from PIL import Image
    except ImportError:
        return None
    cw, ch = canvas
    dur = _probe_duration(video_path)
    tmpd = os.path.join(WORK, "_subdetect")
    os.makedirs(tmpd, exist_ok=True)
    bands = []
    for i, ratio in enumerate((0.15, 0.3, 0.45, 0.6, 0.75, 0.9)):
        png = os.path.join(tmpd, f"probe_{i}.png")
        rc, _ = _run([FFMPEG, "-y", "-ss", f"{dur * ratio:.2f}", "-i", video_path,
                      "-frames:v", "1", "-vf", "scale=480:-2", png])
        if rc != 0 or not os.path.exists(png):
            continue
        try:
            with Image.open(png) as im:
                gray = im.convert("L")
                b = _band_rows(gray, gray.width, gray.height)
                if b:
                    bands.append((b[0] / gray.height, b[1] / gray.height))
        except Exception:
            continue
    if len(bands) < 3:
        print("[字幕] 未探测到原字幕（画面干净），不做任何羽化")
        return None
    top = min(b[0] for b in bands)
    bot = max(b[1] for b in bands)
    band_y = int(top * ch) - int(ch * 0.01)
    band_h = int((bot - top) * ch) + int(ch * 0.02)
    band_y = max(0, min(band_y, ch - 4))
    band_h = max(4, min(band_h, ch - band_y))
    if band_h > ch * 0.25:
        print("[字幕] 底部亮区过大，判定不是字幕条，不做羽化")
        return None
    margin_v = max(0, ch - (band_y + band_h))
    print(f"[字幕] 探测到原字幕条 y={band_y} 高={band_h}px：只羽化这一条，画面保持清晰；"
          f"解说字幕对齐到原字幕同一行")
    return {"band_y": band_y, "band_h": band_h, "margin_v": margin_v}


def _seg_out_duration(win, vdur):
    """解说段成片时长：旁白比窗口长才放慢画面（否则原速播放，旁白结束后原声回满）。"""
    win = max(win, 0.1)
    return max(win, vdur if vdur > 0 else 0.0)


def _seg_cmd_narration(video_path, s, e, vdur, vpath, narration, vertical, inter_path,
                       feather=None, canvas=None, src_has_audio=True):
    """解说段命令（高光段 / 全片段共用一条渲染路径）。

    四条默认铁律全在这里落地：
      1. 单行字幕：长旁白切块后按时间轮播，任意时刻画面上只有一行；
      2. 同行显示：字幕纵向位置取自原字幕 .ass 的 MarginV（拿不到用底部 8%）；
      3. 只羽化原字幕：原字幕带用 delogo/inpaint **彻底擦除**；
         解说字幕显示在**原字幕带上方**，不再同行覆盖，避免视觉上像"把原字幕又配了一遍"；画面其余部分保持清晰；
      4. 弱化原声：解说播放期间把原声压到 ORIGINAL_DUCK，旁白结束后自动回满音量。
    """
    win = max(e - s, 0.1)
    # 旁白比窗口长 → 放慢画面把窗口撑到旁白时长；否则原速(k=1)，绝不无故拉伸。
    k = (vdur / win) if (vdur > 0 and vdur > win) else 1.0
    out_dur = win * k
    cw, ch = canvas if canvas else ((480, 854) if vertical else _probe_size(video_path))
    size = _subtitle_size(ch)
    base_margin = (feather or {}).get("margin_v") or int(ch * 0.08)
    if feather:
        # 解说字幕放在原字幕带「上方」，留足视觉间距，避免与被羽化的原字幕
        # 紧贴在一起被误认为"重新配了一遍原字幕"。
        # y_bottom 是字幕底边距画面底边的距离；band_y 是原字幕带顶边距画面顶边的距离。
        # 竖屏用 12% 间距（太大会跳到画面中上部，喧宾夺主），横屏用 20%。
        gap_pct = 0.12 if vertical else 0.20
        gap = max(int(ch * gap_pct), int(size * 3))
        y_bottom = int(ch - feather["band_y"] + gap)
        # 安全 clamp：不低于默认底部边距，且整行不超出画面顶。
        y_bottom = max(y_bottom, base_margin)
        y_bottom = min(y_bottom, ch - size)
    else:
        y_bottom = base_margin

    # ---- 单行字幕切块 + 时间分配（字幕只在旁白播放期间出现）----
    max_chars = max(8, int((cw - 40) / size))
    chunks = _one_line_chunks(narration, max_chars) or [narration]
    if len(chunks) > 8:  # 限制输入数量，超长旁白把尾巴并成一块
        chunks = chunks[:7] + ["".join(chunks[7:])[:max_chars]]
    sub_span = min(vdur, out_dur) if vdur > 0 else out_dur
    total_chars = sum(len(c) for c in chunks) or 1
    spans, t0 = [], 0.0
    for idx, c in enumerate(chunks):
        t1 = sub_span if idx == len(chunks) - 1 else min(sub_span, t0 + sub_span * len(c) / total_chars)
        spans.append((t0, t1))
        t0 = t1

    v_filters = "[0:v]setpts=PTS-STARTPTS"
    if vertical:
        v_filters += ",scale=480:854:force_original_aspect_ratio=increase,crop=480:854"
    if k != 1.0:
        v_filters += f",setpts={k:.4f}*PTS"
    v_filters += "[vbase]"
    cur = "[vbase]"

    if feather:
        # 只把原字幕那一条横带裁下来高斯模糊再贴回去：画面其余部分一像素不动。
        # enable 限定在旁白播放期间 —— 「有解说字幕时才羽化原字幕」。
        by, bh = feather["band_y"], feather["band_h"]
        sigma = max(6, bh // 3)
        v_filters += (
            f";[vbase]split[fb0][fb1]"
            f";[fb1]crop={cw}:{bh}:0:{by},gblur=sigma={sigma}[fbblur]"
            f";[fb0][fbblur]overlay=0:{by}:enable='between(t,0,{sub_span:.3f})'[vfe]"
        )
        cur = "[vfe]"

    cmd_inputs = []
    for i, (chunk, (a, b)) in enumerate(zip(chunks, spans)):
        png_path = _next_sub_png()
        _render_subtitle_png(chunk, png_path, w=cw, h=ch, size=size, y_bottom=y_bottom)
        cmd_inputs += ["-i", png_path]
        nxt = "[vout]" if i == len(chunks) - 1 else f"[vo{i}]"
        # 字幕 PNG 是单帧静态图，overlay 默认 eof_action=repeat 会保持最后一帧可见；
        # 若用 eof_action=pass，PNG 到达 EOF 后 overlay 直接透传主视频，字幕会“叠了但看不见”。
        # enable='between(t,a,b)' 已经精确控制显示窗口，无需 pass。
        v_filters += (f";{cur}[{2 + i}:v]"
                      f"overlay=0:0:enable='between(t,{a:.3f},{b:.3f})'{nxt}")
        cur = nxt

    # 原声：解说期间压低，旁白结束后恢复原音量（volume 支持 timeline enable）
    if src_has_audio:
        duck = f"volume=volume={ORIGINAL_DUCK:.2f}:enable='lt(t,{sub_span:.3f})'"
        a_filters = (
            f"[0:a]asetpts=PTS-STARTPTS,{duck}"
            + (f",{_atempo_chain(1.0 / k)}" if k != 1.0 else "")
            + ",aresample=44100[oa];"
            f"[1:a]aresample=44100[a1];"
            f"[oa][a1]amix=inputs=2:duration=longest:dropout_transition=0[a]"
        )
    else:
        # 源无音轨：没有原声可弱化，直接用解说音轨（静音源无需混流）
        a_filters = f"[1:a]aresample=44100[a]"
    return [
        FFMPEG, "-y", "-ss", f"{s:.3f}", "-t", f"{win:.3f}", "-i", video_path,
        "-i", vpath, *cmd_inputs,
        "-filter_complex", v_filters + ";" + a_filters,
        "-map", "[vout]", "-map", "[a]", *_VCODEC,
        "-t", f"{out_dur:.3f}",
        "-r", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "44100", "-ac", "2",
        inter_path,
    ]


def _intro_outro_bounds(dur_total, enabled):
    """跳过片头片尾：首尾各砍 min(90s, 8% 时长)；短片或砍完不足 10s 时自动放弃。"""
    if not enabled or dur_total <= 30:
        return 0.0, dur_total
    cut = min(90.0, dur_total * 0.08)
    if dur_total - 2 * cut < 10:
        return 0.0, dur_total
    return cut, dur_total - cut


def _apply_retain_pct(pieces, dur_total, pct):
    """保留全片时长百分比：先砍最长的原片间隙，仍超标再砍最短的解说段。

    pieces: [{kind,s,e,out,...}]，kind 为 'narration' / 'filler'。返回过滤后的列表。
    """
    if not pct:
        return pieces
    target = dur_total * pct / 100.0
    total = sum(p["out"] for p in pieces)
    if total <= target:
        print(f"[保留时长] 目标 {pct:.0f}%（{target:.0f}s），当前 {total:.0f}s，无需裁剪")
        return pieces
    keep = list(pieces)
    for p in sorted([x for x in keep if x["kind"] == "filler"],
                    key=lambda x: x["out"], reverse=True):
        if total <= target:
            break
        keep.remove(p)
        total -= p["out"]
    narr = sorted([x for x in keep if x["kind"] == "narration"], key=lambda x: x["out"])
    for p in narr:
        if total <= target or len([x for x in keep if x["kind"] == "narration"]) <= 1:
            break
        keep.remove(p)
        total -= p["out"]
    print(f"[保留时长] 目标 {pct:.0f}%（{target:.0f}s），裁剪后约 {total:.0f}s"
          f"（原计划 {sum(x['out'] for x in pieces):.0f}s）")
    return keep


def build(video_path, script_path, out_path,
          vertical=False, own_voice=False, voice_override=None,
          original_speed=False, mode=None, highlights=None,
          commentary_type=None, highlight_source=None, intro_highlight=None,
          skip_intro_outro=None, retain_pct=None):
    ensure_dirs()
    with open(script_path, encoding="utf-8") as f:
        script = json.load(f)
    voice = voice_override or script.get("voice", VOICE)
    segs = script.get("segments", [])
    if not segs:
        print("[错误] script.json 没有 segments")
        return False

    dur_total = _probe_duration(video_path)
    src_audio = _has_audio(video_path)
    inter_dir = os.path.join(WORK, "_seg_ff")
    os.makedirs(inter_dir, exist_ok=True)

    # ======== Phase 0: 过滤有效段、准备索引 ========
    valid_segs = []
    for i, seg in enumerate(segs):
        narration = seg.get("narration", "").strip()
        if not narration:
            continue
        valid_segs.append((i, seg, narration))
    if not valid_segs:
        print("[错误] 没有有效片段")
        return False
    print(f"共 {len(valid_segs)} 段，开始批量生成旁白（并发 {_TTS_CONCURRENCY}）...")

    # ======== Phase 1: 批量 TTS（asyncio.gather 全段并发）========
    voice_tasks = []
    voice_results = {}  # i -> (ok, voice_path)
    for i, seg, narration in valid_segs:
        own = os.path.join(WORK, f"{i}.mp3")
        aip = os.path.join(WORK, f"voice_{i}.mp3")
        if own_voice and _valid_audio(own):
            voice_results[i] = (True, own)
        else:
            voice_tasks.append((narration, aip, voice, i))
    if voice_tasks:
        sem = asyncio.Semaphore(_TTS_CONCURRENCY)
        tts_inputs = [(t, p, v) for t, p, v, _ in voice_tasks]
        indexing = [idx for _, _, _, idx in voice_tasks]
        ok_list = asyncio.run(_gen_all_voices(tts_inputs, sem))
        for idx, ok, (_, p, _, _) in zip(indexing, ok_list, voice_tasks):
            voice_results[idx] = (ok, p if ok else "")
    print(f"旁白生成完成（{sum(1 for ok, _ in voice_results.values() if ok)}/{len(valid_segs)} 有效）")

    # ======== Phase 2: 解析剪辑选项 → 排时间轴 → 生成 ffmpeg 命令 ========
    opts = copts.resolve(script, mode=mode, commentary_type=commentary_type,
                         highlight_source=highlight_source,
                         intro_highlight=intro_highlight,
                         skip_intro_outro=skip_intro_outro,
                         retain_pct=retain_pct)
    family = opts["family"]
    mode = copts.legacy_mode(opts["commentary_type"], opts["intro_highlight"])
    lo, hi = _intro_outro_bounds(dur_total, opts["skip_intro_outro"])
    if lo > 0:
        print(f"[跳过片头片尾] 只用 {lo:.0f}s ~ {hi:.0f}s 的内容")
    # 默认必须项：只羽化原字幕（画面不羽化），四种解说类型一视同仁
    canvas = (480, 854) if vertical else _probe_size(video_path)
    feather = _prepare_feather(video_path, vertical, canvas)

    # --- 2a. 解说段：定窗口、对齐旁白时长、防重叠 ---
    narr = []
    for i, seg, narration in valid_segs:
        ok, vpath = voice_results.get(i, (False, ""))
        if not ok:
            print(f"[跳过] 第{i+1}段 旁白生成失败"); continue
        try:
            vdur = _probe_duration(vpath)
        except Exception:
            print(f"[跳过] 第{i+1}段 旁白音频不可读"); continue
        s = max(float(seg.get("start", 0)), lo)
        if original_speed:
            e = s + vdur                      # 画面原速：窗口完全跟随旁白
        elif family == "highlights":
            e = max(float(seg.get("end", s + vdur)), s + vdur)  # 高光窗口至少装得下旁白
        else:
            e = float(seg.get("end", s + vdur))
        e = min(e, hi - 0.1)
        if e - s < 0.2:
            print(f"[跳过] 第{i+1}段窗口过短或落在片头片尾（{s:.1f}~{e:.1f}s）"); continue
        narr.append({"i": i, "s": s, "e": e, "vdur": vdur, "vpath": vpath,
                     "narration": narration, "note": seg.get("note", "")})
    narr.sort(key=lambda x: x["s"])
    for k in range(1, len(narr)):   # 防重叠：把上一段尾收紧到下一段起点前 0.05s
        if narr[k]["s"] < narr[k - 1]["e"]:
            cap = narr[k]["s"] - 0.05
            narr[k - 1]["e"] = cap if cap > narr[k - 1]["s"] else narr[k - 1]["s"] + 0.2
    if not narr:
        print("[错误] 没有可用的解说段（旁白全部失败或全被片头片尾裁掉）")
        return False

    # --- 2b. 排时间轴：解说段 + 中间原片段（原声全音量、无字幕）---
    pieces = []
    for h in narr:
        pieces.append({"kind": "narration", "idx": h["i"], "s": h["s"], "e": h["e"],
                       "out": _seg_out_duration(h["e"] - h["s"], h["vdur"]),
                       "ref": h, "label": h["narration"][:24]})
    filler_idx = 100000
    cursor = lo
    for h in narr:
        if h["s"] - cursor >= 0.3:
            pieces.append({"kind": "filler", "idx": filler_idx, "s": cursor, "e": h["s"],
                           "out": h["s"] - cursor,
                           "label": f"原片 {cursor:.0f}-{h['s']:.0f}s"})
            filler_idx += 1
        cursor = max(cursor, h["e"])
    if hi - cursor >= 0.3:
        pieces.append({"kind": "filler", "idx": filler_idx, "s": cursor, "e": hi,
                       "out": hi - cursor, "label": f"原片 {cursor:.0f}-{hi:.0f}s"})

    # --- 2c. 保留全片时长百分比（先砍最长空白，再砍最短解说）---
    pieces = _apply_retain_pct(pieces, dur_total, opts["retain_pct"])
    pieces.sort(key=lambda p: p["s"])

    # --- 2d. 生成渲染命令（片头精彩片段为可选前置）---
    render_tasks = []   # list of (cmd, idx, narration_summary)
    start_of = {}       # idx -> 时间轴上的起始秒（用于拼接排序）
    if opts["intro_highlight"]:
        cands = [p for p in pieces if p["kind"] == "narration"]
        pick = next((p for p in cands if p["ref"]["note"] == "开场钩子"), None) \
            or (max(cands, key=lambda p: p["out"]) if cands else None)
        if pick:
            h = pick["ref"]
            ip = os.path.join(inter_dir, "seg_-1.mp4")
            render_tasks.append((_seg_cmd_narration(
                video_path, h["s"], h["e"], h["vdur"], h["vpath"], h["narration"],
                vertical, ip, feather=feather, canvas=canvas,
                src_has_audio=src_audio), -1, "片头·精彩片段"))
            start_of[-1] = -1.0
            print(f"[片头高光] 第{h['i']+1}段（{h['s']:.0f}s 处）复制到片头当钩子")

    for p in pieces:
        ip = os.path.join(inter_dir, f"seg_{p['idx']}.mp4")
        if p["kind"] == "narration":
            h = p["ref"]
            cmd = _seg_cmd_narration(video_path, h["s"], h["e"], h["vdur"], h["vpath"],
                                     h["narration"], vertical, ip,
                                     feather=feather, canvas=canvas,
                                     src_has_audio=src_audio)
        else:
            cmd = _build_filler_cmd(video_path, p["s"], p["e"], vertical, ip,
                                    src_has_audio=src_audio)
        render_tasks.append((cmd, p["idx"], p["label"]))
        start_of[p["idx"]] = p["s"]

    n_narr = sum(1 for p in pieces if p["kind"] == "narration")
    n_fill = sum(1 for p in pieces if p["kind"] == "filler")
    print(f"[{opts['label']}] 解说 {n_narr} 段 + 原片 {n_fill} 段"
          + ("，片头插精彩片段" if opts["intro_highlight"] else "")
          + ("，已跳过片头片尾" if lo > 0 else "")
          + (f"，保留约 {opts['retain_pct']:.0f}%" if opts["retain_pct"] else "")
          + ("，原字幕已羽化" if feather else ""))

    # 按时间轴排序，保证拼接顺序 == 视频原顺序（高光段与 filler 交错）
    render_tasks.sort(key=lambda t: start_of.get(t[1], 0))

    if not render_tasks:
        print("[错误] 没有可渲染的片段")
        return False

    # ======== Phase 3: 并行 ffmpeg 切条渲染(支持断点续作) ========
    # 签名带上剪辑选项：选项一改，旧分段缓存立即作废，避免拿着上一套参数的半成品拼接
    sig = json.dumps({"hw": _USE_VT, "vertical": vertical,
                      "ospeed": original_speed, "mode": mode,
                      "feather": bool(feather),
                      "opts": {k: opts[k] for k in
                               ("commentary_type", "intro_highlight",
                                "skip_intro_outro", "retain_pct")}},
                     sort_keys=True)
    progress_path = _seg_progress_path(script_path)
    seg_done = _seg_progress_load(progress_path, sig)
    print(f"开始渲染 {len(render_tasks)} 段（ffmpeg 并发 {_FFMPEG_CONCURRENCY}, "
          f"硬件编码={'开' if _USE_VT else '关'}）...")
    done_idx = set(seg_done)
    skipped = 0
    to_render = []
    for cmd, idx, _ in render_tasks:
        ip = os.path.join(inter_dir, f"seg_{idx}.mp4")
        if idx in done_idx and _valid_video(ip):
            skipped += 1
        else:
            to_render.append((cmd, idx))
    print(f"  已完成 {skipped} 段(跳过), 待渲染 {len(to_render)} 段")
    completed = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=_FFMPEG_CONCURRENCY) as ex:
        future_map = {ex.submit(_render_segment, (cmd, idx)): idx
                      for cmd, idx in to_render}
        for fut in as_completed(future_map):
            rc, err, idx, _ = fut.result()
            if rc != 0:
                failed += 1
                print(f"[错误] 第{idx+1}段渲染失败:\n{err[-800:]}")
            else:
                completed += 1
                done_idx.add(idx)
                _seg_progress_save(progress_path, sig, done_idx)
                seg_narration = next((n for _, i2, n in render_tasks if i2 == idx), "?")
                print(f"  ✓ [{completed}/{len(to_render)}] 第{idx+1}段「{seg_narration}...」")
    if len(done_idx) != len(render_tasks):
        print(f"[未完成] {len(render_tasks) - len(done_idx)} 段未成功, "
              f"本轮结束(守护模式将自动断点续作)")
        return False

    # ======== Phase 4: 拼接(不重编码) ========
    intermediates = [os.path.join(inter_dir, f"seg_{i}.mp4")
                     for _, i, _ in render_tasks if i in done_idx]
    if not intermediates:
        print("[错误] 没有可用片段")
        return False
    print(f"拼接成片（{len(intermediates)} 段）...")
    list_path = os.path.join(inter_dir, "list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in intermediates:
            # Windows 路径反斜杠会被 ffmpeg concat demuxer 当作转义符，统一转成正斜杠
            f.write(f"file '{p.replace(chr(92), '/')}'\n")
    cmd = [
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy",
        out_path,
    ]
    rc, err = _run(cmd)
    if rc != 0:
        print(f"[错误] 拼接失败:\n{err[-800:]}")
        return False
    print(f"\n✅ 成片已生成(ffmpeg+并行优化+断点续作): {out_path}")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python scripts/edit_ffmpeg.py <视频> <script.json> [输出mp4]")
        sys.exit(1)
    vp = sys.argv[1]
    sp = sys.argv[2]
    op = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        OUTPUT, os.path.splitext(os.path.basename(vp))[0] + "_ffmpeg成片.mp4")
    build(vp, sp, op)
