"""server/routers/subtitle.py — 本地视频字幕提取（faster-whisper ASR，MIT 可商用）。

流程：本地视频 → ffmpeg 抽 16k mono 音频 → **能量分段** → faster-whisper（CPU int8）
→ SRT / TXT 输出。模型按需从 HF（默认走 hf-mirror）下载到用户缓存，不进 DMG。

⚠️ 2026-09-23 重写识别链路（回答「为什么接歌词库、录音/英文/嘈杂怎么办」）：
  此前的核心错配是拿 **Silero VAD** 当分段器——它判断的是「像不像人在说话」，
  而字幕要的是「有没有声音」。于是歌曲人声+伴奏被整段丢弃（阿刁只放行 3.7%、
  爱死了昨天 0 段），只能靠一层层打补丁去救（占比<15% / 尾部有能量 / 空结果也兜底）。
  现改为能量分段（_energy_segments），对上述场景一律成立；解码参数同时修正
  （condition_on_previous_text 恒 False 等）。详见 _energy_segments 与步骤 3 的注记。
  歌词库（lrclib）保留为**可选快通道**：只对已收录的出版歌曲生效，可在界面关闭。

job 机制独立于 CONVERT_JOBS：SUBTITLE_JOBS + app.executor，设备隔离与 convert 一致
（X-Device-Id 头查状态 / device= query 下载）。
"""
import app
import json
import os
import re
import time
import shutil
import subprocess
import threading
from pathlib import Path as _Path

from fastapi import APIRouter
from pydantic import BaseModel
from .core import _device_of
from stats import record_event

router = APIRouter()

# 结果预览最多返回多少句（2026-09-22 二次修订）。
#
# 初版设 600，实测被用户判为「没有全片提取」：45 分钟剧集有 872 句，预览滚到底停在
# 第 600 句（31:08），看上去就像识别到一半就断了 —— 明明 SRT 完整到片尾。
# ⇒ 上限必须高到「任何正常片子都碰不到」。按本机实测密度（872 句 / 45.5 分钟 ≈ 0.32 句/秒），
# 5000 句 ≈ 4.3 小时连续对白，单集/单部电影/长直播都覆盖得住；响应约 0.55MB，本机回环无感。
# 仍保留这个上限只为兜住异常输入（十几小时的连续语音），此时 UI 会明确写出「仅显示前 N 句」。
_PREVIEW_MAX_LINES = 5000

# 无 VAD 兜底轮的「幻觉套话」黑名单（2026-09-23）。
# Whisper 在纯伴奏/静音段会高频输出这些训练语料残留（YouTube 片尾感谢、字幕组署名等），
# 属于确定性垃圾内容，与置信度无关，直接按文本匹配丢弃。
_HALLUCINATION_RE = re.compile(
    r"(?i)(thank you for watching|thanks for watching|subscribe to (my|the|our) (channel|channel))"
    r"|(television series exclusive|exclusive broadcast|all rights reserved)"
    # 中文套话：实测在歌曲首尾幻觉出「请不吝点赞 订阅 转发 打赏支持…栏目」「优优独播剧场」。
    # ⚠️ 正则一律写简体：匹配前已由 _to_simplified() 把整句转简，故繁体输出同样命中。
    r"|(请不吝点赞|点赞.{0,6}(订阅|转发)|打赏支持|请订阅|感谢观看|谢谢观看|订阅我的频道"
    r"|字幕由|amara\.org|请点赞|关注频道|请按赞|订阅按赞"
    r"|独播剧场|点栏目|栏目组|请支持|别忘了.{0,4}订阅|一键三连)"
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

try:                                   # 纯 Python 繁→简（zhconv，约 1MB），缺失时静默跳过
    from zhconv import convert as _zhconv_convert
except Exception:                      # noqa: BLE001
    _zhconv_convert = None


def _to_simplified(text: str) -> str:
    """繁→简归一化。faster-whisper 在噪声/伴奏下会整句输出繁体，与简体行混排
    （实测中文+6dB 噪声：「今天我們要討論的是人工智能在語音識別領域…」）。
    zhconv 缺失时原样返回——宁可不转，也不能让字幕提取失败。"""
    if not text or _zhconv_convert is None:
        return text
    try:
        return _zhconv_convert(text, "zh-cn")
    except Exception:                  # noqa: BLE001
        return text

# ── 歌词库直取（2026-09-23）——歌曲字幕的正解，不是听写 ──────────────────────────
# 背景：Whisper 是「听写」模型，听歌必然翻车——实测《爱死了昨天》把
#   「是我 爱死了昨天 / 誓言 割碎你的脸」听成「是我暗死了昨天 是眼隔碎你的脸」，
#   《天使的翅膀》「只留给天空美丽一场」→「只留给天空每一层飞舞的身影」；
#   large-v3 更糟，伴奏段会自信地幻觉出整段英文（"The only thing I know...")。
# 而歌词是现成的权威文本，还自带逐行时间轴（LRC）。命中即秒级产出、逐字准确。
# 数据源 lrclib.net：开放歌词库，免费、无需 key。失败/无网一律静默回落 ASR。
_LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
_LRCLIB_TIMEOUT = 6.0          # 短超时：歌词只是快路径，不能拖慢主流程
_LRCLIB_MAX_DELTA = 6.0        # 时长容差（秒）：超出则判为翻唱/现场版，不采用
_LRC_LINE_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]\s*(.*)")


def _clean_track_keywords(name: str) -> str:
    """从文件名猜「歌名 + 歌手」搜索词。

    实测样本：爱死了昨天-李慧珍-254611 / [m4a]阿刁-赵雷-16827758 / 安琥 - 天使的翅膀[weiyun]
    处理：去扩展名 → 去方括号/括号标记（[weiyun]、(Live)）→ 去纯数字 ID 段 → 分隔符转空格。
    """
    s = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name)
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)|（[^）]*）|【[^】]*】", " ", s)
    s = re.sub(r"(?<!\d)\d{4,}(?!\d)", " ", s)
    s = re.sub(r"[_－—–\-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_lrc(lrc: str, duration: float = 0.0) -> list:
    """解析 LRC（[mm:ss.xx] 文本）→ [(start, end, text)]。

    end 取下一行起点（首行间距过大时钳到 ≤20s），末行补 6s；元数据行（[ti:] 等）跳过。
    """
    rows = []
    for line in (lrc or "").splitlines():
        m = _LRC_LINE_RE.match(line.strip())
        if not m:
            continue
        text = (m.group(4) or "").strip()
        if not text or re.match(r"(?i)(ti|ar|al|by|offset|re|ve)\s*:", text):
            continue
        frac = (m.group(3) or "0").ljust(3, "0")[:3]
        start = int(m.group(1)) * 60 + int(m.group(2)) + int(frac) / 1000.0
        rows.append([start, 0.0, text])
    for i, r in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else (duration or r[0] + 6.0)
        r[1] = max(r[0] + 0.4, min(nxt, r[0] + 20.0))
    return [(a, b, c) for a, b, c in rows]


def _fetch_synced_lyrics(keywords: str, duration: float) -> tuple:
    """查开放歌词库，返回 (rows, 曲目描述)；未命中/无网/超时 → ([], "") 由调用方回落 ASR。

    按 duration 选最贴近的版本，避免把原唱配成翻唱 / 现场版。
    """
    if not keywords:
        return [], ""
    import urllib.parse
    import urllib.request
    try:
        url = _LRCLIB_SEARCH_URL + "?" + urllib.parse.urlencode({"q": keywords})
        req = urllib.request.Request(url, headers={"User-Agent": "VideoDownloader/1.0"})
        with urllib.request.urlopen(req, timeout=_LRCLIB_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001  无网 / DNS 失败 / 超时，一律静默回落
        app.logger.info("lyrics lookup failed (%s): %s", keywords, e)
        return [], ""
    if not isinstance(data, list):
        return [], ""
    best, best_delta = None, 1e9
    for item in data:
        d = item.get("duration") or 0
        if not d:
            continue
        delta = abs(float(d) - duration) if duration > 0 else 0.0
        if delta < best_delta:
            best, best_delta = item, delta
    if best is None or (duration > 0 and best_delta > _LRCLIB_MAX_DELTA):
        return [], ""
    rows = _parse_lrc(best.get("syncedLyrics") or "", duration)
    if not rows:
        return [], ""
    desc = " - ".join(x for x in (best.get("artistName") or "", best.get("trackName") or "") if x)
    return rows, desc


def _wav_duration(path: _Path) -> float:
    """管线固定抽成 16k/mono/pcm_s16le → 时长 ≈ 字节数 / 32000（44 字节头忽略不计）。"""
    try:
        return max(0.0, path.stat().st_size / (16000 * 2))
    except Exception:  # noqa: BLE001
        return 0.0


def _read_wav_f32(wav_path):
    """读 16k mono s16le wav → (float32 数组[-1,1], 采样率)；失败返回 (None, 0)。"""
    try:
        import wave as _wave
        import numpy as _np
        with _wave.open(str(wav_path), "rb") as w:
            sr = w.getframerate() or 16000
            a = _np.frombuffer(w.readframes(w.getnframes()), dtype=_np.int16)
            return a.astype(_np.float32) / 32768.0, sr
    except Exception:  # noqa: BLE001
        return None, 0


def _energy_segments(wav_path, min_sil_s: float = 0.35, max_seg_s: float = 28.0,
                     pad_s: float = 0.10):
    """「有声段」检测 —— 取代 Silero VAD 的分段器。返回 [(start_s, end_s), ...]。

    🔴 为什么必须换掉 faster-whisper 自带的 Silero VAD（2026-09-23）：
    Silero 的训练目标是「这段像不像人在说话」，是个**说话声分类器**。字幕场景要的
    根本不是这件事 —— 我们要的是「这段有没有声音」。两者在以下场景直接冲突：
      · 歌曲「人声+伴奏」→ Silero 判为非语音 → 整段丢弃
      · 嘈杂环境录音（噪声占比高）→ 置信度掉到阈值下 → 丢弃
      · 纯音乐/伴奏段 → 丢弃
    实测三首歌：阿刁(6:18) VAD 只放行 14s（3.7%）、天使的翅膀只到 2:22、
    爱死了昨天放行 **0 段**。此前为绕开它糊了三层补丁（占比<15% / 尾部有能量 /
    空结果也兜底），本质是拿补丁去补一个**根本性错配**。

    能量法只做「有没有能量」的判断，对上述场景一律成立，且无需额外模型依赖。
    自适应阈值：thr = max(p05 * 4, p95 * 0.05) —— 前项压住安静录音的底噪，
    后项压住「整段都很响」的歌曲/噪声（避免全片判为有声而失去分段意义）。
    """
    import numpy as _np
    a, sr = _read_wav_f32(wav_path)
    if a is None or a.size < sr:          # 读不出或不足 1 秒 → 交给调用方整段处理
        return []
    total = a.size / float(sr)
    frame = max(1, int(0.020 * sr))       # 20ms 帧
    hop = max(1, frame // 2)
    if a.size < frame:
        return [(0.0, total)]
    n_frames = 1 + (a.size - frame) // hop
    idx = _np.arange(frame)[None, :] + hop * _np.arange(n_frames)[:, None]
    rms = _np.sqrt(_np.mean(a[idx] * a[idx], axis=1))
    p05 = float(_np.percentile(rms, 5))
    p50 = float(_np.percentile(rms, 50))
    p95 = float(_np.percentile(rms, 95))
    if p95 <= 0:
        return []
    thr = max(p05 * 4.0, p95 * 0.05)
    # ⚠️ 必须再压一道「中位能量」天花板：p05*4 在**动态范围小**的音频上会荒谬地
    #   偏高。全程有声音的素材（歌曲、持续讲话）没有真正的静音段，p05 本身就
    #   是有声区间的低谷，再乘 4 会越过中位能量 → 整片被判成静音（实测合成曲
    #   覆盖率只剩 57%，被切成 83 个碎段）。上限取中位能量的 60%，保证「多数
    #   时候的能量」一定算有声；真正的静音段仍远低于它。
    thr = min(thr, p50 * 0.6)
    if thr <= 0:
        return []
    voiced = rms > thr

    # 1) 把短于 min_sil_s 的静音缺口填平（句间停顿不切段）
    gap_frames = max(1, int(min_sil_s * sr / hop))
    i = 0
    while i < len(voiced):
        if voiced[i]:
            j = i
            while j < len(voiced) and voiced[j]:
                j += 1
            k = j
            while k < len(voiced) and not voiced[k]:
                k += 1
            if k < len(voiced) and (k - j) <= gap_frames:
                voiced[j:k] = True
            i = k
        else:
            i += 1

    # 2) 连通域 → 段；丢弃过短碎段（<0.25s）
    segs = []
    i = 0
    while i < len(voiced):
        if voiced[i]:
            j = i
            while j < len(voiced) and voiced[j]:
                j += 1
            st = i * hop / float(sr)
            ed = min(total, (j - 1) * hop / float(sr) + frame / float(sr))
            if ed - st >= 0.25:
                segs.append([st, ed])
            i = j
        else:
            i += 1

    # 3) 前后补 pad（Whisper 句首句尾需要一点上下文），并按边界钳制、合并紧邻段。
    #    ⚠️ pad 必须在**切分之前**做：切分产生的相邻段首尾是同一个时间点，
    #    若先切后 pad，前后各扩 0.1s 会互相重叠 → 下面的合并判断会把它们
    #    又粘回一整段（实测阿刁被粘回 313s 单段，分片完全失效）。
    res = []
    for st, ed in segs:
        s2 = max(0.0, st - pad_s)
        e2 = min(total, ed + pad_s)
        if res and s2 <= res[-1][1]:      # 与上一段重叠/紧邻 → 合并
            res[-1][1] = max(res[-1][1], e2)
        else:
            res.append([s2, e2])

    # 4) 超长段（整首歌/长录音）在段内能量最低点切开，避免 Whisper 30s 窗口硬切句
    out = []
    for st, ed in res:
        while ed - st > max_seg_s:
            # 在 [st + 0.6·max, st + 1.4·max] 窗口内挑能量最低点当切点：
            # 既保证每段长度接近 max_seg_s（不会退化成 200s 巨段），
            # 又尽量落在句间/换气处（不会把一个词硬切成两半）。
            w0 = int((st + max_seg_s * 0.6) * sr / hop)
            w1 = min(len(rms), int((st + max_seg_s * 1.4) * sr / hop))
            if w1 - w0 >= 4:
                # w0/w1 是**全局**帧下标（rms 与整条音频对齐），换算回时间直接
                # 乘 hop/sr，不要再叠加 st —— 叠加会把切点推到 2·st 之后，
                # 表现为「长段根本切不开」（实测阿刁切出 124s / 100s 巨段）。
                cut_t = (w0 + int(_np.argmin(rms[w0:w1]))) * hop / float(sr)
            else:
                cut_t = st + max_seg_s
            if cut_t <= st + 1.0 or cut_t >= ed - 0.5:
                break                     # 切不动了 → 交给 Whisper 自己滑窗
            out.append([st, cut_t])
            st = cut_t
        out.append([st, ed])
    return [(round(s, 3), round(e, 3)) for s, e in out if e - s > 0.1]


def _wav_region_rms(wav_path, start_s: float, end_s: float) -> float:
    """读 16k mono s16 wav 的 [start_s, end_s) 区间均方根振幅（0-32768 量纲）。
    用于判断「识别没覆盖的尾部」是静音还是音乐——静音不值得重识别。"""
    try:
        import wave as _wave
        import numpy as _np
        with _wave.open(str(wav_path), "rb") as w:
            sr = w.getframerate() or 16000
            total_frames = w.getnframes()
            start = max(0, min(int(start_s * sr), total_frames - 1))
            n = max(0, min(int((end_s - start_s) * sr), total_frames - start))
            if n <= 0:
                return 0.0
            w.setpos(start)
            a = _np.frombuffer(w.readframes(n), dtype=_np.int16).astype(_np.float32)
            return float(_np.sqrt(_np.mean(a * a))) if a.size else 0.0
    except Exception:
        return -1.0   # 读不出当「有能量」处理，宁可多重识别不漏内容

# 国内加速：所有走 huggingface_hub 的下载（faster-whisper 字幕模型、扩散去水印模型）
# 统一走 hf-mirror。
#
# ⚠️ 这是**进程级**设置（环境变量 + huggingface_hub 模块常量），会影响同进程内**所有**
# 使用 huggingface_hub 的功能，因此只在进程启动（本模块 import 期）施加一次，
# **绝不在请求处理期修改** —— 历史缺陷（2026-09-16 修）：原实现放在 _get_model() 里，
# 于是「先跑过一次字幕任务」会顺手把后续其他功能的下载端点也改掉，形成跨功能隐式副作用。
def _apply_hf_mirror() -> None:
    """幂等地把 huggingface_hub 端点指向国内镜像；用户显式配置时以用户为准。"""
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    want = (os.environ.get("HF_ENDPOINT") or "").strip()
    if not want:
        return
    try:
        # 主动 import：确保在本模块 import 时就完成常量修正。否则若其它模块先 import 了
        # huggingface_hub，模块级 constants 会固化成官方端点，之后再设环境变量也不生效。
        from huggingface_hub import constants as _hf_const

        if getattr(_hf_const, "ENDPOINT", "") != want:
            _hf_const.ENDPOINT = want
    except Exception:  # noqa: BLE001  未安装 huggingface_hub 时静默跳过
        pass


_apply_hf_mirror()

SUBTITLE_JOBS: dict = {}
_SUBTITLE_LOCK = threading.Lock()
_SUBTITLE_MODELS: dict = {}          # model_size -> WhisperModel（进程级缓存，避免重复加载）
ALLOWED_MODELS = {"base", "small", "medium", "large-v3", "large-v3-turbo"}
# 🔴 默认档位：small → medium（2026-09-23 实测，Apple M1 · 8GB · int8 · 4 线程 · beam5）
#
# 合成对照集（中文/英文 × 干净/叠 6dB 人声babble噪声，10s 左右）：
#
#   模型       中文干净        英文干净   中文+6dB噪                    英文+6dB噪  速度    体积
#   small      ✅但输出**繁体**  ✅       ❌崩：「人際智能在**靈異**識別領域」 ✅      2.0x    484MB
#   medium     ✅              ✅       ✅全对                        ✅      0.9x    1.5GB
#   large-v3   ✅              ✅       ⚠️错 1 字（人机←人工）          ✅      0.5x    3GB
#
# 两件事值得记住：
#   ① **「越大越准」是错的** —— large-v3 在这组中文噪声样本上反而不如 medium，
#      且慢 1.8 倍、模型体积大一倍（8GB 内存机器上加载就要 2.5 分钟）。
#   ② 默认 small 是此前「识别不准/错字连篇」的一大来源：它在噪声下直接崩坏，
#      而且中文输出繁体（我們/討論/識別），用户看到的就是「字都认错了」。
# ⇒ medium 是质量×速度×内存的甜点，改其为默认。
_DEFAULT_MODEL = "medium"


def _asr_thread_cap() -> int:
    """ASR 线程上限 = **性能核数**（不是 os.cpu_count() 的总核数）。

    🔴 2026-09-22 实测（Apple M1 · 8 核(4P+4E) · 8GB，20 分 50 秒音频，small/int8）：

        线程=8  → 244.7s   (5.1x 实时)
        线程=4  →  99.1s  (12.6x 实时)
        线程=2  →  38.6s  (32.4x 实时)

    原实现 `max(4, os.cpu_count())` = 8，把 4 个能效核也拉进 CTC 推理，再加上超订阅
    （8 worker + 主线程抢 4 个性能核），**实测反而慢 2.5 倍** —— 所谓「会员满速」当时
    是负优化。故上限取 `hw.perflevel0.physicalcpu`（Apple Silicon 的性能核数），
    读不到时回落到 `cpu_count()`，并统一封顶 8。
    """
    try:
        out = subprocess.run(["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                             capture_output=True, text=True, timeout=2)
        v = int((out.stdout or "").strip())
        if v > 0:
            return max(1, min(8, v))
    except Exception:  # noqa: BLE001  非 macOS / 无该键 → 走回落
        pass
    return max(1, min(8, os.cpu_count() or 4))


class SubtitleRequest(app.BaseModel):
    """桌面端本地视频字幕提取请求。"""
    local_path: str
    model_size: str = "small"        # base / small / medium / large-v3
    language: str = ""               # ""=自动检测 / "zh" / "en"
    to_library: bool = False
    fast: bool = True                # 快速模式：贪心解码，实测快约 2 倍（20min 音频 99s→45s），准确度略降
    # 歌词库直取开关（用户可在界面上关掉）。它**只对已收录的出版歌曲**生效：
    # 需同时满足「是纯音频文件」+「歌词库返回曲目」+「时长差 ≤6s」三个条件，
    # 录音、访谈、会议、外语节目、未收录曲目都不会命中，直接走 ASR。
    lyrics: bool = True


def _resolve_safe_local_path(path: str) -> _Path:
    p = _Path(path)
    if not p.is_file():
        raise app.HTTPException(status_code=400, detail=f"文件不存在或不是普通文件：{path}")
    try:
        resolved = p.resolve()
        if not str(resolved).startswith(("/Users/", "/home/", "/Volumes/", "C:\\")):
            raise app.HTTPException(status_code=400, detail=f"路径不在用户目录下：{path}")
        return resolved
    except app.HTTPException:
        raise
    except Exception:
        raise app.HTTPException(status_code=400, detail=f"无法解析路径：{path}")


def _get_model(model_size: str, cpu_threads: int = 4):
    """进程级模型缓存：首次加载/下载耗时，之后秒级。

    cpu_threads 按会员状态区分（免费 4 / 会员满核），模型实例按「模型:线程数」缓存。
    加载/下载失败自动重试 3 次，第 3 次回退本地缓存离线加载（之前下载过就能救回）。
    HF 镜像端点在模块 import 期由 _apply_hf_mirror() 一次性设置，此处不改任何全局状态。
    """
    cache_key = f"{model_size}:{cpu_threads}"
    with _SUBTITLE_LOCK:
        model = _SUBTITLE_MODELS.get(cache_key)
        if model is None:
            from faster_whisper import WhisperModel
            last_err: Exception | None = None
            for attempt in range(1, 4):
                local_only = attempt >= 3   # 第 3 次尝试纯离线（命中本地缓存即成功）
                try:
                    model = WhisperModel(model_size, device="cpu", compute_type="int8",
                                         cpu_threads=cpu_threads,
                                         local_files_only=local_only)
                    break
                except Exception as e:
                    last_err = e
                    app.logger.warning("subtitle model %s load attempt %d failed: %s",
                                       model_size, attempt, e)
                    time.sleep(2 * attempt)
            if model is None:
                raise RuntimeError(
                    f"识别模型（{model_size}）加载/下载失败（已重试 3 次）：{last_err}。"
                    "首次使用需联网下载模型（base≈145MB / small≈484MB / medium≈1.5GB / large-v3≈3GB），"
                    "请检查网络或代理后重试；网络不稳可先换 base 模型。"
                )
            _SUBTITLE_MODELS[cache_key] = model
        return model


def _fmt_ts(seconds: float) -> str:
    """SRT 时间戳 00:00:00,000。"""
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _parse_ts(raw: str) -> float:
    """SRT 时间戳（00:00:01,200）→ 秒；解析失败返回 0。"""
    try:
        hms, _, ms = raw.strip().replace(".", ",").partition(",")
        parts = (hms.split(":") + ["0", "0", "0"])[:3]
        h, m, s = (int(float(p or 0)) for p in parts)
        return h * 3600 + m * 60 + s + (int(float(ms or 0)) / 1000.0)
    except Exception:
        return 0.0


def _short_ts(raw: str) -> str:
    """预览用紧凑时间戳：不足 1 小时显示 mm:ss，超过则 h:mm:ss。"""
    total = int(_parse_ts(raw))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _write_outputs(job: dict, srt_path: _Path, txt_path: _Path, rows: list,
                   to_library: bool, stem: str) -> None:
    """写 SRT + TXT 并标记任务完成——ASR 结果与歌词库直取结果共用同一出口。"""
    job["stage"] = "生成字幕"
    job["progress"] = 96
    with open(srt_path, "w", encoding="utf-8") as fh:
        for i, (st, ed, text) in enumerate(rows, 1):
            fh.write(f"{i}\n{_fmt_ts(st)} --> {_fmt_ts(ed)}\n{text}\n\n")
    with open(txt_path, "w", encoding="utf-8") as fh:
        for _, _, text in rows:
            fh.write(text + "\n")
    if to_library:
        try:
            shutil.copy2(srt_path, app.DOWNLOAD_DIR / srt_path.name)
        except Exception:  # noqa: BLE001  入库失败不影响主产物
            pass
    job["status"] = "completed"
    job["progress"] = 100
    job["srt_file"] = str(srt_path)
    job["txt_file"] = str(txt_path)
    job["srt_name"] = f"{stem}.srt"
    job["txt_name"] = f"{stem}.txt"
    job["lines"] = len(rows)


def _run_subtitle(job_id: str, src: str, model_size: str, language: str, to_library: bool,
                  fast: bool = False, cpu_threads: int = 4, use_lyrics: bool = True) -> None:
    """后台线程：抽音频 → ASR → SRT/TXT，更新 SUBTITLE_JOBS。
    cpu_threads：免费 4 / 会员满核（extract 端点按会员状态决定）。"""
    job = SUBTITLE_JOBS.get(job_id)
    if not job:
        return
    srt_path = None
    txt_path = None
    wav_path = None
    try:
        out_dir = app.SUBTITLE_DIR
        stem = _Path(src).stem or "subtitle"
        srt_path = out_dir / f"sub_{job_id}_{stem}.srt"
        txt_path = out_dir / f"sub_{job_id}_{stem}.txt"
        wav_path = out_dir / f"sub_{job_id}.wav"

        # 1) 抽音频（16k mono pcm，whisper 标准输入）
        job["stage"] = "提取音频"
        job["progress"] = 5
        cmd = [app.FFMPEG_BIN, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", str(wav_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0 or not wav_path.exists():
            raise RuntimeError(f"音频提取失败：{(proc.stderr or '')[-300:]}")
        job["progress"] = 15

        # 1.5) 歌词库直取（仅纯音频文件）：歌曲字幕走「歌词」而非「听写」。
        #   命中 → 秒级产出逐字准确的字幕（带官方逐行时间轴），直接 return，不加载模型。
        #   未命中（无网 / 纯音乐 / 未收录）→ 静默回落下面的 ASR 管线。
        if use_lyrics and _Path(src).suffix.lower() in app.UPLOAD_AUDIO_EXTS:
            job["stage"] = "匹配歌词库"
            dur = _wav_duration(wav_path)
            lyric_rows, lyric_desc = _fetch_synced_lyrics(_clean_track_keywords(stem), dur)
            if lyric_rows:
                app.logger.info("subtitle %s from lyrics library: %s (%d lines)",
                                job_id, lyric_desc, len(lyric_rows))
                _write_outputs(job, srt_path, txt_path, lyric_rows, to_library, stem)
                job["source"] = "lyrics"
                job["lyrics_from"] = lyric_desc
                return

        # 2) 加载模型（首次含下载，可能数分钟）；线程数按会员状态（免费 4 / 会员满核）
        job["stage"] = f"加载模型（{model_size} · {cpu_threads} 线程，首次需下载）"
        model = _get_model(model_size, cpu_threads)

        # 3) 转写：能量分段 → 逐段识别（2026-09-23 重写，不再用 Silero VAD）
        job["stage"] = "识别中" + ("（快速模式）" if fast else "")
        total = _wav_duration(wav_path) or 0.0
        seg_ranges = _energy_segments(wav_path)
        if not seg_ranges:
            # 能量分段失败（读不出 / 全静音）：退回整段识别，总比直接报错强
            seg_ranges = [(0.0, total)] if total > 0 else []
        app.logger.info("subtitle %s energy segments: %d spans over %.0fs",
                        job_id, len(seg_ranges), total)
        audio, sr = _read_wav_f32(wav_path)

        # 🔴 解码参数（这里是此前「幻觉/复读」的直接来源）：
        #   · condition_on_previous_text **恒为 False**。上一版写成 `not fast`：
        #     fast=True 时是 False，非快速档反而变 True —— 与紧邻的注释
        #     「False 还能避免长音频复读/幻觉连锁」自相矛盾。自回归模型把上一段的
        #     错误文本当 prompt 继续解码，一旦某段听错（音乐/噪声）就会滚雪球，
        #     这正是整首歌后半段变成大段英文套话的机制。
        #   · temperature 阶梯 fallback：0.0 解码出的段若压缩比/对数概率异常，
        #     自动升温重试，比「一次定生死」稳。
        #   · 三个阈值一并显式打开，交给 faster-whisper 自己拦截幻觉段。
        #   · vad_filter=False：分段已由 _energy_segments 负责，不再让 Silero 参与。
        rows = []            # (start, end, text)
        eff_lang = (language or "").strip() or None
        # 处理顺序按「段长降序」：让**内容最丰富**的那一段先跑，由它定语言。
        # ⚠️ 实测坑（阿刁）：若按时间顺序跑，第一段是 9-29s 的前奏（纯吉他、无人声），
        #   Whisper 在这种段上把语言判成 en，一旦沿用就把「英文」强加给整首中文歌，
        #   后续每段都被强制英文解码。先跑最长的段（主歌/副歌）再回填，语言才可信。
        #   rows 最后按时间重排，结果顺序不受影响。
        order = sorted(range(len(seg_ranges)),
                       key=lambda i: seg_ranges[i][1] - seg_ranges[i][0], reverse=True)
        for _i in order:
            st, ed = seg_ranges[_i]
            if audio is not None and sr > 0:
                chunk = audio[int(st * sr):int(ed * sr)]
                src_audio = chunk if chunk.size > sr * 0.1 else str(wav_path)
            else:
                src_audio = str(wav_path)
            kw = dict(
                language=eff_lang,
                beam_size=1 if fast else 5,
                condition_on_previous_text=False,
                vad_filter=False,
                temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
                compression_ratio_threshold=2.4,
                log_prob_threshold=-1.0,
                no_speech_threshold=0.6,
            )
            _segs, _info = model.transcribe(src_audio, **kw)
            # 首段检测出的语言沿用给后续段：避免段与段之间语言来回跳
            if not eff_lang and _info is not None and getattr(_info, "language", ""):
                eff_lang = _info.language
            for seg in _segs:
                text = (seg.text or "").strip()
                if not text:
                    continue
                if getattr(seg, "avg_logprob", 0) <= -1.0:
                    continue
                text = _to_simplified(text)     # 先转简，再跑简体套话正则
                if _HALLUCINATION_RE.search(text):
                    app.logger.info("subtitle %s drop hallucinated line: %r", job_id, text[:60])
                    continue
                rows.append((st + float(seg.start), st + float(seg.end), text))
            if total > 0:
                # 段是乱序跑的（先长后短），进度只取历史最大值，不能让它回头
                job["progress"] = max(int(job.get("progress") or 15),
                                      15 + int(min(80, max(0, ed / total * 80))))
        rows.sort()

        # 3.4) 中文主体下的纯拉丁行判为伴奏幻觉（实测《阿刁》间奏段自信地输出
        #      「Zither Harp」——一句英文占 20 秒，正是 Whisper 对无人声段的典型脑补）。
        #      只在「用户未强制 en」且「≥60% 的行含 CJK」时生效，避免误伤英文内容。
        if (language or "").strip().lower() != "en" and rows:
            cjk_rows = sum(1 for _, _, t in rows if _CJK_RE.search(t))
            if cjk_rows >= len(rows) * 0.6:
                kept = []
                for r in rows:
                    if not _CJK_RE.search(r[2]) and re.search(r"[A-Za-z]{3,}", r[2]):
                        app.logger.info("subtitle %s drop latin-only line: %r", job_id, r[2][:60])
                        continue
                    kept.append(r)
                if kept:                      # 极端情况下别把整篇清空
                    rows = kept

        # （原「3.5 音乐兜底二次识别」三层补丁已于 2026-09-23 删除：它是在补偿
        #   Silero VAD 对音乐的误判——占比<15% / 尾部有能量 / 空结果也兜底。
        #   分段改用能量法后，歌曲、录音、嘈杂环境都会被正常切出，不再需要补偿。）

        if not rows:
            raise RuntimeError("未识别到任何语音内容（可能没有对话/人声，或为纯器乐/极强噪音）")

        # 4) 写 SRT + TXT
        _write_outputs(job, srt_path, txt_path, rows, to_library, stem)
        job["language"] = eff_lang or ""
        job["source"] = "asr"
        app.logger.info("subtitle %s done: %d lines (%s)", job_id, len(rows), eff_lang)
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        app.logger.warning("subtitle %s failed: %s", job_id, e)
    finally:
        if wav_path:
            try:
                wav_path.unlink(missing_ok=True)
            except Exception:
                pass


def _device_of_req(request):
    return _device_of(request)


@router.post("/api/subtitle/extract")
def subtitle_extract(payload: SubtitleRequest, request: app.Request) -> dict:
    app._check_rate_limit(request)
    resolved = _resolve_safe_local_path(payload.local_path)
    suffix = resolved.suffix.lower()
    # 🔴 2026-09-22 缺陷修复：此前只收视频后缀，用户选 .m4a 音频（播客/录音/歌曲）
    #    直接 409「请选择视频文件」。而下游管线本就是「ffmpeg -vn 抽 16k mono → ASR」，
    #    对纯音频输入天然兼容（-vn 对无视频流无害），没有任何技术理由拒绝。
    #    ⇒ 放开为视频 + 音频（与桌面选择器 choose_files("media") 的范围对齐）。
    if suffix not in app.UPLOAD_VIDEO_EXTS and suffix not in app.UPLOAD_AUDIO_EXTS:
        raise app.HTTPException(status_code=409, detail="请选择视频或音频文件")
    model_size = payload.model_size if payload.model_size in ALLOWED_MODELS else _DEFAULT_MODEL
    # 会员权益：下载/AI 会员（含捆绑）满核提取；免费版固定 4 线程（按请求用户态判定，C2）
    try:
        is_member = bool(app.current_member_store(request).status()["download_member"]["active"])
    except Exception:
        is_member = False
    # 🧱 字幕提取日配额墙（2026-09-13）：免费 2 次/日 → 会员无限；
    # 本地 faster-whisper 推理不计 AI 积分（仅云端/服务端算力计费）。
    _mstore = app.current_member_store(request)
    _sq = _mstore.quota_state("subtitle")
    if not _sq.get("allowed"):
        _free = int(_sq.get("free_limit", 2))
        raise app.HTTPException(
            status_code=402,
            detail="MEMBER_QUOTA|今日字幕提取免费额度已用尽（" + str(int(_sq.get("limit", _free))) + "/日）— 开通会员可解锁无限次/日，并享满速提取",
        )
    # 满速 = 性能核数（见 _asr_thread_cap 的实测注记：用满总核数会慢 2.5 倍）
    full_threads = _asr_thread_cap()
    cpu_threads = full_threads if is_member else min(4, full_threads)
    dev = _device_of_req(request)
    # 幂等去重：同一文件 + 同一设备已在识别中 → 复用原任务（不吃配额、不重复开线程）。
    # 实测（2026-09-22 日志）：用户等待中重复点两次，两个任务各开 4~8 线程互抢 CPU，
    # 单任务从 99s 劣化到 5 分钟以上 —— 这里直接返回原 job_id。
    src_key = str(resolved)
    with _SUBTITLE_LOCK:
        for _jid, _j in SUBTITLE_JOBS.items():
            if (_j.get("status") == "running" and _j.get("src") == src_key
                    and _j.get("device_id") == dev):
                return {"job_id": _jid, "status": "running",
                        "model": _j.get("model", model_size),
                        "cpu_threads": _j.get("cpu_threads", cpu_threads),
                        "member": is_member, "deduped": True}
    _mstore.use_daily("subtitle", 1)
    # 字幕提取为本地 faster-whisper 推理，不扣 AI 积分（仅云端/服务端算力计费）
    job_id = app.uuid.uuid4().hex[:12]
    with _SUBTITLE_LOCK:
        SUBTITLE_JOBS[job_id] = {
            "status": "running", "stage": "排队中", "progress": 0, "error": "",
            "srt_file": "", "txt_file": "", "srt_name": "", "txt_name": "",
            "lines": 0, "language": "", "cpu_threads": cpu_threads,
            "device_id": dev, "src": src_key, "model": model_size,
        }
    app.executor.submit(_run_subtitle, job_id, str(resolved), model_size,
                        (payload.language or "").strip(), bool(payload.to_library),
                        bool(payload.fast), cpu_threads, bool(payload.lyrics))
    record_event("subtitle", {"model": model_size, "member": is_member})
    return {"job_id": job_id, "status": "running", "model": model_size,
            "cpu_threads": cpu_threads, "member": is_member}


@router.get("/api/subtitle/{job_id}")
def subtitle_status(job_id: str, request: app.Request) -> dict:
    job = SUBTITLE_JOBS.get(job_id)
    if not job or (job.get("device_id") and job["device_id"] != _device_of_req(request)):
        raise app.HTTPException(status_code=404, detail="任务不存在")
    return {"status": job["status"], "stage": job.get("stage", ""), "progress": job.get("progress", 0),
            "error": job.get("error", ""), "srt_name": job.get("srt_name", ""),
            "txt_name": job.get("txt_name", ""), "lines": job.get("lines", 0),
            "language": job.get("language", ""), "cpu_threads": job.get("cpu_threads", 4),
            "source": job.get("source", ""), "lyrics_from": job.get("lyrics_from", "")}


def _subtitle_file(job_id: str, kind: str, request: app.Request) -> _Path:
    job = SUBTITLE_JOBS.get(job_id)
    if not job or (job.get("device_id") and job["device_id"] != _device_of_req(request)):
        raise app.HTTPException(status_code=404, detail="任务不存在")
    key = "srt_file" if kind == "srt" else "txt_file"
    path = _Path(job.get(key, ""))
    if job["status"] != "completed" or not path.is_file():
        raise app.HTTPException(status_code=404, detail="字幕文件不存在")
    return path


@router.get("/api/subtitle/{job_id}/file")
def subtitle_file(job_id: str, request: app.Request, kind: str = "srt") -> app.FileResponse:
    path = _subtitle_file(job_id, kind if kind in ("srt", "txt") else "srt", request)
    return app.FileResponse(path, filename=path.name)


@router.get("/api/subtitle/{job_id}/preview")
def subtitle_preview(job_id: str, request: app.Request) -> dict:
    """识别结果预览：把已落盘的 SRT 解析成逐句结构，供前端在结果卡里直接展示。

    用户要求「识别完要可预览」——此前结果卡只有两个下载按钮，看不到识别出来的内容。
    这里**只读已生成的 SRT**（不重跑 ASR、不额外占用 CPU）。

    返回体除逐句内容外还带 `covered`（首句起点 → 末句终点），让用户一眼看出
    「识别覆盖到片子的哪个位置」，不必靠滚动到底去推断（_PREVIEW_MAX_LINES 见文件头注记）。
    `covered` 按**整个 SRT**统计，即使返回体被截断也仍反映真实覆盖范围。
    """
    path = _subtitle_file(job_id, "srt", request)
    job = SUBTITLE_JOBS.get(job_id) or {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"读取字幕失败：{e}")

    segments: list[dict] = []
    total = 0
    first_start_raw = ""
    last_end_raw = ""
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        lines = [ln for ln in block.strip("\n").split("\n") if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        idx_raw, ts_raw = lines[0].strip(), lines[1].strip()
        if not idx_raw.isdigit():
            continue
        total += 1
        start_raw, _, end_raw = ts_raw.partition("-->")
        if not first_start_raw:
            first_start_raw = start_raw
        last_end_raw = end_raw
        if len(segments) >= _PREVIEW_MAX_LINES:
            continue                      # 仍继续数总句数，只截断返回体
        segments.append({
            "i": int(idx_raw),
            "start": round(_parse_ts(start_raw), 3),
            "end": round(_parse_ts(end_raw), 3),
            "ts": _short_ts(start_raw),
            "text": "\n".join(lines[2:]).strip(),
        })
    covered = {}
    if first_start_raw:
        covered = {"start": _short_ts(first_start_raw), "end": _short_ts(last_end_raw)}
    return {
        "status": job.get("status", "completed"),
        "lines": total,
        "language": job.get("language", ""),
        "segments": segments,
        "truncated": total > len(segments),
        "covered": covered,
    }
