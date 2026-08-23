"""yt-dlp 封装层：解析视频信息 + 执行下载并回报进度。"""

from __future__ import annotations

import logging
import os
import re
import glob
import sqlite3
import subprocess
import sys
import json
import time
import threading
from pathlib import Path
from typing import Any

# 在 import yt_dlp 之前加载本地自定义提取器插件（如 chrqj.com）。
# yt-dlp 会在自身导入时扫描 sys.path 上的 yt_dlp_plugins 包并自动注册其中的 IE。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
try:
    import yt_dlp_plugins  # noqa: F401  (触发插件自动注册)
except ImportError:
    pass

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError, UnsupportedError

from platforms import LinkError, is_china_host
from tasks import DownloadTask, TaskStore
import socket
import urllib.request
import tempfile


def _cookie_diag(key: str, value: str = "") -> None:
    """写 Cookie 诊断日志到临时文件（打包后可读，不影响正常运行）。"""
    try:
        path = os.path.join(tempfile.gettempdir(), "vdl_cookie_diag.txt")
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        with open(path, "a") as f:
            f.write(f"[{ts}] {key}={value}\n")
    except Exception:
        pass
from urllib.parse import urlparse, parse_qsl, urlencode

logger = logging.getLogger(__name__)

SOCKET_TIMEOUT = 30  # 国内 CDN 偶发慢响应，30 秒更稳
PROBE_RETRIES = 1
# 下载健壮性：防止站点/CDN 假死导致任务永久挂起、占满并发槽拖垮后续任务
# 1) 下载阶段：已开始下分片但 N 秒无字节增量 → 判定停滞，自动终止
# 2) 整体硬上限：解析+下载任意阶段超过此秒数 → 强制结束（兜底；腾讯等限速站常需更久）
DOWNLOAD_STALL_TIMEOUT = int(os.environ.get("VDL_DOWNLOAD_STALL_TIMEOUT", "180"))
# 腾讯等站按 IP/单连接限速（实测 ~16KB/s），1800s 仅够下 29MB，故放宽到 7200s 兜底
DOWNLOAD_HARD_TIMEOUT = int(os.environ.get("VDL_DOWNLOAD_HARD_TIMEOUT", "7200"))
WATCHDOG_POLL = int(os.environ.get("VDL_WATCHDOG_POLL", "5"))  # 看门狗轮询间隔（秒）


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


# 常见本地代理端口（仅 macOS 兜底扫描用）：GUI 应用读不到 shell 代理时启用
_PROXY_PORTS = (
    (7890, "http"), (7891, "socks"), (7892, "http"), (7893, "socks"),
    (10808, "http"), (10809, "socks"), (6152, "http"), (6153, "socks"),
    (1079, "http"), (1080, "socks"), (1081, "socks"), (8888, "http"),
)
_PROXY_PROBE_CACHE: str | None = None  # None=未探测, ""=无命中, str=代理串


def _probe_local_proxy_ports() -> str:
    """扫描 127.0.0.1 上的常见代理端口，命中监听的第一个即返回 yt-dlp 代理串。

    仅作兜底：当 scutil 未配置系统代理、但本机确在跑 Clash/V2Ray/Surge 等时启用。
    双击 .app 是 GUI 进程、不继承终端 http_proxy，靠此兜底避免 YouTube 直连 403。
    结果缓存到模块级变量，避免每次 YouTube 解析都重扫（约 3s 开销）。
    """
    global _PROXY_PROBE_CACHE
    if _PROXY_PROBE_CACHE is not None:
        return _PROXY_PROBE_CACHE
    for port, kind in _PROXY_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                _PROXY_PROBE_CACHE = (
                    f"socks5://127.0.0.1:{port}" if kind == "socks"
                    else f"http://127.0.0.1:{port}"
                )
                return _PROXY_PROBE_CACHE
        except OSError:
            pass
        finally:
            s.close()
    _PROXY_PROBE_CACHE = ""
    return ""


def _host_of(url: str) -> str:
    """从链接里取出主机名（去掉 www./m. 前缀），解析失败返回空串。"""
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.removeprefix("www.").removeprefix("m.")
    except ValueError:
        return ""


# B站 长链 → b23.tv 短链：B站 对 www.bilibili.com/video/BVxxx 直链风控更严，
# 手机 App 分享的 b23.tv 短链则能正常解析。这里把长链归一化为短链，绕过该限制。
_BILIBILI_LONG_URL_RE = re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]+)")


def _normalize_bilibili_url(url: str) -> str:
    """把 B站 长链归一化为 b23.tv/BVxxx 短链。

    仅归一化域名路径；保留分P（p）和时间戳（t）参数；去掉 vd_source/spm_id_from
    等追踪参数，避免触发额外风控或污染日志。非 B站 链接原样返回。
    """
    m = _BILIBILI_LONG_URL_RE.match(url)
    if not m:
        return url
    bvid = m.group(1)
    parsed = urlparse(url)
    params: dict[str, str] = {}
    if parsed.query:
        for k, v in parse_qsl(parsed.query):
            if k in ("p", "t"):
                params[k] = v
    query = urlencode(params)
    return f"https://b23.tv/{bvid}" + (f"?{query}" if query else "")


# --------------------------------------------------------------------------- #
# 通用链接归一化：平台无关净化 + B站 长链特例转 b23.tv 短链
# --------------------------------------------------------------------------- #
# 通用追踪/推广参数（地址栏常带，对解析无益；部分平台会据此触发更严的防盗链/风控）
_URL_TRACKING_PARAMS = frozenset({
    "vd_source", "spm_id_from", "from", "from_source",     "share_source",
    "share_medium", "share_from", "share_id", "shareid", "share_channel", "timestamp",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "is_search", "scene", "sid", "campaign", "ame_from", "monitor",
    "xhsshare", "appuid", "wxfcache", "m_source",
})


def _strip_tracking_params(url: str) -> str:
    """通用：剥掉 vd_source/spm_id_from/from/share_* 等追踪参数，其余保留。"""
    parsed = urlparse(url)
    if not parsed.query:
        return url
    kept: list[tuple[str, str]] = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k.lower() in _URL_TRACKING_PARAMS:
            continue
        kept.append((k, v))
    if not kept:
        return parsed._replace(query="").geturl()
    return parsed._replace(query=urlencode(kept)).geturl()


def _normalize_share_url(url: str) -> str:
    """链接归一化入口（平台无关）。

    1. B站：www.bilibili.com/video/BVxxx 长链 → b23.tv/BVxxx 短链
       （B站 对分享短链放行、对长链直链风控更严，已验证有效）。
    2. 其他平台：剥离追踪参数，降低防盗链/风控识别概率。
       不做伪短链转换——抖音/快手/小红书短链是服务端随机 token，
       无法从长链静态推导，强行构造会破坏解析（需调分享 API，不在本范围）。
    """
    # B站 特例优先：长链转 b23.tv 短链；短链本身若带追踪参数再剥一层
    if "bilibili.com" in url or "b23.tv" in url:
        return _strip_tracking_params(_normalize_bilibili_url(url))
    # 其余平台：仅做通用追踪参数净化，零风险
    return _strip_tracking_params(url)


def _resolve_proxy(host: str = "") -> str:
    """按目标站点所在地区分流代理，海外站和国内站互不干扰。

    国内站（B站/抖音/腾讯/chrqj 等）：
      VDL_PROXY_CN（国内出口回源代理）> 直连。
      服务部署在海外（Railway 等）时，国内站会被地理围栏 403，必须配 VDL_PROXY_CN
      指向一台国内机器的 HTTP 代理；本机跑在国内则留空直连即可。

    海外站（YouTube/Twitter 等）：
      VDL_PROXY > macOS 系统代理（scutil）> 标准 http(s)_proxy 环境变量。
      刻意避开 WorkBuddy 注入的 127.0.0.1:57885（实测不通海外）。

    关键：绝不能用同一个变量兜住两边——国内代理出不去海外，海外代理进不来国内。
    """
    if host and is_china_host(host):
        return os.environ.get("VDL_PROXY_CN", "").strip()
    explicit = os.environ.get("VDL_PROXY", "").strip()
    if explicit:
        return explicit
    if sys.platform == "darwin":
        mac = _macos_system_proxy()
        if mac:
            return mac
        # scutil 读空时（双击 .app 是 GUI 进程，不继承终端 http_proxy；
        # Clash 等开了但没写系统代理时）扫描本机常见代理端口兜底
        probed = _probe_local_proxy_ports()
        if probed:
            return probed
    return os.environ.get("https_proxy") or os.environ.get("http_proxy") or ""
DOWNLOAD_RETRIES = 3
# 下载体积上限（MB）：防止被当成免费大盘偷跑带宽 / 撑爆磁盘。设为 0 表示不限。
_MAX_FILE_MB = int(os.environ.get("VDL_MAX_FILE_MB", "2048") or 2048)
_MAX_FILE_BYTES = _MAX_FILE_MB * 1024 * 1024
# 国内站 m3u8 分片并行下载段数。低并发易触发 CDN 慢速 trickle（单连接被限速到几 KB/s），
# 适度提高可让多连接分摊带宽、显著改善长视频下载速度。
# 腾讯等平台实测：单连接限速 ~1KB/s，但**单 IP 总带宽硬顶 ~18KB/s**（与并发数无关）。
# 16 并发已吃满该上限（VPS 实测：5并发=5KB/s, 16并发=18KB/s, 32/64/aria2c 均未突破）。
# 通过 VDL_CONCURRENT_FRAGMENTS 环境变量或下载请求字段可调（1-64，腾讯以外平台可能受益于更高值）。
CONCURRENT_FRAGMENTS = int(os.environ.get("VDL_CONCURRENT_FRAGMENTS", "16") or 16)
# 可选的外部下载器：aria2c 对大量小 .ts 分片可开更多并行连接，某些平台比内置并发上限更高。
# 需本机已安装 aria2c（打包 app 运行时依赖 PATH 上的 aria2c，缺失则自动回退原生下载器）。
# 通过 VDL_DOWNLOADER 环境变量或下载请求字段切换（值为 "aria2c" 时启用）。
VDL_DOWNLOADER = (os.environ.get("VDL_DOWNLOADER") or "native").strip().lower()
_MAX_CONCURRENT = 64  # 单任务并发上限，防止被腾讯封总连接数


def _clamp_concurrency(value: int) -> int:
    if not value or value < 1:
        return CONCURRENT_FRAGMENTS
    return max(1, min(_MAX_CONCURRENT, int(value)))


def _aria2c_path() -> str | None:
    """返回 aria2c 可执行路径；未安装返回 None（调用方回退原生下载器）。

    查找顺序：PATH（本机 brew/apt 安装）→ 打包内置（PyInstaller 冻结的 Resources/bin/aria2c，
    或 macOS .app 的 Contents/Resources/bin/aria2c）。找到打包内置版时把它所在目录前置到
    os.environ["PATH"]，确保 yt-dlp 的 subprocess 能按名检索到（yt-dlp 仅按名调用外部下载器）。
    """
    import shutil

    found = shutil.which("aria2c")
    if found:
        return found
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "bin", "aria2c"))
    exe = sys.executable
    if exe:
        # macOS .app: Contents/MacOS/VideoDownloader -> ../Resources/bin/aria2c
        candidates.append(os.path.join(os.path.dirname(exe), "..", "Resources", "bin", "aria2c"))
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            bin_dir = os.path.dirname(os.path.abspath(c))
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return c
    return None


def _build_aria2c_args(concurrency: int) -> list[str]:
    n = str(_clamp_concurrency(concurrency))
    # -x 每服务器最大连接 / -s 分片数 / -j 整体并行下载数 / -k 最小分片大小
    return ["-x", n, "-s", n, "-j", n, "-k", "1M", "--continue=true", "--max-tries=5"]


def _has_partial(workdir: Path | None) -> bool:
    """工作目录里是否残留可续传的部分文件（.part / .aria2 控制文件 / .FragN 分片）。"""
    if not workdir or not workdir.is_dir():
        return False
    try:
        for p in workdir.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if name.endswith(".part") or name.endswith(".aria2") or ".Frag" in name or name.endswith(".ytdl"):
                return True
        return False
    except OSError:
        return False
MAX_TITLE_CHARS = 80
MAX_HINT_CHARS = 180
DOWNLOAD_PHASE_CEILING = 97.0  # 下载阶段最多显示到 97%，剩余留给合并/转码

# 直链透传：用户贴的是单个可直接下载的媒体文件（.mp4 等）时，让前端直接从源站
# 把文件拉到本地，跳过服务器落盘与带宽消耗（真正只下一遍）。
_DIRECT_EXT_RE = re.compile(
    r"\.(mp4|webm|m4a|mp3|mov|mkv|ogg|flac|avi|wmv|m4v|ts)(\?|#|$|&)", re.IGNORECASE
)
# 这些域名即使是媒体扩展名结尾，也属于需经 yt-dlp 解析的平台，不能用直链透传绕过
_KNOWN_PLATFORM_HOSTS = {
    "bilibili.com", "b23.tv", "douyin.com", "tiktok.com", "tiktokv.com",
    "youtube.com", "youtu.be", "twitch.tv", "twitter.com", "x.com",
    "vimeo.com", "facebook.com", "instagram.com", "weibo.com", "qq.com",
    "v.qq.com", "iqiyi.com", "youku.com", "chrqj.com", "pan.baidu.com",
}


def _looks_like_direct_file(url: str) -> str | None:
    """若 URL 指向单个可直接下载的媒体文件（非已知平台），返回该 URL，否则 None。"""
    host = _host_of(url)
    if not host or host in _KNOWN_PLATFORM_HOSTS or is_china_host(host):
        return None
    if _DIRECT_EXT_RE.search(urlparse(url).path or ""):
        return url
    return None


def _cache_user_cookie(host: str, cookie: str) -> None:
    """把用户在「高级选项」手动粘贴的 Cookie 持久化到本地缓存。

    这样同站点后续解析/下载自动带登录态，不必每次重粘。
    复用 cookie_cache 模块（chmod 600、30 天 TTL、仅本机），合规且不外传。
    """
    try:
        from cookie_cache import _save
        text = cookie.strip()
        if text.lower().startswith("cookie:"):
            text = text[7:].strip()
        if text:
            _save(host, text)
    except Exception:
        pass


def _detect_direct_url(info: dict[str, Any]) -> str | None:
    """yt-dlp 解析结果若本身就是单个可直接下载的媒体文件，返回其直链。"""
    if not info.get("direct"):
        return None
    url = info.get("url") or ""
    if not url:
        return None
    protocol = (info.get("protocol") or "").split("+")[0].lower()
    if protocol not in ("http", "https", ""):
        return None
    if _DIRECT_EXT_RE.search(url) or _DIRECT_EXT_RE.search(f".{info.get('ext') or ''}"):
        return url
    return None


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
WEBM_KEY = "webm"
M4A_KEY = "m4a"


class ResolveError(LinkError):
    """解析阶段失败（链接失效、需要登录、地区限制等）。"""


class ResolveRestricted(LinkError):
    """视频疑似会员专享 / 付费 / 地区限制，yt-dlp 拿不到真实数据流。

    与 ResolveError 的区别：这类链接 yt-dlp 往往不报错，而是回填占位符
    元数据（标题形如 "vqq-video video #xxx"、时长为空），属于确认无解的受限内容。
    """


class DownloadCanceled(Exception):
    """用户主动取消下载。"""


class DownloadPaused(Exception):
    """用户暂停下载——保留 .part 文件，后续可断点续传。"""


# --------------------------------------------------------------------------- #
# 信息解析
# --------------------------------------------------------------------------- #

# —— 强反爬平台：服务端校验游客 Cookie（如抖音 s_v_web_id），匿名请求直接被拒 ——
# 这类平台无法直接匿名下载，需从用户已登录/访问过的浏览器读取 Cookie。
# 一旦检测到本机浏览器含该站 Cookie，VDL 自动注入，实现“粘贴链接即下”。
_COOKIE_HARDENED_DOMAINS: tuple[str, ...] = (
    "douyin.com", "iesdouyin.com",
    "kuaishou.com", "chenzhongtech.com", "gifshow.com",
    "xiaohongshu.com", "xhslink.com",
    "tiktok.com", "instagram.com",
    # 腾讯视频：限免/会员视频走另一套播放 API，需要登录态 cookie；
    # 加入后 app 会自动从本机浏览器读 cookie 并注入请求，提示用户粘贴。
    "v.qq.com",
    # chrqj 影视聚合站：视频流（m3u8/ts CDN）校验播放页会话 Cookie，缺则拒绝。
    # 加入后自动从本机浏览器读该站 Cookie 并注入视频流请求头（无需手动粘贴）。
    "chrqj.com",
)

# 候选浏览器（按优先级）。Chrome/Edge/Brave/Chromium 的 Cookie 解密仅需 cryptography
# （已打包进 .app），不依赖 brotli，故优先；Firefox 需 brotli，暂不入列。
_BROWSER_COOKIE_PROFILES: tuple[tuple[str, str], ...] = (
    ("chrome", "~/Library/Application Support/Google/Chrome/*/Cookies"),
    ("edge", "~/Library/Application Support/Microsoft Edge/*/Cookies"),
    ("brave", "~/Library/Application Support/BraveSoftware/Brave-Browser/*/Cookies"),
    ("chromium", "~/Library/Application Support/Chromium/*/Cookies"),
)


def is_cookie_hardened_host(host: str) -> bool:
    """判断是否为需要浏览器 Cookie 才能解析的强反爬平台。"""
    host = (host or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in _COOKIE_HARDENED_DOMAINS)


def _detect_browser_cookie_source() -> str | None:
    """探测本机已安装且含 Cookie 数据库的浏览器，返回 yt-dlp 可用的浏览器名。"""
    for name, pattern in _BROWSER_COOKIE_PROFILES:
        if glob.glob(os.path.expanduser(pattern)):
            return name
    return None


def _root_domain(host: str) -> str:
    """取根域：v.qq.com → qq.com；www.douyin.com → douyin.com；a.b.com.cn → b.com.cn。"""
    parts = (host or "").strip().lower().split(".")
    if len(parts) <= 2:
        return (host or "").strip().lower()
    if len(parts) >= 3 and parts[-2] in ("com", "net", "org", "gov", "edu", "co") \
            and parts[-1] in ("cn", "hk", "tw", "jp", "uk", "kr", "sg"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# ── CDN 域名 → Cookie 登录域映射 ──────────────────────────────────────
# 部分平台的视频流 URL 托管在独立 CDN 域（如 bilivideo.cn），但浏览器
# Cookie 存在主站域（如 bilibili.com）。_find_host_cookie_profile 按播放
# URL 的 host 查 Cookie 会落空，需回溯到正确的登录域。
_CDN_TO_COOKIE_DOMAIN: dict[str, str] = {
    # B 站：视频 CDN 在 *.bilivideo.[com|cn]，Cookie 在 bilibili.com
    "bilivideo.com": "bilibili.com",
    "bilivideo.cn": "bilibili.com",
}


def _cookie_domains_for_host(host: str) -> list[str]:
    """返回用于查找 Cookie 的候选域名列表（含 CDN→登录域回溯）。

    优先返回 host 自身的根域，再追加映射的登录域（去重）。
    """
    root = _root_domain(host)
    candidates = [root] if root else []
    mapped = _CDN_TO_COOKIE_DOMAIN.get(root)
    if mapped and mapped not in candidates:
        candidates.append(mapped)
    return candidates


def _find_host_cookie_profile(host: str) -> tuple[str, str] | None:
    """探测哪个浏览器的哪个 Profile 含有目标站点的 cookie，返回 (browser, profile)。

    背景：yt-dlp 的 cookiesfrombrowser 若不指定 profile 只读 Default；但用户登录态
    常落在其它 Profile（如 Chrome 的 Profile 33），导致「自动读 cookie」读错地方而落空。
    这里遍历各浏览器的所有 Profile，用 sqlite 直查 Cookies 数据库的 host_key
    是否命中目标根域，返回第一个命中 Profile。

    改进：支持 CDN 域名到 Cookie 登录域的回溯（如 bilivideo.cn → bilibili.com），
    解决 B 站等平台「播放 URL 域 ≠ Cookie 域」导致自动 Cookie 检测失效的问题。
    """
    domains = _cookie_domains_for_host(host)
    if not domains:
        return None
    try:
        import sqlite3 as _sq
    except Exception:
        return None
    for name, pattern in _BROWSER_COOKIE_PROFILES:
        for db in glob.glob(os.path.expanduser(pattern)):
            profile_dir = os.path.basename(os.path.dirname(db))
            try:
                con = _sq.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                try:
                    found = False
                    for domain in domains:
                        row = con.execute(
                            "SELECT 1 FROM cookies WHERE host_key LIKE ? LIMIT 1",
                            (f"%.{domain}",),
                        ).fetchone()
                        # host_key 有的带前导点(.qq.com)、有的是裸域(qq.com)，两种都试
                        if row is None:
                            row = con.execute(
                                "SELECT 1 FROM cookies WHERE host_key = ? LIMIT 1",
                                (domain,),
                            ).fetchone()
                        if row:
                            found = True
                            break
                    if found:
                        return (name, profile_dir)
                finally:
                    con.close()
            except Exception:
                continue
    return None


def get_browser_cookie_header(host: str, url: str) -> str | None:
    """若本机浏览器含目标站点的登录 Cookie，提取并构造可用于请求头的 Cookie 字符串。

    供「在线观看」代理自动携带登录态，免去手动粘贴。返回 None 表示无可用 Cookie
    （浏览器未安装 / 该站未登录 / 解密失败）。复用 _find_host_cookie_profile 定位
    具体 Profile（登录态常不在 Default），再用 yt-dlp 的 cookie 解密能力导出。

    重要：短链域名（如 v.kuaishou.com）的 Cookie Jar 匹配只能拿到设备指纹 Cookie，
    登录 Session 和反爬 token 通常在主域（www.kuaishou.com）或登录子域（id.kuaishou.com）。
    因此对每个候选域名都尝试 add_cookie_header，合并去重后返回完整 Cookie 头。
    """
    found = _find_host_cookie_profile(host)
    if not found:
        return None
    browser, profile = found
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
        from urllib.request import Request
        jar = extract_cookies_from_browser(browser, profile)

        # 生成候选 URL 列表：原始域名 + 常见主域变体
        # 短链/CDN 域名的登录态在主域，必须合并才能拿到完整 Session
        parts = host.split(".")
        candidates = [url]
        if len(parts) >= 2:
            base = ".".join(parts[-2:])  # e.g. kuaishou.com from v.kuaishou.com
            for sub in ("www", "id", "m"):
                c_url = f"https://{sub}.{base}/"
                if c_url != url:
                    candidates.append(c_url)

        merged_names: dict[str, str] = {}  # name → value (后者覆盖前者)
        for c_url in candidates:
            req = Request(c_url)
            jar.add_cookie_header(req)
            hdr = req.get_header("Cookie")
            if hdr:
                for pair in hdr.split("; "):
                    if "=" in pair:
                        name, _, val = pair.partition("=")
                        merged_names[name.strip()] = val.strip()

        return "; ".join(f"{n}={v}" for n, v in merged_names.items()) or None
    except Exception:
        return None


def detect_browser_cookie(host: str) -> dict[str, Any]:
    """探测本机浏览器是否含目标站点的 Cookie，供前端「检测登录态」按钮与解析结果展示。

    复用 _find_host_cookie_profile 的 sqlite 直查逻辑：若命中则返回具体
    (browser, profile)，前端可据此告诉用户「已自动读取，无需手动粘贴」。
    """
    found = _find_host_cookie_profile(host)
    if found:
        return {"available": True, "browser": found[0], "profile": found[1]}
    # 浏览器装了、但该站无 Cookie：仍返回浏览器名，便于提示「请先在浏览器登录」
    b = _detect_browser_cookie_source()
    return {"available": False, "browser": b, "profile": None}


def _base_options(retries: int = DOWNLOAD_RETRIES, host: str = "", *, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    _cookie_diag("base_options_enter", f"host={host!r} cookie_len={len(cookie)}")
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": retries,
        "extractor_retries": retries,
        "ignoreerrors": False,
        "continue": True,   # 断点续传：上次中断的 .part 可从断点接着下，大文件更稳
    }
    # 代理：用户显式传入优先；否则按平台自动策略（VDL_PROXY 环境变量 / 国内站直连 / macOS 系统代理）
    effective_proxy = proxy or _resolve_proxy(host)
    if is_china_host(host):
        # 国内站：海外部署（Railway 等）必须经 VDL_PROXY_CN 回源到国内出口，
        # 否则被地理围栏 403；本机在国内直连时该变量为空，显式置空避免 yt-dlp
        # 误读系统/环境变量里的海外代理导致超时/被拒。
        options["proxy"] = os.environ.get("VDL_PROXY_CN", "").strip()
    elif effective_proxy:
        options["proxy"] = effective_proxy
        # 走代理时（Clash/V2Ray/Surge 等常做 HTTPS MITM 中间人解密），
        # 代理替换了 SSL 证书，必须跳过证书校验否则直接 SSL 握手失败
        options["no_check_certificates"] = True
    # 国内站（B站/抖音等）反爬严格：缺 Referer/UA 常被直接 412，无论是否带 cookie 都先补上浏览器请求头
    headers = options.setdefault("http_headers", {})
    if is_china_host(host):
        # 防盗链 Referer 必须用站点自身 origin（与在线观看代理 _stream_referer 一致），
        # 写死 bilibili.com 会让 chrqj.com 等影视聚合站拿到错误 Referer → CDN 403。
        referer = "https://www.douyin.com/" if "douyin" in host else f"https://{host}/"
        headers.setdefault("Referer", referer)
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
    # Cookie：用户粘贴的会话 Cookie（字符串）优先注入请求头，覆盖环境变量级的浏览器 Cookie
    cookie_text = cookie.strip()
    if cookie_text.lower().startswith("cookie:"):
        cookie_text = cookie_text[7:].strip()
    if cookie_text:
        # 容错：用户从 DevTools 复制的常是「裸值」（只复制了 SESSDATA 那一格的 Value，
        # 没有 SESSDATA= 前缀）。B站 用 SESSDATA 单键鉴权，检测到裸值自动补前缀，
        # 避免「明明填了 Cookie 还 403」的困惑。后端直接把该字符串当 Cookie 头发，
        # 裸值会被 B站 当作无效 Cookie 导致 403。
        is_bili = host and ("bilibili.com" in host or "b23.tv" in host)
        if is_bili and "=" not in cookie_text:
            cookie_text = f"SESSDATA={cookie_text}"
            _cookie_diag("cookie_bare_value_fixed", "bilibili bare SESSDATA auto-prefixed")
        headers["Cookie"] = cookie_text
    # YouTube 专用参数：player_client 选择。
    # 2026-08 起 YouTube 对 web/ios client 强制 SABR 流（DASH only），
    # 导致 extract_info 拿不到任何可下载格式（formats 为空或仅含图片）。
    # android_music / tv_embedded / media_connect / create 仍返回完整格式列表。
    # 注意：yt-dlp 的 player_client 是「合并」模式而非「依次尝试」，
    # 多 client 列表会导致 web 的空 SABR 结果污染整体，必须只传一个。
    if host and ("youtube.com" in host or "youtu.be" in host):
        options.setdefault("extractor_args", {}).setdefault("youtube", {})["player_client"] = ["tv_embedded"]
    elif not cookie_text:
        # 自动登录态：仅当用户未手动粘贴 Cookie 时才尝试（用户粘贴的优先级最高，
        # 避免本机缓存/公共池覆盖用户显式提供的登录态）。
        try:
            from cookie_cache import get_cached_cookie_header
            cached = get_cached_cookie_header(host)
        except Exception as e:
            cached = None
            _cookie_diag("cache_exception", str(e)[:200])
        if cached:
            headers["Cookie"] = cached
            _cookie_diag("cache_hit", f"len={len(cached)} names={[p.split('=')[0] for p in cached.split('; ')]}")
        else:
            # 公共池：服务器（Railway）无本机浏览器、本机缓存也不存在时，用
            # 「用户/开发端上报、验真过的共享登录态」兜底，让 douyin/快手等
            # hardened 站在网页版也能带 Cookie 下载（配合 VDL_PROXY_CN 即闭环）。
            pooled = None
            try:
                from cookie_pool import get_cookie as _pool_get
                pooled = _pool_get(host)
            except Exception as e:
                _cookie_diag("pool_exception", str(e)[:200])
            if pooled:
                headers["Cookie"] = pooled
                _cookie_diag("pool_hit", f"len={len(pooled)}")
            else:
                browser = os.environ.get("VDL_COOKIES_FROM_BROWSER", "").strip()
                _cookie_diag("cache_miss", f"host={host}")
                if browser:
                    options["cookiesfrombrowser"] = (browser,)
                    _cookie_diag("cfb_env", browser)
                else:
                    # 全站默认尝试从本机浏览器读登录态（不再局限于白名单），
                    # 覆盖更多需要 Cookie 的站点（影视聚合站、会员专享、地区限制等）。
                    # 精确定位「含目标站点 cookie」的具体 Profile（登录态常不在 Default）。
                    found = _find_host_cookie_profile(host)
                    _cookie_diag("find_profile", str(found))
                    if found:
                        options["cookiesfrombrowser"] = found  # (browser, profile)
                        _cookie_diag("cfb_found", str(found))
                    else:
                        # 回退：探测不到具体 Profile 时仍用默认（Default）读，至少给一次机会
                        b = _detect_browser_cookie_source()
                        if b:
                            options["cookiesfrombrowser"] = (b,)
    return options


def _clean_message(raw: str) -> str:
    """去掉 yt-dlp 输出里的 ANSI 颜色码与"请去 GitHub 提 issue"之类的噪声。"""
    text = ANSI_PATTERN.sub("", raw)
    text = NOISE_PATTERN.sub("", text)
    text = text.replace("ERROR:", "").strip(" ;\n")
    return " ".join(text.split())[:MAX_HINT_CHARS]


def _effective_cookie_source(options: dict[str, Any], user_cookie: str = "") -> str:
    """根据 _base_options 产物判断 Cookie 实际来源。"""
    if user_cookie.strip():
        return "user_pasted"
    headers = options.get("http_headers", {})
    if headers.get("Cookie"):
        # 用户未粘贴时，_base_options 会从 cache / pool 注入 Cookie
        return "auto"
    if options.get("cookiesfrombrowser"):
        return "browser"
    return "none"


def _build_diag_context(
    url: str,
    cookie: str = "",
    proxy: str = "",
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造下载异常（403 等）所需的诊断上下文。"""
    host = _host_of(url) or ""
    headers = (options or {}).get("http_headers", {})
    effective_proxy = proxy or _resolve_proxy(host)
    return {
        "url": url,
        "host": host,
        "is_hardened": is_cookie_hardened_host(host),
        "is_china": is_china_host(host),
        "cookie_present": bool(headers.get("Cookie")),
        "cookie_source": _effective_cookie_source(options or {}, cookie),
        "proxy_used": bool(effective_proxy),
        "proxy": effective_proxy if effective_proxy else None,
        "referer": headers.get("Referer"),
        "user_agent": headers.get("User-Agent"),
        "is_cloud": os.environ.get("VDL_INSTANCE", "").strip().lower() == "cloud",
    }


def _diag_403(context: dict[str, Any], exc: Exception) -> None:
    """把 403 发生的上下文结构化写入临时日志，供线上排查。"""
    try:
        path = os.path.join(tempfile.gettempdir(), "vdl_403_diag.txt")
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a") as f:
            f.write(
                f"[{ts}] 403 host={context.get('host')} "
                f"url={str(context.get('url', ''))[:120]} "
                f"cookie_source={context.get('cookie_source')} "
                f"proxy={context.get('proxy')} "
                f"referer={context.get('referer')} "
                f"ua={context.get('user_agent')} "
                f"exc={type(exc).__name__}: {str(exc)[:200]}\n"
            )
    except Exception:
        pass


def _friendly_error(exc: Exception, context: dict[str, Any] | None = None) -> ResolveError:
    """把 yt-dlp 的英文异常转成用户能看懂的提示，并对 403 做根因分层。"""
    text = _clean_message(str(exc))
    lowered = text.lower()
    ctx = context or {}
    is_cloud = ctx.get(
        "is_cloud", os.environ.get("VDL_INSTANCE", "").strip().lower() == "cloud"
    )
    cloud_cookie_hint = (
        "网页版由「桌面版 VDL」共享登录态：请在桌面版 VDL 中打开并保持该平台登录，"
        "点『同步 Cookie 到云端』刷新后重试；或直接用桌面版 VDL 解析本链接。"
    )

    # 403 单独做根因分层，并记录结构化诊断日志
    if any(word in lowered for word in ("403", "forbidden", "http error 403")):
        _diag_403(ctx, exc)
        host = ctx.get("host", "")
        hardened = ctx.get("is_hardened", is_cookie_hardened_host(host)) if host else False
        cookie_present = ctx.get("cookie_present", False)
        if hardened and not cookie_present:
            return ResolveError(
                "下载被服务器拒绝（403）：该站需要登录 Cookie",
                cloud_cookie_hint if is_cloud else (
                    "该平台为强反爬站点，未检测到有效 Cookie。"
                    "请在常用浏览器登录该平台后重试，或到「高级选项 → Cookie」手动粘贴 Cookie。"
                ),
                category="cookie_required",
                context=ctx,
            )
        if hardened and cookie_present:
            return ResolveError(
                "下载被服务器拒绝（403）：Cookie 无效或已过期",
                "已检测到 Cookie，但服务器仍拒绝访问。可能原因："
                "① Cookie 已过期，请重新登录并同步；② 账号权限不足；"
                "③ 该视频为会员/付费/地区限制。",
                category="cookie_invalid_or_expired",
                context=ctx,
            )
        # 兜底通用 403
        hint = (
            "该链接被目标网站 CDN 拒绝。可能原因：①该站需要登录或 Cookie；②视频有防盗链/地区限制；"
            "③若为 YouTube：确认代理已开启且对 VDL 生效（双击 .app 不继承终端代理，需在 Clash 开启「系统代理」或 TUN 模式）；"
            "④换更低画质重试；⑤稍后再试"
        )
        # 折中：B站 不进 _COOKIE_HARDENED_DOMAINS（避免一刀切改文案），
        # 仅在通用 403 文案后追加专属提示，保留全部排查步骤。
        if host and ("bilibili.com" in host or "b23.tv" in host):
            hint += "；B站 高画质/会员视频常需登录态 Cookie，请先在本机常用浏览器登录 B站 后重试（VDL 会自动读取浏览器 Cookie 注入请求）。"
        return ResolveError(
            "下载被服务器拒绝（403）",
            hint,
            category="cdn_forbidden",
            context=ctx,
        )

    # 云端(网页版)实例遇到强反爬站需要登录态时，提示用户依赖桌面版共享登录态，
    # 而不是让用户去"本机浏览器登录"（网页版访客没有本机浏览器）。
    cloud_note = ""
    if is_cloud:
        cloud_note = cloud_cookie_hint
    rules: tuple[tuple[tuple[str, ...], str, str], ...] = (
        (("fresh cookies", "not necessarily logged in"), "该平台需要登录/游客 Cookie 才能访问",
         ("请在常用浏览器（Chrome 等）打开并登录过该平台，VDL 会自动读取浏览器 Cookie；"
          "或到「高级选项 → Cookie」手动粘贴该平台的 Cookie 字符串")
         if not cloud_note else cloud_note),
        (("private", "login required", "sign in", "members-only"), "该视频需要登录或为私密内容", "请更换公开可访问的视频链接"),
        (("geo", "not available in your country", "region"), "该视频在当前网络所在地区不可播放", "可尝试更换网络环境后重试"),
        (("unsupported url", "no video"), "无法从该链接中找到视频", "请确认链接指向的是视频播放页，而不是首页或列表页"),
        (("404", "not found", "removed", "unavailable", "does not exist"), "视频不存在或已被删除", "请检查链接是否正确、视频是否仍然在线"),
        (("timed out", "timeout", "connection", "network", "resolve", "proxy", "ssl"), "网络连接超时", "请检查本机网络（部分海外站点需要代理）后重试"),
        (("drm", "protected"), "该视频有版权保护，无法下载", "请通过官方渠道观看"),
        (("extractor error", "keyerror", "unable to extract"), "无法识别该链接对应的视频", "请确认链接完整且指向具体的视频页面"),
        (("ffmpeg", "postprocessing", "post processing", "merging"), "音视频合并失败，可能是该画质源文件格式兼容性问题", "建议：①点「重试」试一次（偶发）；②换 720P 或其他画质重新下载；③仍不行请反馈该链接"),
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


def probe(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """只解析不下载，返回 yt-dlp 的原始 info dict。"""
    url = _normalize_share_url(url)
    # 优酷：零门槛优先走「本机真实浏览器引擎」拦截 ups 自动拿 ckey+m3u8。
    if _host_of(url).endswith("youku.com"):
        # 主路径：浏览器引擎（用户粘贴链接即可，无需任何手动操作）
        try:
            from youku_browser import resolve_via_browser, browser_available
            if browser_available():
                _info = resolve_via_browser(_host_of(url) if False else url)
                if _info:
                    return _info
        except Exception as _be:
            # 浏览器引擎失败（无登录态/被风控/无 playwright），回退手动通道
            _browser_err = str(_be)
        else:
            _browser_err = "浏览器引擎不可用（playwright 未装或缺少优酷登录 profile）"

        # 回退路径：本机 ckey（youku_local）/ 公共池（cookie_pool）
        _yk_cookie = cookie
        _yk_ckey = ""
        if not _yk_cookie:
            try:
                from youku_local import get_local_ckey as _glk
                _lk, _lc = _glk()
                if _lk:
                    _yk_ckey = _lk
                    _yk_cookie = _yk_cookie or _lc
            except Exception:
                pass
            if not _yk_ckey:
                try:
                    from cookie_pool import get_cookie as _gpc, get_ckey as _gpk
                    _yk_cookie = _yk_cookie or (_gpc("youku.com") or "")
                    _yk_ckey = _gpk("youku.com") or ""
                except Exception:
                    pass
        if _yk_ckey:
            _yk_proxy = proxy or _resolve_proxy("v.youku.com")
            try:
                return _youku_info(url, _yk_cookie, _yk_ckey, proxy=_yk_proxy)
            except ResolveError:
                raise
            except Exception as _e:
                raise ResolveError(
                    "优酷解析失败", f"UPS 通道异常：{type(_e).__name__} {_e}", category="parse_failed"
                ) from None
        # 两条路都不通，给出可读提示
        raise ResolveError(
            "优酷解析失败",
            "浏览器引擎未能拿到播放地址（%s）。请确认：本机已安装 Google Chrome 且"
            "优酷登录 profile 有效（首次需在已登录优酷的浏览器里用本 App 的「优酷登录」"
            "卡片完成一次授权）。" % _browser_err,
            category="parse_failed",
        ) from None
    # 用户手动粘贴的 Cookie 持久化缓存：本次解析成功后写盘，
    # 后续同站点解析/下载自动复用，免去每次重粘。
    host = _host_of(url)
    if cookie and host:
        _cache_user_cookie(host, cookie)
    # YouTube 诊断日志（临时，定位代理/Cookie 问题后可移除）
    _debug_log = os.path.join(os.environ.get("TMPDIR", "/tmp"), "vdl_probe_debug.log")
    try:
        with open(_debug_log, "a") as _f:
            _f.write(f"[{__import__('datetime').datetime.now().isoformat()}] URL={url[:80]} host={host}\n")
            _f.write(f"  proxy={proxy or '(auto)'} cookie={'yes' if cookie else 'no'}\n")
            _effective = proxy or _resolve_proxy(host)
            _f.write(f"  effective_proxy={_effective or '(none)'}\n")
            _sys_p = _macos_system_proxy()
            _f.write(f"  macos_system_proxy={_sys_p or '(none)'}\n")
    except Exception:
        pass
    direct = _looks_like_direct_file(url)
    if direct:
        # 本身就是完整媒体文件，跳过 yt-dlp，直接交给前端从源站下载（不走服务器）
        filename = Path(urlparse(url).path).name or "video.mp4"
        return {
            "direct": True,
            "url": url,
            "title": filename,
            "ext": (Path(filename).suffix or ".mp4").lstrip("."),
            "webpage_url": url,
        }
    # 记录最后一次异常信息，用于 info 为空时透传真实原因
    _last_err: str | None = None

    try:
        opts = _base_options(PROBE_RETRIES, _host_of(url), cookie=cookie, proxy=proxy)
        # 解析阶段只拿 info dict，不做格式选择（避免 YouTube 等站因格式不匹配
        # 直接抛 "Requested format is not available"）。下载阶段再由 _format_selector 选格式。
        opts["format"] = None
        # ignoreerrors：仅 YouTube 通过代理时格式列表可能不完整，需要跳过格式错误
        # 让 extract_info 尽量返回能拿到的信息（标题/时长/缩略图等）。
        # ⚠️ 绝不能对所有站点生效！国内站（快手/抖音等）提取失败时，
        # ignoreerrors 会吞掉 ExtractorError/DownloadError 导致 extract_info 返回 None，
        # 真正的错误原因（页面结构变更/需要登录等）被完全丢失。
        _h = _host_of(url)
        if _h and ("youtube.com" in _h or "youtu.be" in _h):
            opts["ignoreerrors"] = "only_download"
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        # 诊断：记录 extract_info 返回值
        try:
            _info_keys = list(info.keys()) if info else ["(None)"]
            _info_title = (info or {}).get("title", "(no title)")
            _fmt_count = len((info or {}).get("formats") or [])
            with open(_debug_log, "a") as _f:
                _f.write(f"  extract_info OK: title={str(_info_title)[:60]} formats={_fmt_count} keys={_info_keys[:15]}\n")
        except Exception:
            pass
    except (UnsupportedError, GeoRestrictedError, DownloadError) as exc:
        raise _friendly_error(exc, _build_diag_context(url, cookie=cookie, proxy=proxy, options=opts)) from exc
    except ExtractorError as exc:
        _last_err = f"{type(exc).__name__}: {str(exc)[:200]}"
        # YouTube 等站格式选择失败时，降级用 extract_flat 重试（只拿元数据，不含格式列表）
        if "format" in str(exc).lower() or "not available" in str(exc).lower():
            try:
                opts2 = _base_options(PROBE_RETRIES, _host_of(url), cookie=cookie, proxy=proxy)
                opts2["extract_flat"] = "in"
                if "youtube.com" in (_host_of(url) or "") or "youtu.be" in (_host_of(url) or ""):
                    opts2.setdefault("extractor_args", {}).setdefault("youtube", {})["player_client"] = ["tv_embedded"]
                with YoutubeDL(opts2) as ydl2:
                    info = ydl2.extract_info(url, download=False)
                    _last_err = None  # 降级成功
            except Exception as fb_err:
                _last_err = f"{_last_err}; 降级: {type(fb_err).__name__}: {str(fb_err)[:150]}"
                raise ResolveError(
                    "视频解析失败",
                    f"建议：①检查代理是否通畅；②在「高级选项」粘贴 Cookie；"
                    f"③更换代理节点。\n详情：{_last_err}"
                ) from exc
        else:
            raise _friendly_error(exc, _build_diag_context(url, cookie=cookie, proxy=proxy, options=opts)) from exc
    except OSError as exc:  # 网络/DNS 层面的错误
        _last_err = f"{type(exc).__name__}: {_clean_message(str(exc))[:200]}"
        raise ResolveError("网络请求失败", _clean_message(str(exc))) from exc
    except Exception as exc:
        # 兜底：捕获任何未预期异常，保留完整错误用于诊断
        _last_err = f"{type(exc).__name__}: {str(exc)[:300]}"

    if not info:
        detail = "请稍后重试或更换链接"
        # 透传诊断信息：如果 extract_info 静默返回空（未抛异常），补充上下文
        diag = _last_err or (
            "extract_info 返回空结果（无异常）。"
            "常见原因：①代理 MITM 导致 SSL 握手失败但被 ignoreerrors 吞掉；"
            "②站点返回空页面；③需要登录 Cookie。建议在「高级选项」粘贴 Cookie 后重试。"
        )
        detail += f"\n\n诊断信息：{diag}"
        raise ResolveError("未获取到视频信息", detail)
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
    options.append(
        {"key": WEBM_KEY, "label": "WebM 格式", "note": "体积小·适合网页嵌入", "approx_size": 0}
    )
    options.append(
        {"key": M4A_KEY, "label": "仅音频 M4A", "note": "无损音轨", "approx_size": audio_size}
    )
    return options


def _format_selector(quality_key: str) -> str:
    if quality_key == BEST_KEY:
        # 优先 H.264(avc1) —— macOS WKWebView / Safari 不支持 AV1 和 VP9 解码，
        # 选了会导致「下载成功但播不了」。[ext=mp4] 不够（YouTube AV1 也是 .mp4），
        # 必须用 [vcodec^=avc1] 锁编码。降级链：H.264≤1080p → H.264任意 → mp4容器 → 兜底
        return (
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio/"
            "bestvideo[vcodec^=avc1]+bestaudio/"
            "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio/bestvideo+bestaudio/bv*+ba/b"
        )
    if quality_key in (AUDIO_KEY, M4A_KEY):
        return "ba/b"
    if quality_key == WEBM_KEY:
        return "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
    height = int(quality_key)
    return (
        f"bv*[vcodec^=avc1][height<={height}]+ba[ext=m4a]/"
        f"bv*[vcodec^=avc1][height<={height}]+ba/"
        f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={height}]+ba/b[height<={height}]/b[height<={height}]"
    )


def is_valid_quality(quality_key: str) -> bool:
    return quality_key in (BEST_KEY, AUDIO_KEY, WEBM_KEY, M4A_KEY) or quality_key in {
        str(h) for h, _ in QUALITY_PRESETS
    }


def quality_label(quality_key: str) -> str:
    if quality_key == BEST_KEY:
        return "最佳画质（自动）"
    if quality_key == AUDIO_KEY:
        return "仅音频 MP3"
    if quality_key == WEBM_KEY:
        return "WebM 格式"
    if quality_key == M4A_KEY:
        return "仅音频 M4A"
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
        self._has_download_step = False

    def __call__(self, payload: dict[str, Any]) -> None:
        if self._task.cancel_requested:
            raise DownloadCanceled()
        if getattr(self._task, "pause_requested", False):
            raise DownloadPaused()
        if payload.get("status") != "downloading":
            return
        self._ensure_title(payload)
        if not self._has_download_step:
            self._task.add_step("下载音视频", "running", f"已选清晰度：{self._task.quality}")
            self._task.log(f"开始下载：{self._task.quality}")
            self._has_download_step = True
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
            self._task.add_step("解析视频信息", "done", f"已获取标题《{title}》")

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
            self._task.add_step("下载音视频", "done", "音视频下载完成")
            self._task.add_step("合并与后处理", "running", "正在合并音视频…")
            self._store.update(self._task.id, status="merging", progress=98.0)


def _download_options(task: DownloadTask, quality_key: str, reporter: _ProgressReporter, *, cookie: str = "", proxy: str = "", format_id: str = "", concurrent_fragments: int = 0, downloader_type: str = "", resume: bool = False) -> dict:
    options = _base_options(DOWNLOAD_RETRIES, _host_of(task.url), cookie=cookie, proxy=proxy) | {
        "format": _format_selector(quality_key),
        "outtmpl": {"default": f"%(title).{MAX_TITLE_CHARS}s.%(ext)s"},
        "paths": {"home": str(task.workdir)},
        "windowsfilenames": True,
        "concurrent_fragment_downloads": _clamp_concurrency(concurrent_fragments),
        "progress_hooks": [reporter],
        "postprocessor_hooks": [reporter.on_postprocess],
        "overwrites": True,
        # HLS 流优先走 Python 原生下载器，下载阶段不用 ffmpeg（沙盒偶发 SIGXCPU 152 强杀）
        # 仅保留最后的 TS→mp4 remux 调用 ffmpeg（快、低风险）
        "hls_prefer_native": True,
    }
    # 断点续传：保留 .part 分片的前提下，显式开启 continue 让 yt-dlp 从上次中断处接上。
    # aria2c 分支已在 _build_aria2c_args 内置 --continue=true；此处覆盖原生下载器场景。
    if resume:
        options["continue"] = True
    # 外部下载器：aria2c（需本机已装）。未安装或类型非 aria2c 时自动回退原生，不影响下载。
    use_aria2c = (downloader_type or VDL_DOWNLOADER) == "aria2c"
    if use_aria2c:
        a2 = _aria2c_path()
        if a2:
            options["downloader"] = "aria2c"
            options["downloader_args"] = {"aria2c": _build_aria2c_args(concurrent_fragments)}
            logger.info("使用 aria2c 下载器（并发=%d, 路径=%s）", _clamp_concurrency(concurrent_fragments), a2)
        else:
            logger.warning("请求 aria2c 但本机未安装，回退原生下载器（请 brew install aria2 或 apt install aria2）")
    if _MAX_FILE_BYTES:
        options["max_filesize"] = _MAX_FILE_BYTES
    if quality_key == AUDIO_KEY:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    elif quality_key == M4A_KEY:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}
        ]
    elif quality_key == WEBM_KEY:
        options["merge_output_format"] = "webm"
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


def _write_sidecar(output: Path, task: "DownloadTask", info: dict[str, Any]) -> None:
    """下载完成后在成品旁写一个 .vdlmeta.json，供本地媒体库展示标题/平台/作者/时长。"""
    try:
        meta = {
            "title": info.get("title") or output.stem,
            "platform": task.platform,
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": int(info.get("duration") or 0),
            "source_url": info.get("webpage_url") or task.url,
            "thumbnail": info.get("thumbnail") or "",
            "completed_at": int(time.time()),
        }
        sidecar = output.with_name(output.stem + ".vdlmeta.json")
        sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("写入元数据侧车失败: %s", output, exc_info=True)


def build_slow_warning(host: str, speed_bps: float) -> dict:
    """构造慢速告警负载：文案 + 建议 + 可一键尝试的更低清晰度。

    国内强反爬站（腾讯/B站等）限速常因单/CDN 节点带宽限制，降清晰度往往换到
    更快的节点；本机 IP 被限则建议代理/VPN。
    """
    speed_txt = _format_bytes(int(speed_bps)) + "/s"
    hardened = host in ("v.qq.com", "bilibili.com", "www.bilibili.com", "iyunying.com")
    message = f"下载速度过慢（{speed_txt}），可能触发了站点限速"
    suggestions = [
        "更换更低清晰度（如 480P / 360P），常分配到更快的 CDN 节点",
        "使用代理 / VPN 绕过本机 IP 限速（已自动注入的浏览器登录态换节点后保留）",
    ]
    if not hardened:
        suggestions.pop(1) if len(suggestions) > 1 else None
    return {
        "level": "warn",
        "speed_bps": int(speed_bps),
        "message": message,
        "suggestions": suggestions,
        "suggested_quality_keys": ["480", "360"] if hardened else ["480", "360"],
    }


def run_download(task: DownloadTask, store: TaskStore, quality_key: str, cookie: str = "", proxy: str = "", max_retries: int = 0, format_id: str = "", concurrent_fragments: int = 0, downloader_type: str = "", resume: bool = False) -> None:
    """在后台线程执行，全部异常都写回任务状态，不向外抛。

    max_retries=N 时，对网络/超时/连接类等「可重试」错误最多再试 N 次（指数退避）。
    重试在 worker 线程内循环进行，不会额外占用并发槽；会员受限 / 链接失效等不可重试
    错误会直接以 failed 结束，避免无效重试浪费带宽。

    健壮性：内置「停滞看门狗 + 整体硬超时」，防止站点/CDN 假死让任务永久挂起、
    占满并发槽拖垮后续所有下载（典型如 m3u8 流慢速 trickle 不触发 socket_timeout）。
    """
    stop = threading.Event()
    last = {"bytes": 0, "disk": 0, "ts": time.time()}
    _workdir = Path(task.workdir) if task.workdir else None

    def _workdir_bytes() -> int:
        """工作目录里最大文件体积——m3u8_native 等协议 yt-dlp 进度钩子只在整段下完才触发，
        不足以作为「还在跑」的信号；用磁盘上文件实际增长作兜底。"""
        if not _workdir or not _workdir.is_dir():
            return 0
        try:
            return max((p.stat().st_size for p in _workdir.iterdir() if p.is_file()), default=0)
        except OSError:
            return 0

    def _watchdog() -> None:
        """下载中但 N 秒无字节增量 → 判定停滞，置取消标记让进度回调抛出终止。
        信号：①yt-dlp 进度钩子报告的 downloaded_bytes ②工作目录里最大文件体积
        （覆盖 m3u8_native/分段合并等无进度钩子场景）"""
        while not stop.is_set() and not task.is_finished:
            time.sleep(WATCHDOG_POLL)
            if task.status != "downloading":
                continue
            cur_disk = _workdir_bytes()
            if cur_disk > last["disk"]:
                last["disk"] = cur_disk
                last["ts"] = time.time()
            if task.downloaded_bytes > last["bytes"]:
                last["bytes"] = task.downloaded_bytes
                last["ts"] = time.time()
            elif cur_disk > last["disk"]:
                pass  # 上一分支已更新时间
            elif time.time() - last["ts"] > DOWNLOAD_STALL_TIMEOUT:
                task.add_step("下载音视频", "error", f"停滞 {DOWNLOAD_STALL_TIMEOUT}s，已自动终止")
                task.log(f"下载停滞超过 {DOWNLOAD_STALL_TIMEOUT}s，自动终止")
                task.cancel_requested = True
                return

    wd = threading.Thread(target=_watchdog, name=f"wd-{task.id}", daemon=True)
    wd.start()
    try:
        for attempt in range(1, max_retries + 2):
            if attempt > 1:
                # 重试前把状态拨回排队，让前端进度条归零、状态显示「重试中」
                store.update(
                    task.id, status="pending", error="", hint="", progress=0.0,
                    downloaded_bytes=0, total_bytes=0, speed=0.0, eta=0,
                )
                last["bytes"] = 0
                last["ts"] = time.time()
            # 实际下载放到子线程，主线程带「整体硬超时」等待，避免解析/下载任意阶段无限挂起
            th = threading.Thread(
                target=_run_once, args=(task, store, quality_key, cookie, proxy, format_id, concurrent_fragments, downloader_type, resume),
                name=f"dl-{task.id}-{attempt}", daemon=True,
            )
            th.start()
            th.join(timeout=DOWNLOAD_HARD_TIMEOUT)
            if th.is_alive():
                task.add_step("下载音视频", "error", f"超过硬上限 {DOWNLOAD_HARD_TIMEOUT}s")
                task.log(f"下载超过整体硬上限 {DOWNLOAD_HARD_TIMEOUT}s，强制结束")
                task.cancel_requested = True
                store.update(
                    task.id, status="failed", error="下载超时",
                    hint="站点响应过慢或连接不稳定，请稍后重试或更换清晰度/代理",
                )
                break
            t = store.get(task.id)
            if t is None:
                return
            if t.status != "failed":
                return  # completed / canceled -> 停止
            if attempt > max_retries:
                return
            if not _is_retryable(t.error):
                return
            time.sleep(min(2 ** attempt, 30))  # 指数退避，最多 30s
    finally:
        stop.set()


def _mark_step_error(task: DownloadTask, detail: str) -> None:
    """根据当前进行中的步骤，把对应步骤标记为失败。"""
    if any(s.get("name") == "合并与后处理" and s.get("status") == "running" for s in task.steps):
        task.add_step("合并与后处理", "error", detail)
    elif any(s.get("name") == "下载音视频" and s.get("status") in ("running", "done") for s in task.steps):
        task.add_step("下载音视频", "error", detail)
    else:
        task.add_step("解析视频信息", "error", detail)


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024:
            return f"{value:.2f} {unit}"
    return f"{value:.2f} PB"


def _run_once(task: DownloadTask, store: TaskStore, quality_key: str, cookie: str = "", proxy: str = "", format_id: str = "", concurrent_fragments: int = 0, downloader_type: str = "", resume: bool = False) -> None:
    """执行一次下载：先解析元数据，再进入实际下载。

    把 extract_info(..., download=False) 与 process_info(info) 拆成两阶段，
    让「解析视频信息」步骤能快速收敛，且下载阶段一旦卡住就能被看门狗识别。
    """
    task.url = _normalize_share_url(task.url)
    reporter = _ProgressReporter(task, store)
    task.add_step("排队等待", "done", "已开始执行")
    task.add_step("解析视频信息", "running", f"正在解析：{task.url[:120]}…")
    info: dict[str, Any] = {}
    try:
        _dl_opts = _download_options(task, quality_key, reporter, cookie=cookie, proxy=proxy, format_id=format_id, concurrent_fragments=concurrent_fragments, downloader_type=downloader_type, resume=resume)
        with YoutubeDL(_dl_opts) as ydl:
            # 阶段 1：只解析元数据，不下载
            info = ydl.extract_info(task.url, download=False) or {}
            if info.get("webpage_url"):
                task.source_url = info["webpage_url"]
            if info.get("title"):
                store.update(task.id, title=info["title"])
                task.add_step("解析视频信息", "done", f"已获取标题《{info['title']}》")
            else:
                task.add_step("解析视频信息", "done", "未获取到标题")

            # 阶段 2：真正开始下载；先把状态置为 downloading，看门狗才能生效
            task.add_step("下载音视频", "running", f"已选清晰度：{task.quality}")
            task.log(f"开始下载：{task.quality}")
            store.update(task.id, status="downloading", progress=0.0)

            # 记录本次 yt-dlp 实际选中的格式 ID（用于 403 降级时向用户说明原因）
            _sel_parts: list[str] = []
            if info.get("requested_formats"):
                _sel_parts = [str(f.get("format_id", "?")) for f in info["requested_formats"]]
            elif info.get("format_id"):
                _sel_parts = [str(info["format_id"])]
            _sel_fmt = "+".join(_sel_parts) or "未知"

            # YouTube 403 自动降级：tv_embedded 等客户端的某些格式 ID
            # （如 AV1 400/39x、部分 H.264 298/18）URL 被 Google CDN 拒绝，
            # 捕获后自动换已知可用格式重试，用户无感知。
            try:
                ydl.process_info(info)
            except (DownloadError, ExtractorError) as _exc:
                _err_str = str(_exc)
                _is_403 = "403" in _err_str or "Forbidden" in _err_str
                _yt_host = _host_of(task.url)
                _is_yt = _yt_host and ("youtube.com" in _yt_host or "youtu.be" in _yt_host)
                if _is_403 and _is_yt:
                    task.log(
                        f"YouTube 格式 {_sel_fmt} 的下载地址被 CDN 拒绝(403，"
                        f"URL 绑定的出口 IP / 签名不匹配)，自动降级重试…"
                    )
                    task.add_step(
                        "下载音视频", "running",
                        f"格式 {_sel_fmt} 被 YouTube 拒绝，自动切换兼容格式…",
                    )
                    # 多轮降级：依次尝试不同格式链，优先 H.264(avc1) 编码
                    # （最不易被 CDN 拒绝），再放宽到 VP9/AV1、降低分辨率，
                    # 最终兜底 best。每一轮独立 try，直到成功或穷尽所有链。
                    _fb_base = _download_options(
                        task, quality_key, reporter, cookie=cookie, proxy=proxy,
                        format_id=format_id, concurrent_fragments=concurrent_fragments,
                        downloader_type=downloader_type, resume=resume,
                    )
                    _fallback_chains = [
                        "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/b[ext=mp4]",
                        "bv*[vcodec^=avc1]+ba/bv*[height<=720]+ba/b[height<=720]",
                        "299+140/248+140/137+140/136+140/135+140/134+140/133+140/160+140",
                        "bv*+ba/best[height<=1080]/best",
                    ]
                    _done = False
                    for _chain in _fallback_chains:
                        try:
                            _fb_opts = dict(_fb_base)
                            _fb_opts["format"] = _chain
                            with YoutubeDL(_fb_opts) as _ydl2:
                                info = _ydl2.extract_info(task.url, download=False) or info
                                if info.get("title"):
                                    store.update(task.id, title=info["title"])
                                _ydl2.process_info(info)
                            _done = True
                            task.log(f"已用兼容格式链 {_chain} 完成下载")
                            break
                        except (DownloadError, ExtractorError) as _e2:
                            _e2_str = str(_e2)
                            if "403" in _e2_str or "Forbidden" in _e2_str:
                                task.log(f"格式链 {_chain} 仍被拒绝，继续尝试下一组…")
                                continue
                            raise
                    if not _done:
                        # 所有格式链都 403：极可能是代理出口 IP 不一致
                        _eff = proxy or _resolve_proxy(_yt_host)
                        _proxy_note = (
                            "（当前生效代理：%s；双击 .app 不继承终端代理，"
                            "请确认 Clash/V2Ray 已开启「系统代理」或 TUN 模式）"
                            % (_eff or "无，直连")
                        )
                        task.log("YouTube 所有兼容格式均被 CDN 拒绝，疑似代理出口 IP 不匹配" + _proxy_note)
                        raise DownloadError(
                            "YouTube 下载被 CDN 全面拒绝(403)：请检查代理设置后重试" + _proxy_note
                        )
                else:
                    raise

            output = _locate_output(info, task.workdir or Path("."))
            _write_sidecar(output, task, info)
    except DownloadPaused:
        task.add_step("下载音视频", "done", "已暂停（可继续下载）")
        task.log("用户暂停下载")
        store.update(task.id, status="paused",
                     progress=task.progress, downloaded_bytes=task.downloaded_bytes)
        # 保留 .part 文件，不清除——后续继续时 yt-dlp 断点续传
    except DownloadCanceled:
        _mark_step_error(task, "用户已取消")
        # 断点续传：取消时【不清除】工作目录里的 .part 分片，仅当确实残留部分文件时标记可续传，
        # 后续「继续下载」让 yt-dlp 从中断处接上（aria2c 走 --continue，原生下载器走 continue=True）。
        resumable = _has_partial(task.workdir)
        store.update(
            task.id, status="canceled", error="已取消下载",
            progress=task.progress, downloaded_bytes=task.downloaded_bytes,
            resumable=resumable,
        )
        if not resumable:
            store.clear_files(task.id)
    except (UnsupportedError, GeoRestrictedError, ExtractorError, DownloadError) as exc:
        err = _friendly_error(exc, _build_diag_context(task.url, cookie=cookie, proxy=proxy, options=_dl_opts))
        _mark_step_error(task, err.message)
        # 下载中断类失败（网络抖动/限速假死）往往残留部分分片，标记可续传
        store.update(task.id, status="failed", error=err.message, hint=err.hint,
                     resumable=_has_partial(task.workdir))
    except (OSError, ResolveError) as exc:
        message = getattr(exc, "message", None) or "下载过程中出现错误"
        _mark_step_error(task, message)
        store.update(task.id, status="failed", error=message, hint=_clean_message(str(exc)),
                     resumable=_has_partial(task.workdir))
    except Exception as exc:  # noqa: BLE001 - 兜底，保证任务状态一定收敛
        logger.exception("下载任务 %s 未预期失败", task.id)
        _mark_step_error(task, "未预期错误")
        store.update(task.id, status="failed", error="下载失败", hint=_clean_message(str(exc)),
                     resumable=_has_partial(task.workdir))
    else:
        if task.cancel_requested or task.is_finished:  # 已被看门狗/硬超时/用户终止，不再写完成态
            return
        # 可选：下载完成后提取文案（口播/简介），失败不影响下载完成态
        if task.extract_mode:
            _run_extraction(task, store, output, info, cookie, proxy)
        task.add_step("合并与后处理", "done", f"输出文件：{output.name}")
        task.add_step("下载完成", "done", f"文件大小：{_format_bytes(output.stat().st_size)}")
        task.log(f"下载完成：{output.name}")
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


def _run_extraction(task: DownloadTask, store: TaskStore, output: Path, info: dict[str, Any],
                    cookie: str = "", proxy: str = "", mode: str | None = None) -> None:
    """在后台线程里执行文案提取，结果写回任务。任何失败都降级处理，不影响下载完成态。

    mode 为 None 时沿用 task.extract_mode；info 为 None 时（重提取场景）改用 task.source_url。
    """
    mode = mode or task.extract_mode
    if mode not in ("spoken", "description", "both"):
        return
    source_url = (info.get("webpage_url") if info else None) or task.source_url or task.url
    task.add_step("提取文案", "running", "正在提取文案…")
    store.update(task.id, extract_status="running")
    workdir = task.workdir

    def progress_cb(stage: str, detail: str) -> None:
        task.add_step("提取文案", "running", detail)

    try:
        from extract_text import extract_all
        result = extract_all(
            str(output), source_url=source_url, cookie=cookie, proxy=proxy,
            mode=mode, workdir=workdir, progress_cb=progress_cb,
        )
        task.extracted_text = result
        task.extract_status = "done"
        task.add_step("提取文案", "done", "文案提取完成")
        store.update(task.id, extracted_text=result, extract_status="done")
    except Exception as exc:  # noqa: BLE001 - 文案提取失败绝不能拖垮下载任务
        logger.exception("提取文案失败 task=%s", task.id)
        err = {"error": str(exc)[:300]}
        task.extracted_text = err
        task.extract_status = "error"
        task.add_step("提取文案", "error", f"文案提取失败：{str(exc)[:120]}")
        store.update(task.id, extracted_text=err, extract_status="error")


def _is_retryable(error: str) -> bool:
    """判断失败原因是否值得自动重试：网络/超时/连接/代理/临时服务端错误可重试，
    会员受限、链接失效等应直接失败，避免无效刷带宽。"""
    if not error:
        return True
    lowered = error.lower()
    keywords = (
        "超时", "timeout", "连接", "connection", "网络", "network", "resolve",
        "代理", "proxy", "ssl", "reset", "refused", "unreachable", "中断",
        "interrupted", "503", "502", "500", "429", "temporary", "temp",
        "ffmpeg", "m3u8", "hls",  # HLS 合并偶发，ffmpeg 偶发退出 → 自动重试
    )
    return any(k in lowered for k in keywords)


def _is_hls_url(url: str) -> bool:
    """粗略判断是否为 HLS 播放清单地址。"""
    return bool(url) and (".m3u8" in url or url.rstrip().endswith(".m3u8"))


def _detect_play_url(info: dict[str, Any]) -> tuple[str | None, bool]:
    """返回适合「在线观看」的播放地址与是否为 HLS。

    HLS 优先（腾讯等站原生就是 m3u8 流，浏览器可经后端代理播放）；
    否则退回 MP4 直链。返回 (url, is_hls)。
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    # 1) 从 formats 里挑分辨率最高的 HLS 流
    cands: list[tuple[int, str]] = []
    for f in formats:
        u = f.get("url") or f.get("manifest_url") or ""
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        is_hls = proto in ("m3u8", "m3u8_native") or _is_hls_url(u)
        if is_hls:
            cands.append((int(f.get("height") or 0), u))
    if cands:
        cands.sort(key=lambda x: x[0], reverse=True)
        return cands[0][1], True
    # 2) 合并 info 本身的 url 若是 HLS
    u = info.get("url") or ""
    if _is_hls_url(u):
        return u, True
    # 3) 普通 MP4 直链（info.direct 标记）
    du = _detect_direct_url(info)
    if du:
        return du, False
    # 3b) 从 formats 中挑最高分辨率非 HLS 直链（覆盖 info.direct=False 的站点）
    #     优先 H.264(avc1) —— macOS WKWebView 不支持 AV1/VP9，选了会导致黑屏
    prog_cands: list[tuple[int, int, int, str]] = []  # (is_avc1, height, tbr, url)
    for f in formats:
        u = f.get("url") or ""
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        if proto in ("m3u8", "m3u8_native") or _is_hls_url(u):
            continue
        h = int(f.get("height") or 0)
        if h:
            tbr = float(f.get("tbr") or 0) or 0.0
            vc = (f.get("vcodec") or "").lower()
            is_avc1 = 1 if ("avc1" in vc or "h264" in vc or "avc" in vc) else 0
            prog_cands.append((is_avc1, h, tbr, u))
    if prog_cands:
        # 先按是否 H.264 降序（H.264 优先），同编码按 height 降序，同 height 按 tbr 降序
        prog_cands.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        return prog_cands[0][3], False
    return None, False


def build_watch_options(info: dict[str, Any]) -> list[dict[str, Any]]:
    """为「在线观看」生成可选清晰度列表（每个清晰度对应一个可直接播放的地址）。

    - 按清晰度（height）去重：同一分辨率下平台常给出多个 format（不同码率/音轨/CDN），
      只保留码率最高、可播放的一个 url，避免下拉出现重复项；
    - 同时覆盖两类可播源：HLS 直播清单（m3u8）与渐进式直链（MP4/WebM，可在 <video> 直接播）；
    - 所有视频清晰度共用同一 HLS url 时，合并为「自动（源站自适应）」；
    - 既无 HLS 又有 MP4 直链时，补一个 MP4 直链选项。
    保证返回的每个 url 都能直接交给后端 /api/stream/proxy 代理播放。
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    by_height: dict[int, dict[str, Any]] = {}   # height -> HLS 最佳可播放项
    prog: dict[int, dict[str, Any]] = {}         # height -> 渐进式直链最佳项
    audio: dict[str, Any] | None = None          # 纯音频 HLS（无 height）
    for f in formats:
        u = (f.get("url") or f.get("manifest_url") or "").strip()
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        is_hls = proto in ("m3u8", "m3u8_native") or _is_hls_url(u)
        height = int(f.get("height") or 0)
        note = (f.get("format_note") or "").strip()
        fid = f.get("format_id") or ""
        tbr = float(f.get("tbr") or 0) or 0.0
        item = {"url": u, "note": note, "fid": fid, "tbr": tbr}
        if is_hls:
            if height:
                cur = by_height.get(height)
                if cur is None or tbr > cur["tbr"]:
                    by_height[height] = item
            elif audio is None:
                audio = item
        else:
            # 渐进式直链：放宽过滤——只要有 url + height 就纳入观看选项。
            # 很多第三方提取器 ext/proto 字段不规范；播放失败由前端 onerror 兜底。
            # 同分辨率优先 H.264(avc1) —— WKWebView 不支持 AV1/VP9
            if height:
                cur = prog.get(height)
                vc = (f.get("vcodec") or "").lower()
                is_avc1 = ("avc1" in vc or "h264" in vc or "avc" in vc)
                cur_vc = (cur.get("vcodec") or "") if cur else ""
                cur_is_avc1 = ("avc1" in cur_vc or "h264" in cur_vc or "avc" in cur_vc) if cur else False
                # 优先 H.264；同编码选高码率
                if cur is None or (is_avc1 and not cur_is_avc1) or (is_avc1 == cur_is_avc1 and tbr > cur["tbr"]):
                    item["_vcodec"] = vc  # 保留编码信息供调试
                    prog[height] = item

    # 合并：同清晰度优先 HLS（自适应更好），无 HLS 才用渐进式直链
    merged: dict[int, tuple[bool, dict[str, Any]]] = {}
    for h, v in by_height.items():
        merged[h] = (True, v)          # (is_hls, item)
    for h, v in prog.items():
        merged.setdefault(h, (False, v))

    opts: list[dict[str, Any]] = []
    video_urls = {v["url"] for v in by_height.values()}
    if len(video_urls) == 1 and by_height:
        # 所有视频清晰度共用同一清单：源站按带宽自适应，无需手动选
        url = next(iter(video_urls))
        tag = f"{max(by_height)}P"
        opts.append({"key": "auto", "label": f"自动（源站自适应） · {tag}",
                     "url": url, "format_id": "", "is_hls": True})
    else:
        for height in sorted(merged, reverse=True):
            is_hls, v = merged[height]
            label = f"{height}P"
            note = v["note"]
            if note and str(height) not in note and note.lower() not in ("hls", "m3u8"):
                label = f"{label} · {note}"
            if not is_hls:
                label += " · MP4"
            opts.append({
                "key": str(height),
                "label": label,
                "url": v["url"],
                "format_id": v["fid"],
                "is_hls": is_hls,
            })

    # 无视频流但有纯音频 HLS 时，单列一个音频选项
    if not opts and audio:
        opts.append({"key": "audio", "label": audio["note"] or "音频",
                     "url": audio["url"], "format_id": "", "is_hls": True})

    if not opts:
        du = _detect_direct_url(info)
        if du:
            opts.append({"key": "mp4", "label": "MP4 直链", "url": du, "format_id": "", "is_hls": False})
    return opts


def summarize(info: dict[str, Any]) -> dict[str, Any]:
    """抽取前端需要的字段。"""
    play_url, is_hls = _detect_play_url(info)
    return {
        "title": info.get("title") or "未命名视频",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
        "view_count": info.get("view_count") or 0,
        "webpage_url": info.get("webpage_url") or "",
        "extractor": info.get("extractor_key") or "",
        "direct_url": _detect_direct_url(info),
        "play_url": play_url,
        "is_hls": is_hls,
        "watch_options": build_watch_options(info),
    }



# ---------------------------------------------------------------------------
# 国内站回源代理（桌面本地版）：桌面 app 跑在用户本机（境内直连），无需隧道，
# 故 _cn_proxy_url 回落空；Railway 网页版覆盖此行为走 127.0.0.1:18889 隧道。
# ---------------------------------------------------------------------------
def _cn_proxy_url() -> str:
    return ""


# ---------------------------------------------------------------------------
# 优酷：yt-dlp 内置 YoukuIE 不生成 ckey 播放签名，缺则 UPS 返回 -3007。
# 专用 UPS 通道：带 Cookie（来自共享池）+ ckey（来自共享池）直接拿 m3u8。
# ckey 由用户从已登录浏览器「Copy as cURL」贡献（有时效，过期重新贡献即可）。
# ---------------------------------------------------------------------------
_YOUKU_M_UA = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36"
)


def _youku_vid(url: str) -> str:
    m = re.search(r"id_([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"vid=([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    return ""


def _youku_ups_ckey(vid: str, cookie: str, ckey: str, utid: str, proxy: str = "") -> list:
    """直接打 UPS 接口（带 ckey），返回 stream 列表。失败抛 ResolveError。"""
    import json as _json
    import ssl as _ssl

    cctx = _ssl.create_default_context()
    cctx.check_hostname = False
    cctx.verify_mode = _ssl.CERT_NONE
    # 桌面本地：proxy 为空则直连（用户本机境内 IP 可直接拿）；
    # 若调用方传入 proxy（如 Railway 隧道）则走代理。
    _proxy = (proxy or "").strip() or _cn_proxy_url()
    _handlers = {}
    if _proxy:
        _handlers["http"] = _proxy
        _handlers["https"] = _proxy
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler(_handlers)) if _handlers else None
    params = (
        f"vid={vid}&ccode=0501&client_ip=0.0.0.0&app_ver=1.0.75&client_ts=1787502724"
        f"&fu=0&vr=0&rst=mp4&dq=mp4&os=android&bt=phone&bd=&tict=0&d=0&needbf=1"
        f"&site=1&aw=w&vs=1.0&pver=1&wintype=xplayer_m3u8&play_ability=1024"
        f"&utid={utid}&ckey={ckey}"
    )
    api = f"https://ups.youku.com/ups/get.json?{params}"
    req = urllib.request.Request(
        api,
        headers={
            "User-Agent": _YOUKU_M_UA,
            "Referer": "https://m.youku.com/",
            "Origin": "https://m.youku.com",
            "Cookie": cookie or "",
        },
    )
    try:
        if _opener:
            with _opener.open(req, timeout=20) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
        else:
            with urllib.request.urlopen(req, timeout=20, context=cctx) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        raise ResolveError(
            "优酷解析失败",
            f"无法连接优酷 UPS 接口（{type(e).__name__}）。",
            category="parse_failed",
        )
    code = data.get("code")
    if code not in (None, 0, "0", 200) and "stream" not in (data.get("data") or {}):
        raise ResolveError(
            "优酷解析失败",
            f"UPS 返回错误码 {code}：{data.get('msg')}。常见为 ckey 过期或 Cookie 失效，"
            f"请在已登录浏览器重新「Copy as cURL」贡献后重试。",
            category="parse_failed",
        )
    d = (data.get("data") or {})
    full = d.get("streams") or []
    if full:
        return full
    pay = d.get("pay") or {}
    if isinstance(pay, dict) and pay.get("can_play") is False:
        raise ResolveError(
            "优酷视频需会员/付费权限",
            "该优酷视频为 VIP/付费专享，当前共享池账号无播放权限，仅能试看。"
            "请用会员账号的 Cookie+ckey 重新贡献，或更换免费视频。",
            category="need_vip",
        )
    _ec = (d.get("e") or {}).get("code")
    raise ResolveError(
        "优酷解析失败",
        f"UPS 未返回可播放流。诊断：e.code={_ec}, ckey_len={len(ckey)}, "
        f"可能为 ckey 失效（有效期约40分钟，请重新贡献）。",
        category="parse_failed",
    )


def _youku_info(url: str, cookie: str = "", ckey: str = "", proxy: str = "") -> dict:
    """优酷专用解析：UPS + ckey 拿 m3u8。返回 yt-dlp 兼容 info dict。"""
    vid = _youku_vid(url)
    if not vid:
        raise ResolveError("优酷解析失败", "无法从链接识别视频 ID。", category="parse_failed")
    utid = ""
    for part in (cookie or "").split(";"):
        k, _, v = part.partition("=")
        if k.strip() == "cna":
            utid = v.strip()
            break
    if not ckey:
        raise ResolveError(
            "优酷需要播放签名(ckey)",
            "该优酷视频需登录态播放签名(ckey)，当前公共池没有有效 ckey。"
            "请在已登录优酷的浏览器中：F12 → Network → 过滤 ups → 刷新 → 右键 "
            "该请求 Copy as cURL，把内容发给我即可自动贡献。",
            category="need_ckey",
        )
    streams = _youku_ups_ckey(vid, cookie, ckey, utid, proxy=proxy)
    if not streams:
        raise ResolveError(
            "优酷解析失败",
            f"UPS 通道未拿到可播放流。ckey_len={len(ckey)}，可能为 ckey 失效。",
            category="parse_failed",
        )
    def _h(s):
        return int(s.get("height") or 0)
    streams_sorted = sorted(streams, key=_h, reverse=True)
    best = streams_sorted[0]
    m3u8 = best.get("m3u8_url") or best.get("playurl") or ""
    if not m3u8:
        for k, v in best.items():
            if "url" in k.lower() and isinstance(v, str) and v.startswith("http"):
                m3u8 = v
                break
    if not m3u8:
        raise ResolveError("优酷解析失败", "UPS 返回的流缺少 m3u8 地址。", category="parse_failed")
    title = f"优酷视频_{vid}"
    try:
        import urllib.request as _ur
        _req = _ur.Request(url, headers={"User-Agent": _YOUKU_M_UA})
        with _ur.urlopen(_req, timeout=10) as _r:
            _mt = re.search(r"<title>(.*?)</title>", _r.read().decode("utf-8", "replace"), re.S)
            if _mt:
                title = re.sub(r"[_-]?优酷.*$", "", _mt.group(1)).strip() or title
    except Exception:
        pass
    return {
        "id": vid,
        "title": title,
        "webpage_url": url,
        "extractor_key": "YoukuCkey",
        "extractor": "youku",
        "ext": "mp4",
        "direct": True,
        "url": m3u8,
        "protocol": "m3u8_native",
        "http_headers": {
            "User-Agent": _YOUKU_M_UA,
            "Referer": "https://m.youku.com/",
            "Cookie": cookie or "",
            "Origin": "https://m.youku.com",
        },
        "_youku_m3u8": True,
    }
