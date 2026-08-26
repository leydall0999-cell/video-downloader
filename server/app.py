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
import urllib.request
import requests  # 解说 worker HTTP 模式客户端（VDL_COMMENTARY_MODE=http 时用到）
import time
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import platform_model as plat

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
    baidu_qr_create,
    baidu_qr_poll,
    baidu_qr_status,
)
from platforms import CHINA_DOMAINS, LinkError, UnsupportedPlatformError, is_china_host, parse_source, platform_catalog
from tasks import TaskStore, TASK_ID_LENGTH
from llm_config import inject_llm_env, get_llm_config, save_llm_config, PROVIDER_PRESETS, DEFAULT_PROVIDER
from commentary_config import inject_commentary_env

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

# ---- 本地视频上传转码（需求文档模块一）：接收上传文件直接转码，复用上面的 ffmpeg 管线 ----
UPLOAD_TMP = DOWNLOAD_DIR / "uploads"
UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
# 上传文件大小上限（字节），默认 2GB，可用 VDL_UPLOAD_MAX_BYTES 覆盖
UPLOAD_MAX_BYTES = int(os.environ.get("VDL_UPLOAD_MAX_BYTES") or 2_000_000_000)
# 允许上传的视频后缀白名单
UPLOAD_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v", ".ts", ".wmv", ".mpeg", ".mpg"}

# ---- PDF / 图片去水印（需求文档模块二）：接收上传图片/PDF 做去水印，依赖 cv2/fitz（缺则降级） ----
DW_DIR = DOWNLOAD_DIR / "dewatermark"
DW_DIR.mkdir(parents=True, exist_ok=True)
DW_JOBS: dict[str, dict] = {}
DW_LOCK = threading.Lock()

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
if not _COMMENTARY_EXPLICIT and plat.is_desktop():
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
BAIDU_APP_ID = os.environ.get("VDL_BAIDU_APP_ID", "").strip()  # AppID（≠AppKey），OAuth device_id 必需
BAIDU_ENABLED = bool(BAIDU_APP_KEY and BAIDU_APP_SECRET and BAIDU_REDIRECT_URI and BAIDU_APP_ID)
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
# 解说 work 目录保留天数（每个 job 是 12 位 hex 目录：upload 副本 + 中间 wav/脚本）。
# 没有清理机制时会长年累积（实测达 21GB），这里按保留期自动清理，可用
# VDL_COMMENTARY_WORK_RETENTION_DAYS 覆盖（默认 7 天）。
COMMENTARY_WORK_RETENTION_DAYS = max(1, int(os.environ.get("VDL_COMMENTARY_WORK_RETENTION_DAYS", "7") or 7))
commentary_jobs: dict[str, dict] = {}
_commentary_lock = threading.Lock()



# ---- AI 去水印（E2FGVI worker，桌面版可选）：local subprocess 或 http worker ----
AI_DEWATERMARK_ENABLED = bool(
    plat.is_desktop()
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
SUB_ENABLED = plat.is_desktop() or bool(os.environ.get("VDL_SUBSCRIPTIONS_ENABLED"))
SUBSCRIBE_PROBE_LIMIT = int(os.environ.get("VDL_SUBSCRIBE_PROBE_LIMIT", "100") or 100)
SUB_CHECK_INTERVAL = int(os.environ.get("VDL_SUB_CHECK_INTERVAL", "1800") or 1800)  # 默认 30 分钟
sub_store = subs_mod.SubscriptionStore(DOWNLOAD_DIR / ".subscriptions.json")
prober = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PROBES, thread_name_prefix="vdl-probe")
cloud_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vdl-cloud")  # 云盘上传独立线程池，避免挤占下载

# ---- 时效自动清理（桌面版功能）：按保留期/容量上限清理下载目录 ----
# 与媒体库同一开关：只有桌面版（或显式开 VDL_LIBRARY_ENABLED）才管理本地磁盘。
RETENTION_ENABLED = (
    plat.is_desktop()
    or bool(os.environ.get("VDL_LIBRARY_ENABLED"))
    or bool(os.environ.get("VDL_RETENTION_ENABLED"))
)
retention_store = retention_mod.RetentionStore(DOWNLOAD_DIR / ".retention.json")

# ---- 一键归档网盘（桌面版功能）：把媒体库文件按模板批量/自动传到用户自己的网盘 ----
# 配置含明文凭据，刻意放在 home 配置目录而不是下载目录 —— 避免用户把整个下载目录
# 同步/打包到网盘时连带泄露密码。
ARCHIVE_ENABLED = (
    plat.is_desktop()
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
    plat.is_desktop()
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
    plat.is_desktop()
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
        if plat.is_desktop() and py == sys.executable:
            if os.environ.get("VDL_COMMENTARY_BUNDLED") == "1" or COMMENTARY_MODE == "bundled":
                return True
            return False
        # 开发/测试模式（非 frozen 且未显式设 VDL_COMMENTARY_PYTHON）信任当前解释器
        if "VDL_COMMENTARY_PYTHON" not in os.environ and not plat.is_desktop():
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
        # 解说/配音音量手动可调配置：注入旁白响度/原声压低/旁白增益
        inject_commentary_env(env)
        return env

    def ready(self) -> bool:
        return not self.issues


COMMENTARY_RT = _CommentaryRuntime()


def _commentary_work_dir() -> Path:
    d = COMMENTARY_WORK_DIR / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _purge_commentary_work() -> int:
    """清理解说 work 目录里超过保留期的旧 job（防无限累积）。

    安全约束：
      1. 只删 12 位 hex 命名的 job 目录（不碰其他目录/文件）；
      2. 跳过 commentary_jobs 中仍 running 的 src_path 对应目录；
      3. 只删 mtime 早于保留期（COMMENTARY_WORK_RETENTION_DAYS，默认 7 天）的目录。
    返回删除数量。
    """
    if not COMMENTARY_WORK_DIR.exists():
        return 0
    cutoff = time.time() - COMMENTARY_WORK_RETENTION_DAYS * 86400
    hex12 = re.compile(r"^[0-9a-f]{12}$")
    running_dirs = set()
    try:
        with _commentary_lock:
            for job in commentary_jobs.values():
                sp = str(job.get("src_path") or "")
                if sp.startswith(str(COMMENTARY_WORK_DIR)):
                    running_dirs.add(os.path.dirname(sp))
    except Exception:
        pass
    removed = 0
    for d in COMMENTARY_WORK_DIR.iterdir():
        try:
            if not d.is_dir() or not hex12.match(d.name):
                continue
            if str(d) in running_dirs:
                continue
            if d.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
        except Exception:
            continue
    if removed:
        logger.info("已清理 %s 个过期解说 work 目录（保留 %s 天）", removed, COMMENTARY_WORK_RETENTION_DAYS)
    return removed





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


def _commentary_run(job_id: str, src_path: str, vertical: bool, voice: str, edit_only: str | None = None, script_only: bool = False, trim_start: float = 0.0, trim_end: float = 0.0, mode: str | None = None, commentary_type: str = "deep_hl", highlight_source: str = "ai", intro_highlight: bool = False, skip_intro_outro: bool = False, no_narrate_intro_outro: bool = True, retain_pct: float | None = None, web: bool = False, one_click: bool = False, title: str = "", style: str = "none", src_filename: str = "") -> None:
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
            # 成片文件名用剧名/标题（而非 job_id 哈希名），与 auto 分支一致
            if title:
                args += ["--out-name", title]
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
            # 成片文件名用剧名/标题（而非 job_id 哈希名），避免成片名是一串无意义 hash
            if title:
                args += ["--out-name", title]
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
            # 优先展示用户上传时的原始文件名，upload 端点会把磁盘文件名统一改名为 upload.<ext>
            # 所以 src_path.basename 永远是 upload.mp4 这种占位名，用 src_filename 兜出真实名
            display_src = (src_filename or "").strip() or Path(src_path).name
            steps[0]["detail"] = f"源视频: {display_src}" + (
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
            # 成片命名：优先按 --out-name（剧名/标题）清洗后的前缀查找；
            # 找不到再退回 job_id(base) 兜底（旧任务或标题为空时）。
            out_stem = _safe_output_stem(title) or base
            candidates = sorted(
                (p for p in out_dir.glob(f"{out_stem}*.mp4") if p.name != in_file.name),
                key=lambda p: p.stat().st_mtime, reverse=True,
            )
            if not candidates:
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
        # 解说 work 目录保留期清理（防 21GB 级无限累积）
        try:
            await asyncio.to_thread(_purge_commentary_work)
        except Exception:
            logger.exception("解说 work 清理失败")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    orphans = store.purge_orphans()
    if orphans:
        logger.info("已清理 %s 个上次运行遗留的任务目录", orphans)
    # 启动即清一次解说 work 过期目录（周期清理在 _cleanup_loop 内）
    try:
        await asyncio.to_thread(_purge_commentary_work)
    except Exception:
        logger.exception("启动时解说 work 清理失败")
    # 桌面版种子下载：启动 libtorrent session（libtorrent 缺失时内部为空操作）
    if TORRENT_ENABLED and torrent_mod.available():
        try:
            torrent_manager.start()
        except Exception:
            logger.exception("启动种子下载管理器失败")
    cleaner = asyncio.create_task(_cleanup_loop())
    # 公共 Cookie 池后台探测（chrqj 等需登录态的站，避免公共池静默失效）
    try:
        _start_cookie_pool_watchdog()
    except Exception:
        logger.exception("启动公共 Cookie 池探测失败")
    yield
    cleaner.cancel()
    if TORRENT_ENABLED:
        try:
            torrent_manager.stop()
        except Exception:
            pass
    # 关闭时终止「AI 解说体验」tab 拉起的 NarratoAI 子进程
    try:
        _narrato_rtr.stop()
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
    # 前端 /api/resolve 已解析出的视频标题（剧集名+单集标题），创建任务时直接写入，
    # 避免下载完成前解说取不到 title 而 fallback 成无意义 hash/upload。
    title: str = Field(default="", max_length=256)
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


class ConvertRequest(BaseModel):
    """格式转换请求（与 routers/convert.py 共享，故保留在 app.py 公开契约层，不迁入 core）。"""
    task_id: str
    target: str
    resolution: str = "original"


def _require_task(task_id: str, device_id: str = ""):
    """取任务。桌面单机无设备隔离，device_id 参数仅为兼容 web 版调用签名保留。"""
    task = store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return task



def _run_convert(job_id: str, src: str, target: str, resolution: str,
                bitrate: str = "", audio: bool = True, rotate: int = 0,
                remux: bool = False, src_is_temp: bool = False) -> None:
    """后台线程：ffmpeg 转码，更新 CONVERT_JOBS 状态。
    新增参数（上传转码用）：bitrate 视频码率、audio 是否保留音轨、
    rotate 竖屏旋转(0/90/180/270)、remux 仅换容器无损(-c copy)。
    src_is_temp：src 是否为上传落盘的临时文件，True 时转码结束（成败都）
    清理，避免 UPLOAD_TMP 无限堆积；task 模式（已下载文件）必须为 False。
    """
    job = CONVERT_JOBS.get(job_id)
    if not job:
        return
    out = None
    try:
        out = Path(job["out_path"])
        cmd = [FFMPEG_BIN, "-y", "-i", src]
        audio_only = target in ("mp3", "m4a")
        if target == "gif":
            cmd += CONVERT_TARGETS["gif"]
        elif remux and rotate == 0:
            # 仅换容器无损复制，忽略码率/分辨率/旋转（旋转需滤镜，与 -c copy 不兼容）
            cmd += ["-c", "copy"]
        else:
            cmd += CONVERT_TARGETS[target]
            if not audio_only:
                vf = []
                if resolution != "original":
                    h = {"1080": "1080", "720": "720", "480": "480"}.get(resolution)
                    if h:
                        vf.append(f"scale=-2:{h}")
                if rotate in (90, 180, 270):
                    tf = {90: "transpose=1", 180: "transpose=3", 270: "transpose=2"}[rotate]
                    vf.append(tf)
                if vf:
                    cmd += ["-vf", ",".join(vf)]
                if bitrate:
                    cmd += ["-b:v", str(bitrate)]
                if not audio:
                    cmd += ["-an"]
        cmd.append(str(out))
        job["stage"] = "转码中"
        job["progress"] = 0
        # 流式读取 ffmpeg stderr，解析总时长与当前进度，实时回写进度百分比
        _re_dur = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
        _re_time = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
        total_dur = 0.0
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, bufsize=1, text=True)
        for line in proc.stderr:
            if not total_dur:
                m = _re_dur.search(line)
                if m:
                    total_dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            m = _re_time.search(line)
            if m and total_dur > 0:
                cur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                job["progress"] = int(min(100, max(0, cur / total_dur * 100)))
        proc.wait(timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg 执行失败")
        if not out.exists() or out.stat().st_size == 0:
            raise RuntimeError("ffmpeg 未产出有效文件")
        # 可选：存入媒体库（DOWNLOAD_DIR 磁盘目录，scan_library 会自动收录）
        if job.get("to_library"):
            dest = DOWNLOAD_DIR / out.name
            if dest.exists() and dest.resolve() != out.resolve():
                stem = out.stem + f"_{uuid.uuid4().hex[:6]}"
                dest = DOWNLOAD_DIR / f"{stem}{out.suffix}"
            shutil.copy2(out, dest)
            try:
                job["library_id"] = library_mod.encode_id(
                    dest.resolve().relative_to(DOWNLOAD_DIR.resolve()).as_posix())
            except Exception:
                job["library_id"] = ""
        job["status"] = "completed"
        logger.info("convert %s done -> %s", job_id, out.name)
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)[:400]
        logger.warning("convert %s failed: %s", job_id, e)
    finally:
        # 上传临时源文件：转码结束（无论成败）后清理，避免 UPLOAD_TMP 无限堆积
        if src_is_temp and src:
            try:
                _src = Path(src)
                if _src.exists() and (out is None or _src.resolve() != out.resolve()):
                    _src.unlink(missing_ok=True)
            except Exception:
                logger.warning("convert %s cleanup temp src failed: %s", job_id, src)



@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """兜底：任何未被具体 handler 覆盖的异常，向前端返回可读的错误原因。

    默认 FastAPI（debug=False）只会返回笼统的 "Internal Server Error"，
    排查时拿不到真实堆栈。这里把异常类型+消息透传给前端，并写服务端日志，
    让用户（双击启动 App 也能）直接看到 500 的真实成因。
    """
    logger.exception("未捕获异常 %s %s: %s", request.method, request.url.path, exc)
    # 不要拦截 FastAPI 自身的 HTTPException（如 402 订阅提示、400 参数错误）
    from fastapi import HTTPException as _HTTPException

    if isinstance(exc, _HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    msg = f"{type(exc).__name__}: {str(exc)[:200]}"
    return JSONResponse(
        status_code=500,
        content={
            "error": msg,
            "hint": "服务端未预期错误，请把上方错误信息反馈，或查看服务端日志",
            "detail": msg,
        },
    )


@app.exception_handler(LinkError)
async def handle_link_error(_: Request, exc: LinkError) -> JSONResponse:
    status = 415 if isinstance(exc, UnsupportedPlatformError) else 400
    content = {"error": exc.message, "hint": exc.hint}
    # 诊断增强：把错误分类与脱敏后的上下文透传给前端，便于精准提示与线上排查
    category = getattr(exc, "category", None) or "unknown"
    content["category"] = category
    ctx = getattr(exc, "context", None) or {}
    if ctx:
        content["diag"] = {
            "host": ctx.get("host"),
            "is_china": ctx.get("is_china"),
            "is_hardened": ctx.get("is_hardened"),
            "cookie_source": ctx.get("cookie_source"),
            "proxy_used": ctx.get("proxy_used"),
            "is_cloud": ctx.get("is_cloud"),
        }
    return JSONResponse(status_code=status, content=content)


@app.exception_handler(downloader.ResolveRestricted)
async def handle_restricted(_: Request, exc: "downloader.ResolveRestricted") -> JSONResponse:
    # 受限内容属于"确认无解"，用 422 与网络/解析异常区分开
    content = {"error": exc.message, "hint": exc.hint}
    content["category"] = getattr(exc, "category", None) or "restricted"
    return JSONResponse(status_code=422, content=content)



# --------------------------------------------------------------------------- #
# 文件系统辅助（桌面版便捷入口）
# --------------------------------------------------------------------------- #

class OpenPathRequest(BaseModel):
    """打开本地目录/文件。仅允许白名单路径（下载目录及其子项）。"""
    path: str = Field(default="", max_length=4096)




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










class CommentaryRenameReq(BaseModel):
    name: str










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


def _probe_video_title(src_path) -> str:
    """用 ffprobe 读取视频 `format.tags.title`（mp4 自带的标题元数据）。

    用途：用户从本地拖拽/选择视频上传时，源文件名常常是「upload.mp4」「VID_xxx.mp4」
    这类无语义名字，导致成片名变成「upload-解说成片...」。mp4 自带的 title 标签
    （yt-dlp/官方下载工具一般会写入）才是真正的剧名/视频标题，优先用它做片名前缀。

    失败/无标签/找不到 ffprobe → 返回空串（调用方继续 fallback 到文件名）。
    """
    try:
        import json as _json
        src_str = str(src_path)
        if not src_str or not Path(src_str).exists():
            return ""
        # PATH 优先解析；找不到时合并 _CommentaryRuntime 探测到的 ffmpeg_dir
        path = os.environ.get("PATH", "") or ""
        ffprobe_bin = shutil.which("ffprobe", path=path) or ""
        if not ffprobe_bin:
            try:
                ffmpeg_dir = getattr(_COMMENTARY_RUNTIME, "ffmpeg_dir", "") or ""
                if ffmpeg_dir:
                    ffprobe_bin = shutil.which("ffprobe", path=ffmpeg_dir + os.pathsep + path) or ""
            except Exception:
                pass
        if not ffprobe_bin:
            return ""
        r = subprocess.run(
            [ffprobe_bin, "-v", "quiet", "-print_format", "json", "-show_format", src_str],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0 or not r.stdout:
            return ""
        data = _json.loads(r.stdout)
        tags = ((data.get("format") or {}).get("tags") or {})
        return (tags.get("title") or "").strip()
    except Exception:
        return ""


def _commentary_title(payload: "CommentaryRequest", src_path: str) -> str:
    """为解说任务推导剧名锚点：优先用下载任务的标题，否则退回源文件名（下载文件名通常含剧集名）。"""
    if getattr(payload, "task_id", ""):
        try:
            t = _require_task(payload.task_id)
            tt = (getattr(t, "title", "") or "").strip()
            if tt:
                return tt
            # 兜底 1：任务步骤里可能已记录「已获取标题《...》」，从中反解。
            # 某些旧任务/解析路径在创建时 title 为空，但下载过程中写入了步骤详情。
            for step in getattr(t, "steps", []) or []:
                detail = (step.get("detail") or "").strip()
                if detail.startswith("已获取标题《") and detail.endswith("》"):
                    tt = detail[len("已获取标题《"):-1].strip()
                    if tt:
                        return tt
            # 兜底 2：从 URL 同步轻量解析一次标题（避免 fallback 到无意义 hash）。
            url = getattr(t, "url", "") or ""
            if url:
                try:
                    info = downloader.probe(url, "", "")
                    tt = (downloader.summarize(info).get("title") or "").strip()
                    if tt:
                        # 顺手回填任务，避免下次再解析
                        store.update(t.id, title=tt)
                        return tt
                except Exception:
                    pass
        except Exception:
            pass
    return Path(src_path).stem or ""


def _meaningful_stem(s: str) -> bool:
    """判断上传文件名 stem 是否「有语义、值得做片名前缀」。

    用户 2026-08-25 明确：片名用「上传前的名字」。但源文件叫 upload.mp4、
    VID_xxx、纯数字或 job_id 哈希（ee6b7cf95736）这类无语义名不能直接上，
    需走 ffprobe/短码兜底。
    """
    s = (s or "").strip()
    if not s or s.lower() == "upload":
        return False
    if s.isdigit():
        return False
    if 12 <= len(s) <= 32 and all(c in "0123456789abcdefABCDEF" for c in s):
        return False  # 哈希形
    return True


def _safe_output_stem(title: str) -> str:
    """与 commentary-pipeline process._sanitize_filename 对齐的成片文件前缀清洗。

    process.py 用 --out-name 的清洗结果作为 final_output_name 的前缀；app 端查找成片时
    必须用同一规则还原该前缀，否则按 job_id 查找会落空（标题里的中文/空格/特殊字符被清洗后
    与原始 title 不同）。
    """
    import re as _re
    s = (title or "").strip()
    s = _re.sub(r'[\\/:\*?"<>\|\x00-\x1f]', "", s)
    s = s.rstrip(" .")
    if not s:
        return ""
    return s[:100]










# ---- 配音试听 / 预览全部 ----

def _commentary_ffmpeg_bin() -> str:
    """返回解说管线可用的 ffmpeg 路径：优先用 CommentaryRuntime 探测到的捆绑 ffmpeg，
    否则回退到全局 FFMPEG_BIN（格式转换模块同款）。"""
    d = getattr(COMMENTARY_RT, "ffmpeg_dir", "") or ""
    if d:
        cand = os.path.join(d, "ffmpeg")
        if os.path.exists(cand):
            return cand
    return FFMPEG_BIN


def _build_loudness_filter(loudness: str | None, boost: str | None) -> str:
    """构造旁白响度后处理的 ffmpeg 音频滤镜串。

    - loudness="off"/空：不做响度标准化，仅用 boost 增益兜底
    - loudness 为数字（如 "-14"）：loudnorm 标准化到该 LUFS 目标
    - boost 额外线性增益（默认 1.0，夹取 0.5~2.0）
    - 末尾统一加 alimiter 限幅，防止拉响导致破音
    """
    ln = (loudness or "").strip().lower()
    do_loudnorm = ln not in ("", "off")
    if do_loudnorm:
        try:
            lv = float(ln)
        except ValueError:
            raise ValueError(f"loudness 参数无效: {loudness!r}（应为 -18~-10 或 off）")
        filt = f"loudnorm=I={lv:.1f}:TP=-1.0:LRA=11:linear=true,"
    else:
        filt = ""
    bv = 1.0
    if boost not in (None, ""):
        try:
            bv = float(boost)
        except (TypeError, ValueError):
            bv = 1.0
    bv = max(0.5, min(2.0, bv))
    return filt + f"volume={bv:.2f},alimiter=limit=0.98:level=disabled"


def _apply_narration_loudness(src_mp3: Path, loudness: str | None, boost: str | None) -> None:
    """对已有旁白 mp3 原地做响度标准化 + 增益（试听即所得，与成片 edit_ffmpeg 一致）。

    失败不致命：保留原 TTS 音频，仅打印告警，不让试听整体失败。
    """
    ff = _commentary_ffmpeg_bin()
    if not ff or not os.path.exists(ff):
        print(f"  [试听] 未找到 ffmpeg（{ff}），跳过响度处理，返回原始旁白")
        return
    try:
        filt = _build_loudness_filter(loudness, boost)
    except ValueError as e:
        print(f"  [试听] 响度参数错误，跳过后处理：{e}")
        return
    tmp = src_mp3.with_suffix(".loud.mp3")
    cmd = [ff, "-y", "-i", str(src_mp3), "-af", filt, "-ar", "44100", "-ac", "2", str(tmp)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        print(f"  [试听] ffmpeg 响度处理异常，跳过：{e}")
        if tmp.exists():
            tmp.unlink()
        return
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 100:
        src_mp3.unlink()
        tmp.rename(src_mp3)
    else:
        msg = (proc.stderr or proc.stdout or "").strip()[-300:]
        print(f"  [试听] ffmpeg 响度处理失败，保留原始旁白：{msg}")
        if tmp.exists():
            tmp.unlink()


def _run_voice_preview(text: str, voice: str, output_mp3: Path, timeout: int = 60,
                       loudness: str | None = None, boost: str | None = None) -> None:
    """用 edge-tts 把一段文本转成指定音色的 mp3。

    bundled 模式：edge_tts 已随包冻结，直接 in-process 调用，避开「subprocess 跑
    .py 脚本 vs PyInstaller frozen exe」的兼容性问题。
    dev 模式：edge_tts 装在 commentary-pipeline .venv 里，subprocess 到 COMMENTARY_RT.python。

    loudness/boost（试听「配音与音量」设置用）：非 None 时对生成好的旁白再做
    ffmpeg 响度标准化 + 增益，使试听与成片响度一致。
    """
    if not COMMENTARY_RT.ready():
        raise RuntimeError("解说环境未就绪：" + "；".join(COMMENTARY_RT.issues))

    # bundled 模式：桌面打包态，edge_tts 已冻结进 exe，直接 in-process 调用
    if plat.is_desktop():
        _run_voice_preview_inprocess(text, voice, output_mp3, timeout=timeout)
    else:
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

    # 音量后处理（试听「配音与音量」设置时才带 loudness/boost）
    if loudness is not None or boost is not None:
        _apply_narration_loudness(output_mp3, loudness, boost)


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






# --------------------------------------------------------------------------- #
# 云盘集成（增值能力）：把已下载文件存到用户自己的网盘（WebDAV / 百度网盘）
# --------------------------------------------------------------------------- #

class CloudSaveRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=16)
    dest_path: str = Field(default="", max_length=1024)
    webdav: dict = Field(default_factory=dict)
    baidu: dict = Field(default_factory=dict)








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




def _baidu_safe_name(name: str) -> str:
    """取网盘文件名的纯文件名部分，剔除路径穿越字符。"""
    s = (name or "").strip()
    if not s or s in ("undefined", "(null)", "None"):
        s = "file"
    base = Path(s).name
    return base or "file"






# ── 百度网盘「分享链接下载」（登录后转存到自己网盘再下，官方通道）──────────
class BaiduShareListRequest(BaseModel):
    url: str = ""
    pwd: str = ""
    dir: str = ""           # 分享内子目录（空=根），用于点击文件夹展开




class BaiduShareDownloadRequest(BaseModel):
    url: str = ""
    pwd: str = ""
    path: str = ""        # 分享内文件路径（来自 share/list 的 path 字段）
    name: str = ""
    token: str = ""       # 可选；缺省回退到本机持久化的令牌
    backend: str = ""     # 空=auto（优先 aria2c 并发，缺失回退 requests）
    # 以下来自 share/list 响应，传入后可跳过重复 verify（避免百度限频）
    sekey: str = ""       # list 返回的 sekey（randsk）
    share_id: int | None = None  # list 返回的 share_id
    uk: int | None = None        # list 返回的 uk
    fs_id: int | None = None     # 要下载的文件的 fs_id（list items 里）
    bduss: str = ""       # 用户提供的百度 BDUSS Cookie（用于高速直链下载）
    dlink: str = ""        # 前端通过 WebView 注入 JS 预取的直链（优先级最高，跳过 transfer+dlink 策略）




# ── 百度令牌本机持久化（每个用户各自存自己机器，重启后免重复授权）────────


class BaiduTokenSet(BaseModel):
    access_token: str = ""
    expires_in: int | None = None
    scope: str = ""
    refresh_token: str = ""






# ── 百度扫码登录（自动获取 BDUSS，免手动复制 Cookie）────────────────










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


def _prune_crypto_jobs() -> None:
    if len(CRYPTO_JOBS) <= 200:
        return
    with CRYPTO_LOCK:
        done = [jid for jid, j in CRYPTO_JOBS.items()
                if j.get("status") in ("completed", "failed", "canceled")]
        for jid in (done[:-50] if len(done) > 50 else []):
            CRYPTO_JOBS.pop(jid, None)




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










# ---- LLM 服务商选择器 API（统一配置，前端面板持久化）----

class LLMConfigRequest(BaseModel):
    provider: str = Field(default="openai", max_length=32)
    api_key: str = Field(default="", max_length=256)
    base_url: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)
    reasoning_effort: str = Field(default="low", max_length=16)
    offpeak_only: bool = Field(default=False)


class CommentaryConfigRequest(BaseModel):
    """解说(配音/音量)手动可调设置。"""
    # 数值型 LUFS（-18~-10）或字符串 "off"。不能用 Any —— 在 PEP 563
    # （from __future__ import annotations）下，Pydantic v2 的 TypeAdapter
    # 无法为 Any 构建完全定义的 core schema，保存端点会抛
    # "is not fully defined; you should define" 错误。
    narration_loudness: int | float | str = Field(default=-14)
    original_duck: float = Field(default=0.10)       # 0.05~0.30
    narration_boost: float = Field(default=1.0)      # 1.0~1.6








# ---- 格式 / 片段增强：对已下载媒体做本地 ffmpeg 加工（转音频 / GIF / 裁剪 / 压缩 / 放大）----
# 与字幕处理同源（基于 lib_id）；产物落源目录并写侧车 → 媒体库自动可见。

class ProcessRequest(BaseModel):
    # 单个文件（向后兼容）
    lib_id: str = Field(default="", max_length=2048)
    # 批量文件
    lib_ids: list[str] = Field(default_factory=list)
    op: str = Field(min_length=1, max_length=16)
    params: dict = Field(default_factory=dict)










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


# ---- 百度网盘直链下载（油猴脚本 → 本接口 → aria2c）----
# 油猴脚本在用户已登录的浏览器里拦截百度 API 拿到 dlink，POST 到这里。
# 后端用 aria2c 多线程下载到本地，彻底摆脱 app 内 WebView 注入 BDUSS 的不稳定链路。
class BaiduDlinkRequest(BaseModel):
    dlink: str = Field(..., min_length=10, max_length=8192)
    filename: str = Field(default="", max_length=255)




# ---- 百度网盘下载（baiduPCS-Go 适配器，替代脆弱的 WebView 注入）----
try:
    import baidu_pcs  # noqa: E402
except Exception as _pcs_err:  # pragma: no cover
    logger.warning("baidu_pcs 加载失败，百度网盘(PCS-Go)功能不可用: %s", _pcs_err)
    baidu_pcs = None

try:
    import baidu_qr  # noqa: E402
    if baidu_pcs is not None:
        baidu_qr.PCS_LOGIN = baidu_pcs
except Exception as _qr_err:  # pragma: no cover
    logger.warning("baidu_qr 加载失败，扫码登录不可用: %s", _qr_err)
    baidu_qr = None

_pcs_tasks: dict[str, dict] = {}
_pcs_lock = threading.Lock()


























# --------------------------------------------------------------------------- #
# App 完整版路由（app-dev）：挂载全部业务模块
#   包含：core(解析/下载) / crypto / fs / cloud(WebDAV 云盘) /
#     convert(视频转换) / dewatermark(PDF/图片去水印) /
#     commentary(视频解说) / library(媒体库) / retention(时效清理) / archive /
#     torrents / subtitles / llm / process / subscriptions /
#     baidu_dlink(百度直链) / pcs(百度网盘 PCS)
#   公共 Cookie 池：代码内联于本文件后部，含接收端 /api/cookie/sync、
#     本机 from-local、查询 status、清理 cache/clear 及后台探测 watchdog。
# 必须在 app.mount("/", StaticFiles) 之前 include，否则 "/" 挂载会前缀匹配吞掉 /api/* 路由
from routers import crypto as _crypto_rtr
app.include_router(_crypto_rtr.router)
from routers import fs as _fs_rtr
app.include_router(_fs_rtr.router)
from routers import convert as _convert_rtr
app.include_router(_convert_rtr.router)
from routers import dewatermark as _dewatermark_rtr
app.include_router(_dewatermark_rtr.router)
from routers import cloud as _cloud_rtr
app.include_router(_cloud_rtr.router)
from routers import core as _core_rtr
app.include_router(_core_rtr.router)
from routers import commentary as _commentary_rtr
app.include_router(_commentary_rtr.router)
from routers import library as _library_rtr
app.include_router(_library_rtr.router)
from routers import retention as _retention_rtr
app.include_router(_retention_rtr.router)
from routers import archive as _archive_rtr
app.include_router(_archive_rtr.router)
from routers import torrents as _torrents_rtr
app.include_router(_torrents_rtr.router)
from routers import subtitles as _subtitles_rtr
app.include_router(_subtitles_rtr.router)
from routers import llm as _llm_rtr
app.include_router(_llm_rtr.router)
from routers import process as _process_rtr
app.include_router(_process_rtr.router)
from routers import subscriptions as _subscriptions_rtr
app.include_router(_subscriptions_rtr.router)
from routers import baidu_dlink as _baidu_dlink_rtr
app.include_router(_baidu_dlink_rtr.router)
from routers import pcs as _pcs_rtr
app.include_router(_pcs_rtr.router)
from routers import narrato as _narrato_rtr
app.include_router(_narrato_rtr.router)

# —— 公共 Cookie 池 + 本机 Cookie 缓存（来自 main 分支，合并时保留）——
# 与「仅本机个人缓存」(cookie_cache.py) 严格隔离：独立存储目录、仅白名单域、入池前验真。
_SYNC_RL = {"ts": {}, "lock": threading.Lock()}


def _sync_rate_ok(ip: str) -> bool:
    """单 IP 30 秒内至多一次，防滥用。"""
    now = time.time()
    with _SYNC_RL["lock"]:
        last = _SYNC_RL["ts"].get(ip, 0)
        if now - last < 30:
            return False
        _SYNC_RL["ts"][ip] = now
        return True


_COOKIE_ALERT_TS: dict[str, float] = {}


def _cookie_pool_alert(domain: str) -> None:
    # 节流：同域 1 小时内只告警一次，避免每次 resolve 刷屏（正常空池是常态噪音）
    _now = time.time()
    if _now - _COOKIE_ALERT_TS.get(domain, 0) < 3600:
        return
    _COOKIE_ALERT_TS[domain] = _now
    msg = (f"[cookie_pool] 域名 {domain} 公共 Cookie 池已空/全部失效，"
           f"网页版 chrqj 将 403，请补充 Cookie（App 端『同步 Cookie 到云端』或设 CHRQJ_COOKIE）")
    logger.warning(msg)
    try:
        alert = Path.home() / ".videodownloader" / "cookie_pool_alert.json"
        alert.write_text(json.dumps({"domain": domain, "ts": int(time.time()), "empty": True}, ensure_ascii=False))
    except Exception:
        pass
    wh = os.environ.get("VDL_COOKIE_ALERT_WEBHOOK")
    if wh:
        try:
            requests.post(wh, json={"text": msg}, timeout=10)
        except Exception:
            pass


def _cookie_pool_watchdog() -> None:
    while True:
        try:
            from cookie_pool import verify_and_prune, all_domains
            for d in all_domains():
                try:
                    if verify_and_prune(d) == 0:
                        _cookie_pool_alert(d)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(1800)  # 30 分钟一轮


def _start_cookie_pool_watchdog() -> None:
    t = threading.Thread(target=_cookie_pool_watchdog, daemon=True)
    t.start()


@app.post("/api/cookie/cache/clear")
def cookie_cache_clear() -> dict:
    """清除本机 Cookie 缓存（仅删 ~/.videodownloader/cookies，不影响浏览器本身）。"""
    from cookie_cache import clear_cookie_cache
    n = clear_cookie_cache()
    return {"ok": True, "cleared": n}


@app.post("/api/cookie/sync")
def cookie_sync(payload: dict, request: Request) -> dict:
    """App 端上报指定站点登录态到公共池（需用户知情同意 + 客户端令牌）。"""
    token = os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    supplied = (payload or {}).get("token", "")
    if not token or supplied != token:
        raise HTTPException(status_code=403, detail="无效令牌")
    domain = (payload or {}).get("domain", "")
    cookie = (payload or {}).get("cookie", "")
    if not domain or not cookie:
        raise HTTPException(status_code=400, detail="缺少 domain/cookie")
    from cookie_pool import add_cookie, verify_cookie, is_allowed
    if not is_allowed(domain):
        raise HTTPException(status_code=400, detail="不支持的域名")
    ip = (request.client.host if request.client else "") or ""
    if not _sync_rate_ok(ip):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    ok = verify_cookie(domain, cookie)
    if ok is False:
        raise HTTPException(status_code=400, detail="Cookie 无效，未能通过目标站验真")
    added = add_cookie(domain, cookie, source="sync")
    return {"ok": True, "added": added, "verified": (ok is True)}


@app.post("/api/cookie/sync/from-local")
def cookie_sync_from_local(payload: dict, request: Request) -> dict:
    """仅本机(App 端)调用：读取本机浏览器指定站点登录态并上报到公共池。"""
    ip = (request.client.host if request.client else "") or ""
    if ip not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="仅允许本机调用")
    from cookie_pool import add_cookie, verify_cookie, is_allowed, _norm_domain
    domain = _norm_domain((payload or {}).get("domain") or "chrqj.com")
    if not is_allowed(domain):
        raise HTTPException(status_code=400, detail="不支持的域名")
    header = downloader.get_browser_cookie_header(domain, f"https://{domain}/")
    if not header:
        raise HTTPException(
            status_code=400,
            detail=f"本机浏览器未检测到 {domain} 登录态，请先在浏览器登录该站",
        )
    ok = verify_cookie(domain, header)
    if ok is False:
        raise HTTPException(status_code=400, detail="本机 Cookie 无效，未能通过目标站验真")
    added = add_cookie(domain, header, source="local")
    return {"ok": True, "added": added, "verified": (ok is True)}


def _cloud_sync_config() -> tuple[str, str]:
    """返回 (sync_url, token)。优先读 env，其次读本机配置文件。

    配置文件路径：~/.videodownloader/cloud_sync.json = {"url": "...", "token": "..."}
    桌面版用此配置把本机浏览器登录态推送到网页版(Railway)公共池。
    """
    url = (os.environ.get("VDL_COOKIE_SYNC_URL") or "").strip()
    token = (os.environ.get("VDL_COOKIE_SYNC_TOKEN") or "").strip()
    cfg = Path.home() / ".videodownloader" / "cloud_sync.json"
    if (not url or not token) and cfg.exists():
        try:
            d = json.loads(cfg.read_text(encoding="utf-8"))
            url = url or (d.get("url") or "").strip()
            token = token or (d.get("token") or "").strip()
        except Exception:
            pass
    return url, token


def _ensure_vps_env() -> None:
    """桌面 App 启动时注入 VPS 解析配置：经线上转发端点复用 VPS worker。

    桌面端无本地隧道，若未显式配置 VDL_WORKER_URL，则复用 cloud_sync 配置
    （~/.videodownloader/cloud_sync.json 的 url/token，即推送用的线上地址）：
      - VDL_WORKER_URL    = <线上地址>（如 https://hanyuxz.top）
      - VDL_WORKER_PROXY  = "off"（直连线上，禁用 18889 本地隧道默认代理）
      - VDL_COOKIE_SYNC_TOKEN = <token>（/v1/resolve 转发端点校验用）
    这样 downloader._call_vps_worker 对 14 个 VPS 平台（抖音/快手/微博/爱奇艺/
    红果/微视/1905…）自动走线上转发，桌面 App 零额外配置获得 VPS 解析能力。
    """
    if os.environ.get("VDL_WORKER_URL"):
        return  # 已显式配置，不覆盖
    url, token = _cloud_sync_config()
    if not url:
        return
    if not os.environ.get("VDL_WORKER_PROXY"):
        os.environ["VDL_WORKER_PROXY"] = "off"
    if not os.environ.get("VDL_COOKIE_SYNC_TOKEN") and token:
        os.environ["VDL_COOKIE_SYNC_TOKEN"] = token
    os.environ["VDL_WORKER_URL"] = url.rstrip("/")
    logger.info("[vps] 桌面端注入 VPS 转发配置: %s", os.environ["VDL_WORKER_URL"])


_ensure_vps_env()


def _push_cookie_to_cloud(domain: str, header: str, url: str, token: str) -> dict:
    """把单站 Cookie 推送到云端公共池（POST /api/cookie/sync）。

    带浏览器 User-Agent 以绕过 Cloudflare 对 Python-urllib 的拦截（error 1010）。
    对 429/5xx 做有限指数退避重试，降低连发导致的限流。
    """
    import time
    import urllib.request
    import urllib.error

    target = url.rstrip("/") + "/api/cookie/sync"
    body = json.dumps({"token": token, "domain": domain, "cookie": header}).encode()
    last_err: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(
            target,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
        except Exception as e:
            last_err = e
            raise
    raise last_err or RuntimeError("推送 Cookie 到云端失败")


@app.post("/api/cookie/sync/to-cloud")
def cookie_sync_to_cloud(request: Request) -> dict:
    """仅本机(App 端)调用：读取本机浏览器各强反爬站的登录态并推送到网页版公共池。

    这是「网页版由桌面版共享登录态」的核心：桌面版常驻时，自动把抖音/B站/快手等
    登录态推送到 Railway 公共池，网页版访客无需手动粘贴即可复用。
    """
    ip = (request.client.host if request.client else "") or ""
    if ip not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="仅允许本机调用")
    from cookie_pool import _root_domains, is_allowed, _norm_domain
    import time
    url, token = _cloud_sync_config()
    if not url or not token:
        raise HTTPException(
            status_code=400,
            detail="未配置云端同步地址/令牌。请在桌面版环境设置 VDL_COOKIE_SYNC_URL 与 "
                   "VDL_COOKIE_SYNC_TOKEN，或写入 ~/.videodownloader/cloud_sync.json",
        )
    results = []
    domains = [d for d in sorted(_root_domains()) if d != "chrqj.com"]
    for idx, domain in enumerate(domains):
        try:
            header = downloader.get_browser_cookie_header(domain, f"https://{domain}/")
        except Exception:
            header = None
        if not header:
            results.append({"domain": domain, "pushed": False, "reason": "本机未登录"})
            continue
        try:
            resp = _push_cookie_to_cloud(domain, header, url, token)
            results.append({
                "domain": domain,
                "pushed": bool(resp.get("ok")),
                "verified": resp.get("verified"),
            })
        except Exception as e:
            results.append({"domain": domain, "pushed": False, "reason": str(e)[:120]})
        # 连发易触发 Cloudflare/Railway 限流，站间间隔 1s（最后一站除外）
        if idx < len(domains) - 1:
            time.sleep(1.0)
    pushed = sum(1 for r in results if r.get("pushed"))
    return {"ok": True, "pushed": pushed, "total": len(results), "results": results}


@app.post("/api/cookie/contribute")
def cookie_contribute(payload: dict, request: Request) -> dict:
    """访客自愿把本次登录态贡献到公共池（需前端显式勾选 + 后端验真）。

    仅白名单域名；单 IP 限频；verify_cookie 验真（明确无效才拒，网络不可达放行）。
    优酷等站可一并贡献 ckey（播放签名），供 UPS 专用通道使用。
    """
    from urllib.parse import urlparse
    url = (payload or {}).get("url", "")
    cookie = (payload or {}).get("cookie", "")
    ckey = (payload or {}).get("ckey", "")
    host = urlparse(url).netloc if url else ""
    from cookie_pool import add_cookie, add_ckey, is_allowed, verify_cookie, _norm_domain, _strip_sub
    domain = _strip_sub(_norm_domain(host))
    if not domain or not is_allowed(domain):
        raise HTTPException(status_code=400, detail="该平台暂不支持公共池贡献")
    cookie = (cookie or "").strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="缺少 cookie")
    if domain == "bilibili.com" and "=" not in cookie:
        cookie = f"SESSDATA={cookie}"
    ip = (request.client.host if request.client else "") or ""
    if not _sync_rate_ok(ip):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    ok = verify_cookie(domain, cookie)
    if ok is False:
        raise HTTPException(
            status_code=400,
            detail="Cookie 无效，未能通过目标站验真（请确认已登录且为完整 Cookie；"
                   "优酷需含登录态字段）",
        )
    added = add_cookie(domain, cookie, source="contrib")
    if ckey:
        add_ckey(domain, ckey, source="contrib")
        added = True
    logger.info("[cookie_pool] contrib domain=%s ip=%s added=%s ckey=%s", domain, ip, added, bool(ckey))
    return {"ok": True, "added": added, "verified": (ok is True), "ckey": bool(ckey)}


@app.get("/api/cookie/status")
def cookie_status(url: str = "") -> dict:
    """查询某链接是否需要 Cookie、本机浏览器是否已有可用登录态、以及公共池新鲜度。"""
    if not url:
        raise HTTPException(status_code=400, detail="请提供链接")
    _assert_safe_url(url)
    url, platform = parse_source(url)
    host = _host_of(url)
    info = downloader.detect_browser_cookie(host)
    pool_ts = None
    try:
        from cookie_pool import _candidates, _pool_file
        for d in _candidates(host):
            f = _pool_file(d)
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    for c in data.get("cookies", []):
                        ts = c.get("ts")
                        if ts and (pool_ts is None or ts > pool_ts):
                            pool_ts = ts
                except Exception:
                    pass
    except Exception:
        pass
    return {
        "host": host,
        "platform": platform.key,
        "needed": downloader.is_cookie_hardened_host(host),
        "available": info["available"],
        "browser": info["browser"],
        "profile": info["profile"],
        "pool_updated_at": pool_ts,
    }


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
