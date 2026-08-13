"""提取文案：从已下载的视频里抽取可读文本。

两类内容，用户可选其一或都要：
- spoken（口播文案）：优先抽内嵌 / 在线字幕（秒出、零网络），找不到字幕时回退到
  whisper 语音转写（复用 AI 解说管线的 faster_whisper 运行时，best-effort）。
- description（发布简介）：用 yt-dlp 抓视频的标题 / 简介 / 标签等发布元数据。

设计要点：
- 纯函数为主，网络 (yt-dlp) 与二进制 (ffmpeg / whisper) 调用全部延迟 import / 子进程，
  模块本身可被单测（不触发网络、不依赖二进制）。
- 提取失败一律「降级」而非抛错：某一项拿不到就返回带 error 的字典，由调用方决定是否影响主流程。
- whisper 转写需要外部解说管线（faster_whisper + 模型），缺失时口播文案只尝试字幕路径。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

# 进度回调签名：progress_cb(stage: str, detail: str) -> None
ProgressCb = Callable[[str, str], None] | None

EXTRACT_SPOKEN = "spoken"
EXTRACT_DESC = "description"
EXTRACT_BOTH = "both"
VALID_MODES = ("", EXTRACT_SPOKEN, EXTRACT_DESC, EXTRACT_BOTH)


# --------------------------------------------------------------------------- #
# 字幕 → 纯文本
# --------------------------------------------------------------------------- #
def srt_to_plaintext(path: str | Path) -> str:
    """把 SRT / VTT 解析成纯文本：去掉序号、时间轴、样式标签，保留分句换行。"""
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    # 去掉 VTT 文件头（"WEBVTT" 到第一个空行）
    if text.lstrip().startswith("WEBVTT"):
        text = re.sub(r"WEBVTT\b.*?\n\n", "", text, count=1, flags=re.DOTALL)
    # 去掉时间轴行：00:00:01,000 --> 00:00:04,000（可能带 position 后缀）
    text = re.sub(
        r"\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}.*",
        "",
        text,
    )
    # 去掉单独成行的序号
    text = re.sub(r"^\s*\d+\s*$", "", text, flags=re.MULTILINE)
    # 去掉 VTT 常见的 <...> 样式标签
    text = re.sub(r"<[^>]+>", "", text)
    # 折叠成连续非空行
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 发布简介（yt-dlp 元数据）
# --------------------------------------------------------------------------- #
def extract_description(source_url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """抓取视频发布元数据（标题 / 简介 / 标签 / 作者）。失败返回带 error 的字典。"""
    if not source_url:
        return {"ok": False, "error": "缺少视频链接，无法获取发布简介"}
    try:
        from yt_dlp import YoutubeDL
    except Exception:
        return {"ok": False, "error": "后端未安装 yt-dlp，无法抓取发布简介"}

    opts: dict[str, Any] = {
        "simulate": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": True,
        "noplaylist": True,
    }
    if cookie.strip():
        c = cookie.strip()
        if c.lower().startswith("cookie:"):
            c = c[7:].strip()
        opts.setdefault("http_headers", {})["Cookie"] = c
    if proxy.strip():
        opts["proxy"] = proxy.strip()
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(source_url, download=False) or {}
    except Exception as exc:  # noqa: BLE001 - 抓取失败不应中断主流程
        return {"ok": False, "error": f"抓取发布简介失败：{str(exc)[:200]}"}

    desc = (info.get("description") or "").strip()
    return {
        "ok": True,
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "description": desc,
        "tags": list(info.get("tags") or []),
        "webpage_url": info.get("webpage_url") or source_url,
        "duration": int(info.get("duration") or 0),
    }


# --------------------------------------------------------------------------- #
# 口播文案（字幕 / 语音转写）
# --------------------------------------------------------------------------- #
def _ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def extract_spoken(
    video_path: str | Path,
    source_url: str = "",
    cookie: str = "",
    proxy: str = "",
    workdir: str | Path | None = None,
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    """提取口播文案：先试内嵌 / 在线字幕（快），没有再回退 whisper 语音转写。"""
    video_path = Path(video_path)
    workdir = Path(workdir) if workdir else video_path.parent
    workdir.mkdir(parents=True, exist_ok=True)

    # 1) 内嵌字幕流
    try:
        import subtitles as _sub
        emb = _sub.extract_embedded_subs(video_path, out_dir=workdir, ffmpeg_bin=_ffmpeg_bin())
        if emb and emb.exists() and emb.stat().st_size > 0:
            return {"ok": True, "source": "embedded", "text": srt_to_plaintext(emb)}
    except Exception:  # noqa: BLE001 - 抽内嵌字幕失败不致命
        pass

    # 2) 在线字幕
    if source_url:
        try:
            import subtitles as _sub
            langs = _sub.list_online_subs(source_url, cookie=cookie, proxy=proxy)
            if langs:
                lang = langs[0]["lang"]
                sub = _sub.extract_online_sub(
                    source_url, lang, cookie=cookie, proxy=proxy,
                    out_dir=workdir, title=video_path.stem,
                )
                if sub and sub.exists() and sub.stat().st_size > 0:
                    return {"ok": True, "source": "online", "lang": lang,
                            "text": srt_to_plaintext(sub)}
        except Exception:  # noqa: BLE001
            pass

    # 3) whisper 语音转写（best-effort，需要外部解说管线）
    if progress_cb:
        progress_cb("whisper", "未找到字幕，尝试 AI 语音转写…")
    res = _whisper_transcribe(video_path, workdir=workdir, progress_cb=progress_cb)
    if res:
        return {"ok": True, "source": "whisper", "text": res["text"],
                "segments": res.get("segments")}
    return {
        "ok": False,
        "error": "未找到字幕，且当前环境未配置语音转写（AI 解说 / whisper），无法提取口播文案",
    }


def _whisper_transcribe_inprocess(
    loc: "CommentaryLocation",
    video_path: str | Path,
    workdir: str | Path | None,
    progress_cb: ProgressCb = None,
) -> dict[str, Any] | None:
    """打包版专用：直接在当前进程内调用随包 frozen 的 faster_whisper + commentary/scripts/transcribe。

    macOS windowed 二进制无法用 `subprocess -c` 重入自身（PyInstaller 限制），故不走子进程。
    包内已冻结 faster_whisper，且随包含 commentary/scripts/transcribe.py 与 models/whisper-base，
    只需把 scripts 目录加入 sys.path 即可正常 import。
    """
    try:
        import faster_whisper  # frozen 包内已冻结，能否 import 即自检可用性
    except Exception:
        return None

    scripts_dir = Path(loc.root) / "scripts"
    if not (scripts_dir / "transcribe.py").exists():
        return None
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp())
    workdir.mkdir(parents=True, exist_ok=True)
    stem = Path(video_path).stem
    audio_path = workdir / f"{stem}.wav"
    out_json = workdir / f"{stem}.transcript.json"

    if progress_cb:
        progress_cb("whisper", "未找到字幕，尝试 AI 语音转写…")
    try:
        from transcribe import transcribe, extract_audio
        extract_audio(str(video_path), str(audio_path))
        segs, _ = transcribe(str(audio_path))
    except Exception:  # noqa: BLE001 - 任一环节失败都视为无法转写
        return None
    try:
        json.dump(segs, open(out_json, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    text = "\n".join(
        (s.get("text") or "").strip() for s in segs if (s.get("text") or "").strip()
    )
    if not text:
        return None
    return {"text": text, "segments": segs}


def _whisper_transcribe(
    video_path: str | Path,
    workdir: str | Path | None = None,
    progress_cb: ProgressCb = None,
) -> dict[str, Any] | None:
    """用 AI 解说管线的 faster_whisper 转写音轨。成功返回 {'text','segments'}，否则 None。"""
    try:
        from commentary_locate import locate_commentary, CommentaryLocation
    except Exception:
        return None
    loc = locate_commentary()
    if loc is None:
        return None
    # 打包版（frozen / windowed）：windowed 二进制无法用 subprocess -c 重入自身，
    # 改走进程内调用（见 _whisper_transcribe_inprocess）。
    if loc.bundled:
        return _whisper_transcribe_inprocess(loc, video_path, workdir, progress_cb)
    # 开发/外部：用解说管线自带的 Python（其 venv 已装 faster_whisper），子进程最稳。
    try:
        chk = subprocess.run(
            [loc.python, "-c", "import faster_whisper"],
            capture_output=True, timeout=60,
        )
        if chk.returncode != 0:
            return None
    except Exception:
        return None

    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp())
    workdir.mkdir(parents=True, exist_ok=True)
    stem = Path(video_path).stem
    audio_path = workdir / f"{stem}.wav"
    out_json = workdir / f"{stem}.transcript.json"

    scripts_dir = str(Path(loc.root) / "scripts")
    # 复用解说管线的 transcribe：先抽音轨再转写，结果写 JSON
    script = (
        "import sys, json\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "from transcribe import transcribe, extract_audio\n"
        f"v = {str(video_path)!r}\n"
        f"a = {str(audio_path)!r}\n"
        f"o = {str(out_json)!r}\n"
        "extract_audio(v, a)\n"
        "segs, _ = transcribe(a)\n"
        "json.dump(segs, open(o, 'w', encoding='utf-8'), ensure_ascii=False)\n"
    )
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        subprocess.run(
            [loc.python, "-c", script],
            capture_output=True, text=True, timeout=1800, env=env, cwd=str(loc.root),
        )
    except Exception:  # noqa: BLE001
        return None
    if not out_json.exists():
        return None
    try:
        segs = json.loads(out_json.read_text(encoding="utf-8"))
    except Exception:
        return None
    text = "\n".join(
        (s.get("text") or "").strip() for s in segs if (s.get("text") or "").strip()
    )
    if not text:
        return None
    return {"text": text, "segments": segs}


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def extract_all(
    video_path: str | Path,
    source_url: str = "",
    cookie: str = "",
    proxy: str = "",
    mode: str = EXTRACT_BOTH,
    workdir: str | Path | None = None,
    progress_cb: ProgressCb = None,
) -> dict[str, Any]:
    """按 mode 提取文案，返回 {mode, spoken, description}。

    mode ∈ {"spoken", "description", "both"}；其余值视为不提取，返回空结构。
    """
    result: dict[str, Any] = {"mode": mode, "spoken": None, "description": None}
    if mode in (EXTRACT_DESC, EXTRACT_BOTH):
        result["description"] = extract_description(source_url, cookie=cookie, proxy=proxy)
    if mode in (EXTRACT_SPOKEN, EXTRACT_BOTH):
        result["spoken"] = extract_spoken(
            video_path, source_url=source_url, cookie=cookie, proxy=proxy,
            workdir=workdir, progress_cb=progress_cb,
        )
    return result
