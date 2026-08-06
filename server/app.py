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
import sys
import shutil
import subprocess
import threading
import requests  # 解说 worker HTTP 模式客户端（VDL_COMMENTARY_MODE=http 时用到）
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import urlparse

import downloader
from platforms import CHINA_DOMAINS, LinkError, UnsupportedPlatformError, is_china_host, parse_source, platform_catalog
from tasks import TaskStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vdl")

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DOWNLOAD_DIR = BASE_DIR / "downloads"

MAX_CONCURRENT_DOWNLOADS = 3
MAX_CONCURRENT_PROBES = 8

# ---- 格式转换（增值能力）：对已下载文件做 ffmpeg 转码，异步 job ----
CONVERT_DIR = DOWNLOAD_DIR / "conversions"
CONVERT_DIR.mkdir(parents=True, exist_ok=True)
CONVERT_JOBS: dict[str, dict] = {}
CONVERT_LOCK = threading.Lock()
FFMPEG_BIN = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
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


def _host_of(url: str) -> str:
    """从链接取出主机名（去掉 www./m. 前缀），解析失败返回空串。"""
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.removeprefix("www.").removeprefix("m.")
    except ValueError:
        return ""

store = TaskStore(DOWNLOAD_DIR)
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS, thread_name_prefix="vdl-dl")
prober = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PROBES, thread_name_prefix="vdl-probe")


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
    cleaner = asyncio.create_task(_cleanup_loop())
    yield
    cleaner.cancel()
    executor.shutdown(wait=False, cancel_futures=True)
    prober.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="视频下载站", version="1.0.0", lifespan=lifespan)

# 双节点部署时，另一个节点的前端需要跨域调本节点 API（含 SSE 进度流与文件下载）
if ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOW_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Subscription-Key"],
    )
    logger.info("CORS 已开启，允许来源：%s", ", ".join(ALLOW_ORIGINS))


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
    _assert_safe_url(payload.url)          # 先拦内网/环回地址，避免可疑 URL 进入解析流程
    url, platform = parse_source(payload.url)
    if not downloader.is_valid_quality(payload.quality):
        raise HTTPException(status_code=400, detail="不支持的清晰度选项")

    task = store.create(
        url=url,
        title="",
        platform=platform.name,
        quality=downloader.quality_label(payload.quality),
    )
    executor.submit(downloader.run_download, task, store, payload.quality, payload.cookie, payload.proxy)
    return {
        "task_id": task.id,
        "status": task.status,
        "quota": {"subscribed": subscribed, "free_used": free_used, "free_daily": free_daily},
    }


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


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
