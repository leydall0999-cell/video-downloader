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
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import urlparse

import downloader
from platforms import LinkError, UnsupportedPlatformError, is_china_host, parse_source, platform_catalog
from tasks import TaskStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vdl")

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DOWNLOAD_DIR = BASE_DIR / "downloads"

MAX_CONCURRENT_DOWNLOADS = 3
MAX_CONCURRENT_PROBES = 8
RESOLVE_TIMEOUT_SECONDS = 40          # 海外站（走代理），留出代理延迟余量
RESOLVE_TIMEOUT_DOMESTIC = 20         # 国内站（腾讯/优酷/B站等直连，本就很快；受限视频也能更快判定）
SSE_INTERVAL_SECONDS = 0.5
SSE_MAX_SECONDS = 60 * 30
CLEANUP_INTERVAL_SECONDS = 600


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


# --------------------------------------------------------------------------- #
# 请求模型 & 错误处理
# --------------------------------------------------------------------------- #

class ResolveRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    quality: str = Field(default=downloader.BEST_KEY, max_length=16)


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


@app.post("/api/resolve")
async def resolve(payload: ResolveRequest) -> dict:
    url, platform = parse_source(payload.url)
    # 国内站直连、本就快，用更短超时；受限视频也能更快判定，不必让用户空等
    host = _host_of(url)
    timeout = RESOLVE_TIMEOUT_DOMESTIC if is_china_host(host) else RESOLVE_TIMEOUT_SECONDS
    loop = asyncio.get_running_loop()
    try:
        info = await asyncio.wait_for(
            loop.run_in_executor(prober, downloader.probe, url),
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
def create_download(payload: DownloadRequest) -> dict:
    url, platform = parse_source(payload.url)
    if not downloader.is_valid_quality(payload.quality):
        raise HTTPException(status_code=400, detail="不支持的清晰度选项")

    task = store.create(
        url=url,
        title="",
        platform=platform.name,
        quality=downloader.quality_label(payload.quality),
    )
    executor.submit(downloader.run_download, task, store, payload.quality)
    return {"task_id": task.id, "status": task.status}


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


@app.delete("/api/tasks/{task_id}")
def cancel_task(task_id: str) -> dict:
    """进行中的任务 → 请求取消并保留记录；已结束的任务 → 连同文件一起清理。"""
    task = _require_task(task_id)
    if task.is_finished:
        store.remove(task_id)
        return {"task_id": task_id, "canceled": False, "removed": True}
    return {"task_id": task_id, "canceled": store.request_cancel(task_id), "removed": False}


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
