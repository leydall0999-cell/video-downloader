"""格式 / 片段增强：对已下载媒体做纯 ffmpeg 本地加工（转音频 / GIF / 裁剪 / 压缩 / 放大）。

设计要点：
- 纯 ffmpeg，无外部依赖（桌面版捆绑 ffmpeg 即可用）。
- 所有函数返回输出路径（Path），失败抛 RuntimeError（由 app.py 转成 HTTP 错误）。
- 输出文件名带语义后缀（如 `<标题>.音频.mp3`、`<标题>.动图.gif`、`<标题>.片段.mp4`），
  并写 `.vdlmeta.json` 侧车继承原视频元信息，使其自动出现在媒体库。
- 「超分」实为 lanczos 高质量放大 + unsharp 锐化（非 AI 超分，无模型依赖）。
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any


def _safe_title(title: str) -> str:
    """清理文件名非法字符（给侧车标题用）。"""
    s = (title or "media").strip()
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    return s[:80] or "media"


def _escape(expr: str) -> str:
    """转义 ffmpeg 滤镜里的特殊字符（冒号 / 反斜杠 / 单引号）。"""
    return expr.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _run(cmd: list[str], timeout: int = 1800) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "")[-600:] or "ffmpeg 执行失败")


def _unique_out(video: Path, suffix: str, ext: str) -> Path:
    """在视频同目录生成不覆盖既有文件的输出路径（中文后缀安全，不用 with_suffix）。"""
    stem = video.stem
    out = video.parent / f"{stem}.{suffix}.{ext}"
    n = 1
    while out.exists():
        out = video.parent / f"{stem}.{suffix}.{n}.{ext}"
        n += 1
    return out


def _unique_dir(video: Path, suffix: str) -> Path:
    """在视频同目录生成不冲突的输出子目录（用于批量抽帧）。"""
    d = video.parent / f"{video.stem}.{suffix}"
    n = 1
    while d.exists():
        d = video.parent / f"{video.stem}.{suffix}.{n}"
        n += 1
    return d


def probe_duration(video: Path, ffmpeg_bin: str = "ffmpeg") -> float:
    """探测媒体时长（秒）。不依赖 ffprobe——直接解析 ffmpeg 的 stderr。失败返回 0。"""
    try:
        proc = subprocess.run([ffmpeg_bin, "-i", str(video)],
                              capture_output=True, text=True, timeout=60)
    except Exception:
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", proc.stderr or "")
    if not m:
        return 0.0
    try:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except ValueError:
        return 0.0


def _write_sidecar(out: Path, source_meta: dict[str, Any], suffix: str) -> None:
    """为加工产物写侧车，继承原视频元信息（标题加后缀）。"""
    sidecar = out.with_name(out.stem + ".vdlmeta.json")
    meta = dict(source_meta or {})
    base_title = meta.get("title") or out.stem
    if suffix not in base_title:
        base_title = f"{base_title}（{suffix}）"
    meta["title"] = base_title
    meta["source_url"] = meta.get("source_url") or ""
    meta["completed_at"] = int(time.time())
    try:
        sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 1. 提取音频
# --------------------------------------------------------------------------- #
def extract_audio(video: Path, out_dir: Path | None = None, fmt: str = "mp3",
                  bitrate: str = "192k", ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """从视频/音频提取并转码为指定格式音频。fmt: mp3/m4a/aac/opus/flac/wav。"""
    out_dir = out_dir or video.parent
    out = _unique_out(video, "音频", fmt)
    cmd = [ffmpeg_bin, "-y", "-i", str(video)]
    if fmt == "mp3":
        cmd += ["-vn", "-c:a", "libmp3lame", "-b:a", bitrate]
    elif fmt in ("m4a", "aac"):
        cmd += ["-vn", "-c:a", "aac", "-b:a", bitrate]
    elif fmt == "opus":
        cmd += ["-vn", "-c:a", "libopus", "-b:a", bitrate]
    elif fmt == "flac":
        cmd += ["-vn", "-c:a", "flac"]
    elif fmt == "wav":
        cmd += ["-vn", "-c:a", "pcm_s16le"]
    else:
        cmd += ["-vn", "-c:a", "copy"]
    cmd.append(str(out))
    try:
        _run(cmd)
    except Exception:
        return None
    return out if (out.exists() and out.stat().st_size > 0) else None


# --------------------------------------------------------------------------- #
# 2. 生成 GIF（双遍 palette，画质更好）
# --------------------------------------------------------------------------- #
def make_gif(video: Path, out_dir: Path | None = None, start: float = 0.0,
             duration: float = 5.0, fps: int = 12, width: int = 480,
             ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """把视频片段做成高质量 GIF。start/duration 秒；fps 帧率；width 像素宽。"""
    out_dir = out_dir or video.parent
    out = _unique_out(video, "动图", "gif")
    palette = out_dir / (out.stem + ".palette.png")
    vf = f"fps={int(fps)},scale={int(width)}:-1:flags=lanczos"
    ss = f"{max(0.0, float(start))}"
    dur = f"{max(0.1, float(duration))}"
    try:
        _run([ffmpeg_bin, "-y", "-ss", ss, "-t", dur, "-i", str(video),
              "-vf", f"{vf},palettegen", str(palette)], timeout=300)
        _run([ffmpeg_bin, "-y", "-ss", ss, "-t", dur, "-i", str(video),
              "-i", str(palette), "-lavfi", f"{vf}[x];[x][1:v]paletteuse", str(out)], timeout=600)
    except Exception:
        return None
    finally:
        try:
            if palette.exists():
                palette.unlink()
        except OSError:
            pass
    return out if (out.exists() and out.stat().st_size > 0) else None


# --------------------------------------------------------------------------- #
# 3. 时间裁剪
# --------------------------------------------------------------------------- #
def trim_video(video: Path, out_dir: Path | None = None, start: float = 0.0,
               end: float = 0.0, reencode: bool = True,
               ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """时间裁剪：start~end（秒）。reencode=True 重编码精确；False 用 -c copy 快但不精确。"""
    out_dir = out_dir or video.parent
    ext = video.suffix.lstrip(".") or "mp4"
    out = _unique_out(video, "片段", ext)
    s = max(0.0, float(start))
    cmd = [ffmpeg_bin, "-y", "-ss", f"{s}", "-i", str(video)]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast",
                "-c:a", "aac", "-movflags", "+faststart"]
    else:
        cmd += ["-c", "copy"]
    if end and float(end) > s:
        cmd += ["-t", f"{float(end) - s}"]
    cmd.append(str(out))
    try:
        _run(cmd)
    except Exception:
        return None
    return out if (out.exists() and out.stat().st_size > 0) else None


# --------------------------------------------------------------------------- #
# 4. 画面裁剪
# --------------------------------------------------------------------------- #
def crop_video(video: Path, out_dir: Path | None = None, crop_expr: str = "",
               ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """画面裁剪：crop_expr 形如 "iw:ih:0:0" 或 "iw/2:ih:0:0"（ffmpeg crop 滤镜）。"""
    if not crop_expr or not crop_expr.strip():
        return None
    out_dir = out_dir or video.parent
    ext = video.suffix.lstrip(".") or "mp4"
    out = _unique_out(video, "裁剪", ext)
    cmd = [ffmpeg_bin, "-y", "-i", str(video), "-vf", f"crop={_escape(crop_expr.strip())}",
           "-c:v", "libx264", "-preset", "veryfast", "-c:a", "copy", str(out)]
    try:
        _run(cmd)
    except Exception:
        return None
    return out if (out.exists() and out.stat().st_size > 0) else None


# --------------------------------------------------------------------------- #
# 5. 压缩（降分辨率 + 提 CRF）
# --------------------------------------------------------------------------- #
def compress_video(video: Path, out_dir: Path | None = None, scale_h: int = 720,
                   crf: int = 28, ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """压缩：分辨率降到 scale_h 高度 + CRF（数值越大压得越狠）。"""
    out_dir = out_dir or video.parent
    out = _unique_out(video, f"压缩{int(scale_h)}p", "mp4")
    cmd = [ffmpeg_bin, "-y", "-i", str(video), "-vf", f"scale=-2:{int(scale_h)}",
           "-c:v", "libx264", "-preset", "medium", "-crf", str(int(crf)),
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    try:
        _run(cmd)
    except Exception:
        return None
    return out if (out.exists() and out.stat().st_size > 0) else None


# --------------------------------------------------------------------------- #
# 6. 放大 / 轻量超分（lanczos 放大 + 可选锐化，非 AI 模型）
# --------------------------------------------------------------------------- #
def upscale_video(video: Path, out_dir: Path | None = None, factor: float = 2.0,
                  sharpen: bool = True, ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """轻量放大（非 AI 超分）：lanczos 缩放 + 可选 unsharp 锐化。factor 放大倍率。"""
    out_dir = out_dir or video.parent
    f = max(0.5, float(factor))
    out = _unique_out(video, f"放大{f}x", "mp4")
    vf = f"scale=iw*{f}:ih*{f}:flags=lanczos"
    if sharpen:
        vf += ",unsharp=5:5:1.0:5:5:0.0"
    cmd = [ffmpeg_bin, "-y", "-i", str(video), "-vf", vf,
           "-c:v", "libx264", "-preset", "medium", "-crf", "20",
           "-c:a", "copy", str(out)]
    try:
        _run(cmd)
    except Exception:
        return None
    return out if (out.exists() and out.stat().st_size > 0) else None


# --------------------------------------------------------------------------- #
# 7. 抽单帧封面
# --------------------------------------------------------------------------- #
def snapshot(video: Path, out_dir: Path | None = None, at: float = 1.0,
             fmt: str = "jpg", width: int = 0, ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """在指定秒抽一帧做封面图。fmt: jpg/png/webp；width>0 时按宽度等比缩放。

    若 seek 超过视频时长导致抽不到帧，自动回退到 0 秒重试。
    """
    out_dir = out_dir or video.parent
    ext = fmt.lower() if fmt.lower() in ("jpg", "png", "webp") else "jpg"
    out = _unique_out(video, "封面", ext)

    def _shot(seek: float | None) -> bool:
        # seek=None 表示完全不加 -ss（单帧素材带 -ss 会把唯一一帧 seek 掉，输出为空）
        pre = [] if seek is None else ["-ss", f"{max(0.0, float(seek))}"]
        cmd = [ffmpeg_bin, "-y", *pre, "-i", str(video)]
        if width and int(width) > 0:
            cmd += ["-vf", f"scale={int(width)}:-1:flags=lanczos"]
        cmd += ["-frames:v", "1"]
        if ext in ("jpg", "jpeg"):
            cmd += ["-q:v", "2"]
        cmd.append(str(out))
        try:
            _run(cmd, timeout=180)
        except Exception:
            return False
        return out.exists() and out.stat().st_size > 0

    if _shot(at):
        return out
    if float(at) > 0 and _shot(0.0):
        return out
    return out if _shot(None) else None


# --------------------------------------------------------------------------- #
# 8. 批量抽帧（输出到子目录，避免刷屏媒体库）
# --------------------------------------------------------------------------- #
def extract_frames(video: Path, out_dir: Path | None = None, start: float = 0.0,
                   end: float = 0.0, interval: float = 1.0, limit: int = 200,
                   fmt: str = "jpg", width: int = 0,
                   ffmpeg_bin: str = "ffmpeg") -> tuple[Path, int] | None:
    """每 interval 秒抽一帧，输出到 `<标题>.抽帧/` 子目录。返回 (目录, 帧数)。

    limit 为最大帧数上限（防止长视频抽出上万张）。end=0 表示抽到结尾。
    """
    out_dir = out_dir or video.parent
    ext = fmt.lower() if fmt.lower() in ("jpg", "png", "webp") else "jpg"
    frames_dir = _unique_dir(video, "抽帧")
    frames_dir.mkdir(parents=True, exist_ok=True)
    iv = float(interval) if float(interval or 0) > 0 else 1.0
    cap = max(1, min(int(limit or 200), 2000))
    s = max(0.0, float(start))
    vf = f"fps=1/{iv}"
    if width and int(width) > 0:
        vf += f",scale={int(width)}:-1:flags=lanczos"
    cmd = [ffmpeg_bin, "-y", "-ss", f"{s}", "-i", str(video)]
    if end and float(end) > s:
        cmd += ["-t", f"{float(end) - s}"]
    cmd += ["-vf", vf, "-vsync", "0", "-frames:v", str(cap)]
    if ext in ("jpg", "jpeg"):
        cmd += ["-q:v", "2"]
    cmd.append(str(frames_dir / f"frame_%04d.{ext}"))
    try:
        _run(cmd, timeout=1800)
    except Exception:
        pass  # 部分成功也算数，下面按实际产出判定
    produced = sorted(frames_dir.glob(f"frame_*.{ext}"))
    if not produced:
        try:
            frames_dir.rmdir()
        except OSError:
            pass
        return None
    return frames_dir, len(produced)


# --------------------------------------------------------------------------- #
# 9. 预览图（contact sheet / 九宫格缩略拼图）
# --------------------------------------------------------------------------- #
def contact_sheet(video: Path, out_dir: Path | None = None, rows: int = 3,
                  cols: int = 4, width: int = 1280, duration: float = 0.0,
                  ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """把视频均匀采样 rows*cols 帧拼成一张预览图（雪碧图）。

    duration<=0 时自动探测时长；探测不到则退化为 `thumbnail` 滤镜按固定间隔取帧。
    """
    out_dir = out_dir or video.parent
    r = max(1, min(int(rows or 3), 10))
    c = max(1, min(int(cols or 4), 10))
    n = r * c
    tile_w = max(120, int(width or 1280) // c)
    out = _unique_out(video, "预览图", "jpg")
    dur = float(duration or 0) or probe_duration(video, ffmpeg_bin)
    if dur > 0.5:
        # 均匀采样：fps = 张数 / 时长（略微收缩避免最后一帧越界）
        rate = n / max(0.5, dur * 0.98)
        vf = f"fps={rate:.6f},scale={tile_w}:-1:flags=lanczos,tile={c}x{r}"
    else:
        vf = f"thumbnail=100,scale={tile_w}:-1:flags=lanczos,tile={c}x{r}"
    cmd = [ffmpeg_bin, "-y", "-i", str(video), "-vf", vf,
           "-frames:v", "1", "-q:v", "3", str(out)]
    try:
        _run(cmd, timeout=900)
    except Exception:
        return None
    return out if (out.exists() and out.stat().st_size > 0) else None


# --------------------------------------------------------------------------- #
# 10. 铃声（片段 + 淡入淡出，m4r 给 iPhone / mp3 给安卓）
# --------------------------------------------------------------------------- #
def make_ringtone(src: Path, out_dir: Path | None = None, start: float = 0.0,
                  duration: float = 30.0, fmt: str = "m4r", fade: float = 1.0,
                  bitrate: str = "192k", ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """从视频/音频截一段做铃声。fmt: m4r(iPhone) / m4a / mp3。fade 为淡入淡出秒数。

    注意：iPhone 铃声时长上限 40 秒，超出由调用方（前端）提示，这里不强制截断。
    """
    out_dir = out_dir or src.parent
    ext = fmt.lower() if fmt.lower() in ("m4r", "m4a", "mp3") else "m4r"
    out = _unique_out(src, "铃声", ext)
    s = max(0.0, float(start))
    d = max(1.0, float(duration or 30))
    f = max(0.0, float(fade or 0))
    cmd = [ffmpeg_bin, "-y", "-ss", f"{s}", "-t", f"{d}", "-i", str(src), "-vn"]
    if f > 0:
        af = f"afade=t=in:st=0:d={f},afade=t=out:st={max(0.0, d - f)}:d={f}"
        cmd += ["-af", af]
    if ext == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate]
    else:
        cmd += ["-c:a", "aac", "-b:a", bitrate]
        if ext == "m4r":
            cmd += ["-f", "ipod"]  # .m4r 需显式指定容器，否则 ffmpeg 认不出扩展名
    cmd.append(str(out))
    try:
        _run(cmd, timeout=600)
    except Exception:
        return None
    return out if (out.exists() and out.stat().st_size > 0) else None
