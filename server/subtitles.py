"""字幕处理：在线字幕提取（yt-dlp）、内嵌字幕抽取（ffmpeg）、硬字幕烧录、可选 LLM 翻译。

设计要点：
- 字幕提取依赖 `source_url`（媒体库侧车里有）；无 source_url 的视频尝试抽「内嵌字幕流」。
- 烧录用 ffmpeg `subtitles` 滤镜，输出到视频同目录的 `<标题>.字幕版.mp4`，并写侧车让媒体库识别。
- 翻译走 OpenAI 兼容接口（用户自备 key，或后端 VDL_LLM_* 环境变量），不强制、失败不影响提取/烧录。
- 依赖（yt_dlp / ffmpeg）延迟导入，模块本身可被单测（不触发网络/二进制）。
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _safe_title(title: str) -> str:
    """清理文件名非法字符，给 yt-dlp outtmpl 用。"""
    s = (title or "video").strip()
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    return s[:80] or "video"


def _escape_ff(path: str) -> str:
    """转义 ffmpeg subtitles 滤镜里的特殊字符（冒号/反斜杠/单引号）。"""
    p = path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return p


# --------------------------------------------------------------------------- #
# 在线字幕（yt-dlp）
# --------------------------------------------------------------------------- #
def list_online_subs(source_url: str, cookie: str = "", proxy: str = "") -> list[dict[str, Any]]:
    """列出视频可用字幕语言（手动 + 自动生成）。失败返回空列表。"""
    if not source_url:
        return []
    try:
        from yt_dlp import YoutubeDL
    except Exception:
        return []
    opts: dict[str, Any] = {
        "simulate": True,
        "listsubtitles": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,
    }
    if cookie:
        opts["http_headers"] = {"Cookie": cookie.strip()}
    if proxy:
        opts["proxy"] = proxy
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(source_url, download=False) or {}
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for auto, bucket in (("automatic", info.get("automatic_captions") or {}),
                         ("", info.get("subtitles") or {})):
        for lang, tracks in bucket.items():
            if lang in seen:
                continue
            seen.add(lang)
            name = (tracks[0].get("name") if tracks else "") or lang
            out.append({"lang": lang, "name": name, "auto": bool(auto)})
    return out


def extract_online_sub(source_url: str, lang: str, cookie: str = "", proxy: str = "",
                       out_dir: Path | None = None, title: str = "") -> Path | None:
    """下载某语言的在线字幕到 out_dir，返回字幕文件路径；失败返回 None。"""
    if not source_url or not lang:
        return None
    try:
        from yt_dlp import YoutubeDL
    except Exception:
        return None
    out_dir = out_dir or Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_title(title) if title else "%(title)s"
    outtmpl = str(out_dir / f"{stem}.%(ext)s")
    opts: dict[str, Any] = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang],
        "subtitlesformat": "srt/vtt/best",
        "outtmpl": outtmpl,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,
    }
    if cookie:
        opts["http_headers"] = {"Cookie": cookie.strip()}
    if proxy:
        opts["proxy"] = proxy
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([source_url])
    except Exception:
        return None
    # 找到刚生成的字幕文件（.srt/.vtt）
    cands = sorted(out_dir.glob(f"{stem}.*"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if title else sorted(out_dir.glob("*.srt") + out_dir.glob("*.vtt"), key=lambda p: p.stat().st_mtime, reverse=True)
    for c in cands:
        if c.suffix.lower() in (".srt", ".vtt"):
            return c
    return None


# --------------------------------------------------------------------------- #
# 内嵌字幕（ffmpeg）
# --------------------------------------------------------------------------- #
def extract_embedded_subs(video_path: Path, out_dir: Path | None = None,
                          ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """用 ffmpeg 抽视频内嵌的字幕流（第一条）。返回字幕文件路径，无则返回 None。"""
    out_dir = out_dir or video_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    # 先探测是否有字幕流
    try:
        probe = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        combined = probe.stderr + probe.stdout
        if "Subtitle:" not in combined and "Stream #" not in combined:
            return None
        if "Subtitle:" not in combined:
            return None
    except Exception:
        return None
    sub_path = out_dir / f"{video_path.stem}.embedded.srt"
    try:
        subprocess.run(
            [ffmpeg_bin, "-y", "-i", str(video_path), "-map", "0:s:0",
             "-c:s", "srt", str(sub_path)],
            capture_output=True, timeout=60,
        )
    except Exception:
        return None
    if sub_path.exists() and sub_path.stat().st_size > 0:
        return sub_path
    return None


# --------------------------------------------------------------------------- #
# 硬字幕烧录（ffmpeg）
# --------------------------------------------------------------------------- #
def burn_subtitle(video_path: Path, sub_path: Path, ffmpeg_bin: str = "ffmpeg") -> Path | None:
    """把字幕烧录成硬字幕，输出 <标题>.字幕版.mp4 到视频同目录。"""
    stem = video_path.stem
    out_path = video_path.parent / f"{stem}.字幕版.mp4"
    n = 1
    while out_path.exists():
        out_path = video_path.parent / f"{stem}.字幕版.{n}.mp4"
        n += 1
    vf = f"subtitles={_escape_ff(str(sub_path))}"
    cmd = [
        ffmpeg_bin, "-y", "-i", str(video_path),
        "-vf", vf,
        "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
        "-c:a", "copy",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=1800)
    except Exception:
        return None
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path
    return None


def _write_subtitle_sidecar(out_path: Path, source_meta: dict[str, Any]) -> None:
    """为烧录产物写侧车，继承原视频元信息（标题加「（字幕版）」）。"""
    sidecar = out_path.with_name(out_path.stem + ".vdlmeta.json")
    try:
        orig = json.loads(sidecar.read_text(encoding="utf-8") or "{}") if False else {}
    except Exception:
        orig = {}
    meta = dict(source_meta or {})
    base_title = meta.get("title") or out_path.stem
    if "（字幕版）" not in base_title:
        base_title = f"{base_title}（字幕版）"
    meta["title"] = base_title
    meta["source_url"] = meta.get("source_url") or ""
    meta["completed_at"] = int(time.time())
    try:
        sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# LLM 翻译（OpenAI 兼容，可选）
# --------------------------------------------------------------------------- #
def translate_srt(text: str, api_key: str = "", base_url: str = "", model: str = "",
                  target_lang: str = "简体中文") -> str:
    """调用 OpenAI 兼容接口翻译 SRT 文本。无 key 时抛 ValueError。"""
    if not api_key:
        api_key = ""
    base_url = (base_url or "https://api.openai.com").rstrip("/")
    model = model or "gpt-4o-mini"
    if not api_key:
        raise ValueError("未配置 LLM API Key，无法翻译")
    url = base_url + "/v1/chat/completions"
    prompt = (
        f"你是专业的字幕翻译。下面是一段 SRT 格式字幕，保持原有的序号和时间轴行（如 `1`、`00:00:01,000 --> 00:00:04,000`）"
        f"完全不变，只把每句对白翻译成{target_lang}，不要增删条目、不要加解释。\n\n{text}"
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"翻译请求失败：{exc}") from exc
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"翻译响应解析失败：{exc}") from exc
