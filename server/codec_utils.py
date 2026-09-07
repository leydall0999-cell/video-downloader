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
    "probe_encoder",
    "available_h264",
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
