"""视频下载站 · FastAPI 后端入口。

路由一览：
    GET    /                      前端页面
    GET    /api/platforms         支持的平台清单
    POST   /api/resolve           解析链接，返回视频信息与可选清晰度
    POST   /api/download          创建下载任务
    GET    /api/tasks/{id}        查询任务状态（轮询兜底）
    GET    /api/tasks/{id}/events SSE 实时进度
    GET    /api/tasks/{id}/file   下载已完成的文件
    DELETE /api/tasks/{id}        取消任务并清理临时文件
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import socket
import logging
import os
import re
import sys
import shutil
import subprocess
import threading
import requests  # 解说 worker HTTP 模式客户端（VDL_COMMENTARY_MODE=http 时用到）
import time
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import File as _FastAPIFile, Form, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import quote, urljoin, urlparse

# 在 import downloader（间接 import yt_dlp）之前，先把本机可能已下载的新版 yt-dlp
# 插入 sys.path 最前，实现「解析器自动更新」无需重新打包。
import ydlp_update
ydlp_update.bootstrap()

import downloader
import library as library_mod
import subtitles as subtitles_mod
import subscriptions as subs_mod
import ffmpeg_tools as fftools
import retention as retention_mod
import archive as archive_mod
import crypto_vault as crypto_mod
import process_queue as pq_mod
import torrent as torrent_mod
from batch import BatchScheduler
from clouddrive import (
    BaiduProvider,
    CloudError,
    WebDAVProvider,
    baidu_auth_url,
    baidu_exchange_token,
    _baidu_callback_html,
    save_baidu_token,
    load_baidu_token,
    clear_baidu_token,
)
from platforms import CHINA_DOMAINS, LinkError, UnsupportedPlatformError, is_china_host, parse_source, platform_catalog
from tasks import TaskStore, TASK_ID_LENGTH
from llm_config import inject_llm_env, get_llm_config, save_llm_config, PROVIDER_PRESETS, DEFAULT_PROVIDER

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vdl")

if getattr(sys, "frozen", False):
    # PyInstaller 打包后资源位置：
    #   - 单文件模式：解压到 sys._MEIPASS
    #   - macOS .app（单文件夹）：数据文件在 Contents/Resources
    #   - 其它单文件夹模式：与可执行文件同目录
    _exe = sys.executable
    _macos_dir = os.path.dirname(_exe)
    _resources = os.path.normpath(os.path.join(_macos_dir, "..", "Resources"))
    if getattr(sys, "_MEIPASS", None):
        _frozen_base = Path(sys._MEIPASS)
    elif os.path.exists(os.path.join(_resources, "web")):
        _frozen_base = Path(_resources)
    else:
        _frozen_base = Path(_macos_dir)
    BASE_DIR = _frozen_base
    WEB_DIR = _frozen_base / "web"
    # 下载产物放在用户目录，避免写入只读的 .app 包内
    DOWNLOAD_DIR = Path.home() / "Downloads" / "VideoDownloader"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    WEB_DIR = BASE_DIR / "web"
    DOWNLOAD_DIR = BASE_DIR / "downloads"

MAX_CONCURRENT_DOWNLOADS = 3
MAX_CONCURRENT_PROBES = 8
# 批量下载相关环境变量（桌面版万能下载器重点能力）：
#   VDL_BATCH_HARD_MAX   线程池硬上限（实际并发不超过它，默认 8）
#   VDL_BATCH_RETRIES    失败自动重试次数（默认 2，指数退避最多 30s）
#   VDL_BATCH_MAX_ITEMS  单次批量最多链接数（默认 50，防误粘贴巨量链接打爆）
VDL_BATCH_HARD_MAX = int(os.environ.get("VDL_BATCH_HARD_MAX", "8") or 8)
BATCH_RETRIES_DEFAULT = int(os.environ.get("VDL_BATCH_RETRIES", "2") or 2)
VDL_BATCH_MAX_ITEMS = int(os.environ.get("VDL_BATCH_MAX_ITEMS", "50") or 50)
SINGLE_DOWNLOAD_RETRIES = 0  # 单条下载默认不自动重试（失败让用户手动点重试）；批量才默认重试

# ---- 格式转换（增值能力）：对已下载文件做 ffmpeg 转码，异步 job ----
CONVERT_DIR = DOWNLOAD_DIR / "conversions"
CONVERT_DIR.mkdir(parents=True, exist_ok=True)
CONVERT_JOBS: dict[str, dict] = {}
CONVERT_LOCK = threading.Lock()
FFMPEG_BIN = os.environ.get("VDL_FFMPEG_BIN") or shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if sys.platform == "darwin" else "")
# 允许的目标格式 -> ffmpeg 参数；resolution 可选 original/1080/720/480
CONVERT_TARGETS = {
    "mp4":  ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart"],
    "mov":  ["-c:v", "libx264", "-c:a", "aac"],
    "mkv":  ["-c:v", "libx264", "-c:a", "aac"],
    "webm": ["-c:v", "libvpx-vp9", "-c:a", "libopus", "-b:v", "1M"],
    "mp3":  ["-vn", "-c:a", "libmp3lame", "-q:a", "4"],
    "m4a":  ["-vn", "-c:a", "aac"],
    "gif":  ["-t", "5", "-vf", "fps=10,scale=480:-1:flags=lanczos"],
}
CONVERT_EXT = {"mp4": "mp4", "mov": "mov", "mkv": "mkv", "webm": "webm", "mp3": "mp3", "m4a": "m4a", "gif": "gif"}

# ---- 双节点分流：国内节点直连国内站，海外节点直连海外站，前端按链接域名自动选 ---- #
# VDL_REGION: 本节点所在区域，"cn"=国内 / "global"=海外（默认海外）
# VDL_PEER_ENDPOINT: 对端节点的完整地址，如 https://cn.example.com（留空=单节点模式）
# VDL_ALLOW_ORIGINS: 允许跨域访问本节点 API 的来源，逗号分隔；"*" 表示全部
NODE_REGION = (os.environ.get("VDL_REGION", "global").strip().lower() or "global")
PEER_ENDPOINT = os.environ.get("VDL_PEER_ENDPOINT", "").strip().rstrip("/")
_allow_raw = os.environ.get("VDL_ALLOW_ORIGINS", "").strip()
ALLOW_ORIGINS = [o.strip().rstrip("/") for o in _allow_raw.split(",") if o.strip()] or ([PEER_ENDPOINT] if PEER_ENDPOINT else [])
RESOLVE_TIMEOUT_SECONDS = 40          # 海外站（走代理），留出代理延迟余量
RESOLVE_TIMEOUT_DOMESTIC = 20         # 国内站（腾讯/优酷/B站等直连，本就很快；受限视频也能更快判定）
SSE_INTERVAL_SECONDS = 0.5
SSE_MAX_SECONDS = 60 * 30
CLEANUP_INTERVAL_SECONDS = 600

# ---- 自动解说（增值功能）：松耦合桥接用户现成的 commentary-pipeline ----
# 复用 commentary-pipeline/process.py 整条管线（转写→配音→出片），本服务只负责
# 把下载好的视频喂进它的 input/、等成片回传，绝不重写解说逻辑。
# 默认关闭：需显式开启 + 配置管线目录，且解说 worker 应在独立机器/进程跑，别和下载抢 CPU。
#   VDL_COMMENTARY_ENABLED=true                      启用
#   VDL_COMMENTARY_DIR=/path/to/commentary-pipeline  管线项目根目录（需有 process.py + input/ + output/）
#   VDL_COMMENTARY_PYTHON=/path/to/python            跑 process.py 的解释器（需装 faster_whisper 等依赖）
#   VDL_COMMENTARY_VOICE=zh-CN-XiaoxiaoNeural        默认配音嗓音
_COMMENTARY_EXPLICIT = "VDL_COMMENTARY_ENABLED" in os.environ
COMMENTARY_ENABLED = os.environ.get("VDL_COMMENTARY_ENABLED", "false").strip().lower() == "true"
# 桌面版自动探测：没显式设 COMMENTARY_ENABLED 时，若管线可用则自动开启。
# 仅 frozen(打包版)生效——dev/在线版须显式开启，避免 import 即改写默认行为。
# 统一走 commentary_locate.locate_commentary()（含包内捆绑候选，践行自包含铁律）。
if not _COMMENTARY_EXPLICIT and getattr(sys, "frozen", False):
    from commentary_locate import locate_commentary
    _loc = locate_commentary()
    if _loc is not None:
        COMMENTARY_ENABLED = True
        if "VDL_COMMENTARY_DIR" not in os.environ:
            os.environ["VDL_COMMENTARY_DIR"] = str(_loc.root)
        if "VDL_COMMENTARY_MODE" not in os.environ:
            # 包内捆绑走 worker 重入(#198 实现)；外部/显式走 local 子进程
            os.environ["VDL_COMMENTARY_MODE"] = "bundled" if _loc.bundled else "local"


# 解说运行环境探测已集中到 _CommentaryRuntime（见下方 _commentary_work_dir 之前的定义），
# 模块加载时一次性解析解释器与工具链，并暴露清晰的诊断信息，避免打包后路径截断导致静默失败。


# 广告位开关：默认关闭。下载站属广告平台高风险类目，默认不挂广告，
# 待流量稳定、确定接入合规广告源后再开。前端据此决定是否渲染广告位容器。
ADS_ENABLED = os.environ.get("VDL_ADS_ENABLED", "false").strip().lower() == "true"
# ---- 格式转换订阅开关（增值能力变现，默认关闭以保持开源全免费体验）----
# 部署者设 VDL_CONVERT_REQUIRE_SUB=true 并填 VDL_CONVERT_SUB_KEY 后进入「订阅墙」模式：
#   免费用户每日限 VDL_CONVERT_FREE_DAILY 次（按客户端 IP 计），超出需订阅；
#   请求头携带正确 X-Subscription-Key 的用户不限次。
# 两个开关任一为空/未设 → 视为未启用订阅墙，所有人免费无限（开源默认）。
CONVERT_REQUIRE_SUB = os.environ.get("VDL_CONVERT_REQUIRE_SUB", "false").strip().lower() == "true"
CONVERT_SUB_KEY = os.environ.get("VDL_CONVERT_SUB_KEY", "").strip()
CONVERT_FREE_DAILY = int(os.environ.get("VDL_CONVERT_FREE_DAILY", "3") or 3)
CONVERT_SUB_ENABLED = CONVERT_REQUIRE_SUB and bool(CONVERT_SUB_KEY)
_convert_quota: dict[str, dict] = {}      # ip -> {"date": "YYYY-MM-DD", "count": int}
_convert_quota_lock = threading.Lock()
# ---- 下载订阅开关（整体 freemium：免费每日限次、订阅无限；默认关闭保持全免费）----
# 与格式转换共用同一把订阅密钥 VDL_CONVERT_SUB_KEY（一个订阅解锁全部增值能力）。
# 单独开关 VDL_DOWNLOAD_REQUIRE_SUB 控制「是否对下载也启用限次」；
# VDL_DOWNLOAD_FREE_DAILY 设置免费用户每日可创建的任务数（默认 10，核心功能给得比转换宽松）。
DOWNLOAD_REQUIRE_SUB = os.environ.get("VDL_DOWNLOAD_REQUIRE_SUB", "false").strip().lower() == "true"
DOWNLOAD_FREE_DAILY = int(os.environ.get("VDL_DOWNLOAD_FREE_DAILY", "10") or 10)
DOWNLOAD_SUB_ENABLED = DOWNLOAD_REQUIRE_SUB and bool(CONVERT_SUB_KEY)
_download_quota: dict[str, dict] = {}     # ip -> {"date": "YYYY-MM-DD", "count": int}
_download_quota_lock = threading.Lock()
# ---- 云盘集成（增值能力）：把已下载文件存到用户自己的网盘（WebDAV / 百度网盘）----
# 默认关闭订阅墙（全免费无限）；开启后免费用户按 IP 每日限次（VDL_CLOUD_FREE_DAILY）。
# 与转换/下载共用同一把订阅主密钥 VDL_CONVERT_SUB_KEY（一个订阅解锁全部增值能力）。
CLOUD_REQUIRE_SUB = os.environ.get("VDL_CLOUD_REQUIRE_SUB", "false").strip().lower() == "true"
CLOUD_FREE_DAILY = int(os.environ.get("VDL_CLOUD_FREE_DAILY", "5") or 5)
CLOUD_SUB_ENABLED = CLOUD_REQUIRE_SUB and bool(CONVERT_SUB_KEY)
_cloud_quota: dict[str, dict] = {}        # ip -> {"date": "YYYY-MM-DD", "count": int}
_cloud_quota_lock = threading.Lock()
# 百度网盘 OAuth：需部署者自备开放平台应用（个人网盘读写的授权）
BAIDU_APP_KEY = os.environ.get("VDL_BAIDU_APP_KEY", "").strip()
BAIDU_APP_SECRET = os.environ.get("VDL_BAIDU_APP_SECRET", "").strip()
BAIDU_REDIRECT_URI = os.environ.get("VDL_BAIDU_REDIRECT_URI", "").strip()
BAIDU_ENABLED = bool(BAIDU_APP_KEY and BAIDU_APP_SECRET and BAIDU_REDIRECT_URI)
# 百度 OAuth state：防止授权码流程被 CSRF 诱导。state 由服务端签发并短期缓存，
# 回调时比对；过期/缺失/不匹配一律拒绝。注意：单实例进程内缓存；多副本部署需换成共享存储。
_BAIDU_STATES: dict[str, float] = {}
_BAIDU_STATES_LOCK = threading.Lock()
_BAIDU_STATE_TTL = 600
_webdav_provider = WebDAVProvider()
_baidu_provider = BaiduProvider()
CLOUD_JOBS: dict[str, dict] = {}
CLOUD_LOCK = threading.Lock()

# ---- 格式 / 片段增强（桌面版本地加工）：基于 lib_id 的 ffmpeg 任务 ----
# process_queue 在 executor 创建后初始化（见下方）
_commentary_dir_raw = os.environ.get("VDL_COMMENTARY_DIR", "").strip()
COMMENTARY_DIR = Path(_commentary_dir_raw) if _commentary_dir_raw else None
# 解说 Python 解释器与工具链(ffmpeg/ffprobe)由下方 _CommentaryRuntime 在模块加载时集中探测。
COMMENTARY_VOICE = os.environ.get("VDL_COMMENTARY_VOICE", "zh-CN-XiaoxiaoNeural").strip() or "zh-CN-XiaoxiaoNeural"
COMMENTARY_TIMEOUT_SECONDS = int(os.environ.get("VDL_COMMENTARY_TIMEOUT", "7200") or 7200)  # 长视频 + whisper 大模型首跑 + edge-tts 排队，默认 2 小时
# 解说 worker 调用模式：local=同机 subprocess(默认) / http=独立 HTTP worker 服务(强机独立部署)
COMMENTARY_MODE = os.environ.get("VDL_COMMENTARY_MODE", "local").strip().lower()
COMMENTARY_ENDPOINT = os.environ.get("VDL_COMMENTARY_ENDPOINT", "").strip().rstrip("/")
COMMENTARY_TOKEN = os.environ.get("VDL_COMMENTARY_TOKEN", "").strip()  # 与 worker 的 WORKER_TOKEN 对应
_HERE = Path(__file__).resolve().parent


def _commentary_is_bundled() -> bool:
    """是否走「单二进制双角色」包内捆绑模式（worker 重入自身，依赖随包内置）。"""
    return os.environ.get("VDL_COMMENTARY_BUNDLED") == "1" or COMMENTARY_MODE == "bundled"


def _commentary_root(sub: str) -> Path:
    """解说管线工作目录(input/output/work)；bundled 模式重定向到可写目录(COMMENTARY_WORK_ROOT)。"""
    wr = os.environ.get("COMMENTARY_WORK_ROOT") or ""
    root = Path(wr) if wr else (COMMENTARY_DIR or Path("."))
    return root / sub
_COMMENTARY_OUT_RAW = os.environ.get("VDL_COMMENTARY_LOCAL_OUTPUT", "").strip()
def _user_data_dir() -> Path:
    """跨平台用户数据目录：Windows 打包后用 %APPDATA%/VideoDownloader（符合 Windows 规范，
    避免凭据被同机其他用户读取）；macOS/Linux 维持 ~/.video-downloader 以兼容现有用户。"""
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


if _COMMENTARY_OUT_RAW:
    COMMENTARY_LOCAL_OUTPUT = Path(_COMMENTARY_OUT_RAW)
elif getattr(sys, "frozen", False):
    # 打包后 _HERE 位于 .app 包内部，若沿用会把上传的大视频写进 /Applications/*.app，
    # 导致替换 app 即丢数据、包体积暴涨、破坏代码签名。一律落到用户目录。
    COMMENTARY_LOCAL_OUTPUT = _user_data_dir() / "commentary_out"
else:
    COMMENTARY_LOCAL_OUTPUT = _HERE.parent / "commentary_out"
COMMENTARY_LOCAL_OUTPUT.mkdir(parents=True, exist_ok=True)
COMMENTARY_WORK_DIR = COMMENTARY_LOCAL_OUTPUT / "work"
COMMENTARY_WORK_DIR.mkdir(parents=True, exist_ok=True)
commentary_jobs: dict[str, dict] = {}
_commentary_lock = threading.Lock()



# ---- AI 去水印（E2FGVI worker，桌面版可选）：local subprocess 或 http worker ----
AI_DEWATERMARK_ENABLED = bool(
    getattr(sys, "frozen", False)
    or bool(os.environ.get("VDL_AI_DEWATERMARK_ENABLED"))
)
AI_DEWATERMARK_MODE = os.environ.get("VDL_AI_DEWATERMARK_MODE", "local").strip().lower()
AI_DEWATERMARK_DIR_RAW = os.environ.get("VDL_AI_DEWATERMARK_DIR", "").strip()
AI_DEWATERMARK_DIR = Path(AI_DEWATERMARK_DIR_RAW) if AI_DEWATERMARK_DIR_RAW else None
AI_DEWATERMARK_PYTHON = os.environ.get("VDL_AI_DEWATERMARK_PYTHON", sys.executable)
AI_DEWATERMARK_ENDPOINT = os.environ.get("VDL_AI_DEWATERMARK_ENDPOINT", "http://127.0.0.1:8101")
AI_DEWATERMARK_TOKEN = os.environ.get("VDL_AI_DEWATERMARK_TOKEN", "").strip()
AI_DEWATERMARK_TIMEOUT = int(os.environ.get("VDL_AI_DEWATERMARK_TIMEOUT", "3600") or 3600)

# 启动时检测 GPU（用于 /api/nodes 告知前端）
def _detect_gpu() -> bool:
    try:
        import torch  # noqa: F401
        return torch.cuda.is_available() or (
            hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
        )
    except ImportError:
        return False

AI_GPU_AVAILABLE = _detect_gpu() if AI_DEWATERMARK_ENABLED else False

# ---- 公开部署护栏：防止实例被当免费下载器薅爆带宽 ---- #
# 设为 0 表示不限制（自托管、内部使用时可关掉）
RATE_LIMIT_PER_HOUR = int(os.environ.get("VDL_RATE_LIMIT_PER_HOUR", "30") or 30)
RATE_LIMIT_WINDOW = 3600
_rate_log: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    """取真实客户端 IP，避免伪造 X-Forwarded-For 头绕过限流。

    优先级：X-Real-IP（Cloudflare 等反代会覆盖用户自填、最可信）
            > X-Forwarded-For 最右段（平台/反代追加的最近一跳，用户无法伪造最右）
            > 直连地址。
    """
    real = request.headers.get("x-real-ip")
    if real:
        return real.split(",")[0].strip()
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


# ---- SSRF 防护：拒绝指向内网 / 环回 / 链路本地 / 云元数据的链接 ----
# 视频站都是公网域名；攻击者若传入内网地址（如 169.254.169.254 云元数据），
# 服务器会去请求并可能泄露凭据，或被当成跳板。入口强制只允许公网可达地址。
_PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15",
        "::1/128", "fc00::/7", "fe80::/10",
    )
]


def _assert_safe_url(url: str) -> None:
    """SSRF 护栏：解析主机名，拒绝落在私有 / 环回 / 链路本地 / 保留网段的地址。"""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise LinkError("链接缺少主机名", "请检查链接是否完整")
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise LinkError("该主机不在允许范围内", "请粘贴公开可访问的视频链接")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # 解析不到的域名交给 yt-dlp 统一报错，这里不拦（避免误杀偶发 DNS 抖动）
        return
    # DNS 污染 / 代理 / CDN 场景下，getaddrinfo 常同时返回「真实公网 IP」和
    # 「保留 / 链路本地假地址」(如本机 youtube.com 解析出 2001::1 这类 Teredo 保留地址)。
    # 只要存在任一公网可达地址即视为合法公开域名，避免被假地址一票否决（与 /api/download
    # 去掉护栏的考量一致）。仅当【所有】解析地址都落在私网 / 环回 / 链路本地 / 保留 /
    # 组播段时才拒绝——这才是真正的内部地址攻击（如 169.254.169.254 云元数据）。
    has_public = False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            has_public = True
            break
    if not has_public:
        raise LinkError(
            "该链接指向非公开网络，已拒绝",
            "只允许下载公开可访问的视频；内网 / 本地 / 云元数据地址不可用",
        )


def _assert_archive_url(url: str) -> None:
    """归档 WebDAV 地址校验（区别于下载用的 _assert_safe_url）。

    桌面版里用户把文件归到「自己的」NAS/网盘是核心场景，因此放行私网 /
    环回 / 链路本地地址（如 https://192.168.1.100:5006/dav、my-nas.local）；
    只拦截非 http(s) 与缺主机名的明显非法写法。
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise LinkError("只支持 http/https 的 WebDAV 地址", "归档目标必须是标准 WebDAV 服务地址")
    if not (parsed.hostname or "").strip():
        raise LinkError("链接缺少主机名", "请检查地址是否完整")


def _check_rate_limit(request: Request) -> None:
    """滑动窗口限流。超限抛 429，并告知还要等多久。"""
    if RATE_LIMIT_PER_HOUR <= 0:
        return
    ip = _client_ip(request)
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_log.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
        if len(hits) >= RATE_LIMIT_PER_HOUR:
            wait = int((RATE_LIMIT_WINDOW - (now - hits[0])) / 60) + 1
            _rate_log[ip] = hits
            raise HTTPException(
                status_code=429,
                detail=f"下载太频繁了，本实例每小时限 {RATE_LIMIT_PER_HOUR} 次，请 {wait} 分钟后再试；"
                       "需要更高额度可以自己部署一份（见项目 README）",
            )
        hits.append(now)
        _rate_log[ip] = hits
        if len(_rate_log) > 10000:        # 兜底清理，避免长期运行内存堆积
            for key in [k for k, v in _rate_log.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
                _rate_log.pop(key, None)


def _today_str() -> str:
    """当前服务器本地日期 YYYY-MM-DD，用于按自然日重置免费额度。"""
    return time.strftime("%Y-%m-%d", time.localtime())


def _subscription_quota(request, *, enabled, sub_key, free_daily, quota_store, quota_lock, label):
    """通用订阅 / 限次校验（格式转换、下载等增值 / 受限能力共用此一处）。

    返回 (subscribed, free_used, free_daily)：
      - 未启用订阅墙（enabled=False）：subscribed=False，免费额度字段为 0（全免费无限）。
      - 启用墙且携带正确订阅密钥：subscribed=True，不受免费额度限制。
      - 启用墙但免费用户：按 IP 当日计数，超出抛 402（前端据此引导订阅）。
    label 仅用于 402 文案（如「转换」「下载」）。
    """
    if not enabled:
        return (False, 0, 0)
    key = (request.headers.get("x-subscription-key") or "").strip()
    if sub_key and key == sub_key:
        return (True, 0, free_daily)
    ip = _client_ip(request)
    today = _today_str()
    with quota_lock:
        rec = quota_store.get(ip)
        if not rec or rec["date"] != today:
            rec = {"date": today, "count": 0}
            quota_store[ip] = rec
        if rec["count"] >= free_daily:
            raise HTTPException(
                status_code=402,
                detail=f"今日免费{label}次数已用完，订阅可解锁无限{label}",
            )
        rec["count"] += 1
        return (False, rec["count"], free_daily)


def _check_convert_quota(request: Request) -> tuple[bool, int, int]:
    """格式转换订阅 / 限次校验（复用通用 _subscription_quota）。"""
    return _subscription_quota(
        request, enabled=CONVERT_SUB_ENABLED, sub_key=CONVERT_SUB_KEY,
        free_daily=CONVERT_FREE_DAILY, quota_store=_convert_quota,
        quota_lock=_convert_quota_lock, label="转换",
    )


def _check_download_quota(request: Request) -> tuple[bool, int, int]:
    """下载订阅 / 限次校验（freemium：免费每日限次，订阅无限）。"""
    return _subscription_quota(
        request, enabled=DOWNLOAD_SUB_ENABLED, sub_key=CONVERT_SUB_KEY,
        free_daily=DOWNLOAD_FREE_DAILY, quota_store=_download_quota,
        quota_lock=_download_quota_lock, label="下载",
    )


def _check_cloud_quota(request: Request) -> tuple[bool, int, int]:
    """云盘存盘订阅 / 限次校验（freemium：免费每日限次，订阅无限）。"""
    return _subscription_quota(
        request, enabled=CLOUD_SUB_ENABLED, sub_key=CONVERT_SUB_KEY,
        free_daily=CLOUD_FREE_DAILY, quota_store=_cloud_quota,
        quota_lock=_cloud_quota_lock, label="存网盘",
    )


def _host_of(url: str) -> str:
    """从链接取出主机名（去掉 www./m. 前缀），解析失败返回空串。"""
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.removeprefix("www.").removeprefix("m.")
    except ValueError:
        return ""

store = TaskStore(DOWNLOAD_DIR)
# 线程池只设硬上限；真正的「同时下几个」由 BatchScheduler 的并发计数器软控（可动态调整）
executor = ThreadPoolExecutor(max_workers=VDL_BATCH_HARD_MAX, thread_name_prefix="vdl-dl")
scheduler = BatchScheduler(executor, default_concurrency=MAX_CONCURRENT_DOWNLOADS)
process_queue = pq_mod.ProcessQueue(executor, default_concurrency=2, hard_max=4)

# ---- 订阅监控（桌面版功能）：本地 JSON 持久化 + 后台定时探查新视频 ----
SUB_ENABLED = bool(getattr(sys, "frozen", False)) or bool(os.environ.get("VDL_SUBSCRIPTIONS_ENABLED"))
SUBSCRIBE_PROBE_LIMIT = int(os.environ.get("VDL_SUBSCRIBE_PROBE_LIMIT", "100") or 100)
SUB_CHECK_INTERVAL = int(os.environ.get("VDL_SUB_CHECK_INTERVAL", "1800") or 1800)  # 默认 30 分钟
sub_store = subs_mod.SubscriptionStore(DOWNLOAD_DIR / ".subscriptions.json")
prober = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PROBES, thread_name_prefix="vdl-probe")
cloud_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vdl-cloud")  # 云盘上传独立线程池，避免挤占下载

# ---- 时效自动清理（桌面版功能）：按保留期/容量上限清理下载目录 ----
# 与媒体库同一开关：只有桌面版（或显式开 VDL_LIBRARY_ENABLED）才管理本地磁盘。
RETENTION_ENABLED = (
    bool(getattr(sys, "frozen", False))
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_RETENTION_ENABLED"))
)
retention_store = retention_mod.RetentionStore(DOWNLOAD_DIR / ".retention.json")

# ---- 一键归档网盘（桌面版功能）：把媒体库文件按模板批量/自动传到用户自己的网盘 ----
# 配置含明文凭据，刻意放在 home 配置目录而不是下载目录 —— 避免用户把整个下载目录
# 同步/打包到网盘时连带泄露密码。
ARCHIVE_ENABLED = (
    bool(getattr(sys, "frozen", False))
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_ARCHIVE_ENABLED"))
)
ARCHIVE_CONFIG_PATH = Path(
    os.environ.get("VDL_ARCHIVE_CONFIG") or (Path.home() / ".video-downloader" / "archive.json")
)
archive_store = archive_mod.ArchiveStore(ARCHIVE_CONFIG_PATH)
ARCHIVE_JOBS: dict[str, dict] = {}
ARCHIVE_LOCK = threading.Lock()

# ---- 库内保险箱（桌面版功能）：选中文件就地 AES 加密为 .vdlenc，播放前临时解密 ----
# 与媒体库同一开关。内存密钥 VAULT_KEY 为 None 即「锁定」态；vault.json 只存 salt+verify，
# 绝不存明文密码或密钥（见 crypto_vault.new_vault / unlock_key）。
CRYPTO_ENABLED = (
    bool(getattr(sys, "frozen", False))
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_CRYPTO_ENABLED"))
)
VAULT_PATH = Path(
    os.environ.get("VDL_VAULT_CONFIG") or (Path.home() / ".video-downloader" / "vault.json")
)
VAULT_LOCK = threading.Lock()
VAULT_KEY: bytes | None = None  # 解锁后驻留内存；锁定/重启即清空
CRYPTO_JOBS: dict[str, dict] = {}
CRYPTO_LOCK = threading.Lock()
# 解密播放临时目录（与下载目录同盘，避免跨卷复制大文件）
VAULT_TMP = DOWNLOAD_DIR / ".vault_tmp"
VAULT_TMP.mkdir(parents=True, exist_ok=True)
CRYPTO_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vdl-crypto")

# ---- 桌面版种子下载（libtorrent 集成）：把 magnet/.torrent 下载到本地媒体库 ----
# 与媒体库同一开关策略（桌面版/显式开启）；libtorrent 为可选依赖，未安装时功能整体禁用。
TORRENT_ENABLED = (
    bool(getattr(sys, "frozen", False))
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_TORRENT_ENABLED"))
)
torrent_manager = torrent_mod.get_manager(DOWNLOAD_DIR)


def _vault_load() -> dict | None:
    try:
        if VAULT_PATH.exists():
            return json.loads(VAULT_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _chmod_600(path: Path) -> None:
    """跨平台收紧文件权限到仅当前用户可读写。Windows 的 os.chmod 只切只读位、不控 ACL，
    故改用 icacls 移除继承并仅授权当前用户（无 pywin32 也可工作）。"""
    if sys.platform == "win32":
        try:
            import subprocess as _sp
            _sp.run(["icacls", str(path), "/inheritance:r",
                     "/grant:r", f"{os.environ.get('USERNAME', '')}:(R,W)"],
                    check=False, capture_output=True)
        except Exception:
            pass
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _vault_save(vault: dict) -> None:
    VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = VAULT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(vault, ensure_ascii=False), encoding="utf-8")
    _chmod_600(tmp)
    tmp.replace(VAULT_PATH)
    _chmod_600(VAULT_PATH)


def _vault_tmp_for(lib_id: str) -> Path:
    VAULT_TMP.mkdir(parents=True, exist_ok=True)
    return VAULT_TMP / (lib_id + ".dec")


def _prune_vault_tmp(max_age_seconds: int = 1800) -> None:
    """清理解密播放的临时文件（超过 30 分钟），避免明文长期留盘。"""
    try:
        now = time.time()
        for f in VAULT_TMP.iterdir():
            if f.is_file() and now - f.stat().st_mtime > max_age_seconds:
                try:
                    f.unlink()
                except OSError:
                    pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 自动解说（松耦合桥接 commentary-pipeline/process.py，不重写解说逻辑）
# --------------------------------------------------------------------------- #


class _CommentaryRuntime:
    """模块加载时一次性探测解说运行环境，集中管理解释器与工具链。

    打包后 sys.executable 是 .app 主程序、子进程 PATH 可能被截断，
    这里把所有不确定性收敛到一处，并暴露清晰的诊断信息（issues/ready）。
    """

    def __init__(self) -> None:
        self.python = self._resolve_python()
        self.python_ok = self._check_python(self.python)
        self.deps_ok = self._check_deps(self.python) if self.python_ok else False
        self.ffmpeg_dir, self.ffprobe_ok = self._resolve_ffmpeg()
        self.issues = self._collect_issues()

    # ---- 解释器 ----
    def _resolve_python(self) -> str:
        """解析跑 process.py 的解释器，统一委托 commentary_locate。"""
        from commentary_locate import locate_commentary
        loc = locate_commentary()
        if loc is None:
            return sys.executable
        return loc.python

    def _check_python(self, py: str) -> bool:
        # 打包后 sys.executable 通常是 .app 主程序，不应作为解释器——
        # 但「单二进制双角色」bundled 模式下，worker 正是用自身(sys.executable)重入，
        # 依赖已随包内置，故该例外下允许 sys.executable 作为解释器。
        if getattr(sys, "frozen", False) and py == sys.executable:
            if os.environ.get("VDL_COMMENTARY_BUNDLED") == "1" or COMMENTARY_MODE == "bundled":
                return True
            return False
        # 开发/测试模式（非 frozen 且未显式设 VDL_COMMENTARY_PYTHON）信任当前解释器
        if "VDL_COMMENTARY_PYTHON" not in os.environ and not getattr(sys, "frozen", False):
            return True
        try:
            r = subprocess.run(
                [py, "-c", "import sys; print(sys.executable)"],
                capture_output=True, text=True, timeout=5,
            )
            return r.returncode == 0 and "python" in (r.stdout or "").lower()
        except Exception:
            return False

    def _check_deps(self, py: str) -> bool:
        if not self.python_ok:
            return False
        try:
            r = subprocess.run(
                [py, "-c", "import faster_whisper, edge_tts"],
                capture_output=True, text=True, timeout=30,
            )
            return r.returncode == 0
        except Exception:
            return False

    # ---- 工具链 ffmpeg / ffprobe ----
    def _resolve_ffmpeg(self) -> tuple[str, bool]:
        """解析 ffmpeg/ffprobe 所在目录；打包后 PATH 可能被截断，这里显式收集所有可能目录。

        策略：
        1. 优先 VDL_FFPROBE_BIN / VDL_FFMPEG_BIN 的目录；
        2. 如果该目录没有 ffprobe，再补充系统常见目录；
        3. 返回所有可用目录（用 ; 拼接），并验证 ffprobe 确实能在合并后的 PATH 里找到。
        """
        dirs: list[str] = []
        seen: set[str] = set()

        def add(d: str | Path) -> None:
            s = str(d)
            if s and s not in seen:
                seen.add(s)
                dirs.append(s)

        # 1) 用户/启动器显式指定的 bin
        for key in ("VDL_FFPROBE_BIN", "VDL_FFMPEG_BIN"):
            v = os.environ.get(key, "").strip()
            if v:
                add(Path(v).parent)

        # 2) 当前进程 PATH 里能直接找到的工具
        for name in ("ffprobe", "ffmpeg"):
            p = shutil.which(name)
            if p:
                add(Path(p).parent)

        # 3) 系统常见目录兜底（按平台补充）
        if sys.platform == "win32":
            add(r"C:\ffmpeg\bin")
            add(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"))
        for fb in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):
            add(fb)

        if not dirs:
            return "", False

        # 4) 验证：把这些目录前置到 PATH 后，ffprobe 是否真的能被发现
        probe_path = os.pathsep.join(dirs + ([os.environ.get("PATH", "")] if os.environ.get("PATH") else []))
        ok = bool(shutil.which("ffprobe", path=probe_path))
        return os.pathsep.join(dirs), ok

    def _collect_issues(self) -> list[str]:
        issues: list[str] = []
        if not self.python_ok:
            issues.append("解说 Python 解释器不可用（请设置 VDL_COMMENTARY_PYTHON 指向装了 faster_whisper/edge_tts 的解释器）")
        elif not self.deps_ok:
            issues.append("解说 Python 缺少依赖（faster_whisper / edge_tts 未安装）")
        if not self.ffprobe_ok:
            issues.append("找不到 ffprobe（请安装 ffmpeg 或设置 VDL_FFPROBE_BIN）")
        return issues

    def env(self) -> dict[str, str]:
        """为 process.py 子进程准备环境变量，关键是把 ffmpeg/ffprobe 目录前置到 PATH，
        并强制 PYTHONIOENCODING=utf-8（Windows 默认 cp936 会让 emoji 日志乱码/抛错）。
        同时注入统一 LLM 配置——若用户已在前端配置了 Key/提供商/模型。
        另外显式设置 COMMENTARY_BASE，确保打包后 config.py 里的 BASE 一定指向管线根目录
        （避免 PyInstaller 的 __file__ 解析差异导致找不到 assets/fonts 下捆绑的中文字体）。"""
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if COMMENTARY_DIR:
            env["COMMENTARY_BASE"] = str(COMMENTARY_DIR)
        if self.ffmpeg_dir:
            env["PATH"] = self.ffmpeg_dir + os.pathsep + env.get("PATH", "")
        # 统一 LLM 配置：仅 Key 非空时注入，无 Key 不污染子进程环境
        inject_llm_env(env)
        return env

    def ready(self) -> bool:
        return not self.issues


COMMENTARY_RT = _CommentaryRuntime()


def _commentary_work_dir() -> Path:
    d = COMMENTARY_WORK_DIR / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d





# 解说任务过程展示：阶段名 + 匹配日志关键词（不区分大小写）
_COMMENTARY_STEP_KEYWORDS = [
    ("准备输入文件", ["准备", "复制", "软链", "输入文件", "symlink", "copy file", "save upload"]),
    ("转写视频语音", ["[1/2] 转写", "whisper", "transcribe", "提取音轨", "转写完成", "transcript"]),
    ("生成 AI 解说词", ["[2/2] 自动解说词", "auto_script", "解说词", "script.json", "生成脚本", "script ready"]),
    ("合成 AI 配音", ["配音", "edge_tts", "tts", "语音合成", "synthesize", "narration"]),
    ("渲染成片", ["剪辑成片", "edit_ffmpeg", "edit.py", "ffmpeg", "渲染", "合并", "concat", "build", "成片"]),
    ("完成", ["全部完成", "成片在", "completed", "done"]),
]


def _ensure_commentary_steps(job: dict) -> list[dict]:
    """初始化或返回解说任务的步骤时间线。"""
    if "steps" not in job or not job["steps"]:
        now = time.time()
        job["steps"] = [
            {"name": name, "status": "pending", "detail": "", "created_at": now, "updated_at": now}
            for name, _ in _COMMENTARY_STEP_KEYWORDS
        ]
    return job["steps"]


def _commentary_log(job: dict, line: str) -> None:
    """给解说任务追加一行带时间戳的运行日志。"""
    if not line:
        return
    ts = time.strftime("%H:%M:%S", time.localtime())
    job.setdefault("logs", []).append(f"{ts}  {line.strip()}")
    if len(job.get("logs", [])) > 200:
        job["logs"][:] = job["logs"][-200:]


def _update_commentary_steps(job: dict, line: str) -> None:
    """根据 process.py 输出关键词自动推进步骤状态。"""
    steps = _ensure_commentary_steps(job)
    line_lower = line.lower()
    matched = -1
    for idx, (name, keywords) in enumerate(_COMMENTARY_STEP_KEYWORDS):
        if any(k.lower() in line_lower for k in keywords):
            matched = idx
            break
    if matched < 0:
        return
    now = time.time()
    for s in steps[:matched]:
        if s["status"] == "pending":
            s["status"] = "done"
            s["updated_at"] = now
    cur = steps[matched]
    cur["status"] = "running"
    cur["detail"] = line[:200]
    cur["updated_at"] = now


def _commentary_mark_error(job_id: str, detail: str) -> None:
    """把当前 running 的步骤标为 error。"""
    with _commentary_lock:
        job = commentary_jobs.get(job_id)
        if not job:
            return
        steps = _ensure_commentary_steps(job)
        now = time.time()
        for s in steps:
            if s["status"] == "running":
                s["status"] = "error"
                s["detail"] = detail[:200]
                s["updated_at"] = now
                break


def _apply_trim(src_path: str, in_dir: Path, start: float, end: float):
    """服务端预处理裁剪：同一源+起止始终产出同名文件，便于 script-only 与后续 render 复用。
    返回 (裁剪后路径, 裁剪后时长)；裁剪失败则回退原路径。"""
    key = hashlib.md5(f"{src_path}|{start:.3f}|{end:.3f}".encode("utf-8")).hexdigest()[:12]
    trim_out = in_dir / f"trim_{key}.mp4"
    if trim_out.exists() and trim_out.stat().st_size > 0:
        return str(trim_out), (end - start)
    dur = max(0.1, end - start)
    try:
        subprocess.run(
            [FFMPEG_BIN, "-y", "-ss", f"{start:.3f}", "-i", str(src_path),
             "-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast",
             "-c:a", "aac", "-movflags", "+faststart", str(trim_out)],
            capture_output=True, text=True, timeout=1800,
        )
    except Exception as exc:
        logger.warning("视频裁剪失败，回退使用原视频：%s", exc)
        return src_path, 0.0
    if not (trim_out.exists() and trim_out.stat().st_size > 0):
        return src_path, 0.0
    return str(trim_out), dur


def _commentary_eta(job: dict, line: str, src_dur: float) -> None:
    """从子进程进度行推算 ETA，写入 job['eta_remaining']（剩余秒）/ eta_done_at（绝对时间戳）。"""
    now = time.time()
    m = re.search(r"共\s*(\d+)\s*段", line)
    if m:
        job["eta_total_segs"] = int(m.group(1))
    m = re.search(r"开始渲染\s*(\d+)\s*段", line)
    if m:
        job["eta_render_total"] = int(m.group(1))
        job["eta_render_start"] = now
    m = re.search(r"\[(\d+)/(\d+)\]", line)
    if m:
        done = int(m.group(1))
        job["eta_render_done"] = done
        rs = job.get("eta_render_start")
        if rs:
            elapsed = now - rs
            if done > 0:
                per = elapsed / done
                total = job.get("eta_render_total") or done
                # 段是并发渲染的（min(cpu,8)），按并行度折算墙钟时间
                par = max(1, min(os.cpu_count() or 4, 8))
                remaining = per * max(0, total - done) / par
                job["eta_remaining"] = int(remaining)
                job["eta_done_at"] = int(now + remaining)
            return
    # 渲染前（转写/TTS）：用源时长做粗估，进入渲染段后由上面的实时进度收敛
    if job.get("eta_render_start") is None and src_dur:
        elapsed = now - job.get("started_at", now)
        coarse = src_dur * 1.2
        rem = max(0, int(coarse - elapsed))
        job["eta_remaining"] = rem
        job["eta_done_at"] = int(now + rem)


def _commentary_option_args(*, commentary_type: str = "deep_hl", highlight_source: str = "ai",
                             intro_highlight: bool = False, skip_intro_outro: bool = False,
                             no_narrate_intro_outro: bool = True, retain_pct: float | None = None,
                             web: bool = False, one_click: bool = False, mode: str | None = None,
                             style: str = "none") -> list:
    """把剪辑选项翻译成 process.py 的命令行参数（local / bundled 模式共用）。"""
    args = ["--commentary-type", commentary_type, "--highlight-source", highlight_source]
    if style and style != "none":
        args += ["--style", style]
    if intro_highlight:
        args.append("--intro-highlight")
    if skip_intro_outro:
        args.append("--skip-intro-outro")
    if not no_narrate_intro_outro:
        # 默认就是「保留片头片尾·不解说」，仅当用户显式要全片解说时才带 --narrate-all
        args.append("--narrate-all")
    if retain_pct is not None:
        args += ["--retain-pct", str(retain_pct)]
    if web:
        args.append("--web")
    if one_click:
        args.append("--one-click")
    if mode:  # 旧版兼容字段，仅当显式传了才带
        args += ["--mode", mode]
    return args


def _commentary_run(job_id: str, src_path: str, vertical: bool, voice: str, edit_only: str | None = None, script_only: bool = False, trim_start: float = 0.0, trim_end: float = 0.0, mode: str | None = None, commentary_type: str = "deep_hl", highlight_source: str = "ai", intro_highlight: bool = False, skip_intro_outro: bool = False, no_narrate_intro_outro: bool = True, retain_pct: float | None = None, web: bool = False, one_click: bool = False, title: str = "", style: str = "none") -> None:
    """后台线程：把下载好的视频喂给 commentary-pipeline，等成片回传。

    复用用户现成的 process.py 整条管线（whisper 转写 → edge-tts 配音 → ffmpeg 出片），
    本函数只负责文件桥接与成片定位。算力由解说 worker 独立承担，不影响下载服务。
    HTTP 模式(VDL_COMMENTARY_MODE=http)下转发给独立 worker 服务。
    """
    if COMMENTARY_MODE == "http":
        return _commentary_run_http(job_id, src_path, vertical, voice, mode,
                                    commentary_type=commentary_type,
                                    highlight_source=highlight_source,
                                    intro_highlight=intro_highlight,
                                    skip_intro_outro=skip_intro_outro,
                                    no_narrate_intro_outro=no_narrate_intro_outro,
                                    retain_pct=retain_pct, web=web,
                                    one_click=one_click, style=style)
    try:
        # 调用前先确认运行环境就绪，失败直接给清晰错误，避免盲目 subprocess 后误报「执行成功」
        if not COMMENTARY_RT.ready():
            raise RuntimeError("解说环境未就绪：" + "；".join(COMMENTARY_RT.issues))
        base = job_id  # 用 job_id 作安全 ascii 文件名，避开中文/空格对 process.py 路径处理的干扰
        # 解说锚点：优先用调用方传入的标题；否则从源文件名推断（下载任务文件名通常含剧集名）。
        # 目的是给 LLM 一个「当前剧集」的锚点，防止解说词凭记忆跑题到别的剧集。
        if not title:
            try:
                title = Path(src_path).stem or ""
            except Exception:
                title = ""
        in_dir = _commentary_root("input")
        out_dir = _commentary_root("output")
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        # ---- 时长裁剪（服务端预处理）----
        # 用确定性命名（源路径+起止哈希）切出裁剪片段，script-only 与后续 render 复用同一文件，避免重复切。
        try:
            src_dur = fftools.probe_duration(Path(src_path), ffmpeg_bin=FFMPEG_BIN)
        except Exception:
            src_dur = 0.0
        ts, te = float(trim_start or 0), float(trim_end or 0)
        use_src = src_path
        if te > ts > 0 and (not src_dur or te <= src_dur + 1.0):
            use_src, _ = _apply_trim(src_path, in_dir, ts, te)

        in_file = in_dir / f"{base}.mp4"
        if in_file.exists() or in_file.is_symlink():
            in_file.unlink()
        try:
            os.symlink(use_src, in_file)
        except OSError:
            shutil.copyfile(use_src, in_file)  # 跨挂载点软链失败则退化为复制

        # 子进程参数：edit_only 真 → 走 --edit-only（吃已审核脚本，不重跑转录）；否则 --auto。
        # 加 -u 关闭 stdout 块缓冲，让进度行实时回流（ETA 才平滑）。
        # bundled 模式：用主程序自身(sys.executable)以 --vdl-commentary-worker 重入为 worker，
        # 依赖随包内置(已砍 torch)，无需外部 Python / pip，契合自包含铁律。
        _bundled = _commentary_is_bundled()
        extra = _commentary_option_args(commentary_type=commentary_type,
                                        highlight_source=highlight_source,
                                        intro_highlight=intro_highlight,
                                        skip_intro_outro=skip_intro_outro,
                                        no_narrate_intro_outro=no_narrate_intro_outro,
                                        retain_pct=retain_pct, web=web,
                                        one_click=one_click, mode=mode,
                                        style=style)
        if edit_only:
            if _bundled:
                args = [sys.executable, "--vdl-commentary-worker",
                        str(in_file), "--edit-only", edit_only]
            else:
                args = [COMMENTARY_RT.python, "-u", "process.py", str(in_file), "--edit-only", edit_only]
            if vertical:
                args.append("--vertical")
            if voice:
                args += ["--voice", voice]
            # 审核后出片：把用户在面板上改过的剪辑选项一并传下去（覆盖脚本自带选项）
            args += extra
        else:
            if _bundled:
                args = [sys.executable, "--vdl-commentary-worker", str(in_file), "--auto"]
            else:
                args = [COMMENTARY_RT.python, "-u", "process.py", str(in_file), "--auto"]
            if vertical:
                args.append("--vertical")
            if voice:
                args += ["--voice", voice]
            if title:
                args += ["--title", title]
            if script_only:
                args.append("--script-only")
            args += extra

        # Popen 实时读取 stdout/stderr，按行追加到 commentary_jobs[job_id]['progress']，
        # 前端轮询时把进度条回显给用户，避免「30 分钟黑屏焦虑」。
        last_lines: list[str] = []

        def _append(line: str) -> None:
            line = line.rstrip("\n")
            if not line:
                return
            last_lines.append(line)
            if len(last_lines) > 80:  # 只保留最近 80 行，避免内存膨胀
                del last_lines[: len(last_lines) - 80]
            with _commentary_lock:
                job = commentary_jobs.setdefault(job_id, {})
                job["progress"] = list(last_lines)
                _commentary_log(job, line)
                _update_commentary_steps(job, line)
                _commentary_eta(job, line, src_dur)

        with _commentary_lock:
            job = commentary_jobs.setdefault(job_id, {})
            job["started_at"] = job.get("started_at") or time.time()
            job["source_duration"] = src_dur
            job["trim_start"] = ts
            job["trim_end"] = te
            job["trim_path"] = use_src if use_src != src_path else ""
            job["src_path"] = src_path
            steps = _ensure_commentary_steps(job)
            steps[0]["status"] = "running"
            steps[0]["detail"] = f"源视频: {Path(src_path).name}" + (
                f"（已裁剪 {ts:.0f}~{te:.0f}s）" if use_src != src_path else "")
            steps[0]["updated_at"] = time.time()

        try:
            proc = subprocess.Popen(
                args, cwd=str(COMMENTARY_DIR),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                env=COMMENTARY_RT.env(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"解说管线无法启动：{exc}") from exc

        assert proc.stdout is not None
        for line in proc.stdout:
            _append(line)
        try:
            ret = proc.wait(timeout=COMMENTARY_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            tail = "\n".join(last_lines[-20:]) or "无输出"
            raise RuntimeError(
                f"解说管线超过 {COMMENTARY_TIMEOUT_SECONDS} 秒未完成，已终止。\n最近输出：\n{tail}"
            )
        if ret != 0:
            tail = "\n".join(last_lines[-20:]) or "无输出"
            raise RuntimeError(f"解说管线退出码 {ret}。\n最近输出：\n{tail}")

        if script_only:
            # --script-only 模式：只出了 script.json，不生成成片
            script_path = _commentary_root("work") / f"{base}.script.json"
            if not script_path.exists():
                tail = "\n".join(last_lines[-20:]) or "无输出"
                raise RuntimeError(f"解说管线执行成功但未找到脚本文件：{script_path}\n输出：\n{tail}")
            with _commentary_lock:
                job = commentary_jobs[job_id]
                _ensure_commentary_steps(job)
                now = time.time()
                # --script-only 只跑到「生成 AI 解说词」为止：配音/渲染尚未执行，
                # 绝不能标 done（否则 UI 全绿、用户误以为成片已生成）。
                _DONE_IN_SCRIPT_ONLY = ("准备输入文件", "转写视频语音", "生成 AI 解说词")
                for s in job["steps"]:
                    if s["name"] in _DONE_IN_SCRIPT_ONLY:
                        s["status"] = "done"
                        s["updated_at"] = now
                    elif s["name"] in ("合成 AI 配音", "渲染成片"):
                        s["status"] = "pending"
                        s["detail"] = "等待脚本审核通过后执行"
                        s["updated_at"] = now
                    elif s["name"] == "完成":
                        s["status"] = "pending"
                        s["detail"] = "解说词已生成，请先审核脚本再渲染成片"
                        s["updated_at"] = now
                _commentary_log(job, "解说词已生成，等待人工审核后再渲染成片")
                job.update(status="script_ready", script_path=str(script_path), output_path="")
        else:
            # 成片命名：<base>_成片.mp4 或 <base>_竖屏成片.mp4
            candidates = sorted(
                (p for p in out_dir.glob(f"{base}*.mp4") if p.name != in_file.name),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            out = next(iter(candidates), None)
            if not out:
                tail = "\n".join(last_lines[-20:]) or "无输出"
                raise RuntimeError(f"解说管线执行成功但未找到成片。process.py 输出：\n{tail}")
            with _commentary_lock:
                job = commentary_jobs[job_id]
                _ensure_commentary_steps(job)
                now = time.time()
                for s in job["steps"]:
                    if s["status"] in ("pending", "running"):
                        s["status"] = "done"
                        s["updated_at"] = now
                job.update(status="completed", output_path=str(out))
    except Exception as exc:  # noqa: BLE001
        _commentary_mark_error(job_id, str(exc))
        with _commentary_lock:
            commentary_jobs.setdefault(job_id, {})["status"] = "failed"
            commentary_jobs[job_id]["error"] = str(exc)[:800]
        logger.exception("解说任务 %s 失败", job_id)


def _commentary_run_http(job_id: str, src_path: str, vertical: bool, voice: str, mode: str | None = None, commentary_type: str = "deep_hl", highlight_source: str = "ai", intro_highlight: bool = False, skip_intro_outro: bool = False, no_narrate_intro_outro: bool = True, retain_pct: float | None = None, web: bool = False, one_click: bool = False, style: str = "none") -> None:
    """HTTP 模式：把已下载视频 POST 给独立解说 worker，轮询取回成片到主站本地。"""
    endpoint = COMMENTARY_ENDPOINT
    headers = {"X-Worker-Token": COMMENTARY_TOKEN} if COMMENTARY_TOKEN else {}
    now = time.time()
    http_steps = [
        {"name": "上传视频到 worker", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
        {"name": "worker 渲染中", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
        {"name": "下载成片", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
        {"name": "完成", "status": "pending", "detail": "", "created_at": now, "updated_at": now},
    ]
    with _commentary_lock:
        job = commentary_jobs.setdefault(job_id, {})
        job["steps"] = http_steps
        job["logs"] = []

    def _set_http_step(idx: int, status: str, detail: str = "") -> None:
        with _commentary_lock:
            steps = commentary_jobs.get(job_id, {}).get("steps", [])
            now2 = time.time()
            for i, s in enumerate(steps):
                if i < idx and s["status"] == "pending":
                    s["status"] = "done"
                    s["updated_at"] = now2
            if 0 <= idx < len(steps):
                steps[idx]["status"] = status
                steps[idx]["detail"] = detail[:200]
                steps[idx]["updated_at"] = now2

    def _http_log(line: str) -> None:
        with _commentary_lock:
            job = commentary_jobs.get(job_id)
            if not job:
                return
            ts = time.strftime("%H:%M:%S", time.localtime())
            job.setdefault("logs", []).append(f"{ts}  {line.strip()}")
            if len(job["logs"]) > 200:
                job["logs"][:] = job["logs"][-200:]

    try:
        _set_http_step(0, "running", f"上传 {Path(src_path).name}")
        with open(src_path, "rb") as fh:
            data = {
                "vertical": "true" if vertical else "false",
                "voice": voice,
                "commentary_type": commentary_type,
                "highlight_source": highlight_source,
                "intro_highlight": "true" if intro_highlight else "false",
                "skip_intro_outro": "true" if skip_intro_outro else "false",
                "no_narrate_intro_outro": "true" if no_narrate_intro_outro else "false",
                "web": "true" if web else "false",
                "one_click": "true" if one_click else "false",
                "style": style or "none",
            }
            if retain_pct is not None:
                data["retain_pct"] = str(retain_pct)
            if mode:
                data["mode"] = mode
            resp = requests.post(
                f"{endpoint}/render",
                files={"video": (f"{job_id}.mp4", fh, "video/mp4")},
                data=data,
                headers=headers,
                timeout=600,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"解说 worker /render 返回 {resp.status_code}: {resp.text[:400]}")
        wjob = resp.json().get("job_id")
        if not wjob:
            raise RuntimeError("解说 worker 未返回 job_id")
        _set_http_step(0, "done", "已提交渲染任务")
        _http_log(f"worker job_id: {wjob}")

        _set_http_step(1, "running", f"worker job: {wjob}")
        deadline = time.time() + COMMENTARY_TIMEOUT_SECONDS
        while time.time() < deadline:
            time.sleep(5)
            st = requests.get(f"{endpoint}/status/{wjob}", headers=headers, timeout=30).json()
            status = st.get("status")
            _http_log(f"worker status: {status}")
            if status == "completed":
                break
            if status == "failed":
                raise RuntimeError("解说 worker 渲染失败: " + str(st.get("error", ""))[:600])
        else:
            raise RuntimeError("解说 worker 渲染超时（超过 VDL_COMMENTARY_TIMEOUT）")
        _set_http_step(1, "done", "worker 渲染完成")

        _set_http_step(2, "running", "正在下载成片")
        fr = requests.get(f"{endpoint}/file/{wjob}", headers=headers, stream=True, timeout=(10, 600))
        if fr.status_code != 200:
            raise RuntimeError(f"解说 worker /file 返回 {fr.status_code}")
        out_path = COMMENTARY_LOCAL_OUTPUT / f"{job_id}.mp4"
        downloaded = 0
        with open(out_path, "wb") as o:
            for chunk in fr.iter_content(1024 * 1024):
                if chunk:
                    o.write(chunk)
                    downloaded += len(chunk)
        _set_http_step(2, "done", f"已下载 {downloaded / 1024 / 1024:.1f} MB")

        _set_http_step(3, "done", "成片已就绪")
        with _commentary_lock:
            commentary_jobs[job_id].update(status="completed", output_path=str(out_path))
    except Exception as exc:  # noqa: BLE001
        _set_http_step(1 if http_steps[0]["status"] == "done" else 0, "error", str(exc)[:200])
        with _commentary_lock:
            commentary_jobs.setdefault(job_id, {})["status"] = "failed"
            commentary_jobs[job_id]["error"] = str(exc)[:800]
        logger.exception("解说任务 %s 失败(http 模式)", job_id)


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        removed = store.purge_expired()
        if removed:
            logger.info("已清理 %s 个过期任务", removed)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    orphans = store.purge_orphans()
    if orphans:
        logger.info("已清理 %s 个上次运行遗留的任务目录", orphans)
    # 桌面版种子下载：启动 libtorrent session（libtorrent 缺失时内部为空操作）
    if TORRENT_ENABLED and torrent_mod.available():
        try:
            torrent_manager.start()
        except Exception:
            logger.exception("启动种子下载管理器失败")
    cleaner = asyncio.create_task(_cleanup_loop())
    yield
    cleaner.cancel()
    if TORRENT_ENABLED:
        try:
            torrent_manager.stop()
        except Exception:
            pass
    executor.shutdown(wait=False, cancel_futures=True)
    prober.shutdown(wait=False, cancel_futures=True)
    cloud_executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="视频下载站", version="1.0.0", lifespan=lifespan)

# 跨域 CORS：公开站默认允许所有来源（allow_credentials=False，不携凭证，安全）。
# 部署者可通过 VDL_ALLOW_ORIGINS 或双节点 PEER_ENDPOINT 限定具体来源；否则回退 "*"。
# 注意：中间件必须始终注册，否则跨域自托管（含带 X-Subscription-Key 的订阅请求）
# 的浏览器预检会被 405 拒绝。
_cors_origins = ALLOW_ORIGINS if ALLOW_ORIGINS else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Subscription-Key", "X-Api-Key"],
)


# 前端脚本未做版本化，pywebview/WKWebView 易缓存旧 app.js，导致修复不生效。
# 对 HTML/JS/CSS 及首页强制 no-store，确保客户端每次都拉取最新前端。
@app.middleware("http")
async def _no_cache_frontend(request: Request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".html", ".css", ".htm")):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp
logger.info("CORS 已开启，允许来源：%s", ", ".join(_cors_origins))


# --------------------------------------------------------------------------- #
# 可选 API 鉴权（对外给用户使用时建议开启）
# 设置 VDL_API_TOKEN 后，除 /api/nodes（前端要先拿到 authRequired 才能引导输入 token）
# 与静态资源外，所有 /api/* 请求必须携带正确 token，否则 401。
# token 取自 Authorization: Bearer <t> 或 X-Api-Key: <t>。
# 未设置则维持当前无鉴权行为（个人本机/私用）。
# --------------------------------------------------------------------------- #
API_TOKEN = (os.environ.get("VDL_API_TOKEN") or "").strip()
AUTH_REQUIRED = bool(API_TOKEN)


class _ApiTokenMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not AUTH_REQUIRED:
            return await call_next(request)
        path = request.url.path
        # 静态资源与节点信息接口放行：前端需先读 authRequired 才能引导用户输入 token
        if not path.startswith("/api/") or path == "/api/nodes":
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        else:
            token = (request.headers.get("X-Api-Key") or "").strip()
        if token and token == API_TOKEN:
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content={"error": "缺少或错误的 API Token", "hint": "服务端已启用 VDL_API_TOKEN，请输入访问令牌"},
        )


app.add_middleware(_ApiTokenMiddleware)


# --------------------------------------------------------------------------- #
# 请求模型 & 错误处理
# --------------------------------------------------------------------------- #

class ResolveRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    quality: str = Field(default=downloader.BEST_KEY, max_length=16)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)
    # 提取文案："" 不提取 / "spoken" 口播文案 / "description" 发布简介 / "both" 两者
    extract_script: str = Field(default="", max_length=16)
    # 精确指定 CDN 源（如腾讯 hd-1/shd-3），空则按清晰度自适应；由「测速选源」自动填入
    format_id: str = Field(default="", max_length=64)
    # 下载加速：m3u8 分片并行段数（0=用默认 32，范围 1-64）。腾讯等单连接限速站提高可线性提速
    concurrent_fragments: int = Field(default=0, ge=0, le=64)
    # 下载器：native（默认，yt-dlp 原生）/ aria2c（外部下载器，需本机已装，缺失自动回退）
    downloader: str = Field(default="native", max_length=16)
    # 在线观看：前端从解析结果传入，存入任务后任务面板可直接打开观看
    play_url: str = Field(default="", max_length=2048)
    watch_options: list[dict] = Field(default_factory=list)
    is_hls: bool = False


@app.exception_handler(LinkError)
async def handle_link_error(_: Request, exc: LinkError) -> JSONResponse:
    status = 415 if isinstance(exc, UnsupportedPlatformError) else 400
    return JSONResponse(status_code=status, content={"error": exc.message, "hint": exc.hint})


@app.exception_handler(downloader.ResolveRestricted)
async def handle_restricted(_: Request, exc: "downloader.ResolveRestricted") -> JSONResponse:
    # 受限内容属于"确认无解"，用 422 与网络/解析异常区分开
    return JSONResponse(status_code=422, content={"error": exc.message, "hint": exc.hint})


def _require_task(task_id: str):
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return task


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.get("/api/platforms")
def list_platforms() -> dict:
    return {"platforms": platform_catalog()}


@app.get("/api/version")
def api_version() -> dict:
    """返回运行实例的构建指纹 + 实际加载的可执行文件路径。

    部署脚本 deploy_mac.sh 用它做自校验：只有运行中的服务返回的指纹与
    刚构建的 build_version.txt 一致、且 exe 路径确实指向目标 app 时，
    才算「部署成功」，否则直接判定失败，杜绝「装的是旧版却以为装好了」。
    """
    # build_version.txt 的位置随运行形态不同：
    #  - 打包后：<exe>/../Resources/build_version.txt（exe = Contents/MacOS/VideoDownloader）
    #  - 开发态：server/app.py 的上两级（仓库根）无此文件，回落 "dev"
    candidates = []
    exe = getattr(sys, "executable", "")
    if exe:
        candidates.append(Path(exe).resolve().parent.parent / "Resources" / "build_version.txt")
    candidates.append(Path(__file__).resolve().parent.parent / "build_version.txt")
    version = "dev"
    for c in candidates:
        if c.exists():
            version = c.read_text(encoding="utf-8").strip()
            break
    return {"version": version, "exe": sys.executable}


@app.get("/api/ydlp/version")
def ydlp_version_api() -> dict:
    """返回当前与最新 yt-dlp 版本，前端据此提示是否需要更新解析器。"""
    return {"current": ydlp_update.current_version(), "latest": ydlp_update.latest_version()}


@app.post("/api/ydlp/update")
async def ydlp_update_api() -> dict:
    """下载最新 yt-dlp 解析器到本机目录（下次启动生效）。"""
    return await asyncio.to_thread(ydlp_update.update)


@app.get("/api/nodes")
def node_info() -> dict:
    """告诉前端：本节点在哪个区、对端节点在哪、哪些域名算国内站。

    前端据此在粘贴链接时自动把请求发到「离目标站点更近」的节点：
    国内站 → cn 节点，海外站 → global 节点。对端为空则退化为单节点模式。
    """
    return {
        "region": NODE_REGION,
        "peer": PEER_ENDPOINT,
        "china_domains": list(CHINA_DOMAINS),
        "commentary_enabled": COMMENTARY_ENABLED,
        "ads_enabled": ADS_ENABLED,
        "convert": {
            "subscription_required": CONVERT_SUB_ENABLED,
            "free_daily": CONVERT_FREE_DAILY,
        },
        "download": {
            "subscription_required": DOWNLOAD_SUB_ENABLED,
            "free_daily": DOWNLOAD_FREE_DAILY,
        },
        "cloud": {
            "subscription_required": CLOUD_SUB_ENABLED,
            "free_daily": CLOUD_FREE_DAILY,
            "providers": (["webdav"] + (["baidu"] if BAIDU_ENABLED else [])),
            "baidu_available": BAIDU_ENABLED,
            "baidu_auth_url": baidu_auth_url(BAIDU_REDIRECT_URI, BAIDU_APP_KEY) if BAIDU_ENABLED else "",
        },
        # 本地媒体库：仅桌面版（frozen）或显式开启时暴露给前端；网页版目录临时、默认关闭
        "library": {
            "enabled": bool(getattr(sys, "frozen", False)) or bool(os.environ.get("VDL_LIBRARY_ENABLED")),
        },
        # 订阅监控：与媒体库同开关策略（桌面版/显式开启）；持久化在本地 JSON
        "subscriptions": {
            "enabled": SUB_ENABLED,
            "probe_limit": SUBSCRIBE_PROBE_LIMIT,
            "check_interval": SUB_CHECK_INTERVAL,
        },
        # 时效自动清理：与媒体库同开关；trash_available 决定「删媒体」档能否开启
        "retention": {
            "enabled": RETENTION_ENABLED,
            "trash_available": retention_mod.trash_available() if RETENTION_ENABLED else False,
        },
        "archive": {
            "enabled": ARCHIVE_ENABLED,
            "baidu_available": BAIDU_ENABLED,
            "configured": (
                archive_store.has_creds(archive_store.get().provider) if ARCHIVE_ENABLED else False
            ),
        },
        "crypto": {
            "enabled": CRYPTO_ENABLED,
            "has_pass": bool(_vault_load()) if CRYPTO_ENABLED else False,
            "locked": VAULT_KEY is None,
        },
        "torrent": {
            "enabled": TORRENT_ENABLED,
            "available": torrent_mod.available(),
        },
        "ai_dewatermark": {
            "enabled": AI_DEWATERMARK_ENABLED,
            "gpu": AI_GPU_AVAILABLE,
        },
        "authRequired": AUTH_REQUIRED,
    }


@app.post("/api/resolve")
async def resolve(payload: ResolveRequest, request: Request) -> dict:
    _check_rate_limit(request)
    _assert_safe_url(payload.url)          # 先拦内网/环回地址，避免可疑 URL 进入解析流程
    url, platform = parse_source(payload.url)
    # 国内站直连、本就快，用更短超时；受限视频也能更快判定，不必让用户空等
    host = _host_of(url)
    # 腾讯视频的 vqq 提取器容易卡在 m3u8 循环（新版页面 pinia 数据提取失败），
    # 给更长超时避免误报；其他国内站保持快速响应。
    if host == "v.qq.com":
        timeout = 35
    elif "youtube.com" in host or "youtu.be" in host:
        # YouTube（尤其走代理时）解析慢：需拉取 player.js + n 参数 + 签名，
        # 代理延迟叠加后 40s 经常不够，给 70s 余量
        timeout = 70
    elif is_china_host(host):
        timeout = RESOLVE_TIMEOUT_DOMESTIC
    else:
        timeout = RESOLVE_TIMEOUT_SECONDS
    loop = asyncio.get_running_loop()
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(prober, downloader.probe, url, payload.cookie, payload.proxy),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        host = _host_of(url)
        if host == "v.qq.com":
            detail = (
                "腾讯视频解析超时。该视频可能是会员/付费内容，或腾讯页面改版导致提取器暂时失效。"
                "建议：①在「高级选项」粘贴浏览器 Cookie 后重试；"
                "②确认视频可公开访问（非 VIP 专享）；③稍后重试或反馈此链接"
            )
        elif "youtube.com" in host or "youtu.be" in host:
            detail = (
                f"YouTube 解析超时（超过 {timeout} 秒）。常见原因：①代理速度慢或不稳定（YouTube 需要拉取 "
                "player.js 签名，代理延迟会叠加）；②该视频可能受限（地区/年龄限制）；"
                "建议：①检查代理是否通畅；②稍后重试；③若持续失败，尝试更换节点或关闭代理直连"
            )
        else:
            detail = (
                f"解析超时（超过 {timeout} 秒）。常见原因：①视频本身受限（限免/会员专享/付费/地区限制，"
                "这类通常需登录 cookie 才能拿到真实流，请到右上角「高级选项」粘贴浏览器 Cookie 后重试）；"
                "②当前网络无法访问该平台（可尝试在「高级选项」设置代理）"
            )
        raise HTTPException(status_code=504, detail=detail) from None
    return {
        "url": url,
        "platform": {"key": platform.key, "name": platform.name},
        "video": downloader.summarize(info),
        "qualities": downloader.build_quality_options(info),
        "sources": [],
    }


def _stream_referer(host: str) -> str:
    """按平台返回防盗链 Referer：腾讯视频 HLS 分片必须带正确的 Referer 才返回 200。

    注意：YouTube / googlevideo.com 等**不在此返回 Referer**——它们靠 URL 签名（ip/n/sig 参数）
    验证请求合法性，带错误 Referer（如 googlevideo.com 自身）反而会触发 403 拒绝。
    调用方应对 YouTube 域跳过 Referer。
    """
    if "v.qq.com" in host:
        return "https://v.qq.com/"
    if "douyin" in host:
        return "https://www.douyin.com/"
    if "bilibili" in host:
        return "https://www.bilibili.com/"
    # YouTube / googlevideo.com 不返回 Referer（由调用方决定是否设置）
    if "googlevideo.com" in host or "youtube.com" in host or "youtu.be" in host:
        return ""
    return f"https://{host}/" if host else "https://v.qq.com/"


def _rewrite_m3u8(text: str, base_url: str, proxy_prefix: str) -> str:
    """把 m3u8 内每条 URL 绝对化后改写成指向本端点的代理 URL。

    - 非注释、非空行即 URL 行（子 playlist / ts 分片），整行改写；
    - #EXT-X-KEY / #EXT-X-MEDIA 等标签行里的 URI="..." 属性也改写（加密流的 key 直连
      会被防盗链 403，必须走本端点带 Referer）。
    这样原生 <video> 播放器解析 master→子 playlist→ts→key 时，每一跳都走本端点。
    """
    uri_re = re.compile(r'(URI=")([^"]+)(")')

    def _rewrite_uri(m: "re.Match") -> str:
        seg = m.group(2).strip()
        abs_url = urljoin(base_url, seg)
        return m.group(1) + proxy_prefix + quote(abs_url, safe="") + m.group(3)

    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith("#"):
            if "URI=" in line:
                line = uri_re.sub(_rewrite_uri, line)
            out.append(line)
            continue
        abs_url = urljoin(base_url, stripped)
        out.append(proxy_prefix + quote(abs_url, safe=""))
    return "\n".join(out)


@app.get("/api/stream/proxy")
def stream_proxy(u: str = "", cookie: str = "", request: Request = None):
    """在线观看流代理：浏览器（WKWebView）直连腾讯会被防盗链 403，且原生 HLS 无法自定义
    Referer 头。这里由后端带 Referer/Cookie 去源站拉取回传，从而绕开防盗链。

    - 对非 m3u8（MP4/ts 分片等）原样流式透传；
    - 对 m3u8 清单：把内部相对/绝对 URL 改写为指向本端点的代理 URL，这样原生 <video>
      播放器解析 master→子 playlist→ts 分片时，每一跳都走本端点（后端统一带 Referer），
      无需 hls.js，macOS 原生 HLS 即可播放。
    """
    if not u:
        raise HTTPException(status_code=400, detail="缺少 u 参数")
    _assert_safe_url(u)  # SSRF 护栏：拒绝内网 / 环回 / 保留地址
    host = _host_of(u)
    # 代理：YouTube 等站的视频 URL 绑定出口 IP（URL 内含 ip/n 参数签名），
    # 必须与解析时使用同一代理，否则源站 403 拒绝或超时。
    # 国内站直连不走代理（避免不必要的延迟），其余走系统/自动代理。
    _proxies: dict[str, str] | None = None
    if not is_china_host(host):
        _proxy_url = downloader._resolve_proxy(host)
        if _proxy_url:
            _proxies = {"http": _proxy_url, "https": _proxy_url}
    user_cookie = (cookie or "").strip()
    if user_cookie.lower().startswith("cookie:"):
        user_cookie = user_cookie[7:].strip()
    cookie_text = user_cookie
    used_auto_cookie = False
    # 用户未手动粘贴 Cookie 时，自动探测本机浏览器登录态并携带，免去手动操作
    if not cookie_text:
        try:
            auto = downloader.get_browser_cookie_header(host, u)
        except Exception:
            auto = None
        if auto:
            cookie_text = auto
            used_auto_cookie = True
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Range": "bytes=0-",
    }
    _ref = _stream_referer(host)
    if _ref:
        headers["Referer"] = _ref
    if cookie_text:
        headers["Cookie"] = cookie_text
    # 转发客户端的 Range 头（<video> seek 时会带），否则用默认值
    _client_range = None
    if request:
        _cr = request.headers.get("range")
        if _cr:
            _client_range = _cr
            headers["Range"] = _cr

    try:
        resp = requests.get(u, headers=headers, stream=True, timeout=(10, 120), proxies=_proxies)
    except Exception as exc:  # noqa: BLE001 - 上游不可达，统一转 502 让前端提示
        raise HTTPException(status_code=502, detail=f"上游拉取失败：{_clean_message(str(exc))}") from None
    if resp.status_code >= 400:
        detail = f"上游返回 {resp.status_code}"
        if resp.status_code in (401, 403):
            if used_auto_cookie:
                detail += "（已自动携带浏览器登录态仍被拒，可能需先在浏览器登录该平台，或手动粘贴 Cookie）"
            elif cookie_text:
                detail += "（防盗链被拒，可在「高级选项」重新粘贴 Cookie 后重试）"
            else:
                detail += "（防盗链被拒，可能需要登录 Cookie，请在「高级选项」粘贴浏览器 Cookie 后重试）"
        resp.close()
        raise HTTPException(status_code=resp.status_code, detail=detail)

    content_type = (resp.headers.get("Content-Type") or "").lower()
    is_m3u8 = ("mpegurl" in content_type or content_type in ("application/x-mpegurl", "")
               or ".m3u8" in u)
    base = (str(request.base_url).rstrip("/") if request is not None else "http://127.0.0.1")
    proxy_prefix = f"{base}/api/stream/proxy?u="

    if is_m3u8:
        raw = resp.content.decode("utf-8", errors="replace")
        resp.close()
        # 仅当确实是 HLS 清单时才改写，避免误伤（例如 .m3u8 后缀的其它文本）
        if raw.lstrip().startswith("#EXTM3U"):
            rewritten = _rewrite_m3u8(raw, u, proxy_prefix)
            return Response(
                rewritten,
                media_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
            )
        # 不是真正的 m3u8：当普通文本透传
        return Response(
            raw,
            media_type=(content_type or "application/octet-stream"),
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    def _gen():
        try:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    # 转发上游的播放关键头，让浏览器 <video> 能正常 seek/缓冲
    _resp_headers = {
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
        "X-Accel-Buffering": "no",
    }
    # Accept-Ranges：告诉浏览器支持字节范围请求
    if resp.headers.get("Accept-Ranges"):
        _resp_headers["Accept-Ranges"] = resp.headers["Accept-Ranges"]
    # Content-Length / Content-Range：文件大小和范围（seek 必需）
    if resp.status_code == 206 and resp.headers.get("Content-Range"):
        _resp_headers["Content-Range"] = resp.headers["Content-Range"]
    if resp.headers.get("Content-Length"):
        _resp_headers["Content-Length"] = resp.headers["Content-Length"]

    return StreamingResponse(
        _gen(),
        media_type=(content_type or "application/octet-stream"),
        headers=_resp_headers,
        status_code=resp.status_code,  # 206 Partial Content 或 200
    )


@app.get("/api/cookie/status")
def cookie_status(url: str = "") -> dict:
    """探测本机浏览器是否含目标站点的登录 Cookie，供前端「检测登录态」与解析后自动提示。

    返回 available/browser/profile：前端据此告知用户「已自动读取，无需手动粘贴」
    或「未检测到，请先在浏览器登录该平台，或手动粘贴 Cookie」。
    """
    if not url:
        raise HTTPException(status_code=400, detail="请提供链接")
    _assert_safe_url(url)
    url, platform = parse_source(url)
    host = _host_of(url)
    info = downloader.detect_browser_cookie(host)
    return {
        "host": host,
        "platform": platform.key,
        "needed": downloader.is_cookie_hardened_host(host),
        "available": info["available"],
        "browser": info["browser"],
        "profile": info["profile"],
    }


@app.post("/api/cookie/cache/clear")
def cookie_cache_clear() -> dict:
    """清除本机 Cookie 缓存（仅删 ~/.videodownloader/cookies，不影响浏览器本身）。"""
    from cookie_cache import clear_cookie_cache
    n = clear_cookie_cache()
    return {"ok": True, "cleared": n}


def _valid_extract_mode(value: str) -> str:
    """校验并归一化文案提取模式，非法值回退为不提取。"""
    return value if value in ("spoken", "description", "both") else ""


@app.post("/api/download")
def create_download(payload: DownloadRequest, request: Request) -> dict:
    _check_rate_limit(request)
    subscribed, free_used, free_daily = _check_download_quota(request)
    # 注意：源视频 URL 不做 SSRF 的 DNS 解析拦截——它经过 parse_source 限定为已知公开平台，
    # 且代理/CDN/沙盒网络下 gethostbyname 常把公网域名解析成保留地址导致误杀（能解析却不能下载）。
    # SSRF 护栏仅保留在云盘目标地址（cloud_save 的 WebDAV URL）。
    url, platform = parse_source(payload.url)
    if not downloader.is_valid_quality(payload.quality):
        raise HTTPException(status_code=400, detail="不支持的清晰度选项")
    extract_mode = _valid_extract_mode(payload.extract_script)

    task = store.create(
        url=url,
        title="",
        platform=platform.name,
        quality=downloader.quality_label(payload.quality),
        quality_key=payload.quality,
        extract_mode=extract_mode,
        concurrent_fragments=payload.concurrent_fragments,
        downloader_type=payload.downloader,
        cookie=payload.cookie,
        proxy=payload.proxy,
        play_url=payload.play_url,
        watch_options=payload.watch_options,
        is_hls=payload.is_hls,
    )
    scheduler.submit(downloader.run_download, task, store, payload.quality, payload.cookie, payload.proxy, SINGLE_DOWNLOAD_RETRIES, payload.format_id, payload.concurrent_fragments, payload.downloader)
    return {
        "task_id": task.id,
        "status": task.status,
        "quota": {"subscribed": subscribed, "free_used": free_used, "free_daily": free_daily},
    }


# ---- 批量下载：一次提交多个链接，受全局并发上限统一调度 ----
class BatchRequest(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=VDL_BATCH_MAX_ITEMS)
    quality: str = Field(default=downloader.BEST_KEY, max_length=16)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)
    concurrency: int = Field(default=0, ge=0, le=VDL_BATCH_HARD_MAX)
    retries: int = Field(default=-1, ge=-1, le=10)
    extract_script: str = Field(default="", max_length=16)


@app.post("/api/batch")
def create_batch(payload: BatchRequest, request: Request) -> dict:
    _check_rate_limit(request)
    urls = [u.strip() for u in payload.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="没有提供有效的链接")
    if not downloader.is_valid_quality(payload.quality):
        raise HTTPException(status_code=400, detail="不支持的清晰度选项")
    extract_mode = _valid_extract_mode(payload.extract_script)
    if payload.concurrency > 0:
        scheduler.set_concurrency(payload.concurrency)
    retries = payload.retries if payload.retries >= 0 else BATCH_RETRIES_DEFAULT

    task_ids: list[str] = []
    skipped = 0
    quota_exhausted = False
    for u in urls:
        # 免费额度逐条消耗；耗尽时停止后续创建（已创建的照常排队下载）
        try:
            _check_download_quota(request)
        except HTTPException as exc:
            if exc.status_code == 402:
                quota_exhausted = True
                break
            raise
        try:
            url, platform = parse_source(u)
        except (UnsupportedPlatformError, LinkError):
            skipped += 1
            continue
        task = store.create(
            url=url, title="", platform=platform.name,
            quality=downloader.quality_label(payload.quality), quality_key=payload.quality,
            extract_mode=extract_mode,
        )
        scheduler.submit(downloader.run_download, task, store, payload.quality, payload.cookie, payload.proxy, retries)
        task_ids.append(task.id)
    if not task_ids:
        if quota_exhausted:
            raise HTTPException(status_code=402, detail="今日免费下载次数已用完，订阅可解锁无限下载")
        raise HTTPException(status_code=400, detail="链接均无法识别，请确认是视频播放页链接")
    return {"task_ids": task_ids, "count": len(task_ids), "skipped": skipped, "quota_exhausted": quota_exhausted}


@app.get("/api/tasks")
def list_tasks() -> dict:
    """列出当前所有任务（含排队 / 进行中 / 已完成），供前端队列概览。"""
    tasks = [t.to_public_dict() for t in store.list_all()]
    stats = {"pending": 0, "downloading": 0, "merging": 0, "completed": 0, "failed": 0, "canceled": 0}
    for t in tasks:
        stats[t["status"]] = stats.get(t["status"], 0) + 1
    stats["active"] = scheduler.active_count()
    return {"tasks": tasks, "stats": stats, "concurrency": scheduler.concurrency}


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str) -> dict:
    task = _require_task(task_id)
    if task.status not in ("failed", "canceled"):
        raise HTTPException(status_code=400, detail="仅失败 / 已取消的任务可以重试")
    task.cancel_requested = False
    # 断点续传：工作目录残留 .part 分片则从中断处接上（复用并发/下载器/cookie/proxy），
    # 否则从头重下。
    resume = downloader._has_partial(task.workdir)
    # 复用首次下载时的关键参数，避免续传时退化成默认配置（尤其 cookie 决定能否取到源）
    store.update(
        task_id, status="pending", error="", hint="",
        progress=task.progress if resume else 0.0,
        downloaded_bytes=task.downloaded_bytes if resume else 0,
        total_bytes=task.total_bytes if resume else 0,
        speed=0.0, eta=0, filesize=0, filename="",
        resumable=False,
    )
    scheduler.submit(
        downloader.run_download, task, store, task.quality_key,
        task.cookie, task.proxy, BATCH_RETRIES_DEFAULT, "",
        task.concurrent_fragments, task.downloader_type, resume,
    )
    return {"task_id": task_id, "status": "pending", "resume": resume}


@app.post("/api/tasks/{task_id}/extract-text")
def reextract_text(task_id: str) -> dict:
    """对已完成任务重新提取文案（如首次语音转写超时，可点重试）。"""
    task = _require_task(task_id)
    if not task.extract_mode:
        raise HTTPException(status_code=400, detail="该任务未开启文案提取")
    if not task.filepath or not Path(task.filepath).exists():
        raise HTTPException(status_code=400, detail="任务文件不存在，无法提取文案")
    executor.submit(
        downloader._run_extraction, task, store, Path(task.filepath), None, "", "",
        mode=task.extract_mode,
    )
    return {"task_id": task_id, "status": "running"}


@app.post("/api/tasks/cancel-all")
def cancel_all_tasks() -> dict:
    """取消所有进行中 / 排队中的任务；已完成与失败的任务保留（不删文件）。"""
    canceled = 0
    for t in store.list_all():
        if not t.is_finished and store.request_cancel(t.id):
            canceled += 1
    return {"canceled": canceled}


@app.get("/api/batch/config")
def batch_config() -> dict:
    return {"concurrency": scheduler.concurrency, "hard_max": VDL_BATCH_HARD_MAX, "retries": BATCH_RETRIES_DEFAULT}


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str) -> dict:
    return _require_task(task_id).to_public_dict()


@app.get("/api/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request) -> StreamingResponse:
    _require_task(task_id)

    async def event_stream():
        elapsed = 0.0
        while elapsed < SSE_MAX_SECONDS:
            if await request.is_disconnected():
                return
            task = store.get(task_id)
            if task is None:
                yield _sse({"status": "failed", "error": "任务已过期"})
                return
            yield _sse(task.to_public_dict())
            if task.is_finished:
                return
            await asyncio.sleep(SSE_INTERVAL_SECONDS)
            elapsed += SSE_INTERVAL_SECONDS

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/tasks/{task_id}/file")
def download_file(task_id: str) -> FileResponse:
    task = _require_task(task_id)
    if task.status != "completed" or not task.filepath or not task.filepath.exists():
        raise HTTPException(status_code=409, detail="文件尚未准备好")
    _ext = task.filepath.suffix.lower()
    _mt = {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
    }.get(_ext, "application/octet-stream")
    return FileResponse(
        path=task.filepath,
        filename=task.filepath.name,
        media_type=_mt,
    )


# ---- 格式转换：对已下载完成的文件做 ffmpeg 转码（增值能力） ----
class ConvertRequest(BaseModel):
    task_id: str
    target: str
    resolution: str = "original"


@app.post("/api/convert")
def create_convert(payload: ConvertRequest, request: Request) -> dict:
    _check_rate_limit(request)
    subscribed, free_used, free_daily = _check_convert_quota(request)
    task = _require_task(payload.task_id)
    if task.status != "completed" or not task.filepath or not task.filepath.exists():
        raise HTTPException(status_code=409, detail="原任务文件尚未准备好，无法转换")
    target = payload.target
    if target not in CONVERT_TARGETS:
        raise HTTPException(status_code=400, detail="不支持的目标格式")
    job_id = uuid.uuid4().hex[:12]
    ext = CONVERT_EXT[target]
    out_path = CONVERT_DIR / f"{task.id}_conv_{job_id}.{ext}"
    with CONVERT_LOCK:
        CONVERT_JOBS[job_id] = {
            "status": "running",
            "out_path": str(out_path),
            "error": "",
            "filename": out_path.name,
        }
    executor.submit(_run_convert, job_id, str(task.filepath), target, payload.resolution or "original")
    return {
        "job_id": job_id,
        "status": "running",
        "quota": {"subscribed": subscribed, "free_used": free_used, "free_daily": free_daily},
    }


@app.get("/api/convert/{job_id}")
def convert_status(job_id: str) -> dict:
    job = CONVERT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="转换任务不存在")
    return {"status": job["status"], "error": job.get("error", ""), "filename": job.get("filename", "")}


@app.get("/api/convert/{job_id}/file")
def convert_file(job_id: str) -> FileResponse:
    job = CONVERT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="转换任务不存在")
    if job["status"] != "completed":
        raise HTTPException(status_code=409, detail="转换尚未完成")
    out = Path(job["out_path"])
    if not out.exists():
        raise HTTPException(status_code=410, detail="转换文件已清理")
    return FileResponse(path=str(out), filename=out.name, media_type="application/octet-stream")


def _run_convert(job_id: str, src: str, target: str, resolution: str) -> None:
    """后台线程：ffmpeg 转码，更新 CONVERT_JOBS 状态。"""
    job = CONVERT_JOBS.get(job_id)
    if not job:
        return
    try:
        out = Path(job["out_path"])
        cmd = [FFMPEG_BIN, "-y", "-i", src]
        if target == "gif":
            cmd += CONVERT_TARGETS["gif"]
        else:
            cmd += CONVERT_TARGETS[target]
            if resolution != "original" and target not in ("mp3", "m4a"):
                h = {"1080": "1080", "720": "720", "480": "480"}.get(resolution)
                if h:
                    cmd += ["-vf", f"scale=-2:{h}"]
        cmd.append(str(out))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "")[-500:] or "ffmpeg 执行失败")
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("ffmpeg 未产出有效文件")
        job["status"] = "completed"
        logger.info("convert %s done -> %s", job_id, out.name)
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        logger.warning("convert %s failed: %s", job_id, e)


@app.delete("/api/tasks/{task_id}")
def cancel_task(task_id: str) -> dict:
    """进行中的任务 → 请求取消并保留记录；已结束的任务 → 连同文件一起清理。"""
    task = _require_task(task_id)
    if task.is_finished:
        store.remove(task_id)
        return {"task_id": task_id, "canceled": False, "removed": True}
    return {"task_id": task_id, "canceled": store.request_cancel(task_id), "removed": False}


@app.post("/api/tasks/{task_id}/pause")
def pause_task(task_id: str) -> dict:
    """暂停正在下载的任务——保留 .part 文件，后续可断点续传。"""
    task = _require_task(task_id)
    if task.is_finished:
        return {"task_id": task_id, "paused": False, "message": "任务已结束，无法暂停"}
    if task.status == "paused":
        return {"task_id": task_id, "paused": True, "message": "已暂停"}
    task.pause_requested = True
    task.add_step("下载音视频", "pending", "正在暂停…")
    store.update(task.id, status="pausing")
    return {"task_id": task_id, "paused": True}


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str) -> dict:
    """继续被暂停的下载——yt-dlp 自动从已下载的 .part 文件断点续传。"""
    task = _require_task(task_id)
    if task.status not in ("paused",):
        return {"task_id": task_id, "resumed": False, "message": "任务未处于暂停状态"}
    task.pause_requested = False
    task.add_step("下载音视频", "running", "继续下载…")
    task.log("用户继续下载（断点续传）")
    store.update(task.id, status="downloading")
    # 重新提入调度器，yt-dlp continuedl 自动从 .part 文件恢复
    scheduler.submit(downloader.run_download, task, store, task.quality_key, "", "", SINGLE_DOWNLOAD_RETRIES)
    return {"task_id": task_id, "resumed": True}


# --------------------------------------------------------------------------- #
# 文件系统辅助（桌面版便捷入口）
# --------------------------------------------------------------------------- #

class OpenPathRequest(BaseModel):
    """打开本地目录/文件。仅允许白名单路径（下载目录及其子项）。"""
    path: str = Field(default="", max_length=4096)


@app.post("/api/fs/open")
def fs_open(payload: OpenPathRequest) -> dict:
    """在系统文件管理器中打开本地路径。

    桌面版用户从浏览器里点「打开下载目录」→ 弹系统通知 / 调起 Finder。
    仅允许打开 DOWNLOAD_DIR 及其子项，不开放任意路径（防误开系统关键目录）。
    """
    if sys.platform != "darwin" and sys.platform != "win32":
        raise HTTPException(status_code=400, detail="该接口仅在桌面端可用")

    raw = (payload.path or "").strip()
    target = Path(raw).expanduser().resolve() if raw else DOWNLOAD_DIR.resolve()

    # 白名单：必须在 DOWNLOAD_DIR 下（除非用户显式请求 DOWNLOAD_DIR 本身）
    try:
        target.relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        if target != DOWNLOAD_DIR.resolve():
            raise HTTPException(status_code=403, detail="只允许打开下载目录及其子路径")

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在：{target}")

    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["explorer", str(target)])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"打开失败：{e}")

    return {"opened": str(target), "platform": sys.platform}


# --------------------------------------------------------------------------- #
# 自动解说（增值功能）：下载完 → 一键生成解说成片。壳，逻辑全在 commentary-pipeline
# --------------------------------------------------------------------------- #

class CommentaryRequest(BaseModel):
    task_id: str = Field(default="", max_length=64)
    file_id: str = Field(default="", max_length=2048)
    vertical: bool = False
    voice: str = Field(default="", max_length=64)
    trim_start: float = Field(default=0.0, ge=0.0)
    trim_end: float = Field(default=0.0, ge=0.0)
    # 剪辑选项（与 commentary-pipeline 的 commentary_options 模型一致）
    commentary_type: str = Field(default="deep_hl",
                                 description="解说类型: deep_hl=高光处叠加深度解说; normal_hl=高光部分普通解说; "
                                             "full_normal=全片普通解说; full_deep=全片深入解说")
    highlight_source: str = Field(default="ai", description="高光来源: ai=AI自动挑; manual=人工在审核面板挑")
    intro_highlight: bool = Field(default=False, description="片头插入最精彩片段当钩子")
    skip_intro_outro: bool = Field(default=False, description="去片头片尾(自动检测边界后剪掉)")
    no_narrate_intro_outro: bool = Field(default=True,
                                         description="保留片头片尾但不解说(默认开启；与 skip_intro_outro 互斥，后者优先)")
    retain_pct: float | None = Field(default=None, description="保留全片时长百分比(10~100, 不填=不裁剪)")
    web: bool = Field(default=False, description="联网搜索资料辅助发挥")
    one_click: bool = Field(default=False, description="一键生成: 全片深入解说+AI联网+片头插精彩片段")
    style: str = Field(default="none", description="解说口吻风格: none=默认; funny=搞笑; serious=严肃; domineering=霸道; angry=愤青; suspense=悬疑; healing=治愈; sarcastic=毒舌")
    mode: str | None = Field(default=None,
                             description="(旧版兼容) 三选一解说模式，会被上面的新选项覆盖")


class ScriptUpdateRequest(BaseModel):
    """PUT /api/commentary/script/{job_id}：提交人工修改后的解说词与全局配音。"""
    title: str = Field(default="", max_length=256)
    voice: str = Field(default="", max_length=64)
    segments: list = Field(default_factory=list)  # [{start, end, narration, note, voice?}, ...]


@app.post("/api/commentary")
def create_commentary(payload: CommentaryRequest) -> dict:
    if not COMMENTARY_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未启用解说功能")
    if COMMENTARY_MODE == "http":
        if not COMMENTARY_ENDPOINT:
            raise HTTPException(status_code=503, detail="解说 worker 未配置（VDL_COMMENTARY_MODE=http 但缺少 VDL_COMMENTARY_ENDPOINT）")
    else:
        if not COMMENTARY_DIR or not (COMMENTARY_DIR / "process.py").exists():
            raise HTTPException(status_code=503, detail="解说管线未配置（VDL_COMMENTARY_DIR 缺失或不含 process.py）")

    src_path = _resolve_source(payload)

    job_id = uuid.uuid4().hex[:12]
    with _commentary_lock:
        commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                   "steps": [], "logs": []}
    executor.submit(_commentary_run, job_id, src_path, payload.vertical, payload.voice or COMMENTARY_VOICE,
                    trim_start=payload.trim_start, trim_end=payload.trim_end,
                    mode=payload.mode, commentary_type=payload.commentary_type,
                    highlight_source=payload.highlight_source,
                    intro_highlight=payload.intro_highlight,
                    skip_intro_outro=payload.skip_intro_outro,
                    no_narrate_intro_outro=payload.no_narrate_intro_outro,
                    retain_pct=payload.retain_pct, web=payload.web,
                    one_click=payload.one_click,
                    title=_commentary_title(payload, src_path),
                    style=payload.style)
    return {"job_id": job_id, "status": "running"}


@app.post("/api/commentary/upload")
def create_commentary_upload(
    file: UploadFile = _FastAPIFile(...),
    vertical: bool = Form(False),
    voice: str = Form(""),
    trim_start: float = Form(0.0),
    trim_end: float = Form(0.0),
    mode: str = Form("highlights"),
) -> dict:
    """上传本地视频 → 直接生成解说成片。"""
    if not COMMENTARY_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未启用解说功能")
    suffix = Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mkv", ".mov", ".webm", ".avi"}:
        raise HTTPException(status_code=409, detail="请上传视频文件")
    work_dir = _commentary_work_dir()
    dest = work_dir / f"upload{suffix}"
    try:
        with dest.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()

    job_id = uuid.uuid4().hex[:12]
    with _commentary_lock:
        commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                   "steps": [], "logs": []}
    executor.submit(_commentary_run, job_id, str(dest), vertical, voice or COMMENTARY_VOICE,
                    trim_start=trim_start, trim_end=trim_end, mode=mode,
                    title=Path(file.filename).stem if file.filename else "")
    return {"job_id": job_id, "status": "running"}


@app.post("/api/commentary/script-only/upload")
def create_script_only_upload(
    file: UploadFile = _FastAPIFile(...),
    vertical: bool = Form(False),
    voice: str = Form(""),
    trim_start: float = Form(0.0),
    trim_end: float = Form(0.0),
    mode: str = Form("highlights"),
    commentary_type: str = Form("deep_hl"),
    highlight_source: str = Form("ai"),
    intro_highlight: bool = Form(False),
    skip_intro_outro: bool = Form(False),
    no_narrate_intro_outro: bool = Form(True),
    retain_pct: float = Form(None),
    web: bool = Form(False),
    one_click: bool = Form(False),
    style: str = Form("none"),
) -> dict:
    """上传本地视频 → 只生成脚本不渲染成片。"""
    if not COMMENTARY_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未启用解说功能")
    if COMMENTARY_MODE == "http":
        raise HTTPException(status_code=400, detail="脚本审核模式暂不支持 HTTP worker，请使用 local 模式")
    suffix = Path(file.filename or "upload.mp4").suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mkv", ".mov", ".webm", ".avi"}:
        raise HTTPException(status_code=409, detail="请上传视频文件")
    work_dir = _commentary_work_dir()
    dest = work_dir / f"upload{suffix}"
    try:
        with dest.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存上传文件失败：{e}")
    finally:
        file.file.close()

    job_id = uuid.uuid4().hex[:12]
    src_path = str(dest)
    with _commentary_lock:
        commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "script_path": "",
                                   "progress": [], "steps": [], "logs": [], "src_path": src_path}
    executor.submit(_commentary_run, job_id, src_path, vertical, voice or COMMENTARY_VOICE, script_only=True,
                    trim_start=trim_start, trim_end=trim_end, mode=mode,
                    commentary_type=commentary_type, highlight_source=highlight_source,
                    intro_highlight=intro_highlight, skip_intro_outro=skip_intro_outro,
                    no_narrate_intro_outro=no_narrate_intro_outro,
                    retain_pct=retain_pct, web=web, one_click=one_click,
                    title=Path(file.filename).stem if file.filename else "",
                    style=style)
    return {"job_id": job_id, "status": "running"}


# 独立「解说成片」标签页：列出所有已生成成片，并支持按 id 直接下载/播放。
# id 为成片绝对路径的 urlsafe base64，便于无状态回查且防止路径穿越。
def _commentary_roots() -> list[Path]:
    """扫描两个可能的输出目录：HTTP 模式落地到 COMMENTARY_LOCAL_OUTPUT，
    本地模式落地到 COMMENTARY_DIR/output。"""
    roots = []
    if COMMENTARY_LOCAL_OUTPUT and COMMENTARY_LOCAL_OUTPUT.exists() and COMMENTARY_LOCAL_OUTPUT.is_dir():
        roots.append(COMMENTARY_LOCAL_OUTPUT)
    if COMMENTARY_DIR:
        d = _commentary_root("output")
        if d.exists() and d.is_dir():
            roots.append(d)
    return roots


def _decode_commentary_id(cid: str) -> Path:
    try:
        raw = base64.urlsafe_b64decode(cid.encode("ascii")).decode("utf-8")
    except Exception:
        raise HTTPException(status_code=400, detail="非法的成片标识")
    p = Path(raw).resolve()
    allowed = {r.resolve() for r in _commentary_roots()}
    if not any(p == root or str(p).startswith(str(root) + os.sep) for root in allowed):
        raise HTTPException(status_code=403, detail="越权访问被拒绝")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="成片不存在或已被清理")
    return p


@app.get("/api/commentary/diagnostics")
def commentary_diagnostics() -> dict:
    """返回解说运行环境诊断信息，让桌面用户一眼看清 python / ffprobe 是否就绪。"""
    return {
        "enabled": COMMENTARY_ENABLED,
        "mode": COMMENTARY_MODE,
        "dir": str(COMMENTARY_DIR) if COMMENTARY_DIR else None,
        "python": COMMENTARY_RT.python,
        "python_ok": COMMENTARY_RT.python_ok,
        "deps_ok": COMMENTARY_RT.deps_ok,
        "ffmpeg_dir": COMMENTARY_RT.ffmpeg_dir,
        "ffprobe_ok": COMMENTARY_RT.ffprobe_ok,
        "ready": COMMENTARY_RT.ready(),
        "issues": COMMENTARY_RT.issues,
        "frozen": getattr(sys, "frozen", False),
    }


@app.get("/api/commentary/list")
def commentary_list() -> dict:
    """按修改时间倒序列出所有已生成的解说成片。"""
    items = []
    seen: set[str] = set()
    for root in _commentary_roots():
        for p in root.iterdir():
            if not p.is_file() or p.suffix.lower() not in (".mp4", ".mkv", ".mov", ".webm"):
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            cid = base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii")
            items.append({
                "id": cid,
                "name": p.name,
                "size": p.stat().st_size,
                "mtime": int(p.stat().st_mtime),
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"items": items}


@app.get("/api/commentary/file/{cid}")
def commentary_file_by_id(cid: str) -> FileResponse:
    p = _decode_commentary_id(cid)
    return FileResponse(str(p), filename=p.name, media_type="video/mp4")


@app.delete("/api/commentary/file/{cid}")
def commentary_delete_by_id(cid: str) -> dict:
    """把已生成的解说成片移入系统回收站，拒绝直接硬删用户资产。"""
    p = _decode_commentary_id(cid)
    allowed_roots = _commentary_roots()
    if not allowed_roots:
        raise HTTPException(status_code=503, detail="解说输出目录未配置")
    try:
        resolved = p.resolve()
    except Exception:
        resolved = p
    in_allowed_root = any(
        resolved == root.resolve() or root.resolve() in resolved.parents
        for root in allowed_roots
    )
    if not in_allowed_root:
        raise HTTPException(status_code=403, detail="文件路径不在解说输出目录内")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="成片文件不存在")
    if not retention_mod.trash_available():
        raise HTTPException(status_code=503, detail="系统回收站不可用，拒绝直接删除")
    # 先记录大小：移动成片到回收站后原路径已不存在，再 stat 会抛异常导致 500。
    try:
        file_size = resolved.stat().st_size
    except Exception:
        file_size = 0
    if not retention_mod.move_to_trash(resolved):
        raise HTTPException(status_code=500, detail="移入回收站失败")
    return {"deleted": True, "trashed": True, "name": p.name, "size": file_size}


class CommentaryRenameReq(BaseModel):
    name: str


@app.put("/api/commentary/file/{cid}")
def commentary_rename_by_id(cid: str, payload: CommentaryRenameReq) -> dict:
    """重命名已生成的解说成片（仅改文件名，不移动目录，保留原扩展名）。"""
    p = _decode_commentary_id(cid)
    allowed_roots = _commentary_roots()
    if not allowed_roots:
        raise HTTPException(status_code=503, detail="解说输出目录未配置")
    resolved = p.resolve()
    in_allowed_root = any(
        resolved == root.resolve() or root.resolve() in resolved.parents
        for root in allowed_roots
    )
    if not in_allowed_root:
        raise HTTPException(status_code=403, detail="文件路径不在解说输出目录内")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="成片文件不存在")

    new_name = (payload.name or "").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="新文件名不能为空")
    # 去掉可能用于路径穿越的字符
    new_name = new_name.replace("/", "_").replace("\\", "_").replace("..", "_").strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="新文件名非法")
    # 保留原扩展名（若新名未带扩展名）
    if "." not in new_name:
        new_name = new_name + p.suffix
    # 防止覆盖已有文件：重名自动追加 (1)/(2)…
    dest = resolved.parent / new_name
    n = 1
    while dest.exists():
        stem, suffix = new_name.rsplit(".", 1) if "." in new_name else (new_name, "")
        dest = resolved.parent / f"{stem} ({n}){('.' + suffix) if suffix else ''}"
        n += 1
    try:
        resolved.rename(dest)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重命名失败：{e}")
    new_cid = base64.urlsafe_b64encode(str(dest.resolve()).encode("utf-8")).decode("ascii")
    return {"renamed": True, "id": new_cid, "name": dest.name}


@app.post("/api/commentary/file/{cid}/save")
def commentary_save_to_downloads(cid: str) -> dict:
    """把已生成的解说成片复制到「下载」文件夹。

    桌面版 WebView 的 <a download> 在 cocoa/WKWebView 下不会触发本机保存，
    而 pywebview 原生 Api 也未暴露 save_commentary_file 桥接；因此由本地
    FastAPI 后端（与 app 同机同用户运行，有权限写 ~/Downloads）直接复制文件，
    前端点击「下载」时调用本接口即可真正把成片落到下载目录。
    """
    p = _decode_commentary_id(cid)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="成片文件不存在")
    downloads = Path.home() / "Downloads"
    try:
        downloads.mkdir(parents=True, exist_ok=True)
    except Exception:
        raise HTTPException(status_code=500, detail="无法访问下载文件夹")
    # 避免覆盖已有同名文件：dst 已存在则追加 (1)/(2)…
    stem, suffix = p.stem, p.suffix
    dst = downloads / p.name
    n = 1
    while dst.exists():
        dst = downloads / f"{stem} ({n}){suffix}"
        n += 1
    try:
        shutil.copy2(str(p), str(dst))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"复制失败：{e}")
    return {"saved": True, "path": str(dst), "name": dst.name}


@app.get("/api/commentary/{job_id}")
def commentary_status(job_id: str) -> dict:
    with _commentary_lock:
        job = commentary_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="解说任务不存在或已过期")
    # script_ready 也算就绪态（脚本已生成，等待人工确认后渲染）
    ready = job["status"] in ("completed", "script_ready")
    steps = job.get("steps") or []
    if not steps:
        # 旧任务或 HTTP 模式可能还没初始化步骤，兜底生成一个简单时间线
        steps = [{"name": "处理中", "status": "running" if job["status"] == "running" else "done",
                  "detail": "", "created_at": time.time(), "updated_at": time.time()}]
    result = {"job_id": job_id, "status": job["status"], "error": job.get("error", ""),
              "ready": ready, "progress": job.get("progress", []),
              "steps": steps, "logs": job.get("logs", []),
              "eta_remaining": job.get("eta_remaining"),
              "eta_done_at": job.get("eta_done_at"),
              "started_at": job.get("started_at"),
              "source_duration": job.get("source_duration")}
    if job.get("script_path"):
        result["script_path"] = job["script_path"]
    return result


@app.get("/api/commentary/{job_id}/file")
def commentary_file(job_id: str) -> FileResponse:
    with _commentary_lock:
        job = commentary_jobs.get(job_id)
    if job and job["status"] == "completed" and job.get("output_path"):
        path = Path(job["output_path"])
        if path.exists():
            return FileResponse(path=str(path), filename=path.name, media_type="application/octet-stream")
        # 任务标记完成但成片文件已丢失/被清理 → 410 Gone，不应再走 cid 解码分支（否则误报 409）
        raise HTTPException(status_code=410, detail="成片文件已清理或丢失")
    # 兼容「已生成成片列表」卡片的 cid（base64 编码的文件路径标识）：
    # 桌面版桥接 save_commentary_file(cid) 会请求 /api/commentary/{cid}/file，
    # 命中本路由；此处按 cid 解码并校验后直接返回文件，使列表卡片也能下载。
    try:
        p = _decode_commentary_id(job_id)
    except Exception:
        raise HTTPException(status_code=409, detail="成片尚未就绪或标识无效")
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")


# ---- 脚本审核专用路由 ----

def _resolve_source(payload: CommentaryRequest) -> str:
    """解析视频来源（task_id 或 file_id），返回绝对路径。"""
    if payload.file_id:
        p = library_mod._resolve_safe(DOWNLOAD_DIR, payload.file_id)
        if not p:
            raise HTTPException(status_code=404, detail="媒体库文件不存在")
        suffix = p.suffix.lower()
        if suffix == library_mod.ENCRYPTED_EXT:
            raise HTTPException(status_code=409, detail="加密文件不支持生成解说，请先在保险箱解锁")
        if suffix not in library_mod.VIDEO_EXTS:
            raise HTTPException(status_code=409, detail="该文件不是视频，无法生成解说成片")
        return str(p)
    elif payload.task_id:
        task = _require_task(payload.task_id)
        if task.status != "completed" or not task.filepath or not task.filepath.exists():
            raise HTTPException(status_code=409, detail="下载任务尚未完成，无法生成解说")
        return str(task.filepath)
    else:
        raise HTTPException(status_code=400, detail="请提供 task_id 或 file_id")


def _commentary_title(payload: "CommentaryRequest", src_path: str) -> str:
    """为解说任务推导剧名锚点：优先用下载任务的标题，否则退回源文件名（下载文件名通常含剧集名）。"""
    if getattr(payload, "task_id", ""):
        try:
            t = _require_task(payload.task_id)
            tt = getattr(t, "title", "") or ""
            if tt:
                return tt
        except Exception:
            pass
    return Path(src_path).stem or ""


@app.post("/api/commentary/script-only")
def create_script_only(payload: CommentaryRequest) -> dict:
    """只做转写+解说词生成，不渲染成片。返回 job_id 供前端轮询，
    拿到 script.json 后展示可编辑解说词面板。"""
    if not COMMENTARY_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未启用解说功能")
    if COMMENTARY_MODE == "http":
        if not COMMENTARY_ENDPOINT:
            raise HTTPException(status_code=503, detail="解说 worker 未配置")
        raise HTTPException(status_code=400, detail="脚本审核模式暂不支持 HTTP worker，请使用 local 模式")

    src_path = _resolve_source(payload)
    job_id = uuid.uuid4().hex[:12]
    with _commentary_lock:
        commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": "", "script_path": "",
                                   "progress": [], "src_path": src_path}
    executor.submit(_commentary_run, job_id, src_path, payload.vertical, payload.voice or COMMENTARY_VOICE,
                    script_only=True, trim_start=payload.trim_start, trim_end=payload.trim_end,
                    mode=payload.mode, commentary_type=payload.commentary_type,
                    highlight_source=payload.highlight_source,
                    intro_highlight=payload.intro_highlight,
                    skip_intro_outro=payload.skip_intro_outro,
                    no_narrate_intro_outro=payload.no_narrate_intro_outro,
                    retain_pct=payload.retain_pct, web=payload.web,
                    one_click=payload.one_click,
                    title=_commentary_title(payload, src_path),
                    style=payload.style)
    return {"job_id": job_id, "status": "running"}


@app.get("/api/commentary/script/{job_id}")
def get_script(job_id: str) -> dict:
    """获取已生成脚本文件内容（script.json）。"""
    with _commentary_lock:
        job = commentary_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job["status"] != "script_ready" or not job.get("script_path"):
        raise HTTPException(status_code=409, detail="脚本尚未就绪（当前状态: " + job["status"] + "）")
    script_path = Path(job["script_path"])
    if not script_path.exists():
        raise HTTPException(status_code=410, detail="脚本文件已被清理")
    try:
        data = json.loads(script_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取脚本文件失败：{e}")
    return {"job_id": job_id, "title": data.get("title", ""),
            "voice": data.get("voice", ""),
            "segments": data.get("segments", []), "segment_count": len(data.get("segments", []))}


@app.put("/api/commentary/script/{job_id}")
def update_script(job_id: str, payload: ScriptUpdateRequest) -> dict:
    """人工修改后提交更新脚本（写回 script.json）。
    保留原始时间戳（前端发 start/end=0），只更新 narration / voice。
    """
    with _commentary_lock:
        job = commentary_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job["status"] != "script_ready" or not job.get("script_path"):
        raise HTTPException(status_code=409, detail="脚本尚未就绪，无法修改（当前状态: " + job["status"] + "）")
    script_path = Path(job["script_path"])

    # 读现有脚本以保留时间戳
    try:
        existing = json.loads(script_path.read_text(encoding="utf-8"))
    except Exception:
        existing = {}
    existing_segs = existing.get("segments", [])

    # 合并：payload 里每个 seg 的 narration 写回对应 idx 的原始 seg
    merged = []
    for i, pseg in enumerate(payload.segments):
        orig = existing_segs[i] if i < len(existing_segs) else {}
        merged.append({
            "start": orig.get("start", pseg.get("start", 0)),
            "end": orig.get("end", pseg.get("end", 0)),
            "narration": pseg.get("narration", orig.get("narration", "")),
            "note": pseg.get("note", orig.get("note", "")),
        })

    data = {
        "title": payload.title or existing.get("title", ""),
        "voice": payload.voice or existing.get("voice", job.get("voice", "")),
        "segments": merged,
        # 保留原始 mode（如高光解说模式），否则保存后再渲染会丢失高光标记
        "mode": existing.get("mode", ""),
        # 保留原始 options（剪辑选项：解说类型/高光来源/片头高光/联网/保留时长等），
        # 否则审核后渲染会丢失用户在面板上的剪辑选择
        "options": existing.get("options", {}),
    }
    try:
        script_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入脚本失败：{e}")

    # 更新内存中的 voice 偏好（渲染时会用到）
    if payload.voice:
        with _commentary_lock:
            commentary_jobs[job_id]["voice"] = payload.voice

    return {"job_id": job_id, "status": "updated", "segment_count": len(payload.segments)}


@app.post("/api/commentary/render/{job_id}")
def render_script(job_id: str, vertical: bool = Form(False), voice: str = Form("")) -> dict:
    """用已审核的脚本渲染成片（process.py --edit-only）。

    剪辑选项直接沿用 script.json 中已保存的 options（生成脚本时写入、人工审核时可改），
    避免用默认值覆盖用户当初的选择（例如一键生成的全片深入+联网会被 deep_hl 默认值冲掉）。
    """
    if not COMMENTARY_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未启用解说功能")
    if COMMENTARY_MODE == "http":
        raise HTTPException(status_code=400, detail="脚本渲染暂不支持 HTTP worker 模式，请使用 local 模式")

    with _commentary_lock:
        job = commentary_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job["status"] != "script_ready" or not job.get("script_path"):
        raise HTTPException(status_code=409, detail="请先生成脚本再渲染（当前状态: " + job["status"] + "）")
    script_path = job["script_path"]
    # 从 script.json 的 segments 里找回原始视频名 + 已保存的剪辑选项
    try:
        seg_data = json.loads(Path(script_path).read_text(encoding="utf-8"))
        title = seg_data.get("title", "")
        saved = seg_data.get("options") or {}
    except Exception:
        title = ""
        saved = {}

    commentary_type = saved.get("commentary_type", "deep_hl")
    highlight_source = saved.get("highlight_source", "ai")
    intro_highlight = bool(saved.get("intro_highlight", False))
    skip_intro_outro = bool(saved.get("skip_intro_outro", False))
    no_narrate_intro_outro = bool(saved.get("no_narrate_intro_outro", True))
    retain_pct = saved.get("retain_pct")
    web = bool(saved.get("web", False))
    one_click = bool(saved.get("one_click", False))

    # 反查原始视频路径：优先用脚本任务记录的 src_path，避免按 title/job_id 在 input 目录里猜错。
    src_path = job.get("src_path")
    if not src_path:
        # 兼容旧任务：按 title 或 job_id 在 input 目录搜索（已不推荐）
        base_name = title or job_id
        in_dir = _commentary_root("input")
        src_candidates = list(in_dir.glob(f"{base_name}.*")) or list(in_dir.glob(f"{job_id}.*"))
        if not src_candidates:
            raise HTTPException(status_code=404, detail=f"找不到原始视频文件（input/{base_name}.* 或 input/{job_id}.*）")
        src_path = str(src_candidates[0])

    # 复用父任务的裁剪参数：同一源+起止会命中确定性命名的裁剪文件，直接吃裁剪后视频渲染
    trim_start = float(job.get("trim_start", 0.0) or 0.0)
    trim_end = float(job.get("trim_end", 0.0) or 0.0)

    # 用新的 job_id 提交渲染（保留原 script 关联）
    render_job_id = uuid.uuid4().hex[:12]
    with _commentary_lock:
        commentary_jobs[render_job_id] = {"status": "running", "error": "", "output_path": "", "progress": [],
                                          "parent_script_job": job_id, "steps": [], "logs": []}
    v = voice or job.get("voice", "") or COMMENTARY_VOICE
    executor.submit(_commentary_run, render_job_id, src_path, vertical, v, edit_only=script_path,
                    trim_start=trim_start, trim_end=trim_end,
                    commentary_type=commentary_type, highlight_source=highlight_source,
                    intro_highlight=intro_highlight, skip_intro_outro=skip_intro_outro,
                    no_narrate_intro_outro=no_narrate_intro_outro,
                    retain_pct=retain_pct, web=web, one_click=one_click)
    return {"job_id": render_job_id, "status": "running", "script_job": job_id}


# ---- 配音试听 / 预览全部 ----

def _run_voice_preview(text: str, voice: str, output_mp3: Path, timeout: int = 60) -> None:
    """用 edge-tts 把一段文本转成指定音色的 mp3。

    bundled 模式：edge_tts 已随包冻结，直接 in-process 调用，避开「subprocess 跑
    .py 脚本 vs PyInstaller frozen exe」的兼容性问题。
    dev 模式：edge_tts 装在 commentary-pipeline .venv 里，subprocess 到 COMMENTARY_RT.python。
    """
    if not COMMENTARY_RT.ready():
        raise RuntimeError("解说环境未就绪：" + "；".join(COMMENTARY_RT.issues))

    # bundled 模式：sys.frozen = True，edge_tts 已冻结进 exe，直接 in-process 调用
    if getattr(sys, "frozen", False):
        _run_voice_preview_inprocess(text, voice, output_mp3, timeout=timeout)
        return

    # dev/外部：subprocess 走 commentary-pipeline 的 venv
    if not COMMENTARY_DIR or not (COMMENTARY_DIR / "scripts" / "voice_preview.py").exists():
        raise RuntimeError("voice_preview.py 不存在（请在 commentary-pipeline/scripts/ 下创建）")
    script = COMMENTARY_DIR / "scripts" / "voice_preview.py"
    cmd = [COMMENTARY_RT.python, str(script), text, voice, str(output_mp3)]
    try:
        proc = subprocess.run(cmd, cwd=str(COMMENTARY_DIR), capture_output=True,
                              text=True, timeout=timeout, env=COMMENTARY_RT.env())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"edge-tts 生成超时（>{timeout}s）") from exc
    except Exception as exc:
        raise RuntimeError(f"edge-tts 调用失败：{exc}") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "无输出").strip()[:400]
        raise RuntimeError(f"edge-tts 退出码 {proc.returncode}: {msg}")
    if not output_mp3.exists() or output_mp3.stat().st_size < 100:
        raise RuntimeError("edge-tts 未产出有效音频文件")


def _run_voice_preview_inprocess(text: str, voice: str, output_mp3: Path, timeout: int = 60) -> None:
    """bundled 模式：in-process 调 edge_tts 合成 mp3，省去 PyInstaller frozen exe 跑 .py 脚本的兼容坑。"""
    import asyncio
    import edge_tts

    FALLBACK_VOICES = [
        "zh-CN-YunjianNeural",
        "zh-CN-YunyangNeural",
        "zh-CN-YunxiaNeural",
        "zh-CN-XiaoxiaoNeural",
    ]

    async def _try(text: str, v: str, timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(edge_tts.Communicate(text, v).save(str(output_mp3)), timeout=timeout_s)
            return output_mp3.exists() and output_mp3.stat().st_size > 100
        except Exception:
            return False

    async def _main():
        for _ in range(2):
            if await _try(text, voice, 20):
                return
        tried = [voice]
        for fb in FALLBACK_VOICES:
            if fb == voice:
                continue
            tried.append(fb)
            if await _try(text, fb, 20):
                return
        raise RuntimeError(f"edge-tts 生成失败: 已尝试 {', '.join(tried)}，均无法合成该文本")

    try:
        asyncio.run(_main())
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"edge-tts 调用失败：{exc}") from exc
    if not output_mp3.exists() or output_mp3.stat().st_size < 100:
        raise RuntimeError("edge-tts 未产出有效音频文件")


@app.post("/api/commentary/voice-preview")
def voice_preview(
    voice: str = Form(...),
    text: str = Form("你好，我是视频解说员。我将为你解说这段视频。"),
) -> FileResponse:
    """配音试听：把指定文本用指定 voice 转成 mp3 返回给前端播放。"""
    if not COMMENTARY_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未启用解说功能")
    if COMMENTARY_MODE == "http":
        raise HTTPException(status_code=400, detail="配音试听需在 local 模式使用")
    # 1. 先校验输入（不等 COMMENTARY_DIR 挂掉）
    voice = (voice or "").strip()
    if not voice.startswith("zh-"):
        raise HTTPException(status_code=400, detail=f"voice 必须是 zh-CN-* 音色，当前: {voice}")
    # FastAPI Form() 有 bug：空字符串会落回默认值（即使前端显式发 text=），所以加 fallback
    text = (text or "").strip()[:500] if text else ""
    if not text:
        text = "你好，我是视频解说员。我将为你解说这段视频。"
    # 2. 再检查资源可用性
    if not COMMENTARY_DIR or not (COMMENTARY_DIR / "scripts" / "voice_preview.py").exists():
        raise HTTPException(status_code=503, detail="解说管线未配置（VDL_COMMENTARY_DIR 缺失或不含 voice_preview.py）")

    out_dir = _commentary_root("work") / "voice_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{uuid.uuid4().hex[:12]}.mp3"
    try:
        _run_voice_preview(text, voice, out_path, timeout=45)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=f"试听生成失败：{e}")
    # 加过期清理保护：定时任务会清理 work/voice_preview/ 下超过 1 天的文件（用户本机 .cleanup）
    return FileResponse(path=str(out_path), filename="preview.mp3", media_type="audio/mpeg")


@app.post("/api/commentary/preview/{job_id}")
def preview_segments(
    job_id: str,
    voice: str = Form(""),
    max_segments: int = Form(3),
) -> FileResponse:
    """用当前 voice 朗读 script.json 里前 N 段的 narration，拼接成一段 mp3 返回。
    主要给「预览全部」按钮用——按全脚本生成太长，前 3 段够判断音色和节奏。
    """
    if not COMMENTARY_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未启用解说功能")
    if COMMENTARY_MODE == "http":
        raise HTTPException(status_code=400, detail="预览需在 local 模式使用")
    # 1. 先校验 job 状态（不等资源检查）
    with _commentary_lock:
        job = commentary_jobs.get(job_id)
    if not job or job["status"] != "script_ready" or not job.get("script_path"):
        raise HTTPException(status_code=409, detail="请先生成脚本再预览（当前状态: " + (job or {}).get("status", "missing") + "）")
    script_path = Path(job["script_path"])
    if not script_path.exists():
        raise HTTPException(status_code=410, detail="脚本文件已被清理")
    try:
        data = json.loads(script_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取脚本失败：{e}")
    segs = [s for s in (data.get("segments") or []) if (s.get("narration") or "").strip()]
    if not segs:
        raise HTTPException(status_code=409, detail="脚本里没有可朗读的 narration")
    n = max(1, min(int(max_segments), len(segs), 6))
    chosen = segs[:n]
    v = (voice or "").strip() or data.get("voice") or job.get("voice", "") or COMMENTARY_VOICE
    if not v.startswith("zh-"):
        raise HTTPException(status_code=400, detail=f"voice 必须是 zh-CN-* 音色")
    # 2. 再检查资源可用性
    if not COMMENTARY_DIR or not (COMMENTARY_DIR / "scripts" / "voice_preview.py").exists():
        raise HTTPException(status_code=503, detail="解说管线未配置（VDL_COMMENTARY_DIR 缺失或不含 voice_preview.py）")

    out_dir = _commentary_root("work") / "voice_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / uuid.uuid4().hex[:12]
    tmp_dir.mkdir(parents=True, exist_ok=True)
    final_mp3 = tmp_dir / "preview.mp3"
    # 逐段生成
    try:
        clips = []
        for i, seg in enumerate(chosen, 1):
            seg_mp3 = tmp_dir / f"seg{i:02d}.mp3"
            narration = (seg.get("narration") or "").strip()
            if not narration:
                continue
            try:
                _run_voice_preview(narration, v, seg_mp3, timeout=45)
            except RuntimeError as e:
                raise RuntimeError(f"第 {i} 段朗读失败：{e}")
            clips.append(seg_mp3)
        if not clips:
            raise RuntimeError("没有可用 narration 可朗读")
        # 用 ffmpeg concat demuxer 拼接
        list_file = tmp_dir / "concat.txt"
        list_file.write_text("\n".join(f"file '{p.name}'" for p in clips), encoding="utf-8")
        concat_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                      "-i", str(list_file), "-c", "copy", str(final_mp3)]
        proc = subprocess.run(concat_cmd, cwd=str(tmp_dir), capture_output=True, text=True, timeout=60,
                              env={"PATH": COMMENTARY_RT.ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")})
        if proc.returncode != 0 or not final_mp3.exists():
            # concat 失败时退到单段：直接把第一段作为 preview（保证有声音）
            try:
                shutil.copyfile(clips[0], final_mp3)
            except Exception as e:
                raise RuntimeError(f"拼接失败且回退也失败：{e}; 原 stderr: {(proc.stderr or '')[:200]}")
    except RuntimeError as e:
        # 清理临时目录（保留 final_mp3 不存在路径下不会报错）
        try: shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception: pass
        raise HTTPException(status_code=500, detail=f"预览生成失败：{e}")
    # 返回临时文件，让客户端下载/播放
    return FileResponse(path=str(final_mp3), filename="preview.mp3", media_type="audio/mpeg")


# --------------------------------------------------------------------------- #
# 云盘集成（增值能力）：把已下载文件存到用户自己的网盘（WebDAV / 百度网盘）
# --------------------------------------------------------------------------- #

class CloudSaveRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=16)
    dest_path: str = Field(default="", max_length=1024)
    webdav: dict = Field(default_factory=dict)
    baidu: dict = Field(default_factory=dict)


@app.get("/api/cloud/providers")
def cloud_providers() -> dict:
    """列出本实例可用的云盘类型与百度授权地址。"""
    providers = ["webdav"]
    if BAIDU_ENABLED:
        providers.append("baidu")
    return {
        "providers": providers,
        "baidu_available": BAIDU_ENABLED,
        "baidu_auth_url": baidu_auth_url(BAIDU_REDIRECT_URI, BAIDU_APP_KEY) if BAIDU_ENABLED else "",
    }


@app.get("/api/cloud/baidu/auth_url")
def cloud_baidu_auth_url() -> dict:
    if not BAIDU_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
    state = secrets.token_urlsafe(16)
    now = time.time()
    with _BAIDU_STATES_LOCK:
        _BAIDU_STATES[state] = now + _BAIDU_STATE_TTL
        # 顺手清理过期条目，避免长期运行堆积
        expired = [s for s, exp in _BAIDU_STATES.items() if exp < now]
        for s in expired:
            _BAIDU_STATES.pop(s, None)
    return {"auth_url": baidu_auth_url(BAIDU_REDIRECT_URI, BAIDU_APP_KEY, state)}


@app.get("/api/cloud/baidu/callback")
def cloud_baidu_callback(code: str = "", state: str = ""):
    """OAuth 回调：用 code 换取 access_token，返回把令牌回传给 opener 的页面（服务端不存令牌）。"""
    if not BAIDU_ENABLED:
        return HTMLResponse(_baidu_callback_html(error="该实例未配置百度网盘凭据"))
    # state 校验：拒绝被诱导发起的授权（CSRF），过期/缺失/不匹配均拒绝
    with _BAIDU_STATES_LOCK:
        exp = _BAIDU_STATES.pop(state, None)
    if exp is None or time.time() > exp:
        return HTMLResponse(_baidu_callback_html(error="授权状态校验失败，请重新点击授权"))
    try:
        token = baidu_exchange_token(code, BAIDU_REDIRECT_URI, BAIDU_APP_KEY, BAIDU_APP_SECRET)
    except CloudError as exc:
        return HTMLResponse(_baidu_callback_html(error=exc.message))
    save_baidu_token(token)
    return HTMLResponse(_baidu_callback_html(token=token.get("access_token", "")))


# ── 百度网盘「下载到本机」（官方 PCS，速度由账号等级决定）─────────────────
# 内存任务表：仅保存下载进度，不持久化（百度 dlink 短时效，断点续传意义不大）。
_baidu_dl_tasks: dict[str, dict] = {}
_baidu_dl_lock = threading.Lock()


class BaiduDownloadRequest(BaseModel):
    token: str = ""
    fs_id: int = 0
    path: str = ""
    name: str = ""
    backend: str = ""  # 空=auto（优先 aria2c 并发，缺失回退 requests）


@app.get("/api/cloud/baidu/list")
def cloud_baidu_list(path: str = "/", token: str = ""):
    """浏览用户网盘目录：返回归一化文件列表（文件夹在前）。"""
    if not BAIDU_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
    if not token:
        raise HTTPException(status_code=400, detail="缺少 access_token（请先完成百度授权）")
    try:
        data = _baidu_provider.list_files(token, path)
    except CloudError as exc:
        raise HTTPException(status_code=502, detail=exc.message + (("：" + exc.hint) if exc.hint else ""))
    items = data.get("list") or []
    files = [
        {
            "fs_id": it.get("fs_id"),
            "path": it.get("path"),
            "name": it.get("server_filename") or it.get("filename") or "",
            "size": it.get("size", 0),
            "isdir": bool(it.get("isdir")),
            "mtime": it.get("server_mtime", 0),
        }
        for it in items
    ]
    files.sort(key=lambda x: (not x["isdir"], -x["mtime"]))
    return {"path": path or "/", "list": files, "has_more": bool(data.get("has_more"))}


def _baidu_safe_name(name: str) -> str:
    """取网盘文件名的纯文件名部分，剔除路径穿越字符。"""
    base = Path(name or "").name
    return base or "file"


@app.post("/api/cloud/baidu/download")
def cloud_baidu_download(payload: BaiduDownloadRequest):
    """把网盘文件下载到本机 ~/Downloads/VideoDownloader/baidu/，后台线程跑，轮询进度。"""
    if not BAIDU_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
    token = (payload.token or "").strip()
    if not token or not payload.fs_id or not payload.path:
        raise HTTPException(status_code=400, detail="缺少 token / fs_id / path")
    name = _baidu_safe_name(payload.name) or _baidu_safe_name(payload.path)
    tid = secrets.token_hex(8)
    with _baidu_dl_lock:
        _baidu_dl_tasks[tid] = {
            "status": "pending", "progress": 0, "total": 0,
            "error": "", "name": name, "filepath": "",
        }

    def _worker() -> None:
        dest = DOWNLOAD_DIR / "baidu" / name
        try:
            with _baidu_dl_lock:
                _baidu_dl_tasks[tid].update(status="downloading")
            if dest.exists():
                # 同名文件加序号，避免覆盖已下好的
                stem = dest.stem
                suffix = dest.suffix
                i = 1
                while dest.exists():
                    dest = dest.with_name(f"{stem}({i}){suffix}")
                    i += 1

            def _prog(done: int, total: int) -> None:
                with _baidu_dl_lock:
                    t = _baidu_dl_tasks[tid]
                    t["progress"] = done
                    if total:
                        t["total"] = total

            _baidu_provider.download(
                token, int(payload.fs_id), payload.path, dest,
                progress=_prog, backend=payload.backend or "auto",
            )
            with _baidu_dl_lock:
                _baidu_dl_tasks[tid].update(
                    status="completed",
                    progress=_baidu_dl_tasks[tid]["total"] or _baidu_dl_tasks[tid]["progress"],
                    filepath=str(dest),
                )
        except CloudError as exc:
            with _baidu_dl_lock:
                _baidu_dl_tasks[tid].update(
                    status="failed",
                    error=exc.message + (("：" + exc.hint) if exc.hint else ""),
                )
        except Exception as exc:  # noqa: BLE001 — 兜底，避免后台线程静默崩溃
            with _baidu_dl_lock:
                _baidu_dl_tasks[tid].update(status="failed", error=str(exc))

    threading.Thread(target=_worker, name=f"vdl-baidudl-{tid}", daemon=True).start()
    return {"task_id": tid, "name": name}


@app.get("/api/cloud/baidu/task/{tid}")
def cloud_baidu_task(tid: str):
    with _baidu_dl_lock:
        t = _baidu_dl_tasks.get(tid)
    if not t:
        raise HTTPException(status_code=404, detail="下载任务不存在")
    return t


# ── 百度网盘「分享链接下载」（登录后转存到自己网盘再下，官方通道）──────────
class BaiduShareListRequest(BaseModel):
    url: str = ""
    pwd: str = ""


@app.post("/api/cloud/baidu/share/list")
def cloud_baidu_share_list(payload: BaiduShareListRequest):
    """列出分享链接里的文件（看分享内容本身无需登录）。"""
    if not BAIDU_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
    if not payload.url.strip():
        raise HTTPException(status_code=400, detail="缺少分享链接")
    try:
        result = _baidu_provider.share_list(payload.url, payload.pwd)
    except CloudError as exc:
        raise HTTPException(status_code=502, detail=exc.message + (("：" + exc.hint) if exc.hint else ""))
    return result


class BaiduShareDownloadRequest(BaseModel):
    url: str = ""
    pwd: str = ""
    path: str = ""        # 分享内文件路径（来自 share/list 的 path 字段）
    name: str = ""
    token: str = ""       # 可选；缺省回退到本机持久化的令牌
    backend: str = ""     # 空=auto（优先 aria2c 并发，缺失回退 requests）


@app.post("/api/cloud/baidu/share/download")
def cloud_baidu_share_download(payload: BaiduShareDownloadRequest):
    """把分享里的某个文件转存到用户自己网盘并从自己网盘下载到本机（后台任务）。"""
    if not BAIDU_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
    token = (payload.token or "").strip() or (load_baidu_token() or {}).get("access_token") or ""
    if not token:
        raise HTTPException(status_code=400, detail="请先完成百度账号授权")
    if not payload.url.strip() or not payload.path:
        raise HTTPException(status_code=400, detail="缺少分享链接或文件路径")
    name = _baidu_safe_name(payload.name) or _baidu_safe_name(payload.path)
    tid = secrets.token_hex(8)
    with _baidu_dl_lock:
        _baidu_dl_tasks[tid] = {
            "status": "pending", "progress": 0, "total": 0,
            "error": "", "name": name, "filepath": "",
        }

    def _worker() -> None:
        dest = DOWNLOAD_DIR / "baidu" / "share" / name
        try:
            with _baidu_dl_lock:
                _baidu_dl_tasks[tid].update(status="transferring")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                stem, suffix = dest.stem, dest.suffix
                i = 1
                while dest.exists():
                    dest = dest.with_name(f"{stem}({i}){suffix}")
                    i += 1

            def _prog(done: int, total: int) -> None:
                with _baidu_dl_lock:
                    t = _baidu_dl_tasks[tid]
                    t["progress"] = done
                    if total:
                        t["total"] = total

            _baidu_provider.download_share(
                payload.url, payload.pwd, payload.path, dest, token,
                progress=_prog, backend=payload.backend or "auto",
            )
            with _baidu_dl_lock:
                _baidu_dl_tasks[tid].update(
                    status="completed",
                    progress=_baidu_dl_tasks[tid]["total"] or _baidu_dl_tasks[tid]["progress"],
                    filepath=str(dest),
                )
        except CloudError as exc:
            with _baidu_dl_lock:
                _baidu_dl_tasks[tid].update(
                    status="failed",
                    error=exc.message + (("：" + exc.hint) if exc.hint else ""),
                )
        except Exception as exc:  # noqa: BLE001 — 兜底，避免后台线程静默崩溃
            with _baidu_dl_lock:
                _baidu_dl_tasks[tid].update(status="failed", error=str(exc))

    threading.Thread(target=_worker, name=f"vdl-baidushare-{tid}", daemon=True).start()
    return {"task_id": tid, "name": name}


# ── 百度令牌本机持久化（每个用户各自存自己机器，重启后免重复授权）────────
@app.get("/api/cloud/baidu/token")
def cloud_baidu_token_get():
    if not BAIDU_ENABLED:
        return {"logged_in": False, "reason": "未配置百度网盘凭据"}
    data = load_baidu_token() or {}
    tok = data.get("access_token") or ""
    return {
        "logged_in": bool(tok),
        "access_token": tok,
        "expires_in": data.get("expires_in"),
        "scope": data.get("scope"),
    }


class BaiduTokenSet(BaseModel):
    access_token: str = ""
    expires_in: int | None = None
    scope: str = ""
    refresh_token: str = ""


@app.post("/api/cloud/baidu/token")
def cloud_baidu_token_set(payload: BaiduTokenSet):
    tok = (payload.access_token or "").strip()
    if not tok:
        raise HTTPException(status_code=400, detail="缺少 access_token")
    save_baidu_token(payload.model_dump())
    return {"ok": True}


@app.delete("/api/cloud/baidu/token")
def cloud_baidu_token_del():
    clear_baidu_token()
    return {"ok": True}


@app.post("/api/cloud/save")
def cloud_save(payload: CloudSaveRequest, request: Request) -> dict:
    subscribed, free_used, free_daily = _check_cloud_quota(request)
    task = _require_task(payload.task_id)
    if task.status != "completed" or not task.filepath or not task.filepath.exists():
        raise HTTPException(status_code=409, detail="下载任务尚未完成，无法存到网盘")
    provider = payload.provider
    if provider == "webdav":
        inst = _webdav_provider
        creds = payload.webdav or {}
        # SSRF 防护：拒绝指向内网 / 环回 / 云元数据的 WebDAV 地址，避免本服务被当跳板
        wurl = (creds.get("url") or "").strip()
        if wurl:
            try:
                _assert_safe_url(wurl)
            except LinkError as exc:
                raise HTTPException(status_code=400, detail="WebDAV 地址不在允许范围内：" + exc.message)
    elif provider == "baidu":
        if not BAIDU_ENABLED:
            raise HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
        inst = _baidu_provider
        creds = payload.baidu or {}
    else:
        raise HTTPException(status_code=400, detail="不支持的网盘类型")
    job_id = uuid.uuid4().hex[:12]
    _prune_cloud_jobs()
    with CLOUD_LOCK:
        CLOUD_JOBS[job_id] = {"status": "running", "error": "", "remote_path": "", "progress": 0.0}
    cloud_executor.submit(_run_cloud, job_id, inst, str(task.filepath), payload.dest_path, creds)
    return {
        "job_id": job_id,
        "status": "running",
        "quota": {"subscribed": subscribed, "free_used": free_used, "free_daily": free_daily},
    }


@app.get("/api/cloud/status/{job_id}")
def cloud_status(job_id: str) -> dict:
    with CLOUD_LOCK:
        job = CLOUD_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="云盘任务不存在")
    return {
        "status": job["status"],
        "error": job.get("error", ""),
        "remote_path": job.get("remote_path", ""),
        "progress": job.get("progress", 0.0),
    }


def _run_cloud(job_id: str, inst, src: str, dest_path: str, creds: dict) -> None:
    """后台线程：把本地文件上传到用户云盘，更新 CLOUD_JOBS 状态。"""
    with CLOUD_LOCK:
        job = CLOUD_JOBS.get(job_id)
    if not job:
        return
    try:
        def _progress(sent: int, total: int) -> None:
            job["progress"] = round(sent / total * 100, 1) if total else 0.0

        remote = inst.upload(Path(src), dest_path, creds, progress=_progress)
        job["status"] = "completed"
        job["remote_path"] = remote
        logger.info("cloud %s -> %s", job_id, remote)
    except CloudError as exc:
        job["status"] = "failed"
        job["error"] = exc.message + (("：" + exc.hint) if exc.hint else "")
        logger.warning("cloud %s failed: %s", job_id, job["error"])
    except Exception as exc:  # noqa: BLE001
        job["status"] = "failed"
        job["error"] = str(exc)[:400]
        logger.exception("cloud %s failed", job_id)


def _prune_cloud_jobs() -> None:
    """云盘任务字典只增不删会内存泄漏；超过阈值时清理最旧的已完成/失败任务（保留最新 200 条）。"""
    if len(CLOUD_JOBS) <= 500:
        return
    with CLOUD_LOCK:
        done = [jid for jid, j in CLOUD_JOBS.items() if j.get("status") in ("completed", "failed")]
        for jid in (done[:-200] if len(done) > 200 else []):
            CLOUD_JOBS.pop(jid, None)


# ---- 本地媒体库（桌面版功能）：浏览 / 播放 / 删除已下载的媒体文件 ----
@app.get("/api/library")
def library_list(q: str = "", platform: str = "", kind: str = "all") -> dict:
    items = library_mod.scan_library(DOWNLOAD_DIR)
    if q:
        ql = q.lower()
        items = [
            i for i in items
            if ql in (i["title"] or "").lower()
            or ql in (i["name"] or "").lower()
            or ql in (i["uploader"] or "").lower()
        ]
    if platform:
        items = [i for i in items if i["platform"] == platform]
    if kind in ("video", "audio"):
        items = [i for i in items if i["kind"] == kind]
    return {"items": items, "total": len(items)}


@app.get("/api/library/file/{lib_id}")
def library_file(lib_id: str) -> FileResponse:
    p = library_mod._resolve_safe(DOWNLOAD_DIR, lib_id)
    if not p:
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path=p, filename=p.name, media_type="application/octet-stream")


@app.get("/api/library/thumb/{lib_id}")
def library_thumb(lib_id: str) -> FileResponse:
    p = library_mod.get_thumbnail(DOWNLOAD_DIR, lib_id, FFMPEG_BIN)
    if not p:
        raise HTTPException(status_code=404, detail="无缩略图")
    return FileResponse(path=p, media_type="image/jpeg")


@app.delete("/api/library/{lib_id}")
def library_delete(lib_id: str) -> dict:
    if not library_mod.delete_item(DOWNLOAD_DIR, lib_id):
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"deleted": True}


# ---- 时效自动清理：按保留期/容量上限清理下载目录（预览 → 执行，媒体走回收站） ----

class RetentionConfigRequest(BaseModel):
    auto_enabled: bool | None = None
    interval_hours: float | None = Field(default=None, ge=0.25, le=168)
    temp_enabled: bool | None = None
    temp_days: float | None = Field(default=None, ge=0, le=3650)
    frames_enabled: bool | None = None
    frames_days: float | None = Field(default=None, ge=0, le=3650)
    thumbs_enabled: bool | None = None
    thumbs_days: float | None = Field(default=None, ge=0, le=3650)
    media_enabled: bool | None = None
    media_days: float | None = Field(default=None, ge=1, le=3650)
    quota_enabled: bool | None = None
    quota_gb: float | None = Field(default=None, ge=1, le=100000)
    media_use_trash: bool | None = None


class RetentionRunRequest(BaseModel):
    # 只清指定档位；留空=按当前配置全清。前端「预览后执行」会带上用户勾选的档位。
    categories: list[str] | None = None


def _require_retention() -> None:
    if not RETENTION_ENABLED:
        raise HTTPException(status_code=404, detail="自动清理仅桌面版可用")


@app.get("/api/retention/config")
def retention_config_get() -> dict:
    _require_retention()
    cfg = retention_store.get()
    return {
        "config": cfg.to_dict(),
        "labels": retention_mod.CATEGORY_LABELS,
        "trash_available": retention_mod.trash_available(),
        "usage": retention_mod.disk_usage(DOWNLOAD_DIR),
    }


@app.post("/api/retention/config")
def retention_config_set(req: RetentionConfigRequest) -> dict:
    _require_retention()
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    # 安全阀：没有可用回收站时不允许开启「删媒体本体」，避免静默硬删用户资产
    if not retention_mod.trash_available():
        if fields.get("media_enabled") or fields.get("quota_enabled"):
            raise HTTPException(status_code=400, detail="系统回收站不可用，无法开启媒体清理（拒绝直接硬删）")
    if fields.get("media_use_trash") is False:
        raise HTTPException(status_code=400, detail="媒体清理必须走回收站，不允许关闭")
    cfg = retention_store.update(**fields)
    return {"config": cfg.to_dict()}


@app.post("/api/retention/scan")
def retention_scan() -> dict:
    """只算不删：返回将被清理的分档清单与可释放空间。"""
    _require_retention()
    cfg = retention_store.get()
    plan = retention_mod.scan(DOWNLOAD_DIR, cfg)
    plan["usage"] = retention_mod.disk_usage(DOWNLOAD_DIR)
    # 每档只回传前 50 条明细，避免上千条把响应撑爆；总数/总大小已单列
    for cat, entries in plan["categories"].items():
        plan["categories"][cat] = {
            "count": len(entries),
            "size": sum(e["size"] for e in entries),
            "items": entries[:50],
        }
    return plan


@app.post("/api/retention/run")
def retention_run(req: RetentionRunRequest) -> dict:
    _require_retention()
    cfg = retention_store.get()
    cats = req.categories or None
    if cats:
        unknown = [c for c in cats if c not in retention_mod.CATEGORY_LABELS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知清理类别：{', '.join(unknown)}")
    try:
        result = retention_mod.run(DOWNLOAD_DIR, cfg, cats)
    except Exception as exc:  # noqa: BLE001
        logger.exception("手动清理失败")
        raise HTTPException(status_code=500, detail=f"清理失败：{str(exc)[:200]}")
    retention_store.update(last_run=result["ran_at"], last_freed=result["freed"],
                           last_removed=result["removed"])
    result["usage"] = retention_mod.disk_usage(DOWNLOAD_DIR)
    result["freed_text"] = retention_mod.human_size(result["freed"])
    return result


# ---- 一键归档网盘：按模板把媒体库文件批量 / 自动上传到用户自己的网盘 ----

class ArchiveConfigRequest(BaseModel):
    auto_enabled: bool | None = None
    interval_hours: float | None = Field(default=None, ge=0.25, le=720)
    provider: str | None = Field(default=None, max_length=16)
    dest_template: str | None = Field(default=None, max_length=512)
    include_video: bool | None = None
    include_audio: bool | None = None
    include_image: bool | None = None
    min_age_minutes: float | None = Field(default=None, ge=0, le=10080)
    max_file_gb: float | None = Field(default=None, ge=0, le=1024)
    delete_after: bool | None = None
    webdav: dict | None = None
    baidu: dict | None = None


class ArchiveRunRequest(BaseModel):
    lib_ids: list[str] = Field(default_factory=list, max_length=2000)


class ArchiveForgetRequest(BaseModel):
    rel: str = Field(default="", max_length=1024)


def _require_archive() -> None:
    if not ARCHIVE_ENABLED:
        raise HTTPException(status_code=404, detail="网盘归档仅桌面版可用")


def _archive_provider(cfg) -> tuple:
    """按配置取上传器与凭据，顺带做 SSRF / 配置完整性校验。"""
    if cfg.provider == "webdav":
        creds = archive_store.get_creds("webdav")
        url = (creds.get("url") or "").strip()
        if not url:
            raise HTTPException(status_code=400, detail="尚未配置 WebDAV 地址")
        try:
            _assert_archive_url(url)
        except LinkError as exc:
            raise HTTPException(status_code=400, detail="WebDAV 地址不合法：" + exc.message)
        return _webdav_provider.upload, creds
    if cfg.provider == "baidu":
        if not BAIDU_ENABLED:
            raise HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
        creds = archive_store.get_creds("baidu")
        if not (creds.get("token") or "").strip():
            raise HTTPException(status_code=400, detail="尚未完成百度网盘授权")
        return _baidu_provider.upload, creds
    raise HTTPException(status_code=400, detail="不支持的网盘类型")


@app.get("/api/archive/config")
def archive_config_get() -> dict:
    _require_archive()
    cfg = archive_store.get()
    return {
        "config": cfg.to_dict(),
        "creds": archive_store.creds_masked(),
        "configured": archive_store.has_creds(cfg.provider),
        "providers": ["webdav"] + (["baidu"] if BAIDU_ENABLED else []),
        "tokens": archive_mod.TEMPLATE_TOKENS,
        "default_template": archive_mod.DEFAULT_TEMPLATE,
        "trash_available": retention_mod.trash_available(),
        "records": archive_store.records(30),
    }


@app.post("/api/archive/config")
def archive_config_set(req: ArchiveConfigRequest) -> dict:
    _require_archive()
    data = req.model_dump()
    webdav = data.pop("webdav", None)
    baidu = data.pop("baidu", None)
    fields = {k: v for k, v in data.items() if v is not None}

    if fields.get("provider") and fields["provider"] not in ("webdav", "baidu"):
        raise HTTPException(status_code=400, detail="不支持的网盘类型")
    if fields.get("provider") == "baidu" and not BAIDU_ENABLED:
        raise HTTPException(status_code=503, detail="该实例未配置百度网盘应用凭据")
    # 安全阀：没有可用回收站时不允许开「归档后删本地」，避免静默硬删用户资产
    if fields.get("delete_after") and not retention_mod.trash_available():
        raise HTTPException(status_code=400, detail="系统回收站不可用，无法开启「归档后删本地」（拒绝直接硬删）")

    if webdav is not None:
        url = (webdav.get("url") or "").strip()
        if url:
            try:
                _assert_archive_url(url)
            except LinkError as exc:
                raise HTTPException(status_code=400, detail="WebDAV 地址不合法：" + exc.message)
        archive_store.set_creds("webdav", {
            "url": url,
            "user": (webdav.get("user") or "").strip(),
            "pass": webdav.get("pass") or "",
        })
    if baidu is not None:
        archive_store.set_creds("baidu", {"token": (baidu.get("token") or "").strip()})

    cfg = archive_store.update(**fields)
    return {
        "config": cfg.to_dict(),
        "creds": archive_store.creds_masked(),
        "configured": archive_store.has_creds(cfg.provider),
    }


@app.post("/api/archive/scan")
def archive_scan() -> dict:
    """只算不传：列出待归档文件与目标远端路径，前端必须先看这个再执行。"""
    _require_archive()
    cfg = archive_store.get()
    items = library_mod.scan_library(DOWNLOAD_DIR)
    pend = archive_mod.pending_items(items, cfg, archive_store)
    return {
        "count": len(pend),
        "size": sum(p["size"] for p in pend),
        "size_text": archive_mod.human_size(sum(p["size"] for p in pend)),
        "items": pend[:200],
        "truncated": len(pend) > 200,
        "configured": archive_store.has_creds(cfg.provider),
        "provider": cfg.provider,
    }


@app.post("/api/archive/run")
def archive_run(req: ArchiveRunRequest) -> dict:
    _require_archive()
    cfg = archive_store.get()
    upload_fn, creds = _archive_provider(cfg)

    items = library_mod.scan_library(DOWNLOAD_DIR)
    pend = archive_mod.pending_items(items, cfg, archive_store)
    if req.lib_ids:
        wanted = set(req.lib_ids)
        pend = [p for p in pend if p["id"] in wanted]
    if not pend:
        raise HTTPException(status_code=409, detail="没有待归档的文件")

    job_id = uuid.uuid4().hex[:12]
    _prune_archive_jobs()
    with ARCHIVE_LOCK:
        ARCHIVE_JOBS[job_id] = {
            "status": "running", "index": 0, "total": len(pend), "current": "",
            "file_percent": 0.0, "uploaded": 0, "failed": 0, "errors": [],
            "cancel": False, "started_at": int(time.time()),
        }
    cloud_executor.submit(_run_archive_job, job_id, pend, cfg, upload_fn, creds)
    return {"job_id": job_id, "total": len(pend)}


@app.get("/api/archive/status/{job_id}")
def archive_status(job_id: str) -> dict:
    _require_archive()
    with ARCHIVE_LOCK:
        job = ARCHIVE_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="归档任务不存在")
    return {k: v for k, v in job.items() if k != "cancel"}


@app.post("/api/archive/cancel/{job_id}")
def archive_cancel(job_id: str) -> dict:
    _require_archive()
    with ARCHIVE_LOCK:
        job = ARCHIVE_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="归档任务不存在")
        job["cancel"] = True
    return {"canceling": True}


@app.post("/api/archive/forget")
def archive_forget(req: ArchiveForgetRequest) -> dict:
    """清除归档记录，让文件下次重新上传（例如网盘那头被误删了）。"""
    _require_archive()
    n = archive_store.forget(req.rel)
    return {"cleared": n}


def _run_archive_job(job_id: str, targets: list[dict], cfg, upload_fn, creds: dict) -> None:
    with ARCHIVE_LOCK:
        job = ARCHIVE_JOBS.get(job_id)
    if not job:
        return

    def _progress(p: dict) -> None:
        job.update(p)

    def _stop() -> bool:
        return bool(job.get("cancel"))

    try:
        result = archive_mod.run_archive(
            DOWNLOAD_DIR, targets, cfg, archive_store,
            uploader=upload_fn, creds=creds,
            on_progress=_progress, should_stop=_stop,
            trash=retention_mod.move_to_trash, trash_ok=retention_mod.trash_available,
        )
        job.update({
            "status": "canceled" if job.get("cancel") else "completed",
            "uploaded": result["uploaded"], "failed": result["failed"],
            "skipped": result["skipped"], "deleted": result["deleted"],
            "bytes": result["bytes"], "bytes_text": result["bytes_text"],
            "errors": result["errors"][:20], "file_percent": 100.0,
        })
        archive_store.update(last_run=result["ran_at"], last_uploaded=result["uploaded"],
                             last_failed=result["failed"])
        logger.info("归档 %s：上传 %s 个 / %s，失败 %s",
                    job_id, result["uploaded"], result["bytes_text"], result["failed"])
    except Exception as exc:  # noqa: BLE001
        job.update({"status": "failed", "errors": [str(exc)[:300]]})
        logger.exception("归档任务 %s 异常", job_id)


def _prune_archive_jobs() -> None:
    """归档任务字典只增不删会内存泄漏；超阈值时清理最旧的已结束任务。"""
    if len(ARCHIVE_JOBS) <= 200:
        return
    with ARCHIVE_LOCK:
        done = [jid for jid, j in ARCHIVE_JOBS.items()
                if j.get("status") in ("completed", "failed", "canceled")]
        for jid in (done[:-50] if len(done) > 50 else []):
            ARCHIVE_JOBS.pop(jid, None)


# --------------------------------------------------------------------------- #
# 库内保险箱：选中文件就地 AES 加密 / 解密 + 解密播放
# --------------------------------------------------------------------------- #
class CryptoSetPassRequest(BaseModel):
    passwd: str = Field(min_length=1, max_length=512)
    confirm: str = Field(default="", max_length=512)
    old: str = Field(default="", max_length=512)


class CryptoUnlockRequest(BaseModel):
    passwd: str = Field(min_length=1, max_length=512)


class CryptoIdsRequest(BaseModel):
    lib_ids: list[str] = Field(default_factory=list)


def _require_crypto() -> None:
    if not CRYPTO_ENABLED:
        raise HTTPException(status_code=404, detail="保险箱功能未启用")


def _require_unlocked() -> None:
    if VAULT_KEY is None:
        raise HTTPException(status_code=423, detail="保险箱已锁定，请先解锁")


def _crypto_job_status(job_id: str) -> dict:
    with CRYPTO_LOCK:
        job = CRYPTO_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.get("/api/crypto/status")
def crypto_status() -> dict:
    _require_crypto()
    return {
        "enabled": CRYPTO_ENABLED,
        "has_pass": bool(_vault_load()),
        "locked": VAULT_KEY is None,
    }


@app.post("/api/crypto/set-pass")
def crypto_set_pass(req: CryptoSetPassRequest) -> dict:
    _require_crypto()
    vault = _vault_load()
    if vault is not None:
        # 已有密码：必须提供正确的旧密码方可修改
        if not req.old or not crypto_mod.verify_passphrase(req.old, vault):
            raise HTTPException(status_code=400, detail="旧密码错误")
    else:
        if req.passwd != req.confirm:
            raise HTTPException(status_code=400, detail="两次输入的密码不一致")
    if len(req.passwd) < 4:
        raise HTTPException(status_code=400, detail="密码至少 4 位")
    new_vault = crypto_mod.new_vault(req.passwd)
    _vault_save(new_vault)
    # 设完即解锁，立即可用
    global VAULT_KEY
    VAULT_KEY = crypto_mod.unlock_key(req.passwd, new_vault)
    return {"has_pass": True, "locked": False}


@app.post("/api/crypto/unlock")
def crypto_unlock(req: CryptoUnlockRequest) -> dict:
    _require_crypto()
    vault = _vault_load()
    if not vault:
        raise HTTPException(status_code=400, detail="尚未设置保险箱密码")
    if not crypto_mod.verify_passphrase(req.passwd, vault):
        raise HTTPException(status_code=401, detail="密码错误")
    global VAULT_KEY
    VAULT_KEY = crypto_mod.unlock_key(req.passwd, vault)
    return {"locked": False}


@app.post("/api/crypto/lock")
def crypto_lock() -> dict:
    _require_crypto()
    global VAULT_KEY
    VAULT_KEY = None
    return {"locked": True}


def _kind_of(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in library_mod.AUDIO_EXTS:
        return "audio"
    if suf in library_mod.IMAGE_EXTS:
        return "image"
    return "video"


def _run_crypto_job(job_id: str, lib_ids: list[str], mode: str) -> None:
    """mode = 'encrypt' | 'decrypt'。加密把原件移回收站保底；解密还原原名并删除 .vdlenc。"""
    total = len(lib_ids)
    done = 0
    errors: list[str] = []
    with CRYPTO_LOCK:
        job = CRYPTO_JOBS.get(job_id)
        if job:
            job["status"] = "running"
            job["total"] = total

    for lid in lib_ids:
        with CRYPTO_LOCK:
            job = CRYPTO_JOBS.get(job_id)
            if not job or job.get("cancel"):
                with CRYPTO_LOCK:
                    if job:
                        job["status"] = "canceled"
                return
        src = library_mod._resolve_safe(DOWNLOAD_DIR, lid)
        if not src:
            errors.append(lid + ": 文件不存在")
            done += 1
            with CRYPTO_LOCK:
                if job:
                    job["done"] = done
            continue
        try:
            if mode == "encrypt":
                if src.suffix.lower() == library_mod.ENCRYPTED_EXT:
                    errors.append(src.name + ": 已是加密文件")
                else:
                    dst = src.parent / (src.name + library_mod.ENCRYPTED_EXT)
                    crypto_mod.encrypt_file(src, dst, VAULT_KEY, src.name, _kind_of(src))
                    # 原件移回收站保底，绝不静默硬删
                    retention_mod.move_to_trash(src)
            else:  # decrypt
                if src.suffix.lower() != library_mod.ENCRYPTED_EXT:
                    errors.append(src.name + ": 不是加密文件")
                else:
                    orig_name, _kind, _ext = crypto_mod.read_header(src)
                    out = src.parent / orig_name
                    if out.exists():
                        errors.append(src.name + ": 还原目标已存在，跳过")
                    else:
                        tmp_dec = src.parent / "tmp_dec"
                        crypto_mod.decrypt_file(src, tmp_dec, VAULT_KEY)
                        tmp_dec.replace(out)
                        src.unlink()  # 解密成功才删 .vdlenc
        except Exception as exc:  # 单文件失败不中断整批
            errors.append(src.name + ": " + type(exc).__name__ + " " + str(exc)[:120])
        done += 1
        with CRYPTO_LOCK:
            if job:
                job["done"] = done
                job["errors"] = errors

    with CRYPTO_LOCK:
        job = CRYPTO_JOBS.get(job_id)
        if job and job.get("status") != "canceled":
            job["status"] = "completed"
            job["errors"] = errors


@app.post("/api/crypto/encrypt")
def crypto_encrypt(req: CryptoIdsRequest) -> dict:
    _require_crypto()
    _require_unlocked()
    if not req.lib_ids:
        raise HTTPException(status_code=400, detail="未选择文件")
    job_id = "cry_" + uuid.uuid4().hex[:12]
    with CRYPTO_LOCK:
        CRYPTO_JOBS[job_id] = {"status": "queued", "done": 0, "total": len(req.lib_ids),
                               "errors": [], "cancel": False, "mode": "encrypt"}
    CRYPTO_EXECUTOR.submit(_run_crypto_job, job_id, list(req.lib_ids), "encrypt")
    _prune_crypto_jobs()
    return {"job_id": job_id}


@app.post("/api/crypto/decrypt")
def crypto_decrypt(req: CryptoIdsRequest) -> dict:
    _require_crypto()
    _require_unlocked()
    if not req.lib_ids:
        raise HTTPException(status_code=400, detail="未选择文件")
    job_id = "cry_" + uuid.uuid4().hex[:12]
    with CRYPTO_LOCK:
        CRYPTO_JOBS[job_id] = {"status": "queued", "done": 0, "total": len(req.lib_ids),
                               "errors": [], "cancel": False, "mode": "decrypt"}
    CRYPTO_EXECUTOR.submit(_run_crypto_job, job_id, list(req.lib_ids), "decrypt")
    _prune_crypto_jobs()
    return {"job_id": job_id}


@app.get("/api/crypto/job/{job_id}")
def crypto_job(job_id: str) -> dict:
    _require_crypto()
    return _crypto_job_status(job_id)


@app.post("/api/crypto/cancel/{job_id}")
def crypto_cancel(job_id: str) -> dict:
    _require_crypto()
    with CRYPTO_LOCK:
        job = CRYPTO_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        job["cancel"] = True
    return {"canceled": True}


def _prune_crypto_jobs() -> None:
    if len(CRYPTO_JOBS) <= 200:
        return
    with CRYPTO_LOCK:
        done = [jid for jid, j in CRYPTO_JOBS.items()
                if j.get("status") in ("completed", "failed", "canceled")]
        for jid in (done[:-50] if len(done) > 50 else []):
            CRYPTO_JOBS.pop(jid, None)


@app.get("/api/library/encfile/{lib_id}")
def library_encfile(lib_id: str) -> FileResponse:
    """解密播放：把 .vdlenc 临时解密到 .vault_tmp 并返回（带 Range 支持）。锁定时 423。"""
    _require_crypto()
    _require_unlocked()
    src = library_mod._resolve_safe(DOWNLOAD_DIR, lib_id)
    if not src or src.suffix.lower() != library_mod.ENCRYPTED_EXT:
        raise HTTPException(status_code=404, detail="加密文件不存在")
    try:
        orig_name, _kind, _ext = crypto_mod.read_header(src)
    except Exception:
        raise HTTPException(status_code=400, detail="加密文件损坏")
    tmp = _vault_tmp_for(lib_id)
    try:
        crypto_mod.decrypt_file(src, tmp, VAULT_KEY)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="解密失败：" + type(exc).__name__)
    ext = Path(orig_name).suffix.lower()
    media = {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".mov": "video/quicktime",
        ".webm": "video/webm", ".avi": "video/x-msvideo", ".m4v": "video/mp4",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".flac": "audio/flac", ".ogg": "audio/ogg", ".wav": "audio/wav",
        ".opus": "audio/ogg", ".gif": "image/gif", ".webp": "image/webp",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    }.get(ext, "application/octet-stream")
    return FileResponse(path=str(tmp), filename=orig_name, media_type=media)


# ---- 桌面版种子下载（libtorrent 集成）：magnet/.torrent → 本地媒体库 ----
class TorrentAddRequest(BaseModel):
    uri: str = Field(min_length=1, max_length=8192)
    name: str = Field(default="", max_length=512)
    paused: bool = False
    save_path: str = Field(default="", max_length=4096)  # 相对 DOWNLOAD_DIR 的子目录；留空=根
    file_priorities: dict[int, int] = Field(default_factory=dict)  # {文件下标: 优先级(0=跳过)}


class TorrentRemoveRequest(BaseModel):
    delete_files: bool = False


class TorrentFilesRequest(BaseModel):
    priorities: dict[int, int] = Field(default_factory=dict)  # {文件下标: 优先级(0=跳过)}


def _require_torrent() -> None:
    if not (TORRENT_ENABLED and torrent_mod.available() and torrent_manager is not None):
        raise HTTPException(status_code=404, detail="种子下载功能未启用（需桌面版并安装 libtorrent 或 aria2）")


@app.get("/api/torrents")
def torrent_list() -> dict:
    _require_torrent()
    return {"items": torrent_manager.list(), "available": True}


@app.post("/api/torrents/add")
def torrent_add(req: TorrentAddRequest) -> dict:
    _require_torrent()
    try:
        return torrent_manager.add(
            uri=req.uri, name=req.name or None, paused=req.paused,
            save_path=req.save_path or None,
            file_priorities={int(k): int(v) for k, v in req.file_priorities.items()} or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/torrents/add-file")
async def torrent_add_file(
    torrent: UploadFile | None = _FastAPIFile(default=None),
    name: str = Form(default=""),
    paused: bool = Form(default=False),
    save_path: str = Form(default=""),
) -> dict:
    _require_torrent()
    if not torrent:
        raise HTTPException(status_code=400, detail="未收到 .torrent 文件")
    data = await torrent.read()
    if not data:
        raise HTTPException(status_code=400, detail=".torrent 文件为空")
    try:
        return torrent_manager.add(
            torrent_data=data, name=name or None, paused=paused, save_path=save_path or None,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/torrents/{tid}")
def torrent_detail(tid: str) -> dict:
    _require_torrent()
    item = torrent_manager.get(tid)
    if not item:
        raise HTTPException(status_code=404, detail="种子不存在")
    return item


@app.post("/api/torrents/{tid}/pause")
def torrent_pause(tid: str) -> dict:
    _require_torrent()
    if not torrent_manager.pause(tid):
        raise HTTPException(status_code=404, detail="种子不存在")
    return {"paused": True}


@app.post("/api/torrents/{tid}/resume")
def torrent_resume(tid: str) -> dict:
    _require_torrent()
    if not torrent_manager.resume(tid):
        raise HTTPException(status_code=404, detail="种子不存在")
    return {"paused": False}


@app.post("/api/torrents/{tid}/remove")
def torrent_remove(tid: str, req: TorrentRemoveRequest) -> dict:
    _require_torrent()
    if not torrent_manager.remove(tid, delete_files=req.delete_files):
        raise HTTPException(status_code=404, detail="种子不存在")
    return {"removed": True}


@app.post("/api/torrents/{tid}/files")
def torrent_set_files(tid: str, req: TorrentFilesRequest) -> dict:
    _require_torrent()
    if not torrent_manager.set_file_priorities(tid, {int(k): int(v) for k, v in req.priorities.items()}):
        raise HTTPException(status_code=404, detail="种子不存在或尚无元数据")
    return {"updated": True}


# ---- 字幕处理：在线字幕提取 / 内嵌字幕抽取 / 硬字幕烧录 / 可选 LLM 翻译 ----

class SubListRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    cookie: str = Field(default="", max_length=8192)


class SubExtractRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    lang: str = Field(min_length=1, max_length=16)
    cookie: str = Field(default="", max_length=8192)


class SubBurnRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    sub_rel: str = Field(min_length=1, max_length=256)  # 相对视频目录的字幕文件名


class SubTranslateRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    sub_rel: str = Field(min_length=1, max_length=256)
    api_key: str = Field(default="", max_length=512)
    base_url: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)
    target: str = Field(default="简体中文", max_length=32)


def _resolve_lib_video(lib_id: str) -> Path:
    p = library_mod._resolve_safe(DOWNLOAD_DIR, lib_id)
    if not p or p.suffix.lower() not in library_mod.VIDEO_EXTS:
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return p


@app.post("/api/subtitles/list")
def sub_list(req: SubListRequest) -> dict:
    video = _resolve_lib_video(req.lib_id)
    meta = library_mod._load_sidecar(video)
    subs = subtitles_mod.list_online_subs(meta.get("source_url") or "", req.cookie)
    return {"subs": subs}


@app.post("/api/subtitles/extract")
def sub_extract(req: SubExtractRequest) -> dict:
    video = _resolve_lib_video(req.lib_id)
    meta = library_mod._load_sidecar(video)
    out_dir = video.parent
    sub = subtitles_mod.extract_online_sub(
        meta.get("source_url") or "", req.lang, req.cookie, "", out_dir, meta.get("title") or ""
    )
    if not sub and not meta.get("source_url"):
        # 无源链接则尝试抽内嵌字幕流
        sub = subtitles_mod.extract_embedded_subs(video, out_dir, FFMPEG_BIN)
    if not sub or not sub.exists():
        raise HTTPException(status_code=404, detail="未找到该语言的字幕（源站无此字幕且无内嵌字幕流）")
    rel = sub.relative_to(out_dir).as_posix()
    return {"sub_rel": rel, "lang": req.lang, "size": sub.stat().st_size}


@app.post("/api/subtitles/burn")
def sub_burn(req: SubBurnRequest) -> dict:
    video = _resolve_lib_video(req.lib_id)
    out_dir = video.parent
    sub_path = (out_dir / req.sub_rel).resolve()
    if out_dir.resolve() not in sub_path.parents or not sub_path.exists():
        raise HTTPException(status_code=404, detail="字幕文件不存在")
    out = subtitles_mod.burn_subtitle(video, sub_path, FFMPEG_BIN)
    if not out:
        raise HTTPException(status_code=500, detail="烧录失败，请检查字幕文件格式")
    meta = library_mod._load_sidecar(video)
    subtitles_mod._write_subtitle_sidecar(out, meta)
    new_id = library_mod.encode_id(out.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
    return {"lib_id": new_id, "name": out.name, "title": (meta.get("title") or out.stem) + "（字幕版）"}


@app.post("/api/subtitles/translate")
def sub_translate(req: SubTranslateRequest) -> dict:
    video = _resolve_lib_video(req.lib_id)
    out_dir = video.parent
    sub_path = (out_dir / req.sub_rel).resolve()
    if out_dir.resolve() not in sub_path.parents or not sub_path.exists():
        raise HTTPException(status_code=404, detail="字幕文件不存在")
    text = sub_path.read_text(encoding="utf-8", errors="ignore")
    # 用统一 LLM 配置做 fallback：用户在前端留空时自动取已保存的 Key/URL/Model
    llm = get_llm_config()
    api_key = req.api_key or llm.get("api_key", "")
    base_url = req.base_url or llm.get("base_url", "")
    model = req.model or llm.get("model", "")
    try:
        translated = subtitles_mod.translate_srt(text, api_key, base_url, model, req.target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    t = (req.target or "简体中文").strip().lower()
    if any(k in t for k in ("zh", "chinese", "中", "简")):
        ext = "zh"
    else:
        ext = re.split(r"[^a-z]", t)[0][:4] or "zh"
    base_stem = re.sub(r"\.(zh|en|ja|ko|fr|de|es|ru|pt|it)$", "", sub_path.stem, flags=re.I)
    new_path = sub_path.with_name(f"{base_stem}.{ext}.srt")
    new_path.write_text(translated, encoding="utf-8")
    return {"sub_rel": new_path.relative_to(out_dir).as_posix(), "lang": req.target, "text": translated}


# ---- LLM 服务商选择器 API（统一配置，前端面板持久化）----

class LLMConfigRequest(BaseModel):
    provider: str = Field(default="openai", max_length=32)
    api_key: str = Field(default="", max_length=256)
    base_url: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)


@app.get("/api/llm/providers")
def llm_providers() -> dict:
    """返回可用的提供商预设（供前端下拉菜单）。"""
    return {"providers": PROVIDER_PRESETS, "default": DEFAULT_PROVIDER}


@app.get("/api/llm/config")
def llm_config_get() -> dict:
    """返回当前 LLM 配置（前端面板回填）。api_key 脱敏返回，仅显示首尾各 4 位。"""
    cfg = get_llm_config()
    key = cfg.get("api_key", "")
    if len(key) > 8:
        cfg["api_key"] = key[:4] + "****" + key[-4:]
    return cfg


@app.post("/api/llm/config")
def llm_config_save(req: LLMConfigRequest) -> dict:
    """保存 LLM 配置。如果前端传了脱敏的 api_key(含 ****)则沿用已有 Key 不覆盖。"""
    current = get_llm_config()
    data = {
        "provider": req.provider,
        "api_key": req.api_key if "****" not in (req.api_key or "") else current.get("api_key", ""),
        "base_url": req.base_url,
        "model": req.model,
        "max_tokens": current.get("max_tokens", 4096),
        "temperature": current.get("temperature", 0.7),
    }
    save_llm_config(data)
    return {"ok": True}


# ---- 格式 / 片段增强：对已下载媒体做本地 ffmpeg 加工（转音频 / GIF / 裁剪 / 压缩 / 放大）----
# 与字幕处理同源（基于 lib_id）；产物落源目录并写侧车 → 媒体库自动可见。

class ProcessRequest(BaseModel):
    # 单个文件（向后兼容）
    lib_id: str = Field(default="", max_length=2048)
    # 批量文件
    lib_ids: list[str] = Field(default_factory=list)
    op: str = Field(min_length=1, max_length=16)
    params: dict = Field(default_factory=dict)


@app.post("/api/process/run")
def process_run(req: ProcessRequest) -> dict:
    if not (getattr(sys, "frozen", False) or os.environ.get("VDL_LIBRARY_ENABLED")):
        raise HTTPException(status_code=403, detail="当前部署未启用本地加工功能")
    if req.op not in ("audio", "gif", "trim", "crop", "compress", "upscale",
                      "frame", "frames", "sheet", "ringtone", "dewatermark",
                      "ai_dewatermark"):
        raise HTTPException(status_code=400, detail="不支持的处理类型")

    # 解析来源：lib_ids 批量优先，否则单个 lib_id
    skipped = []
    if req.lib_ids:
        sources = []
        skipped = []
        for lid in req.lib_ids:
            if not lid or not lid.strip():
                continue
            p = library_mod._resolve_safe(DOWNLOAD_DIR, lid.strip())
            if not p or not p.is_file():
                skipped.append(lid)
                continue
            sources.append((lid.strip(), p))
        if not sources:
            raise HTTPException(status_code=400, detail="lib_ids 中没有有效文件")
    elif req.lib_id:
        p = library_mod._resolve_safe(DOWNLOAD_DIR, req.lib_id)
        if not p or not p.is_file():
            raise HTTPException(status_code=404, detail="源文件不存在")
        sources = [(req.lib_id, p)]
    else:
        raise HTTPException(status_code=400, detail="请提供 lib_id 或 lib_ids")

    import uuid as _uuid
    jobs_out = []
    for lid, src_path in sources:
        jid = _uuid.uuid4().hex[:12]
        name = src_path.name
        process_queue.submit(jid, name, lid, req.op, _run_process, jid, str(src_path), req.op, req.params or {})
        jobs_out.append({"job_id": jid, "lib_id": lid, "name": name})

    if len(jobs_out) == 1 and not skipped:
        return {"job_id": jobs_out[0]["job_id"], "status": "running"}
    result = {"jobs": jobs_out, "total": len(jobs_out), "status": "queued"}
    if skipped:
        result["skipped"] = skipped
        result["skipped_count"] = len(skipped)
    return result


@app.get("/api/process/queue")
def process_queue_list() -> dict:
    if not (getattr(sys, "frozen", False) or os.environ.get("VDL_LIBRARY_ENABLED")):
        raise HTTPException(status_code=403, detail="当前部署未启用本地加工功能")
    return process_queue.get_queue()


@app.post("/api/process/concurrency")
def process_set_concurrency(req: dict = None) -> dict:
    if not (getattr(sys, "frozen", False) or os.environ.get("VDL_LIBRARY_ENABLED")):
        raise HTTPException(status_code=403, detail="当前部署未启用本地加工功能")
    n = int((req or {}).get("n", process_queue.concurrency))
    process_queue.set_concurrency(n)
    return {"concurrency": process_queue.concurrency}


@app.get("/api/process/{job_id}")
def process_status(job_id: str) -> dict:
    with process_queue.lock:
        job = process_queue.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="处理任务不存在")
    steps = job.get("steps") or []
    if not steps:
        steps = [{"name": "处理中", "status": "running" if job["status"] == "running" else "done",
                  "detail": "", "created_at": time.time(), "updated_at": time.time()}]
    return {"status": job["status"], "error": job.get("error", ""),
            "lib_id": job.get("lib_id", ""), "name": job.get("name", ""),
            "count": job.get("count", 0), "is_dir": job.get("is_dir", False),
            "steps": steps, "logs": job.get("logs", [])}


def _run_ai_dewatermark(job_id: str, src: str, params: dict) -> None:
    """AI 去水印：调 watermark-removal worker（HTTP 或本地 subprocess），轮询完成。"""
    import json, time as _time
    import requests as _requests

    x = int(params.get("x", 0) or 0)
    y = int(params.get("y", 0) or 0)
    w = int(params.get("w", 100) or 100)
    h = int(params.get("h", 50) or 50)
    band = int(params.get("band", 5) or 5)
    mode = str(params.get("mode", "auto") or "auto")

    try:
        if AI_DEWATERMARK_MODE == "http":
            resp = _requests.post(
                f"{AI_DEWATERMARK_ENDPOINT}/render",
                json={"video": src, "x": x, "y": y, "w": w, "h": h, "band": band, "mode": mode},
                headers={"X-Worker-Token": AI_DEWATERMARK_TOKEN} if AI_DEWATERMARK_TOKEN else {},
                timeout=30,
            )
            resp.raise_for_status()
            worker_job = resp.json()["job_id"]
        else:
            # local 模式：subprocess 调 process.py
            proc_dir = AI_DEWATERMARK_DIR or (Path(__file__).resolve().parent.parent / "watermark-removal")
            proc = proc_dir / "process.py"
            if not proc.exists():
                raise RuntimeError(f"AI 去水印管线未找到：{proc}")
            import subprocess as _sp
            result = _sp.run(
                [AI_DEWATERMARK_PYTHON, str(proc), src,
                 "--x", str(x), "--y", str(y), "--w", str(w), "--h", str(h),
                 "--band", str(band), "--mode", mode],
                capture_output=True, text=True, timeout=AI_DEWATERMARK_TIMEOUT,
                env=dict(os.environ, E2FGVI_BASE=str(proc_dir / "E2FGVI")),
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "AI 去水印失败")
            # local 模式同步完成，直接走收尾
            out = Path(src).parent / f"{Path(src).stem}_AI去水印.mp4"
            if not out.exists():
                raise RuntimeError("AI 去水印未生成输出文件")
            meta = library_mod._load_sidecar(Path(src))
            fftools._write_sidecar(out, meta, "AI去水印")
            new_id = library_mod.encode_id(out.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
            with process_queue.lock:
                process_queue.jobs[job_id].update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)
            logger.info("ai_dewatermark %s done -> %s", job_id, out.name)
            return

        # HTTP 模式：轮询 worker 直到完成
        deadline = _time.time() + AI_DEWATERMARK_TIMEOUT
        while _time.time() < deadline:
            _time.sleep(5)
            st = _requests.get(
                f"{AI_DEWATERMARK_ENDPOINT}/status/{worker_job}",
                headers={"X-Worker-Token": AI_DEWATERMARK_TOKEN} if AI_DEWATERMARK_TOKEN else {},
                timeout=10,
            ).json()
            if st["status"] == "completed":
                break
            if st["status"] == "failed":
                raise RuntimeError(st.get("error", "AI 去水印 worker 失败"))
        else:
            raise RuntimeError("AI 去水印超时")

        # 下载成片
        dl = _requests.get(
            f"{AI_DEWATERMARK_ENDPOINT}/file/{worker_job}",
            headers={"X-Worker-Token": AI_DEWATERMARK_TOKEN} if AI_DEWATERMARK_TOKEN else {},
            timeout=60,
        )
        dl.raise_for_status()
        out = Path(src).parent / f"{Path(src).stem}_AI去水印.mp4"
        out.write_bytes(dl.content)

        meta = library_mod._load_sidecar(Path(src))
        fftools._write_sidecar(out, meta, "AI去水印")
        new_id = library_mod.encode_id(out.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
        with process_queue.lock:
            process_queue.jobs[job_id].update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)
        logger.info("ai_dewatermark %s (http) done -> %s", job_id, out.name)

    except Exception as e:
        with process_queue.lock:
            process_queue.jobs[job_id]["status"] = "failed"
            process_queue.jobs[job_id]["error"] = str(e)[:400]
        logger.warning("ai_dewatermark %s failed: %s", job_id, e)


def _process_log(job_id: str, line: str) -> None:
    if not line:
        return
    with process_queue.lock:
        job = process_queue.jobs.get(job_id)
        if not job:
            return
        ts = time.strftime("%H:%M:%S", time.localtime())
        job.setdefault("logs", []).append(f"{ts}  {line.strip()}")
        if len(job.get("logs", [])) > 200:
            job["logs"][:] = job["logs"][-200:]


def _process_set_step(job_id: str, idx: int, status: str, detail: str = "") -> None:
    with process_queue.lock:
        job = process_queue.jobs.get(job_id)
        if not job:
            return
        steps = job.get("steps") or []
        now = time.time()
        for s in steps[:idx]:
            if s["status"] == "pending":
                s["status"] = "done"
                s["updated_at"] = now
        if 0 <= idx < len(steps):
            steps[idx]["status"] = status
            steps[idx]["detail"] = detail[:200]
            steps[idx]["updated_at"] = now


def _run_process(job_id: str, src: str, op: str, params: dict) -> None:
    """后台线程：按 op 调用 ffmpeg_tools，产物落源目录并写侧车，更新 process_queue.jobs。"""
    with process_queue.lock:
        job = process_queue.jobs.get(job_id)
    if not job:
        return
    _process_set_step(job_id, 1, "done", f"源文件: {Path(src).name}")
    _process_set_step(job_id, 2, "running", f"操作: {op}")
    _process_log(job_id, f"开始处理: {op} -> {src}")
    try:
        src_path = Path(src)
        out_dir = src_path.parent
        meta = library_mod._load_sidecar(src_path)
        p = params or {}
        out = None
        suffix = ""
        if op == "audio":
            out = fftools.extract_audio(src_path, out_dir,
                                       fmt=str(p.get("fmt", "mp3")),
                                       bitrate=str(p.get("bitrate", "192k")),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = "音频"
        elif op == "gif":
            out = fftools.make_gif(src_path, out_dir,
                                  start=float(p.get("start", 0) or 0),
                                  duration=float(p.get("duration", 5) or 5),
                                  fps=int(p.get("fps", 12) or 12),
                                  width=int(p.get("width", 480) or 480),
                                  ffmpeg_bin=FFMPEG_BIN)
            suffix = "动图"
        elif op == "trim":
            out = fftools.trim_video(src_path, out_dir,
                                    start=float(p.get("start", 0) or 0),
                                    end=float(p.get("end", 0) or 0),
                                    reencode=bool(p.get("reencode", True)),
                                    ffmpeg_bin=FFMPEG_BIN)
            suffix = "片段"
        elif op == "crop":
            out = fftools.crop_video(src_path, out_dir,
                                    crop_expr=str(p.get("crop_expr", "")),
                                    ffmpeg_bin=FFMPEG_BIN)
            suffix = "裁剪"
        elif op == "compress":
            sh = int(p.get("scale_h", 720) or 720)
            out = fftools.compress_video(src_path, out_dir, scale_h=sh,
                                        crf=int(p.get("crf", 28) or 28),
                                        ffmpeg_bin=FFMPEG_BIN)
            suffix = f"压缩{sh}p"
        elif op == "upscale":
            fac = float(p.get("factor", 2) or 2)
            out = fftools.upscale_video(src_path, out_dir, factor=fac,
                                       sharpen=bool(p.get("sharpen", True)),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = f"放大{fac}x"
        elif op == "frame":
            out = fftools.snapshot(src_path, out_dir,
                                  at=float(p.get("at", 1) or 0),
                                  fmt=str(p.get("fmt", "jpg")),
                                  width=int(p.get("width", 0) or 0),
                                  ffmpeg_bin=FFMPEG_BIN)
            suffix = "封面"
        elif op == "sheet":
            out = fftools.contact_sheet(src_path, out_dir,
                                       rows=int(p.get("rows", 3) or 3),
                                       cols=int(p.get("cols", 4) or 4),
                                       width=int(p.get("width", 1280) or 1280),
                                       duration=float(meta.get("duration") or 0),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = "预览图"
        elif op == "ringtone":
            out = fftools.make_ringtone(src_path, out_dir,
                                       start=float(p.get("start", 0) or 0),
                                       duration=float(p.get("duration", 30) or 30),
                                       fmt=str(p.get("fmt", "m4r")),
                                       fade=float(p.get("fade", 1) or 0),
                                       ffmpeg_bin=FFMPEG_BIN)
            suffix = "铃声"
        elif op == "dewatermark":
            out = fftools.remove_watermark(src_path, out_dir,
                                          x=int(p.get("x", 0) or 0),
                                          y=int(p.get("y", 0) or 0),
                                          w=int(p.get("w", 100) or 100),
                                          h=int(p.get("h", 50) or 50),
                                          show=bool(p.get("show", False)),
                                          band=int(p.get("band", 10) or 10),
                                          ffmpeg_bin=FFMPEG_BIN)
            if out and bool(p.get("show", False)):
                # show 模式：仅画框不做处理，不写侧车，提示用户再指定位置提交
                _process_set_step(job_id, 3, "done", f"预览框: {out.name}")
                _process_log(job_id, f"dewatermark-show 完成: {out.name}")
                with process_queue.lock:
                    job.update(status="completed", out_path=str(out), lib_id="", name=out.name)
                logger.info("process %s (dewatermark-show) done", job_id)
                return
            suffix = "去水印"
        elif op == "ai_dewatermark":
            _run_ai_dewatermark(job_id, str(src_path), p)
            return  # 异步 worker，不在此处收尾
        elif op == "frames":
            # 批量抽帧：产物是一个子目录（不是单文件），单独收尾
            res = fftools.extract_frames(src_path, out_dir,
                                        start=float(p.get("start", 0) or 0),
                                        end=float(p.get("end", 0) or 0),
                                        interval=float(p.get("interval", 1) or 1),
                                        limit=int(p.get("limit", 200) or 200),
                                        fmt=str(p.get("fmt", "jpg")),
                                        width=int(p.get("width", 0) or 0),
                                        ffmpeg_bin=FFMPEG_BIN)
            if not res:
                raise RuntimeError("未抽到任何帧（检查起止时间是否超出视频时长）")
            frames_dir, count = res
            _process_set_step(job_id, 3, "done", f"{count} 帧 -> {frames_dir.name}")
            _process_log(job_id, f"抽帧完成: {count} 帧")
            with process_queue.lock:
                job.update(status="completed", out_path=str(frames_dir), lib_id="",
                           name=frames_dir.name, count=count, is_dir=True)
            logger.info("process %s (frames) done -> %s (%d 帧)", job_id, frames_dir.name, count)
            return
        else:
            raise ValueError("不支持的处理类型")
        if not out or not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("处理未产出有效文件")
        fftools._write_sidecar(out, meta, suffix)
        new_id = library_mod.encode_id(out.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
        _process_set_step(job_id, 3, "done", f"输出: {out.name}")
        _process_log(job_id, f"处理完成: {out.name}")
        with process_queue.lock:
            job.update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)
        logger.info("process %s (%s) done -> %s", job_id, op, out.name)
    except Exception as e:  # noqa: BLE001
        _process_set_step(job_id, 2, "error", str(e)[:200])
        _process_log(job_id, f"处理失败: {e}")
        with process_queue.lock:
            job["status"] = "failed"
            job["error"] = str(e)[:400]
        logger.warning("process %s (%s) failed: %s", job_id, op, e)


# ---- 订阅监控：关注频道/UP 主，自动下载新发布的视频 ----

def _run_subscription_check(sub: "subs_mod.Subscription") -> dict:
    """探查频道新视频并加入下载队列；更新已知 id 基线。返回结果摘要。

    基线策略：仅把"成功创建下载任务"的视频 id 计入 last_video_ids，
    解析失败的 id 不计入（下次 check 仍可重试）；任务最终失败由用户在下载面板手动重试。
    """
    items = subs_mod.probe_channel(sub.url, sub.cookie, sub.proxy, limit=SUBSCRIBE_PROBE_LIMIT)
    known = set(sub.last_video_ids)
    fresh = [it for it in items if it["id"] not in known]
    task_ids: list[str] = []
    failed: list[str] = []
    submitted_ids: set[str] = set()
    for it in fresh:
        if not it.get("url"):
            continue
        try:
            url, platform = parse_source(it["url"])
        except (UnsupportedPlatformError, LinkError):
            failed.append(it["title"])
            continue
        task = store.create(
            url=url, title=it["title"], platform=platform.name,
            quality=downloader.quality_label(sub.quality_key), quality_key=sub.quality_key,
        )
        scheduler.submit(downloader.run_download, task, store, sub.quality_key, sub.cookie, sub.proxy, SINGLE_DOWNLOAD_RETRIES)
        task_ids.append(task.id)
        submitted_ids.add(it["id"])
    # 合并基线（保留最近最多 200 条），仅计入成功提交任务的 id
    new_baseline = list(sub.last_video_ids)
    for vid in submitted_ids:
        if vid not in new_baseline:
            new_baseline.append(vid)
    sub_store.update(sub.id, last_video_ids=new_baseline[:200], last_checked=time.time())
    return {"sub_id": sub.id, "checked": len(items), "new_videos": fresh, "task_ids": task_ids, "failed": failed}


class SubscribeRequest(BaseModel):
    url: str = Field(min_length=4, max_length=8192)
    name: str = Field(default="", max_length=128)
    quality: str = Field(default=downloader.BEST_KEY, max_length=16)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)
    auto_check: bool = True


@app.get("/api/subscriptions")
def list_subscriptions() -> dict:
    return {"subscriptions": [s.to_public_dict() for s in sub_store.list_all()], "enabled": SUB_ENABLED}


@app.post("/api/subscriptions")
def add_subscription(payload: SubscribeRequest) -> dict:
    if not SUB_ENABLED:
        raise HTTPException(status_code=403, detail="当前部署未启用订阅功能")
    if not downloader.is_valid_quality(payload.quality):
        raise HTTPException(status_code=400, detail="不支持的清晰度选项")
    url, platform = parse_source(payload.url)  # 校验为已知公开平台
    sid = uuid.uuid4().hex[:TASK_ID_LENGTH]
    # 首次添加只记录基线（不下载历史视频），之后发布的新视频才自动下载
    items = subs_mod.probe_channel(url, payload.cookie, payload.proxy, limit=SUBSCRIBE_PROBE_LIMIT)
    baseline = [it["id"] for it in items][:200]
    sub = subs_mod.Subscription(
        id=sid, url=url, name=payload.name or platform.name,
        platform=platform.name, quality_key=payload.quality,
        quality_label=downloader.quality_label(payload.quality),
        cookie=payload.cookie, proxy=payload.proxy, auto_check=payload.auto_check,
        last_video_ids=baseline, last_checked=time.time(), created_at=time.time(),
    )
    sub_store.add(sub)
    return sub.to_public_dict()


@app.delete("/api/subscriptions/{sub_id}")
def remove_subscription(sub_id: str) -> dict:
    if not sub_store.remove(sub_id):
        raise HTTPException(status_code=404, detail="订阅不存在")
    return {"deleted": True}


@app.post("/api/subscriptions/{sub_id}/check")
def check_subscription_route(sub_id: str) -> dict:
    sub = sub_store.get(sub_id)
    if not sub:
        raise HTTPException(status_code=404, detail="订阅不存在")
    try:
        result = _run_subscription_check(sub)
    except Exception as exc:  # noqa: BLE001
        logger.exception("订阅 %s 手动检查失败", sub_id)
        raise HTTPException(status_code=502, detail=f"探查频道失败：{str(exc)[:200]}")
    return result


def _subscription_watchdog() -> None:
    """后台常驻：周期性对开启 auto_check 的订阅探查新视频并下载。"""
    while True:
        time.sleep(SUB_CHECK_INTERVAL)
        try:
            for sub in sub_store.list_all():
                if not sub.auto_check:
                    continue
                try:
                    _run_subscription_check(sub)
                except Exception:
                    logger.exception("订阅 %s 自动检查失败", sub.id)
        except Exception:
            logger.exception("订阅 watchpod 异常")


if SUB_ENABLED:
    _sub_watchdog = threading.Thread(target=_subscription_watchdog, name="vdl-sub-watchdog", daemon=True)
    _sub_watchdog.start()


def _retention_watchdog() -> None:
    """后台常驻：按 interval_hours 周期执行时效清理。

    启动后先静置 120 秒再首跑 —— 避免「刚开 App 就开始删东西」，也给用户留出
    改配置的窗口。总开关 auto_enabled 关闭时只空转，不碰磁盘。
    """
    time.sleep(120)
    while True:
        cfg = retention_store.get()
        interval = max(0.25, float(cfg.interval_hours or 6.0)) * 3600
        if not cfg.auto_enabled:
            time.sleep(min(interval, 1800))
            continue
        try:
            result = retention_mod.run(DOWNLOAD_DIR, cfg)
            if result["removed"] or result["failed"]:
                logger.info("自动清理：移除 %s 项，释放 %s，失败 %s 项",
                            result["removed"], retention_mod.human_size(result["freed"]),
                            result["failed"])
                retention_store.update(last_run=result["ran_at"], last_freed=result["freed"],
                                       last_removed=result["removed"])
        except Exception:
            logger.exception("自动清理执行异常")
        time.sleep(interval)


if RETENTION_ENABLED:
    _ret_watchdog = threading.Thread(target=_retention_watchdog, name="vdl-retention", daemon=True)
    _ret_watchdog.start()


def _archive_watchdog() -> None:
    """后台常驻：按 interval_hours 周期把新文件自动归档到用户网盘。

    启动后静置 180 秒再首跑 —— 刚开 App 时下载/加工可能正忙，不跟它抢上行带宽。
    auto_enabled 关闭或凭据没配好时只空转，不发任何网络请求。
    """
    time.sleep(180)
    while True:
        cfg = archive_store.get()
        interval = max(0.25, float(cfg.interval_hours or 6.0)) * 3600
        if not cfg.auto_enabled or not archive_store.has_creds(cfg.provider):
            time.sleep(min(interval, 1800))
            continue
        try:
            items = library_mod.scan_library(DOWNLOAD_DIR)
            pend = archive_mod.pending_items(items, cfg, archive_store)
            if pend:
                upload_fn, creds = _archive_provider(cfg)
                result = archive_mod.run_archive(
                    DOWNLOAD_DIR, pend, cfg, archive_store,
                    uploader=upload_fn, creds=creds,
                    trash=retention_mod.move_to_trash, trash_ok=retention_mod.trash_available,
                )
                logger.info("自动归档：上传 %s 个 / %s，失败 %s 个",
                            result["uploaded"], result["bytes_text"], result["failed"])
                archive_store.update(last_run=result["ran_at"],
                                     last_uploaded=result["uploaded"],
                                     last_failed=result["failed"])
        except HTTPException as exc:
            logger.warning("自动归档跳过：%s", exc.detail)
        except Exception:
            logger.exception("自动归档执行异常")
        time.sleep(interval)


if ARCHIVE_ENABLED:
    _arc_watchdog = threading.Thread(target=_archive_watchdog, name="vdl-archive", daemon=True)
    _arc_watchdog.start()


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
