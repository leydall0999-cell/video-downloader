"""server/routers/core.py — app.py 引擎层核心路由（Phase 3 抽取）。

承载下载 / 任务 / 解析 / 版本 / 平台 / 节点 / 流式代理 / Cookie 等核心 API。
通过 `import app` + 复用 app 模块级符号（downloader / store / scheduler / plat /
各 ENABLED 常量 / 辅助函数等；本文件在 app.py 末尾才被 import，符号已就绪），
handler 原样搬入、零改引用。路由用 @router.get/post 挂载，已在 app.py 末尾 include。
"""
import app
from fastapi import APIRouter
router = APIRouter()


def _device_of(request: app.Request) -> str:
    """取设备 ID：优先请求头 X-Device-Id（fetch 请求），其次 query device=
    （EventSource / <a href> 文件下载无法带自定义 header，走 query）。"""
    dev = (request.headers.get("X-Device-Id") or "").strip()
    if not dev:
        dev = (request.query_params.get("device") or "").strip()
    return dev[:64]


@router.get('/api/platforms')
def list_platforms() -> dict:
    return {'platforms': app.platform_catalog()}

def _read_build_version() -> str:
    """模块导入时读取一次，冻结为本进程「构建时」的真实版本。

    关键修复（2026-08-28）：原先每次请求都从磁盘现读 build_version.txt。
    当部署把新包覆盖到 /Applications 后，旧进程仍活着，却从新包的
    build_version.txt 读到新版本号 → 报假版本，无法识别 stale 进程
    （曾导致「修了没生效」误判、用户大怒）。

    改为导入时冻结一次：旧进程始终报自己启动时的旧号，新进程报新号，
    一眼可辨；同时让 deploy_mac.sh 的自校验真正强制重启旧进程，
    而不是被旧进程用新盘的版本号糊弄过去。
    """
    candidates = []
    exe = getattr(app.sys, 'executable', '')
    if exe:
        candidates.append(app.Path(exe).resolve().parent.parent / 'Resources' / 'build_version.txt')
    candidates.append(app.Path(__file__).resolve().parent.parent / 'build_version.txt')
    for c in candidates:
        if c.exists():
            return c.read_text(encoding='utf-8').strip()
    return 'dev'


# 导入即冻结：此后无论磁盘 build_version.txt 是否被新部署覆盖，
# 本进程始终报告自己构建时的版本。
BUILD_VERSION = _read_build_version()


@router.get('/api/version')
def api_version() -> dict:
    """返回运行实例的构建指纹 + 实际加载的可执行文件路径。

    部署脚本 deploy_mac.sh 用它做自校验：只有运行中的服务返回的指纹与
    刚构建的 build_version.txt 一致、且 exe 路径确实指向目标 app 时，
    才算「部署成功」，否则直接判定失败，杜绝「装的是旧版却以为装好了」。

    注意：version 在模块导入时已冻结（见 BUILD_VERSION），旧进程永远报旧号，
    这是识别 stale 进程的关键。
    """
    return {'version': BUILD_VERSION, 'exe': app.sys.executable}

@router.get('/api/ydlp/version')
def ydlp_version_api() -> dict:
    """返回当前与最新 yt-dlp 版本，前端据此提示是否需要更新解析器。"""
    return {'current': app.ydlp_update.current_version(), 'latest': app.ydlp_update.latest_version()}

@router.post('/api/ydlp/update')
async def ydlp_update_api() -> dict:
    """下载最新 yt-dlp 解析器到本机目录（下次启动生效）。"""
    return await app.asyncio.to_thread(app.ydlp_update.update)

@router.get('/api/nodes')
def node_info() -> dict:
    """告诉前端：本节点在哪个区、对端节点在哪、哪些域名算国内站。

    前端据此在粘贴链接时自动把请求发到「离目标站点更近」的节点：
    国内站 → cn 节点，海外站 → global 节点。对端为空则退化为单节点模式。
    """
    info = {'region': app.NODE_REGION, 'peer': app.PEER_ENDPOINT, 'china_domains': list(app.CHINA_DOMAINS), 'commentary_enabled': app.COMMENTARY_ENABLED, 'ads_enabled': app.ADS_ENABLED, 'convert': {'subscription_required': app.CONVERT_SUB_ENABLED, 'free_daily': app.CONVERT_FREE_DAILY, 'targets': list(app.CONVERT_TARGETS.keys())}, 'download': {'subscription_required': app.DOWNLOAD_SUB_ENABLED, 'free_daily': app.DOWNLOAD_FREE_DAILY}, 'library': {'enabled': app.plat.is_desktop() or bool(app.os.environ.get('VDL_LIBRARY_ENABLED'))}, 'subscriptions': {'enabled': app.SUB_ENABLED, 'probe_limit': app.SUBSCRIBE_PROBE_LIMIT, 'check_interval': app.SUB_CHECK_INTERVAL}, 'retention': {'enabled': app.RETENTION_ENABLED, 'trash_available': app.retention_mod.trash_available() if app.RETENTION_ENABLED else False}, 'crypto': {'enabled': app.CRYPTO_ENABLED, 'has_pass': bool(app._vault_load()) if app.CRYPTO_ENABLED else False, 'locked': app.VAULT_KEY is None}, 'torrent': {'enabled': app.TORRENT_ENABLED, 'available': app.torrent_mod.available()}, 'ai_dewatermark': {'enabled': app.AI_DEWATERMARK_ENABLED, 'gpu': app.AI_GPU_AVAILABLE, 'image_ai': bool(app.dwc_ai_available)}, 'authRequired': app.AUTH_REQUIRED, 'profile': ('web' if app.os.environ.get('VDL_INSTANCE') == 'cloud' else 'app')}
    caps = app.plat.node_capabilities()
    return {k: v for k, v in info.items() if k not in app.plat.NODE_GROUPS or k in caps}

@router.post('/api/resolve')
async def resolve(payload: app.ResolveRequest, request: app.Request) -> dict:
    # 2026-09-06 优化：解析不再设配额墙（解析预览放开，会员墙挪到「点清晰度下载」处）
    app._check_rate_limit(request)
    app._assert_safe_url(payload.url)
    url, platform = app.parse_source(payload.url)
    host = app._host_of(url)
    if host == 'v.qq.com':
        timeout = 35
    elif 'youtube.com' in host or 'youtu.be' in host:
        timeout = 70
    elif app.is_china_host(host):
        timeout = app.RESOLVE_TIMEOUT_DOMESTIC
    else:
        timeout = app.RESOLVE_TIMEOUT_SECONDS
    loop = app.asyncio.get_running_loop()
    try:
        info = await app.asyncio.wait_for(loop.run_in_executor(app.prober, app.downloader.probe, url, payload.cookie, payload.proxy), timeout=timeout)
    except app.asyncio.TimeoutError:
        host = app._host_of(url)
        if host == 'v.qq.com':
            detail = '腾讯视频解析超时。该视频可能是会员/付费内容，或腾讯页面改版导致提取器暂时失效。建议：①在「高级选项」粘贴浏览器 Cookie 后重试；②确认视频可公开访问（非 VIP 专享）；③稍后重试或反馈此链接'
        elif 'youtube.com' in host or 'youtu.be' in host:
            detail = f'YouTube 解析超时（超过 {timeout} 秒）。常见原因：①代理速度慢或不稳定（YouTube 需要拉取 player.js 签名，代理延迟会叠加）；②该视频可能受限（地区/年龄限制）；建议：①检查代理是否通畅；②稍后重试；③若持续失败，尝试更换节点或关闭代理直连'
        else:
            detail = f'解析超时（超过 {timeout} 秒）。常见原因：①视频本身受限（限免/会员专享/付费/地区限制，这类通常需登录 cookie 才能拿到真实流，请到右上角「高级选项」粘贴浏览器 Cookie 后重试）；②当前网络无法访问该平台（可尝试在「高级选项」设置代理）'
        raise app.HTTPException(status_code=504, detail=detail) from None
    return {'url': url, 'platform': {'key': platform.key, 'name': platform.name}, 'video': app.downloader.summarize(info), 'qualities': app.downloader.build_quality_options(info), 'sources': []}

def _download_gate_error() -> str | None:
    """下载配额门（2026-09-06）：免费 10 次/日 → 会员 1000 次/日。放行返回 None，否则返回引导文案。"""
    qs = app.member_store.quota_state('download')
    if qs.get('allowed'):
        return None
    if qs.get('tier') == 'free':
        return f"今日免费下载次数已用尽（{qs.get('limit', 10)}/日）— 开通下载会员可解锁 {qs.get('member_limit') or 1000} 次/日"
    return f"今日下载配额已用尽（{qs.get('limit')}/日）"

def _stream_referer(host: str) -> str:
    """按平台返回防盗链 Referer：腾讯视频 HLS 分片必须带正确的 Referer 才返回 200。

    注意：YouTube / googlevideo.com 等**不在此返回 Referer**——它们靠 URL 签名（ip/n/sig 参数）
    验证请求合法性，带错误 Referer（如 googlevideo.com 自身）反而会触发 403 拒绝。
    调用方应对 YouTube 域跳过 Referer。
    """
    if 'v.qq.com' in host:
        return 'https://v.qq.com/'
    if 'douyin' in host:
        return 'https://www.douyin.com/'
    if 'bilibili' in host:
        return 'https://www.bilibili.com/'
    if 'googlevideo.com' in host or 'youtube.com' in host or 'youtu.be' in host:
        return ''
    return f'https://{host}/' if host else 'https://v.qq.com/'

def _rewrite_m3u8(text: str, base_url: str, proxy_prefix: str) -> str:
    """把 m3u8 内每条 URL 绝对化后改写成指向本端点的代理 URL。

    - 非注释、非空行即 URL 行（子 playlist / ts 分片），整行改写；
    - #EXT-X-KEY / #EXT-X-MEDIA 等标签行里的 URI="..." 属性也改写（加密流的 key 直连
      会被防盗链 403，必须走本端点带 Referer）。
    这样原生 <video> 播放器解析 master→子 playlist→ts→key 时，每一跳都走本端点。
    """
    uri_re = app.re.compile('(URI=")([^"]+)(")')

    def _rewrite_uri(m: 're.Match') -> str:
        seg = m.group(2).strip()
        abs_url = app.urljoin(base_url, seg)
        return m.group(1) + proxy_prefix + app.quote(abs_url, safe='') + m.group(3)
    out: list[str] = []
    for line in text.split('\n'):
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        if stripped.startswith('#'):
            if 'URI=' in line:
                line = uri_re.sub(_rewrite_uri, line)
            out.append(line)
            continue
        abs_url = app.urljoin(base_url, stripped)
        out.append(proxy_prefix + app.quote(abs_url, safe=''))
    return '\n'.join(out)

@router.get('/api/stream/proxy')
def stream_proxy(u: str='', cookie: str='', request: app.Request=None):
    """在线观看流代理：浏览器（WKWebView）直连腾讯会被防盗链 403，且原生 HLS 无法自定义
    Referer 头。这里由后端带 Referer/Cookie 去源站拉取回传，从而绕开防盗链。

    - 对非 m3u8（MP4/ts 分片等）原样流式透传；
    - 对 m3u8 清单：把内部相对/绝对 URL 改写为指向本端点的代理 URL，这样原生 <video>
      播放器解析 master→子 playlist→ts 分片时，每一跳都走本端点（后端统一带 Referer），
      无需 hls.js，macOS 原生 HLS 即可播放。
    """
    if not u:
        raise app.HTTPException(status_code=400, detail='缺少 u 参数')
    app._assert_safe_url(u)
    host = app._host_of(u)
    _proxies: dict[str, str] | None = None
    if not app.is_china_host(host):
        _proxy_url = app.downloader._resolve_proxy(host)
        if _proxy_url:
            _proxies = {'http': _proxy_url, 'https': _proxy_url}
    user_cookie = (cookie or '').strip()
    if user_cookie.lower().startswith('cookie:'):
        user_cookie = user_cookie[7:].strip()
    cookie_text = user_cookie
    used_auto_cookie = False
    if not cookie_text:
        try:
            auto = app.downloader.get_browser_cookie_header(host, u)
        except Exception:
            auto = None
        if auto:
            cookie_text = auto
            used_auto_cookie = True
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'Range': 'bytes=0-'}
    _ref = _stream_referer(host)
    if _ref:
        headers['Referer'] = _ref
    if cookie_text:
        headers['Cookie'] = cookie_text
    _client_range = None
    if request:
        _cr = request.headers.get('range')
        if _cr:
            _client_range = _cr
            headers['Range'] = _cr
    try:
        resp = app.requests.get(u, headers=headers, stream=True, timeout=(10, 120), proxies=_proxies)
    except Exception as exc:
        raise app.HTTPException(status_code=502, detail=f'上游拉取失败：{app.downloader._clean_message(str(exc))}') from None
    if resp.status_code >= 400:
        detail = f'上游返回 {resp.status_code}'
        if resp.status_code in (401, 403):
            if used_auto_cookie:
                detail += '（已自动携带浏览器登录态仍被拒，可能需先在浏览器登录该平台，或手动粘贴 Cookie）'
            elif cookie_text:
                detail += '（防盗链被拒，可在「高级选项」重新粘贴 Cookie 后重试）'
            else:
                detail += '（防盗链被拒，可能需要登录 Cookie，请在「高级选项」粘贴浏览器 Cookie 后重试）'
        resp.close()
        raise app.HTTPException(status_code=resp.status_code, detail=detail)
    content_type = (resp.headers.get('Content-Type') or '').lower()
    is_m3u8 = 'mpegurl' in content_type or content_type in ('application/x-mpegurl', '') or '.m3u8' in u
    base = str(request.base_url).rstrip('/') if request is not None else 'http://127.0.0.1'
    proxy_prefix = f'{base}/api/stream/proxy?u='
    if is_m3u8:
        raw = resp.content.decode('utf-8', errors='replace')
        resp.close()
        if raw.lstrip().startswith('#EXTM3U'):
            rewritten = _rewrite_m3u8(raw, u, proxy_prefix)
            return app.Response(rewritten, media_type='application/vnd.apple.mpegurl', headers={'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*'})
        return app.Response(raw, media_type=content_type or 'application/octet-stream', headers={'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*'})

    def _gen():
        try:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            resp.close()
    _resp_headers = {'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*', 'X-Accel-Buffering': 'no'}
    if resp.headers.get('Accept-Ranges'):
        _resp_headers['Accept-Ranges'] = resp.headers['Accept-Ranges']
    if resp.status_code == 206 and resp.headers.get('Content-Range'):
        _resp_headers['Content-Range'] = resp.headers['Content-Range']
    if resp.headers.get('Content-Length'):
        _resp_headers['Content-Length'] = resp.headers['Content-Length']
    return app.StreamingResponse(_gen(), media_type=content_type or 'application/octet-stream', headers=_resp_headers, status_code=resp.status_code)

@router.get('/api/cookie/status')
def cookie_status(url: str='') -> dict:
    """探测本机浏览器是否含目标站点的登录 Cookie，供前端「检测登录态」与解析后自动提示。

    返回 available/browser/profile：前端据此告知用户「已自动读取，无需手动粘贴」
    或「未检测到，请先在浏览器登录该平台，或手动粘贴 Cookie」。
    """
    if not url:
        raise app.HTTPException(status_code=400, detail='请提供链接')
    app._assert_safe_url(url)
    url, platform = app.parse_source(url)
    host = app._host_of(url)
    info = app.downloader.detect_browser_cookie(host)
    return {'host': host, 'platform': platform.key, 'needed': app.downloader.is_cookie_hardened_host(host), 'available': info['available'], 'browser': info['browser'], 'profile': info['profile']}

@router.post('/api/cookie/cache/clear')
def cookie_cache_clear() -> dict:
    """清除本机 Cookie 缓存（仅删 ~/.videodownloader/cookies，不影响浏览器本身）。"""
    from cookie_cache import clear_cookie_cache
    n = clear_cookie_cache()
    return {'ok': True, 'cleared': n}

def _valid_extract_mode(value: str) -> str:
    """校验并归一化文案提取模式，非法值回退为不提取。"""
    return value if value in ('spoken', 'description', 'both') else ''

@router.post('/api/download')
def create_download(payload: app.DownloadRequest, request: app.Request) -> dict:
    app._check_rate_limit(request)
    # 会员下载配额（2026-09-06）：免费 10 次/日 → 会员 1000 次/日；不足 402 + MEMBER_QUOTA|
    _err = _download_gate_error()
    if _err:
        raise app.HTTPException(status_code=402, detail='MEMBER_QUOTA|' + _err)
    url, platform = app.parse_source(payload.url)
    if not app.downloader.is_valid_quality(payload.quality):
        raise app.HTTPException(status_code=400, detail='不支持的清晰度选项')
    extract_mode = _valid_extract_mode(payload.extract_script)
    task = app.store.create(url=url, title=(payload.title or ''), platform=platform.name, quality=app.downloader.quality_label(payload.quality), quality_key=payload.quality, extract_mode=extract_mode, concurrent_fragments=payload.concurrent_fragments, downloader_type=payload.downloader, cookie=payload.cookie, proxy=payload.proxy, play_url=payload.play_url, watch_options=payload.watch_options, is_hls=payload.is_hls)
    app.scheduler.submit(app.downloader.run_download, task, app.store, payload.quality, payload.cookie, payload.proxy, app.SINGLE_DOWNLOAD_RETRIES, payload.format_id, payload.concurrent_fragments, payload.downloader)
    # 任务创建成功才计费（失败/被拒不烧免费额度）
    _charged = app.member_store.use_daily('download', 1)
    _qs = app.member_store.quota_state('download')
    return {'task_id': task.id, 'status': task.status,
            'quota': {'subscribed': _qs.get('tier') == 'member',
                      'free_used': _charged.get('used', _qs.get('used', 0)),
                      'free_daily': _qs.get('limit', 10)}}

class BatchRequest(app.BaseModel):
    urls: list[str] = app.Field(default_factory=list, max_length=app.VDL_BATCH_MAX_ITEMS)
    quality: str = app.Field(default=app.downloader.BEST_KEY, max_length=16)
    cookie: str = app.Field(default='', max_length=8192)
    proxy: str = app.Field(default='', max_length=256)
    concurrency: int = app.Field(default=0, ge=0, le=app.VDL_BATCH_HARD_MAX)
    retries: int = app.Field(default=-1, ge=-1, le=10)
    extract_script: str = app.Field(default='', max_length=16)

@router.post('/api/batch')
def create_batch(payload: BatchRequest, request: app.Request) -> dict:
    app._check_rate_limit(request)
    urls = [u.strip() for u in payload.urls if u.strip()]
    if not urls:
        raise app.HTTPException(status_code=400, detail='没有提供有效的链接')
    if not app.downloader.is_valid_quality(payload.quality):
        raise app.HTTPException(status_code=400, detail='不支持的清晰度选项')
    extract_mode = _valid_extract_mode(payload.extract_script)
    if payload.concurrency > 0:
        app.scheduler.set_concurrency(payload.concurrency)
    retries = payload.retries if payload.retries >= 0 else app.BATCH_RETRIES_DEFAULT
    task_ids: list[str] = []
    skipped = 0
    quota_exhausted = False
    for u in urls:
        _err = _download_gate_error()
        if _err:
            quota_exhausted = True
            break
        try:
            url, platform = app.parse_source(u)
        except (app.UnsupportedPlatformError, app.LinkError):
            skipped += 1
            continue
        task = app.store.create(url=url, title='', platform=platform.name, quality=app.downloader.quality_label(payload.quality), quality_key=payload.quality, extract_mode=extract_mode)
        app.scheduler.submit(app.downloader.run_download, task, app.store, payload.quality, payload.cookie, payload.proxy, retries)
        app.member_store.use_daily('download', 1)  # 每个成功创建的任务计 1 次下载配额
        task_ids.append(task.id)
    if not task_ids:
        if quota_exhausted:
            raise app.HTTPException(status_code=402, detail='MEMBER_QUOTA|' + (_download_gate_error() or '今日免费下载次数已用尽 — 开通下载会员可解锁'))
        raise app.HTTPException(status_code=400, detail='链接均无法识别，请确认是视频播放页链接')
    return {'task_ids': task_ids, 'count': len(task_ids), 'skipped': skipped, 'quota_exhausted': quota_exhausted}

@router.get('/api/tasks')
def list_tasks() -> dict:
    """列出当前所有任务（含排队 / 进行中 / 已完成），供前端队列概览。"""
    tasks = [t.to_public_dict() for t in app.store.list_all()]
    stats = {'pending': 0, 'downloading': 0, 'merging': 0, 'completed': 0, 'failed': 0, 'canceled': 0}
    for t in tasks:
        stats[t['status']] = stats.get(t['status'], 0) + 1
    stats['active'] = app.scheduler.active_count()
    return {'tasks': tasks, 'stats': stats, 'concurrency': app.scheduler.concurrency}

@router.post('/api/tasks/{task_id}/retry')
def retry_task(task_id: str) -> dict:
    task = app._require_task(task_id)
    if task.status not in ('failed', 'canceled'):
        raise app.HTTPException(status_code=400, detail='仅失败 / 已取消的任务可以重试')
    task.cancel_requested = False
    resume = app.downloader._has_partial(task.workdir)
    app.store.update(task_id, status='pending', error='', hint='', progress=task.progress if resume else 0.0, downloaded_bytes=task.downloaded_bytes if resume else 0, total_bytes=task.total_bytes if resume else 0, speed=0.0, eta=0, filesize=0, filename='', resumable=False)
    app.scheduler.submit(app.downloader.run_download, task, app.store, task.quality_key, task.cookie, task.proxy, app.BATCH_RETRIES_DEFAULT, '', task.concurrent_fragments, task.downloader_type, resume)
    return {'task_id': task_id, 'status': 'pending', 'resume': resume}

@router.post('/api/tasks/{task_id}/extract-text')
def reextract_text(task_id: str) -> dict:
    """对已完成任务重新提取文案（如首次语音转写超时，可点重试）。"""
    task = app._require_task(task_id)
    if not task.extract_mode:
        raise app.HTTPException(status_code=400, detail='该任务未开启文案提取')
    if not task.filepath or not app.Path(task.filepath).exists():
        raise app.HTTPException(status_code=400, detail='任务文件不存在，无法提取文案')
    app.executor.submit(app.downloader._run_extraction, task, app.store, app.Path(task.filepath), None, '', '', mode=task.extract_mode)
    return {'task_id': task_id, 'status': 'running'}

@router.post('/api/tasks/cancel-all')
def cancel_all_tasks() -> dict:
    """取消所有进行中 / 排队中的任务；已完成与失败的任务保留（不删文件）。"""
    canceled = 0
    for t in app.store.list_all():
        if not t.is_finished and app.store.request_cancel(t.id):
            canceled += 1
    return {'canceled': canceled}

@router.get('/api/batch/config')
def batch_config() -> dict:
    return {'concurrency': app.scheduler.concurrency, 'hard_max': app.VDL_BATCH_HARD_MAX, 'retries': app.BATCH_RETRIES_DEFAULT}

@router.get('/api/tasks/{task_id}')
def task_status(task_id: str) -> dict:
    return app._require_task(task_id).to_public_dict()

@router.get('/api/tasks/{task_id}/events')
async def task_events(task_id: str, request: app.Request) -> app.StreamingResponse:
    app._require_task(task_id)

    async def event_stream():
        elapsed = 0.0
        while elapsed < app.SSE_MAX_SECONDS:
            if await request.is_disconnected():
                return
            task = app.store.get(task_id)
            if task is None:
                yield _sse({'status': 'failed', 'error': '任务已过期'})
                return
            yield _sse(task.to_public_dict())
            if task.is_finished:
                return
            await app.asyncio.sleep(app.SSE_INTERVAL_SECONDS)
            elapsed += app.SSE_INTERVAL_SECONDS
    return app.StreamingResponse(event_stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})

def _sse(data: dict) -> str:
    return f'data: {app.json.dumps(data, ensure_ascii=False)}\n\n'

@router.get('/api/tasks/{task_id}/file')
def download_file(task_id: str, download: int=0) -> app.Response:
    task = app._require_task(task_id)
    if task.status != 'completed' or not task.filepath or (not task.filepath.exists()):
        raise app.HTTPException(status_code=409, detail='文件尚未准备好')
    _ext = task.filepath.suffix.lower()
    _mt = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mkv': 'video/x-matroska', '.m4a': 'audio/mp4', '.mp3': 'audio/mpeg'}.get(_ext, 'application/octet-stream')
    if download:
        # 强制下载：流式传输，避免 read_bytes() 把大文件读进内存；
        # Content-Disposition 的 filename= 只能是 ASCII，中文走 filename*=UTF-8''。
        import urllib.parse
        from starlette.responses import FileResponse
        _encoded = urllib.parse.quote(task.filepath.name)
        _ascii = task.filepath.name.encode('ascii', 'ignore').decode() or 'download'
        return FileResponse(
            path=task.filepath,
            media_type='application/octet-stream',
            headers={'Content-Disposition': f"attachment; filename=\"{_ascii}\"; filename*=UTF-8''{_encoded}"},
        )
    return app.FileResponse(path=task.filepath, filename=task.filepath.name, media_type=_mt)

@router.delete('/api/tasks/{task_id}')
def cancel_task(task_id: str) -> dict:
    """进行中的任务 → 请求取消并保留记录；已结束的任务 → 连同文件一起清理。"""
    task = app._require_task(task_id)
    if task.is_finished:
        app.store.remove(task_id)
        return {'task_id': task_id, 'canceled': False, 'removed': True}
    return {'task_id': task_id, 'canceled': app.store.request_cancel(task_id), 'removed': False}

@router.post('/api/tasks/{task_id}/pause')
def pause_task(task_id: str) -> dict:
    """暂停正在下载的任务——保留 .part 文件，后续可断点续传。"""
    task = app._require_task(task_id)
    if task.is_finished:
        return {'task_id': task_id, 'paused': False, 'message': '任务已结束，无法暂停'}
    if task.status == 'paused':
        return {'task_id': task_id, 'paused': True, 'message': '已暂停'}
    task.pause_requested = True
    task.add_step('下载音视频', 'pending', '正在暂停…')
    app.store.update(task.id, status='pausing')
    return {'task_id': task_id, 'paused': True}

@router.post('/api/tasks/{task_id}/resume')
def resume_task(task_id: str) -> dict:
    """继续被暂停的下载——yt-dlp 自动从已下载的 .part 文件断点续传。"""
    task = app._require_task(task_id)
    if task.status not in ('paused',):
        return {'task_id': task_id, 'resumed': False, 'message': '任务未处于暂停状态'}
    task.pause_requested = False
    task.add_step('下载音视频', 'running', '继续下载…')
    task.log('用户继续下载（断点续传）')
    app.store.update(task.id, status='downloading')
    app.scheduler.submit(app.downloader.run_download, task, app.store, task.quality_key, '', '', app.SINGLE_DOWNLOAD_RETRIES)
    return {'task_id': task_id, 'resumed': True}
