"""server/routers/core.py — app.py 引擎层核心路由（Phase 3 抽取）。

承载下载 / 任务 / 解析 / 版本 / 平台 / 节点 / 流式代理 / Cookie 等核心 API。
通过 `import app` + 复用 app 模块级符号（downloader / store / scheduler / plat /
各 ENABLED 常量 / 辅助函数等；本文件在 app.py 末尾才被 import，符号已就绪），
handler 原样搬入、零改引用。路由用 @router.get/post 挂载，已在 app.py 末尾 include。
"""
import app
from fastapi import APIRouter

# 复用 app.py 共享内核：app.py 在文件末尾才 import 本模块，此时其模块级符号已全部定义，
# 整体镜像进本模块命名空间，handler 即可用 bare name 直接引用（与 app.py 内部一致）。
# 注：这些全局量（*_ENABLED / NODE_REGION 等）在 app.py 中均为运行期不变的常量，
# 镜像快照与实时查表行为等价，无状态滞后风险。排除 'app' 本身，避免实例覆盖模块绑定。
_g = {k: v for k, v in vars(app).items() if not k.startswith("__") and k != "app"}
globals().update(_g)

router = APIRouter()

@router.get("/api/platforms")
def list_platforms() -> dict:
    return {"platforms": platform_catalog()}



@router.get("/api/version")
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



@router.get("/api/ydlp/version")
def ydlp_version_api() -> dict:
    """返回当前与最新 yt-dlp 版本，前端据此提示是否需要更新解析器。"""
    return {"current": ydlp_update.current_version(), "latest": ydlp_update.latest_version()}



@router.post("/api/ydlp/update")
async def ydlp_update_api() -> dict:
    """下载最新 yt-dlp 解析器到本机目录（下次启动生效）。"""
    return await asyncio.to_thread(ydlp_update.update)



@router.get("/api/nodes")
def node_info() -> dict:
    """告诉前端：本节点在哪个区、对端节点在哪、哪些域名算国内站。

    前端据此在粘贴链接时自动把请求发到「离目标站点更近」的节点：
    国内站 → cn 节点，海外站 → global 节点。对端为空则退化为单节点模式。
    """
    info = {
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
            "baidu_auth_url": baidu_auth_url(BAIDU_REDIRECT_URI, BAIDU_APP_KEY, app_id=BAIDU_APP_ID) if BAIDU_ENABLED else "",
        },
        # 本地媒体库：仅桌面版（frozen）或显式开启时暴露给前端；网页版目录临时、默认关闭
        "library": {
            "enabled": plat.is_desktop() or bool(os.environ.get("VDL_LIBRARY_ENABLED")),
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
    caps = plat.node_capabilities()
    return {k: v for k, v in info.items() if k not in plat.NODE_GROUPS or k in caps}



@router.post("/api/resolve")
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



@router.get("/api/stream/proxy")
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



@router.get("/api/cookie/status")
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



@router.post("/api/cookie/cache/clear")
def cookie_cache_clear() -> dict:
    """清除本机 Cookie 缓存（仅删 ~/.videodownloader/cookies，不影响浏览器本身）。"""
    from cookie_cache import clear_cookie_cache
    n = clear_cookie_cache()
    return {"ok": True, "cleared": n}



def _valid_extract_mode(value: str) -> str:
    """校验并归一化文案提取模式，非法值回退为不提取。"""
    return value if value in ("spoken", "description", "both") else ""



@router.post("/api/download")
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



class BatchRequest(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=VDL_BATCH_MAX_ITEMS)
    quality: str = Field(default=downloader.BEST_KEY, max_length=16)
    cookie: str = Field(default="", max_length=8192)
    proxy: str = Field(default="", max_length=256)
    concurrency: int = Field(default=0, ge=0, le=VDL_BATCH_HARD_MAX)
    retries: int = Field(default=-1, ge=-1, le=10)
    extract_script: str = Field(default="", max_length=16)



@router.post("/api/batch")
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



@router.get("/api/tasks")
def list_tasks() -> dict:
    """列出当前所有任务（含排队 / 进行中 / 已完成），供前端队列概览。"""
    tasks = [t.to_public_dict() for t in store.list_all()]
    stats = {"pending": 0, "downloading": 0, "merging": 0, "completed": 0, "failed": 0, "canceled": 0}
    for t in tasks:
        stats[t["status"]] = stats.get(t["status"], 0) + 1
    stats["active"] = scheduler.active_count()
    return {"tasks": tasks, "stats": stats, "concurrency": scheduler.concurrency}



@router.post("/api/tasks/{task_id}/retry")
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



@router.post("/api/tasks/{task_id}/extract-text")
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



@router.post("/api/tasks/cancel-all")
def cancel_all_tasks() -> dict:
    """取消所有进行中 / 排队中的任务；已完成与失败的任务保留（不删文件）。"""
    canceled = 0
    for t in store.list_all():
        if not t.is_finished and store.request_cancel(t.id):
            canceled += 1
    return {"canceled": canceled}



@router.get("/api/batch/config")
def batch_config() -> dict:
    return {"concurrency": scheduler.concurrency, "hard_max": VDL_BATCH_HARD_MAX, "retries": BATCH_RETRIES_DEFAULT}



@router.get("/api/tasks/{task_id}")
def task_status(task_id: str) -> dict:
    return _require_task(task_id).to_public_dict()



@router.get("/api/tasks/{task_id}/events")
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



@router.get("/api/tasks/{task_id}/file")
def download_file(task_id: str, download: int = 0) -> Response:
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
    # download=1 → 保存到本机（<a download> 点击），强制附件下载不内联播放
    # download=0 → 本地播放（<video src>），返回正确 MIME 让浏览器解码
    if download:
        from starlette.responses import Response as RawResponse
        _body = task.filepath.read_bytes()
        import urllib.parse
        _encoded = urllib.parse.quote(task.filepath.name)
        return RawResponse(
            content=_body,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{task.filepath.name}"; filename*=UTF-8\'\'{_encoded}'},
        )
    return FileResponse(
        path=task.filepath,
        filename=task.filepath.name,
        media_type=_mt,
    )



@router.delete("/api/tasks/{task_id}")
def cancel_task(task_id: str) -> dict:
    """进行中的任务 → 请求取消并保留记录；已结束的任务 → 连同文件一起清理。"""
    task = _require_task(task_id)
    if task.is_finished:
        store.remove(task_id)
        return {"task_id": task_id, "canceled": False, "removed": True}
    return {"task_id": task_id, "canceled": store.request_cancel(task_id), "removed": False}



@router.post("/api/tasks/{task_id}/pause")
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



@router.post("/api/tasks/{task_id}/resume")
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

