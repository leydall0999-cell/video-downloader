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
import contextlib
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import File as _FastAPIFile, Form, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import urlparse

import downloader
import library as library_mod
import subtitles as subtitles_mod
import subscriptions as subs_mod
import ffmpeg_tools as fftools
import retention as retention_mod
import archive as archive_mod
import crypto_vault as crypto_mod
import torrent as torrent_mod
from batch import BatchScheduler
from clouddrive import (
    BaiduProvider,
    CloudError,
    WebDAVProvider,
    baidu_auth_url,
    baidu_exchange_token,
    _baidu_callback_html,
)
from platforms import CHINA_DOMAINS, LinkError, UnsupportedPlatformError, is_china_host, parse_source, platform_catalog
from tasks import TaskStore, TASK_ID_LENGTH

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
FFMPEG_BIN = os.environ.get("VDL_FFMPEG_BIN") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
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
#   VDL_COMMENTARY_VOICE=zh-CN-YunxiNeural           默认配音嗓音
COMMENTARY_ENABLED = os.environ.get("VDL_COMMENTARY_ENABLED", "false").strip().lower() == "true"
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
PROCESS_JOBS: dict[str, dict] = {}
PROCESS_LOCK = threading.Lock()
_commentary_dir_raw = os.environ.get("VDL_COMMENTARY_DIR", "").strip()
COMMENTARY_DIR = Path(_commentary_dir_raw) if _commentary_dir_raw else None
COMMENTARY_PYTHON = os.environ.get("VDL_COMMENTARY_PYTHON", sys.executable)
COMMENTARY_VOICE = os.environ.get("VDL_COMMENTARY_VOICE", "zh-CN-YunxiNeural").strip() or "zh-CN-YunxiNeural"
COMMENTARY_TIMEOUT_SECONDS = int(os.environ.get("VDL_COMMENTARY_TIMEOUT", "1800") or 1800)  # 长视频渲染可能很久
# 解说 worker 调用模式：local=同机 subprocess(默认) / http=独立 HTTP worker 服务(强机独立部署)
COMMENTARY_MODE = os.environ.get("VDL_COMMENTARY_MODE", "local").strip().lower()
COMMENTARY_ENDPOINT = os.environ.get("VDL_COMMENTARY_ENDPOINT", "").strip().rstrip("/")
COMMENTARY_TOKEN = os.environ.get("VDL_COMMENTARY_TOKEN", "").strip()  # 与 worker 的 WORKER_TOKEN 对应
_HERE = Path(__file__).resolve().parent
_COMMENTARY_OUT_RAW = os.environ.get("VDL_COMMENTARY_LOCAL_OUTPUT", "").strip()
COMMENTARY_LOCAL_OUTPUT = Path(_COMMENTARY_OUT_RAW) if _COMMENTARY_OUT_RAW else (_HERE.parent / "commentary_out")
COMMENTARY_LOCAL_OUTPUT.mkdir(parents=True, exist_ok=True)
commentary_jobs: dict[str, dict] = {}
_commentary_lock = threading.Lock()

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
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
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
torrent_manager = torrent_mod.TorrentManager(DOWNLOAD_DIR)


def _vault_load() -> dict | None:
    try:
        if VAULT_PATH.exists():
            return json.loads(VAULT_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _vault_save(vault: dict) -> None:
    VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = VAULT_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(vault, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(VAULT_PATH)
    try:
        os.chmod(VAULT_PATH, 0o600)
    except OSError:
        pass


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

def _commentary_run(job_id: str, src_path: str, vertical: bool, voice: str) -> None:
    """后台线程：把下载好的视频喂给 commentary-pipeline，等成片回传。

    复用用户现成的 process.py 整条管线（whisper 转写 → edge-tts 配音 → ffmpeg 出片），
    本函数只负责文件桥接与成片定位。算力由解说 worker 独立承担，不影响下载服务。
    HTTP 模式(VDL_COMMENTARY_MODE=http)下转发给独立 worker 服务。
    """
    if COMMENTARY_MODE == "http":
        return _commentary_run_http(job_id, src_path, vertical, voice)
    try:
        base = job_id  # 用 job_id 作安全 ascii 文件名，避开中文/空格对 process.py 路径处理的干扰
        in_dir = COMMENTARY_DIR / "input"
        out_dir = COMMENTARY_DIR / "output"
        in_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        in_file = in_dir / f"{base}.mp4"
        if in_file.exists() or in_file.is_symlink():
            in_file.unlink()
        try:
            os.symlink(src_path, in_file)
        except OSError:
            shutil.copyfile(src_path, in_file)  # 跨挂载点软链失败则退化为复制

        args = [COMMENTARY_PYTHON, "process.py", str(in_file), "--auto"]
        if vertical:
            args.append("--vertical")
        if voice:
            args += ["--voice", voice]
        proc = subprocess.run(
            args, cwd=str(COMMENTARY_DIR), capture_output=True, text=True,
            timeout=COMMENTARY_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "解说管线执行失败").strip()[-800:])

        # 成片命名：<base>_成片.mp4 或 <base>_竖屏成片.mp4
        candidates = sorted(
            (p for p in out_dir.glob(f"{base}*.mp4") if p.name != in_file.name),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        out = next(iter(candidates), None)
        if not out:
            raise RuntimeError("解说管线执行成功但未找到成片，请检查 output/ 目录")
        with _commentary_lock:
            commentary_jobs[job_id].update(status="completed", output_path=str(out))
    except Exception as exc:  # noqa: BLE001
        with _commentary_lock:
            commentary_jobs.setdefault(job_id, {})["status"] = "failed"
            commentary_jobs[job_id]["error"] = str(exc)[:800]
        logger.exception("解说任务 %s 失败", job_id)


def _commentary_run_http(job_id: str, src_path: str, vertical: bool, voice: str) -> None:
    """HTTP 模式：把已下载视频 POST 给独立解说 worker，轮询取回成片到主站本地。"""
    endpoint = COMMENTARY_ENDPOINT
    headers = {"X-Worker-Token": COMMENTARY_TOKEN} if COMMENTARY_TOKEN else {}
    try:
        with open(src_path, "rb") as fh:
            resp = requests.post(
                f"{endpoint}/render",
                files={"video": (f"{job_id}.mp4", fh, "video/mp4")},
                data={"vertical": "true" if vertical else "false", "voice": voice},
                headers=headers,
                timeout=600,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"解说 worker /render 返回 {resp.status_code}: {resp.text[:400]}")
        wjob = resp.json().get("job_id")
        if not wjob:
            raise RuntimeError("解说 worker 未返回 job_id")

        deadline = time.time() + COMMENTARY_TIMEOUT_SECONDS
        while time.time() < deadline:
            time.sleep(5)
            st = requests.get(f"{endpoint}/status/{wjob}", headers=headers, timeout=30).json()
            status = st.get("status")
            if status == "completed":
                break
            if status == "failed":
                raise RuntimeError("解说 worker 渲染失败: " + str(st.get("error", ""))[:600])
        else:
            raise RuntimeError("解说 worker 渲染超时（超过 VDL_COMMENTARY_TIMEOUT）")

        fr = requests.get(f"{endpoint}/file/{wjob}", headers=headers, stream=True, timeout=(10, 600))
        if fr.status_code != 200:
            raise RuntimeError(f"解说 worker /file 返回 {fr.status_code}")
        out_path = COMMENTARY_LOCAL_OUTPUT / f"{job_id}.mp4"
        with open(out_path, "wb") as o:
            for chunk in fr.iter_content(1024 * 1024):
                if chunk:
                    o.write(chunk)

        with _commentary_lock:
            commentary_jobs[job_id].update(status="completed", output_path=str(out_path))
    except Exception as exc:  # noqa: BLE001
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
        "authRequired": AUTH_REQUIRED,
    }


@app.post("/api/resolve")
async def resolve(payload: ResolveRequest, request: Request) -> dict:
    _check_rate_limit(request)
    _assert_safe_url(payload.url)          # 先拦内网/环回地址，避免可疑 URL 进入解析流程
    url, platform = parse_source(payload.url)
    # 国内站直连、本就快，用更短超时；受限视频也能更快判定，不必让用户空等
    host = _host_of(url)
    timeout = RESOLVE_TIMEOUT_DOMESTIC if is_china_host(host) else RESOLVE_TIMEOUT_SECONDS
    loop = asyncio.get_running_loop()
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(prober, downloader.probe, url, payload.cookie, payload.proxy),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                f"解析超时（超过 {timeout} 秒）。常见原因是该视频为会员专享 / 付费 / 地区限制内容，"
                "或当前网络无法访问；此类受限内容通常无法解析下载"
            ),
        ) from None
    return {
        "url": url,
        "platform": {"key": platform.key, "name": platform.name},
        "video": downloader.summarize(info),
        "qualities": downloader.build_quality_options(info),
    }


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

    task = store.create(
        url=url,
        title="",
        platform=platform.name,
        quality=downloader.quality_label(payload.quality),
        quality_key=payload.quality,
    )
    scheduler.submit(downloader.run_download, task, store, payload.quality, payload.cookie, payload.proxy, SINGLE_DOWNLOAD_RETRIES)
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


@app.post("/api/batch")
def create_batch(payload: BatchRequest, request: Request) -> dict:
    _check_rate_limit(request)
    urls = [u.strip() for u in payload.urls if u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail="没有提供有效的链接")
    if not downloader.is_valid_quality(payload.quality):
        raise HTTPException(status_code=400, detail="不支持的清晰度选项")
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
    store.update(
        task_id, status="pending", error="", hint="", progress=0.0,
        downloaded_bytes=0, total_bytes=0, speed=0.0, eta=0, filesize=0, filename="",
    )
    scheduler.submit(downloader.run_download, task, store, task.quality_key, "", "", BATCH_RETRIES_DEFAULT)
    return {"task_id": task_id, "status": "pending"}


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
    return FileResponse(
        path=task.filepath,
        filename=task.filepath.name,
        media_type="application/octet-stream",
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


# --------------------------------------------------------------------------- #
# 自动解说（增值功能）：下载完 → 一键生成解说成片。壳，逻辑全在 commentary-pipeline
# --------------------------------------------------------------------------- #

class CommentaryRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=64)
    vertical: bool = True
    voice: str = Field(default="", max_length=64)


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
    task = _require_task(payload.task_id)
    if task.status != "completed" or not task.filepath or not task.filepath.exists():
        raise HTTPException(status_code=409, detail="下载任务尚未完成，无法生成解说")
    job_id = uuid.uuid4().hex[:12]
    with _commentary_lock:
        commentary_jobs[job_id] = {"status": "running", "error": "", "output_path": ""}
    executor.submit(_commentary_run, job_id, str(task.filepath), payload.vertical, payload.voice or COMMENTARY_VOICE)
    return {"job_id": job_id, "status": "running"}


@app.get("/api/commentary/{job_id}")
def commentary_status(job_id: str) -> dict:
    with _commentary_lock:
        job = commentary_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="解说任务不存在或已过期")
    return {"job_id": job_id, "status": job["status"], "error": job.get("error", ""),
            "ready": job["status"] == "completed"}


@app.get("/api/commentary/{job_id}/file")
def commentary_file(job_id: str) -> FileResponse:
    with _commentary_lock:
        job = commentary_jobs.get(job_id)
    if not job or job["status"] != "completed" or not job.get("output_path"):
        raise HTTPException(status_code=409, detail="成片尚未就绪")
    path = Path(job["output_path"])
    if not path.exists():
        raise HTTPException(status_code=410, detail="成片文件已清理")
    return FileResponse(path=str(path), filename=path.name, media_type="application/octet-stream")


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
    return HTMLResponse(_baidu_callback_html(token=token.get("access_token", "")))


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
    if not (TORRENT_ENABLED and torrent_mod.available()):
        raise HTTPException(status_code=404, detail="种子下载功能未启用（需桌面版并安装 libtorrent）")


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
    try:
        translated = subtitles_mod.translate_srt(text, req.api_key, req.base_url, req.model, req.target)
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


# ---- 格式 / 片段增强：对已下载媒体做本地 ffmpeg 加工（转音频 / GIF / 裁剪 / 压缩 / 放大）----
# 与字幕处理同源（基于 lib_id）；产物落源目录并写侧车 → 媒体库自动可见。

class ProcessRequest(BaseModel):
    lib_id: str = Field(min_length=1)
    op: str = Field(min_length=1, max_length=16)
    params: dict = Field(default_factory=dict)


@app.post("/api/process/run")
def process_run(req: ProcessRequest) -> dict:
    if not (getattr(sys, "frozen", False) or os.environ.get("VDL_LIBRARY_ENABLED")):
        raise HTTPException(status_code=403, detail="当前部署未启用本地加工功能")
    if req.op not in ("audio", "gif", "trim", "crop", "compress", "upscale",
                      "frame", "frames", "sheet", "ringtone"):
        raise HTTPException(status_code=400, detail="不支持的处理类型")
    src = library_mod._resolve_safe(DOWNLOAD_DIR, req.lib_id)
    if not src or not src.is_file():
        raise HTTPException(status_code=404, detail="源文件不存在")
    job_id = uuid.uuid4().hex[:12]
    with PROCESS_LOCK:
        PROCESS_JOBS[job_id] = {"status": "running", "error": "", "out_path": "",
                                "lib_id": "", "name": "", "count": 0, "is_dir": False}
    executor.submit(_run_process, job_id, str(src), req.op, req.params or {})
    return {"job_id": job_id, "status": "running"}


@app.get("/api/process/{job_id}")
def process_status(job_id: str) -> dict:
    with PROCESS_LOCK:
        job = PROCESS_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="处理任务不存在")
    return {"status": job["status"], "error": job.get("error", ""),
            "lib_id": job.get("lib_id", ""), "name": job.get("name", ""),
            "count": job.get("count", 0), "is_dir": job.get("is_dir", False)}


def _run_process(job_id: str, src: str, op: str, params: dict) -> None:
    """后台线程：按 op 调用 ffmpeg_tools，产物落源目录并写侧车，更新 PROCESS_JOBS。"""
    with PROCESS_LOCK:
        job = PROCESS_JOBS.get(job_id)
    if not job:
        return
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
            with PROCESS_LOCK:
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
        with PROCESS_LOCK:
            job.update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)
        logger.info("process %s (%s) done -> %s", job_id, op, out.name)
    except Exception as e:  # noqa: BLE001
        with PROCESS_LOCK:
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
