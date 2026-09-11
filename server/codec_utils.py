"""LGPL 合规的编码器 / 滤镜选择工具。

背景
----
App 捆绑的 ffmpeg 为 **LGPL 构建**（configure 不带 ``--enable-gpl``），因此：

- 不含 ``libx264`` / ``libx265``（GPL 库，会令整个 ffmpeg 转为 GPL v2+）
- 不含 ``delogo`` / ``boxblur`` / ``hqdn3d`` 等 GPL 滤镜

H.264 / HEVC 编码改由 **VideoToolbox 硬件编码**（Apple 系统框架，非 GPL）承担，
失败时回退 **libopenh264**（BSD-2-Clause，Cisco 开源）软件编码。

这样就同时满足：
1. 视频不进 GPL 领地（合规）；
2. macOS 上硬编比 libx264 软编更快（性能不降反升）。

对外接口
--------
- :func:`h264_args` / :func:`hevc_args`：给出当前 ffmpeg 可用的编码参数
- :func:`delogo_filter`：``delogo`` 滤镜的 LGPL 等价实现
- :func:`probe_encoder`：查询某个编码器是否可用
"""

from __future__ import annotations

import subprocess

__all__ = [
    "h264_args",
    "hevc_args",
    "delogo_filter",
    "audio_encode_args",
    "probe_encoder",
    "available_h264",
    "available_hevc",
    "res_cap_kbps",
    "target_bitrate_kbps",
    "rate_controlled_args",
]

# 编码器探测结果缓存：{ffmpeg_bin: encoders 文本}
_ENC_CACHE: dict[str, str] = {}


def _encoders_text(ffmpeg_bin: str) -> str:
    """取 ``ffmpeg -encoders`` 输出并缓存（进程调用较贵，且结果不会变）。"""
    if ffmpeg_bin not in _ENC_CACHE:
        try:
            proc = subprocess.run(
                [ffmpeg_bin, "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=30,
            )
            _ENC_CACHE[ffmpeg_bin] = proc.stdout or ""
        except Exception:
            _ENC_CACHE[ffmpeg_bin] = ""
    return _ENC_CACHE[ffmpeg_bin]


def probe_encoder(ffmpeg_bin: str, name: str) -> bool:
    """判断指定编码器在当前 ffmpeg 中是否存在。"""
    return name in _encoders_text(ffmpeg_bin)


def available_h264(ffmpeg_bin: str) -> str:
    """返回当前可用的 H.264 编码器，优先级：VideoToolbox 硬编 > libopenh264 软编。"""
    if probe_encoder(ffmpeg_bin, "h264_videotoolbox"):
        return "h264_videotoolbox"
    if probe_encoder(ffmpeg_bin, "libopenh264"):
        return "libopenh264"
    return "mpeg4"  # 兜底：LGPL，兼容性最好但体积偏大


def available_hevc(ffmpeg_bin: str) -> str:
    """返回当前可用的 HEVC 编码器。"""
    if probe_encoder(ffmpeg_bin, "hevc_videotoolbox"):
        return "hevc_videotoolbox"
    return "mpeg4"


# 质量档位 → VideoToolbox 的 -q:v（0~100，越大质量越好）
_VT_QUALITY = {"fast": 45, "balanced": 60, "high": 78}
# 质量档位 → libopenh264 的目标码率（openh264 不支持 CRF，只能控码率）
_O264_BITRATE = {"fast": "2500k", "balanced": "4500k", "high": "8000k"}


def h264_args(ffmpeg_bin: str, quality: str = "balanced",
              pix_fmt: str | None = None) -> list[str]:
    """生成 H.264 编码参数（LGPL 安全，不再使用 libx264）。

    :param quality: ``fast`` / ``balanced`` / ``high``，大致对应原 libx264
                    preset veryfast+crf28 / crf23 / crf18 的观感档位。
    :param pix_fmt: 需要时强制像素格式（如烧字幕常需 ``yuv420p``）。
    """
    q = quality if quality in _VT_QUALITY else "balanced"
    enc = available_h264(ffmpeg_bin)
    if enc == "h264_videotoolbox":
        # -allow_sw 1：硬编不可用时允许 VT 内部走软件，避免直接失败
        # -realtime 0：关闭实时模式，换取更好的压缩质量
        args = ["-c:v", "h264_videotoolbox", "-q:v", str(_VT_QUALITY[q]),
                "-allow_sw", "1", "-realtime", "0"]
    elif enc == "libopenh264":
        args = ["-c:v", "libopenh264", "-b:v", _O264_BITRATE[q]]
    else:
        args = ["-c:v", "mpeg4", "-q:v", "3"]
    if pix_fmt:
        args += ["-pix_fmt", pix_fmt]
    return args


def hevc_args(ffmpeg_bin: str, quality: str = "balanced") -> list[str]:
    """生成 HEVC 编码参数（LGPL 安全，不再使用 libx265）。"""
    q = quality if quality in _VT_QUALITY else "balanced"
    enc = available_hevc(ffmpeg_bin)
    if enc == "hevc_videotoolbox":
        args = ["-c:v", "hevc_videotoolbox", "-q:v", str(_VT_QUALITY[q]),
                "-allow_sw", "1", "-realtime", "0"]
    else:
        args = ["-c:v", "mpeg4", "-q:v", "3"]
    return args


# --------------------------------------------------------------------------- #
# 码率受控编码（压缩路由专用，2026-09-11）
#
# 为什么压缩不能沿用上面的 -q:v 恒定质量模式
# ------------------------------------------
# VideoToolbox 的 ``-q:v`` 是**恒定质量**语义：它按「画质」重新分配码率，完全
# 不理会源文件原本的码率水平。对网站在线播放那种已经压过的低码率源（实测
# 864x486 / 629 kbps / 25fps 电视剧），``-q:v 60`` 会把它重编成约 2.5 Mbps,
# 产物达**源体积的 284%**；程序随后判定「原文件已足够小」回退输出原文件副本，
# 用户白等一场。HEVC 档同理（215%）。
#
# 解决：压缩改用「平均码率 + 峰值上限」双约束（VBR 受限），目标码率同时受
#   ① 分辨率建议码率 —— 按画面尺寸给合理预算（纯码率模式下防止高码率源被
#      压得不够小）；
#   ② 源码率钳制 —— 乘一个 <1 的系数，从机制上保证产物一定小于源文件。
# 两者取小。实测同一素材 864x486/629k 在 balanced 档 → 目标 453k，产物降到
# 源码的 72%（真实省 28%），编码速度与恒质量模式完全一致（23x realtime）。
# --------------------------------------------------------------------------- #

# 分辨率（短边像素）→ 建议码率上限（kbps）
_RES_BITRATE = ((480, 1200), (720, 2500), (1080, 4500), (1440, 8000))
_RES_BITRATE_MAX = 16000
# 档位 → 相对「分辨率建议码率」的倍率（放宽/收紧画面预算）
_LEVEL_CAP = {"fast": 0.65, "balanced": 1.0, "high": 1.3}
# 档位 → 相对「源码率」的钳制倍率（保证产物必然小于源；实测 0.72 约省 28%）
_LEVEL_SRC = {"fast": 0.50, "balanced": 0.72, "high": 0.90}
# HEVC 同画质约省 25% 码率 → 目标码率同步下调，省下的体积才拿得到
_HEVC_GAIN = 0.75
# 保护下限：再低画面就会出现可感知的糊块
_MIN_KBPS = 120


def res_cap_kbps(short_side: int) -> int:
    """按画面短边给码率预算（kbps）；短边 <=480 给 1200k，逐档上调。"""
    try:
        s = int(short_side or 0)
    except (TypeError, ValueError):
        s = 0
    if s <= 0:
        return _RES_BITRATE_MAX
    for edge, cap in _RES_BITRATE:
        if s <= edge:
            return cap
    return _RES_BITRATE_MAX


def target_bitrate_kbps(src_kbps, short_side, quality: str = "balanced",
                        codec: str = "h264") -> int:
    """算出目标平均码率（kbps）：分辨率预算与源码率钳制取小。

    :param src_kbps: 源视频平均码率（kbps）；未知传 0/None 则只用分辨率预算
    :param short_side: 源画面短边像素（宽高中较小者）
    :param quality: ``fast`` / ``balanced`` / ``high``
    :param codec: ``h264`` / ``hevc``（hevc 同画质再省约 25% 码率）
    """
    q = quality if quality in _LEVEL_CAP else "balanced"
    cap = res_cap_kbps(short_side) * _LEVEL_CAP[q]
    try:
        src = float(src_kbps or 0)
    except (TypeError, ValueError):
        src = 0.0
    if src > 0:
        cap = min(cap, src * _LEVEL_SRC[q])
    if (codec or "").lower() == "hevc":
        cap *= _HEVC_GAIN
    return max(_MIN_KBPS, int(round(cap)))


def rate_controlled_args(ffmpeg_bin: str, codec: str = "h264",
                         target_kbps: int = 0,
                         pix_fmt: str | None = None) -> list[str]:
    """按目标码率生成 VideoToolbox 编码参数（VBR 受限：平均 + 峰值双约束）。

    :param target_kbps: 目标平均码率（kbps）。<=0 表示没有码率依据，此时
                        回退到恒定质量模式（h264_args / hevc_args），行为与旧版一致。
    """
    hevc = (codec or "").lower() == "hevc"
    if target_kbps and target_kbps > 0:
        t = int(target_kbps)
        peak = max(t + 1, int(round(t * 1.3)))      # 峰值上限：给运动场景留余量
        buf = max(peak * 2, int(round(t * 2)))      # 缓冲区 2x 峰值，避免码率抖动
        enc = available_hevc(ffmpeg_bin) if hevc else available_h264(ffmpeg_bin)
        if enc in ("h264_videotoolbox", "hevc_videotoolbox"):
            args = ["-c:v", enc, "-b:v", f"{t}k", "-maxrate", f"{peak}k",
                    "-bufsize", f"{buf}k", "-allow_sw", "1", "-realtime", "0"]
        elif enc == "libopenh264":
            args = ["-c:v", "libopenh264", "-b:v", f"{t}k"]
        else:
            args = ["-c:v", "mpeg4", "-q:v", "3"]
        if pix_fmt:
            args += ["-pix_fmt", pix_fmt]
        return args
    if hevc:
        return hevc_args(ffmpeg_bin, quality="balanced")
    return h264_args(ffmpeg_bin, quality="balanced", pix_fmt=pix_fmt)


def delogo_filter(x: int, y: int, w: int, h: int, band: int = 10) -> str:
    """``delogo`` 滤镜的 **LGPL 等价实现**。

    ffmpeg 的 ``delogo`` 属 GPL 滤镜，LGPL 构建下不可用。这里用
    ``split → crop 水印区 → gblur 模糊 → overlay 贴回`` 的滤镜链替代：

    - ``split`` 复制一份原图作底；
    - ``crop`` 只裁出水印矩形，避免模糊扩散到整帧；
    - ``gblur``（LGPL）对裁剪块做高斯模糊抹掉水印；
    - ``overlay`` 将模糊块贴回原坐标。

    :param band: 原 delogo 的模糊带宽语义，这里映射为高斯 sigma（band/2，钳制 2~30）。
    """
    sigma = max(2.0, min(float(band), 60.0) / 2.0)
    return (
        f"split[base][wm];"
        f"[wm]crop={w}:{h}:{x}:{y},gblur=sigma={sigma}[blur];"
        f"[base][blur]overlay={x}:{y}"
    )


# 有损音频格式：这些支持 -b:a 码率档位；flac/wav 为无损，忽略码率
LOSSY_AUDIO = {"mp3", "m4a", "aac", "opus", "wma", "mp2"}


def audio_encode_args(target: str, bitrate: str = "") -> list[str] | None:
    """按目标音频格式 + 音质档位生成编码参数（LGPL 安全）。

    音乐/音频转换专用：比 ``CONVERT_TARGETS`` 的固定参数多一个码率档位，
    让用户可选 128k / 192k / 256k / 320k。无损格式（flac / wav）忽略码率。

    :param target: 目标格式键（mp3 / m4a / aac / opus / flac / wav / wma / mp2）
    :param bitrate: 音质档位字符串，如 ``"320k"``；留空则用各格式的默认档
    :return: ffmpeg 参数列表；非音频格式返回 ``None``
    """
    t = (target or "").strip().lower()
    br = (bitrate or "").strip()
    if t == "mp3":
        # 指定码率走 CBR（音乐场景更直观），否则用 VBR -q:a 4
        return ["-vn", "-c:a", "libmp3lame"] + (["-b:a", br] if br else ["-q:a", "4"])
    if t in ("m4a", "aac"):
        return ["-vn", "-c:a", "aac", "-b:a", br or "192k"]
    if t == "opus":
        return ["-vn", "-c:a", "libopus", "-b:a", br or "128k"]
    if t == "wma":
        return ["-vn", "-c:a", "wmav2", "-b:a", br or "192k"]
    if t == "mp2":
        return ["-vn", "-c:a", "mp2", "-b:a", br or "192k"]
    if t == "flac":
        return ["-vn", "-c:a", "flac"]            # 无损，码率无效
    if t == "wav":
        return ["-vn", "-c:a", "pcm_s16le"]       # 无损，码率无效
    return None
