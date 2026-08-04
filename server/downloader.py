"""yt-dlp 封装层：解析视频信息 + 执行下载并回报进度。"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError, UnsupportedError

from platforms import LinkError, is_china_host
from tasks import DownloadTask, TaskStore
import urllib.request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SOCKET_TIMEOUT = 15
PROBE_RETRIES = 1


def _macos_system_proxy() -> str:
    """读取 macOS 系统代理设置（系统偏好里开启的那个），返回 yt-dlp 可用的代理 URL。

    优先顺序：HTTPS 代理 > HTTP 代理 > SOCKS 代理。macOS 里这些通常都是一个
    HTTP CONNECT 代理，所以用 http:// 形式返回。
    """
    try:
        out = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    values: dict[str, str] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        values[key.strip()] = val.strip()
    host = values.get("HTTPSProxy") or values.get("HTTPProxy")
    port = values.get("HTTPSPort") or values.get("HTTPPort")
    if host and port:
        return f"http://{host}:{port}"
    socks_host = values.get("SOCKSProxy")
    socks_port = values.get("SOCKSPort")
    if socks_host and socks_port:
        return f"socks5://{socks_host}:{socks_port}"
    return ""


def _host_of(url: str) -> str:
    """从链接里取出主机名（去掉 www./m. 前缀），解析失败返回空串。"""
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.removeprefix("www.").removeprefix("m.")
    except ValueError:
        return ""


def _resolve_proxy(host: str = "") -> str:
    """决定 yt-dlp 使用的代理，优先级：VDL_PROXY > 国内站直连 > macOS 系统代理 > 标准环境变量。

    - 显式 VDL_PROXY 永远优先（手动覆盖）。
    - 国内站点（腾讯/优酷/爱奇艺/B站等）直连即可，走海外代理反而会因跨境/节点问题超时，故强制不走代理。
    - 否则 macOS 上优先用系统设置的真实客户端（scutil），刻意避开 WorkBuddy 注入的 57885（实测不通海外）。
    """
    explicit = os.environ.get("VDL_PROXY", "").strip()
    if explicit:
        return explicit
    if host and is_china_host(host):
        return ""  # 国内站直连
    if sys.platform == "darwin":
        mac = _macos_system_proxy()
        if mac:
            return mac
    return os.environ.get("https_proxy") or os.environ.get("http_proxy") or ""
DOWNLOAD_RETRIES = 3
CONCURRENT_FRAGMENTS = 4
MAX_TITLE_CHARS = 80
MAX_HINT_CHARS = 180
DOWNLOAD_PHASE_CEILING = 97.0  # 下载阶段最多显示到 97%，剩余留给合并/转码

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
NOISE_PATTERN = re.compile(
    r"(please report this issue.*|Confirm you are on the latest version.*|"
    r"\s*;?\s*filling out the appropriate issue template.*)",
    re.IGNORECASE | re.DOTALL,
)

QUALITY_PRESETS: tuple[tuple[int, str], ...] = (
    (2160, "4K 2160P"),
    (1440, "2K 1440P"),
    (1080, "1080P 高清"),
    (720, "720P 高清"),
    (480, "480P 标清"),
    (360, "360P 流畅"),
)

BEST_KEY = "best"
AUDIO_KEY = "audio"


class ResolveError(LinkError):
    """解析阶段失败（链接失效、需要登录、地区限制等）。"""


class ResolveRestricted(LinkError):
    """视频疑似会员专享 / 付费 / 地区限制，yt-dlp 拿不到真实数据流。

    与 ResolveError 的区别：这类链接 yt-dlp 往往不报错，而是回填占位符
    元数据（标题形如 "vqq-video video #xxx"、时长为空），属于确认无解的受限内容。
    """


class DownloadCanceled(Exception):
    """用户主动取消下载。"""


# --------------------------------------------------------------------------- #
# 信息解析
# --------------------------------------------------------------------------- #

def _base_options(retries: int = DOWNLOAD_RETRIES, host: str = "") -> dict[str, Any]:
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": retries,
        "extractor_retries": retries,
        "ignoreerrors": False,
    }
    # 访问海外站点时通过代理：VDL_PROXY 显式指定，国内站直连，否则自动读取 macOS 系统代理 / 标准环境变量
    proxy = _resolve_proxy(host)
    if proxy:
        options["proxy"] = proxy
    # 部分站点（如 B 站高码率、小红书）需要登录态，可指定浏览器 Cookie 来源
    browser = os.environ.get("VDL_COOKIES_FROM_BROWSER", "").strip()
    if browser:
        options["cookiesfrombrowser"] = (browser,)
    return options


def _clean_message(raw: str) -> str:
    """去掉 yt-dlp 输出里的 ANSI 颜色码与"请去 GitHub 提 issue"之类的噪声。"""
    text = ANSI_PATTERN.sub("", raw)
    text = NOISE_PATTERN.sub("", text)
    text = text.replace("ERROR:", "").strip(" ;\n")
    return " ".join(text.split())[:MAX_HINT_CHARS]


def _friendly_error(exc: Exception) -> ResolveError:
    """把 yt-dlp 的英文异常转成用户能看懂的提示。"""
    text = _clean_message(str(exc))
    lowered = text.lower()
    rules: tuple[tuple[tuple[str, ...], str, str], ...] = (
        (("private", "login required", "sign in", "members-only"), "该视频需要登录或为私密内容", "请更换公开可访问的视频链接"),
        (("geo", "not available in your country", "region"), "该视频在当前网络所在地区不可播放", "可尝试更换网络环境后重试"),
        (("unsupported url", "no video"), "无法从该链接中找到视频", "请确认链接指向的是视频播放页，而不是首页或列表页"),
        (("404", "not found", "removed", "unavailable", "does not exist"), "视频不存在或已被删除", "请检查链接是否正确、视频是否仍然在线"),
        (("timed out", "timeout", "connection", "network", "resolve", "proxy", "ssl"), "网络连接超时", "请检查本机网络（部分海外站点需要代理）后重试"),
        (("drm", "protected"), "该视频有版权保护，无法下载", "请通过官方渠道观看"),
        (("extractor error", "keyerror", "unable to extract"), "无法识别该链接对应的视频", "请确认链接完整且指向具体的视频页面"),
    )
    for keywords, message, hint in rules:
        if any(word in lowered for word in keywords):
            return ResolveError(message, hint)
    return ResolveError("视频解析失败", text)


def _is_restricted_placeholder(info: dict[str, Any]) -> bool:
    """判断 yt-dlp 是否只扒到一个"壳"——标题是占位符、时长缺失。

    腾讯等平台的会员/付费受限视频，提取器不会报错，而是回填形如
    "vqq-video video #q4100..." 的占位标题且 duration 为空。这是确认无解的受限内容。
    """
    title = info.get("title")
    duration = info.get("duration")
    if duration is None and isinstance(title, str) and title.startswith("vqq-video video #"):
        return True
    # 兜底：标题完全缺失、时长缺失、且无缩略图 —— 视为根本没有解析到内容
    if duration is None and not title and not info.get("thumbnail"):
        return True
    return False


def probe(url: str) -> dict[str, Any]:
    """只解析不下载，返回 yt-dlp 的原始 info dict。"""
    try:
        with YoutubeDL(_base_options(PROBE_RETRIES, _host_of(url))) as ydl:
            info = ydl.extract_info(url, download=False)
    except (UnsupportedError, GeoRestrictedError, ExtractorError, DownloadError) as exc:
        raise _friendly_error(exc) from exc
    except OSError as exc:  # 网络/DNS 层面的错误
        raise ResolveError("网络请求失败", _clean_message(str(exc))) from exc

    if not info:
        raise ResolveError("未获取到视频信息", "请稍后重试或更换链接")
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ResolveError("该链接是一个空的合集", "请粘贴单个视频的播放地址")
        info = entries[0]
    if info.get("is_live"):
        raise ResolveError("暂不支持下载正在直播的内容", "请等直播结束生成回放后再试")
    if _is_restricted_placeholder(info):
        raise ResolveRestricted(
            "该视频疑似会员 / 付费受限，本工具暂不支持",
            "腾讯等平台的会员专享、付费或地区限制内容需官方客户端或登录态才能获取，"
            "标准下载方式无法解析。请更换为公开可播放的视频链接",
        )
    return info


# --------------------------------------------------------------------------- #
# 清晰度选项
# --------------------------------------------------------------------------- #

def _best_audio_size(formats: list[dict]) -> int:
    sizes = [
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    return max(sizes, default=0)


def _video_size_at(formats: list[dict], height: int) -> int:
    sizes = [
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in formats
        if f.get("height") == height and f.get("vcodec") not in (None, "none")
    ]
    return max(sizes, default=0)


def build_quality_options(info: dict[str, Any]) -> list[dict[str, Any]]:
    """把 yt-dlp 冗长的 format 列表压缩成几个用户看得懂的选项。"""
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    heights = sorted({f["height"] for f in formats if f.get("height")}, reverse=True)
    audio_size = _best_audio_size(formats)

    options: list[dict[str, Any]] = [
        {"key": BEST_KEY, "label": "最佳画质（自动）", "note": "视频+音频自动合并", "approx_size": 0}
    ]
    max_height = heights[0] if heights else 0
    for height, label in QUALITY_PRESETS:
        if max_height and height > max_height:
            continue
        video_size = _video_size_at(formats, height)
        options.append(
            {
                "key": str(height),
                "label": label,
                "note": "MP4",
                "approx_size": (video_size + audio_size) if video_size else 0,
            }
        )
    options.append(
        {"key": AUDIO_KEY, "label": "仅音频 MP3", "note": "提取音轨", "approx_size": audio_size}
    )
    return options


def _format_selector(quality_key: str) -> str:
    if quality_key == BEST_KEY:
        return "bv*+ba/b"
    if quality_key == AUDIO_KEY:
        return "ba/b"
    height = int(quality_key)
    return f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"


def is_valid_quality(quality_key: str) -> bool:
    return quality_key in (BEST_KEY, AUDIO_KEY) or quality_key in {
        str(h) for h, _ in QUALITY_PRESETS
    }


def quality_label(quality_key: str) -> str:
    if quality_key == BEST_KEY:
        return "最佳画质（自动）"
    if quality_key == AUDIO_KEY:
        return "仅音频 MP3"
    return dict(QUALITY_PRESETS).get(int(quality_key), f"{quality_key}P")


# --------------------------------------------------------------------------- #
# 下载执行
# --------------------------------------------------------------------------- #

class _ProgressReporter:
    """把 yt-dlp 的分流进度聚合成单调递增的整体百分比。"""

    def __init__(self, task: DownloadTask, store: TaskStore) -> None:
        self._task = task
        self._store = store
        self._streams: dict[str, tuple[int, int]] = {}
        self._last_progress = 0.0

    def __call__(self, payload: dict[str, Any]) -> None:
        if self._task.cancel_requested:
            raise DownloadCanceled()
        if payload.get("status") != "downloading":
            return
        self._ensure_title(payload)
        key = payload.get("filename") or payload.get("tmpfilename") or "stream"
        downloaded = int(payload.get("downloaded_bytes") or 0)
        total = int(payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0)
        self._streams[key] = (downloaded, max(total, downloaded))
        self._push(payload)

    def _ensure_title(self, payload: dict[str, Any]) -> None:
        """首个进度回调就把标题回填，避免任务卡片长时间显示"解析中"。"""
        if self._task.title:
            return
        title = (payload.get("info_dict") or {}).get("title")
        if title:
            self._store.update(self._task.id, title=title)

    def _push(self, payload: dict[str, Any]) -> None:
        done = sum(d for d, _ in self._streams.values())
        total = sum(t for _, t in self._streams.values())
        percent = (done / total * 100) if total else 0.0
        self._last_progress = min(max(self._last_progress, percent), DOWNLOAD_PHASE_CEILING)
        self._store.update(
            self._task.id,
            status="downloading",
            progress=self._last_progress,
            downloaded_bytes=done,
            total_bytes=total,
            speed=float(payload.get("speed") or 0.0),
            eta=int(payload.get("eta") or 0),
        )

    def on_postprocess(self, payload: dict[str, Any]) -> None:
        if payload.get("status") == "started":
            self._store.update(self._task.id, status="merging", progress=98.0)


def _download_options(task: DownloadTask, quality_key: str, reporter: _ProgressReporter) -> dict:
    options = _base_options(DOWNLOAD_RETRIES, _host_of(task.url)) | {
        "format": _format_selector(quality_key),
        "outtmpl": {"default": f"%(title).{MAX_TITLE_CHARS}s.%(ext)s"},
        "paths": {"home": str(task.workdir)},
        "windowsfilenames": True,
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENTS,
        "progress_hooks": [reporter],
        "postprocessor_hooks": [reporter.on_postprocess],
        "overwrites": True,
    }
    if quality_key == AUDIO_KEY:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        options["merge_output_format"] = "mp4"
    return options


def _locate_output(info: dict[str, Any], workdir: Path) -> Path:
    """优先用 yt-dlp 回报的路径，兜底扫描工作目录里最大的成品文件。"""
    for entry in info.get("requested_downloads") or []:
        path = entry.get("filepath")
        if path and Path(path).exists():
            return Path(path)
    candidates = [p for p in workdir.glob("*") if p.is_file() and p.suffix != ".part"]
    if not candidates:
        raise ResolveError("下载完成但未找到输出文件", "请重试一次")
    return max(candidates, key=lambda p: p.stat().st_size)


def run_download(task: DownloadTask, store: TaskStore, quality_key: str) -> None:
    """在后台线程中执行，全部异常都写回任务状态，不向外抛。"""
    reporter = _ProgressReporter(task, store)
    try:
        with YoutubeDL(_download_options(task, quality_key, reporter)) as ydl:
            info = ydl.extract_info(task.url, download=True) or {}
        if info.get("title"):
            store.update(task.id, title=info["title"])
        output = _locate_output(info, task.workdir or Path("."))
    except DownloadCanceled:
        store.update(task.id, status="canceled", error="已取消下载", progress=0.0)
        store.clear_files(task.id)
    except (UnsupportedError, GeoRestrictedError, ExtractorError, DownloadError) as exc:
        err = _friendly_error(exc)
        store.update(task.id, status="failed", error=err.message, hint=err.hint)
    except (OSError, ResolveError) as exc:
        message = getattr(exc, "message", None) or "下载过程中出现错误"
        store.update(task.id, status="failed", error=message, hint=_clean_message(str(exc)))
    except Exception as exc:  # noqa: BLE001 - 兜底，保证任务状态一定收敛
        logger.exception("下载任务 %s 未预期失败", task.id)
        store.update(task.id, status="failed", error="下载失败", hint=_clean_message(str(exc)))
    else:
        if task.cancel_requested:  # 取消请求赶在最后一个进度回调之后到达
            store.update(task.id, status="canceled", error="已取消下载", progress=0.0)
            store.clear_files(task.id)
            return
        store.update(
            task.id,
            status="completed",
            progress=100.0,
            filepath=output,
            filename=output.name,
            filesize=output.stat().st_size,
            speed=0.0,
            eta=0,
        )


def summarize(info: dict[str, Any]) -> dict[str, Any]:
    """抽取前端需要的字段。"""
    return {
        "title": info.get("title") or "未命名视频",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
        "view_count": info.get("view_count") or 0,
        "webpage_url": info.get("webpage_url") or "",
        "extractor": info.get("extractor_key") or "",
    }
