"""server/routers/core.py — app.py 引擎层核心路由（Phase 3 抽取）。

承载下载 / 任务 / 解析 / 版本 / 平台 / 节点 / 流式代理 / Cookie 等核心 API。
通过 `import app` + 复用 app 模块级符号（downloader / store / scheduler / plat /
各 ENABLED 常量 / 辅助函数等；本文件在 app.py 末尾才被 import，符号已就绪），
handler 原样搬入、零改引用。路由用 @router.get/post 挂载，已在 app.py 末尾 include。
"""
import os
import subprocess
import json as _json
import time as _time
from pathlib import Path as _Path

import app
from fastapi import APIRouter
router = APIRouter()

def _git_sha(repo_root: str) -> str:
    """读取仓库当前 checkout 的真实 git commit SHA；Railway 部署无 .git 时 fallback 到环境变量。"""
    try:
        out = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            sha = out.stdout.strip()
            if sha:
                return sha
    except Exception:
        pass
    return os.environ.get('RAILWAY_GIT_COMMIT_SHA', '')

@router.get('/api/platforms')
def list_platforms() -> dict:
    return {'platforms': app.platform_catalog()}

@router.get('/api/version')
def api_version() -> dict:
    """返回运行实例的真实代码版本指纹。

    关键字段：
    - git_sha：当前运行代码 checkout 的 git commit（Railway 部署到哪版一目了然）
    - railway_commit / railway_deployment / railway_branch：Railway 注入的环境变量
    以前只返回 build_version.txt 的 'dev'，无法确认线上跑的是哪次提交，
    排查「改了没生效」类问题纯靠猜日志。现在直接暴露真实 SHA。
    """
    candidates = []
    exe = getattr(app.sys, 'executable', '')
    if exe:
        candidates.append(app.Path(exe).resolve().parent.parent / 'Resources' / 'build_version.txt')
    candidates.append(app.Path(__file__).resolve().parent.parent / 'build_version.txt')
    version = 'dev'
    for c in candidates:
        if c.exists():
            version = c.read_text(encoding='utf-8').strip()
            break
    repo_root = str(app.Path(__file__).resolve().parent.parent.parent)
    # 调试：公共池实际写入位置（排查 Railway 持久卷是否生效、Cookie 落到哪）
    pool_info = {}
    try:
        import cookie_pool as _cp
        d = _cp._POOL_DIR
        files = []
        if d.exists():
            for f in d.iterdir():
                if f.is_file():
                    files.append({'name': f.name, 'size': f.stat().st_size})
        pool_info = {
            'dir': str(d),
            'mount_env': os.environ.get('RAILWAY_VOLUME_MOUNT_PATH', ''),
            'files': files,
        }
    except Exception as e:
        pool_info = {'error': str(e)}
    return {
        'version': version,
        'exe': app.sys.executable,
        'git_sha': _git_sha(repo_root),
        'railway_commit': os.environ.get('RAILWAY_GIT_COMMIT_SHA', ''),
        'railway_deployment': os.environ.get('RAILWAY_DEPLOYMENT_ID', ''),
        'railway_branch': os.environ.get('RAILWAY_GIT_BRANCH', ''),
        'cookie_pool': pool_info,
    }

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
    info = {'region': app.NODE_REGION, 'peer': app.PEER_ENDPOINT, 'china_domains': list(app.CHINA_DOMAINS), 'commentary_enabled': app.COMMENTARY_ENABLED, 'ads_enabled': app.ADS_ENABLED, 'convert': {'subscription_required': app.CONVERT_SUB_ENABLED, 'free_daily': app.CONVERT_FREE_DAILY, 'max_upload_bytes': app.UPLOAD_MAX_BYTES, 'targets': list(app.CONVERT_TARGETS_AVAILABLE)}, 'download': {'subscription_required': app.DOWNLOAD_SUB_ENABLED, 'free_daily': app.DOWNLOAD_FREE_DAILY}, 'library': {'enabled': app.plat.is_desktop() or bool(app.os.environ.get('VDL_LIBRARY_ENABLED'))}, 'subscriptions': {'enabled': app.SUB_ENABLED, 'probe_limit': app.SUBSCRIBE_PROBE_LIMIT, 'check_interval': app.SUB_CHECK_INTERVAL}, 'retention': {'enabled': app.RETENTION_ENABLED, 'trash_available': app.retention_mod.trash_available() if app.RETENTION_ENABLED else False}, 'crypto': {'enabled': app.CRYPTO_ENABLED, 'has_pass': bool(app._vault_load()) if app.CRYPTO_ENABLED else False, 'locked': app.VAULT_KEY is None}, 'torrent': {'enabled': app.TORRENT_ENABLED, 'available': app.torrent_mod.available()}, 'ai_dewatermark': {'enabled': app.AI_DEWATERMARK_ENABLED, 'gpu': app.AI_GPU_AVAILABLE, 'image_ai': bool(app.dwc_ai_available)}, 'authRequired': app.AUTH_REQUIRED, 'profile': ('web' if app.os.environ.get('VDL_INSTANCE') == 'cloud' else 'app')}
    caps = app.plat.node_capabilities()
    return {k: v for k, v in info.items() if k not in app.plat.NODE_GROUPS or k in caps}

@router.get('/api/admin/disk-usage')
def disk_usage() -> dict:
    """磁盘/目录占用诊断（运维端点，URL 隐秘；建议生产受 VDL_API_TOKEN 保护）。
    用于排查 ffmpeg 写输出 ENOSPC、容器磁盘满等问题。"""
    import shutil as _shutil
    total, used, free = _shutil.disk_usage(app.DOWNLOAD_DIR)

    def _dir_size(p):
        s = 0
        try:
            for f in p.rglob('*'):
                if f.is_file():
                    try: s += f.stat().st_size
                    except OSError: pass
        except OSError:
            pass
        return s

    return {
        'disk_total': total, 'disk_used': used, 'disk_free': free,
        'disk_used_pct': round(used * 100 / total, 1) if total else None,
        'download_dir_size': _dir_size(app.DOWNLOAD_DIR),
        'convert_dir_size': _dir_size(app.CONVERT_DIR),
        'convert_dir_files': sum(1 for _ in app.CONVERT_DIR.iterdir() if _.is_file()),
    }


@router.post('/api/resolve')
async def resolve(payload: app.ResolveRequest, request: app.Request) -> dict:
    app._check_rate_limit(request)
    app._assert_safe_url(payload.url)
    url, platform = app.parse_source(payload.url)
    host = app._host_of(url)
    if app.downloader._is_douyin_host(host):
        # 抖音走 VPS Playwright 真实浏览器解析，起 Chromium + 页面加载实测 25-30s
        timeout = 75
    elif app.downloader._is_iqiyi_host(host):
        # 爱奇艺 VPS Playwright worker：起 Chromium + 等播放器发 m3u8 请求，实测 30-50s
        timeout = 90
    elif 'bestv.com.cn' in host:
        # 百视TV wasm 签名 worker：实测 40-60s，默认 45s 会误超时
        timeout = 80
    elif host == 'v.qq.com':
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
        if app.downloader._is_douyin_host(host):
            detail = '抖音解析超时。抖音已升级反爬，网页端依赖 VPS 真实浏览器解析，偶发加载较慢。建议：①稍后重试；②确认链接是单个视频播放页（而非首页/列表）；③仍失败请反馈该链接'
        elif app.downloader._is_iqiyi_host(host):
            detail = '爱奇艺解析超时。依赖 VPS Playwright worker（启动 Chromium + 等播放器发 m3u8 请求）。建议：①稍后重试；②确认链接是分享页或 v_xxx.html 而非首页；③若持续失败请反馈该链接'
        elif host == 'v.qq.com':
            detail = '腾讯视频解析超时。该视频可能是会员/付费内容，或腾讯页面改版导致提取器暂时失效。建议：①在「高级选项」粘贴浏览器 Cookie 后重试；②确认视频可公开访问（非 VIP 专享）；③稍后重试或反馈此链接'
        elif 'youtube.com' in host or 'youtu.be' in host:
            detail = f'YouTube 解析超时（超过 {timeout} 秒）。常见原因：①代理速度慢或不稳定（YouTube 需要拉取 player.js 签名，代理延迟会叠加）；②该视频可能受限（地区/年龄限制）；建议：①检查代理是否通畅；②稍后重试；③若持续失败，尝试更换节点或关闭代理直连'
        else:
            detail = f'解析超时（超过 {timeout} 秒）。常见原因：①视频本身受限（限免/会员专享/付费/地区限制，这类通常需登录 cookie 才能拿到真实流，请到右上角「高级选项」粘贴浏览器 Cookie 后重试）；②当前网络无法访问该平台（可尝试在「高级选项」设置代理）'
        raise app.HTTPException(status_code=504, detail=detail) from None
    return {'url': url, 'platform': {'key': platform.key, 'name': platform.name}, 'video': app.downloader.summarize(info), 'qualities': app.downloader.build_quality_options(info), 'sources': []}


@router.post('/api/playlist')
async def playlist(payload: app.ResolveRequest, request: app.Request) -> dict:
    """解析歌单/专辑（网易云歌单、榜单、喜马拉雅专辑），返回曲目列表。

    与 /api/resolve 不同：不返回单个视频，而是返回 {title, count, items[]}，
    前端展示列表后由用户逐个/批量发起下载（每项是独立的单曲链接，复用单曲下载链路）。
    """
    app._check_rate_limit(request)
    app._assert_safe_url(payload.url)
    url, platform = app.parse_source(payload.url)
    timeout = 75  # 喜马拉雅专辑走 VPS Playwright（~12s）+ 网易云歌单 yt-dlp 提取
    loop = app.asyncio.get_running_loop()
    try:
        data = await app.asyncio.wait_for(
            loop.run_in_executor(app.prober, app.downloader.probe_playlist, url),
            timeout=timeout,
        )
    except app.asyncio.TimeoutError:
        raise app.HTTPException(status_code=504, detail='歌单/专辑解析超时（超过 75 秒）。常见原因：专辑集数过多、或当前网络访问该平台较慢。建议稍后重试') from None
    return {
        'url': url,
        'platform': {'key': platform.key, 'name': platform.name},
        'title': data.get('title') or '歌单/专辑',
        'count': data.get('count') or 0,
        'items': data.get('items') or [],
    }

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

_MEDIA_EXT_BY_TYPE = {
    'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov',
    'video/x-matroska': '.mkv', 'video/mpeg': '.mpg', 'video/x-flv': '.flv',
    'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/aac': '.aac',
    'audio/ogg': '.ogg', 'audio/wav': '.wav', 'audio/x-wav': '.wav',
}

@router.get('/api/media/proxy')
def media_proxy(u: str='', cookie: str='', dl: str='', request: app.Request=None):
    """下载用媒体中继（对标 DataTool 的 /api/proxy/media）。

    为什么下载必须走服务端中继：浏览器 `fetch` 跨域拿不到 CDN 的
    `Content-Length` / `Content-Range`（CDN 不回 `Access-Control-Expose-Headers`），
    没有总长度就无法分片、也无法画进度条，只能退化成一个整文件请求。
    本端点把源站响应连同 Range 语义一并透传，前端即可做
    「10MB/片 · 3 并发 · 3 重试」的断点式分片下载。

    与 `/api/stream/proxy` 的分工（**互不影响，别合并**）：
      - `/api/stream/proxy` 服务于 `<video>` 在线播放，会改写 m3u8 内部 URL、强制带 Referer；
      - 本端点只做**字节透传**，不改写任何内容，专供下载。
    只透传不改写，是为了让 `Content-Length` 与 `Content-Range` 保持源站原值，
    前端才能据此切分片；一旦改写（如 gzip）长度就对不上，分片必然错位。
    """
    if not u:
        raise app.HTTPException(status_code=400, detail='缺少 u 参数')
    app._assert_safe_url(u)
    host = app._host_of(u)
    _proxies: dict | None = None
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
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        # 强制 identity：带 gzip 时源站给的 Content-Length 是压缩前长度，
        # 透传给前端会导致分片区间错位（分片下载的经典坑）。
        'Accept-Encoding': 'identity',
    }
    _ref = _stream_referer(host)
    if _ref:
        headers['Referer'] = _ref
    if cookie_text:
        headers['Cookie'] = cookie_text
    _client_range: str | None = None
    if request:
        _cr = request.headers.get('range')
        if _cr:
            _client_range = _cr
            headers['Range'] = _cr
    try:
        resp = app.requests.get(u, headers=headers, stream=True, timeout=(10, 300), proxies=_proxies)
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
    content_type = resp.headers.get('Content-Type') or 'application/octet-stream'
    _resp_headers = {
        'Cache-Control': 'no-store',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Range',
        'Access-Control-Expose-Headers': 'Content-Length, Content-Range, Accept-Ranges, Content-Type',
        'X-Accel-Buffering': 'no',
        # 下载语义：始终声明支持 Range，这样前端 3 路并发各拿一段互不干扰。
        'Accept-Ranges': resp.headers.get('Accept-Ranges') or 'bytes',
    }
    if resp.headers.get('Content-Length'):
        _resp_headers['Content-Length'] = resp.headers['Content-Length']
    if _client_range and resp.status_code == 206 and resp.headers.get('Content-Range'):
        _resp_headers['Content-Range'] = resp.headers['Content-Range']
    if dl:
        _base = content_type.split(';')[0].strip().lower()
        _ext = _MEDIA_EXT_BY_TYPE.get(_base, '')
        _fname = dl if ('.' in dl.rsplit('/', 1)[-1]) else f'{dl}{_ext}'
        _resp_headers['Content-Disposition'] = (
            "attachment; filename*=UTF-8''" + app.quote(_fname, safe='')
        )

    def _gen():
        try:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        finally:
            resp.close()
    return app.StreamingResponse(_gen(), media_type=content_type, headers=_resp_headers, status_code=resp.status_code)

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

def _device_of(request: app.Request) -> str:
    """取设备 ID：优先请求头 X-Device-Id（fetch 请求），其次 query device=
    （EventSource / <a href> 文件下载无法带自定义 header，走 query）。"""
    dev = (request.headers.get("X-Device-Id") or "").strip()
    if not dev:
        dev = (request.query_params.get("device") or "").strip()
    return dev[:64]


# ---- V1 下载配额墙（2026-09-28 修复「任务成功创建后 used 恒 0」）-------------- #
# 规格（V1 方案 6.7）：/api/download、/api/batch 设墙 —— 免费 10 次/日、会员 1000 次/日；
# 任务成功创建才计费；超限 402。此前会员引擎的 use_daily 从未被下载链路调用，
# used 永远是 0，免费限额形同虚设。
#
# 计数权威 = cn 节点（会员 store 本地落盘 memberships/{uid}.json，token 也是 cn 签发
# 的 HMAC —— hk 本机验不了、也读不到 cn 的文件）。故：
#   · cn / 单节点（桌面、无回派目标）：直接 current_member_store 预检 + 计数；
#   · global 节点（hk）：把原始 Authorization 原样回派 cn 的 /api/member/quota/use
#     （check_only 预检 → 创建成功后回派计数）；cn 不可达时 fail-open（不挡下载）；
#   · 匿名（无/无效 token）：**一律 403 拒绝**（2026-09-28 用户拍板：网页版与 App 看齐，
#     下载必须登录 —— 匿名共享池方案作废）。hk 无法本地验 cn token，靠回派响应的
#     NO_AUTH 识别匿名并同样 403。会员引擎异常仍 fail-open（已登录用户不因故障被挡）。

# 下载强制登录的统一文案（前端据此弹登录框，见 web/app.js 的 needLogin 处理）
_LOGIN_REQUIRED_MSG = "下载前请先登录账号（免费账号每日 10 次下载额度，注册即得）"


def _quota_relay_base() -> str:
    """global 节点回派配额的目标（cn 权威）。VDL_QUOTA_RELAY_URL 优先，缺省复用
    VDL_WORKER_URL（海外→国内任务回派本就指向 cn，同一条链路不新增配置）。"""
    return (os.environ.get("VDL_QUOTA_RELAY_URL")
            or os.environ.get("VDL_WORKER_URL") or "").strip().rstrip("/")


def _relay_member_quota(request, payload: dict):
    """把配额检查/计数回派给 cn。返回 cn 响应 dict；任何失败返回 None（fail-open）。"""
    base = _quota_relay_base()
    if not base:
        return None
    import urllib.request as _ureq
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    auth = request.headers.get("Authorization")
    if auth:  # 原样带上 cn 签发的 bearer，由 cn 验签并定位用户
        headers["Authorization"] = auth
    req = _ureq.Request(base + "/api/member/quota/use",
                        data=_json.dumps(payload).encode("utf-8"),
                        headers=headers, method="POST")
    try:
        with _ureq.urlopen(req, timeout=10) as r:
            return _json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except Exception as e:  # noqa: BLE001 — 配额服务异常绝不挡下载
        try:
            app.logger.warning("[member-quota] 回派 cn 失败（fail-open）: %s", str(e)[:160])
        except Exception:  # noqa: BLE001
            pass
        return None


def _member_quota_gate(request, need: int = 1) -> dict:
    """下载配额预检（不计数）。超限抛 402；放行返回 gate 描述（供计数阶段复用）。

    need：本次打算创建的任务数（批量用）。剩余不足时按剩余数放行（调用方据此
    截断批量，与旧 IP 配额「创建到额度耗尽为止」的语义一致）。
    """
    try:
        if app.NODE_REGION != "cn" and _quota_relay_base():
            res = _relay_member_quota(request, {"resource": "download", "n": need,
                                                "check_only": True})
            if isinstance(res, dict) and res.get("ok") is False:
                if res.get("code") == "MEMBER_QUOTA":
                    raise app.HTTPException(status_code=402,
                                            detail=res.get("error") or "今日免费下载次数已用尽")
                if res.get("code") == "NO_AUTH":
                    # hk 验不了 cn 的 token，匿名/无效 token 由 cn 判定后回传
                    raise app.HTTPException(status_code=403, detail=_LOGIN_REQUIRED_MSG)
            remaining = res.get("remaining") if isinstance(res, dict) else None
            return {"mode": "relay", "remaining": remaining if isinstance(remaining, int) else None}
        # 下载必须登录（2026-09-28 与 App 行为对齐）：匿名一律 403，配额按账号计
        if not app.get_current_user_id(request):
            raise app.HTTPException(status_code=403, detail=_LOGIN_REQUIRED_MSG)
        store = app.current_member_store(request)
        q = store.quota_state("download")
        if q.get("unlimited") or q.get("unknown") or q.get("allowed", True):
            return {"mode": "local", "store": store, "remaining": q.get("remaining")}
        if q.get("tier") == "free":
            detail = (f"今日免费下载次数已用尽（{q['limit']}/日），"
                      f"开通下载会员可解锁 {q.get('member_limit', 0)} 次/日")
        else:
            detail = f"今日下载配额已用尽（{q['limit']}/日）"
        raise app.HTTPException(status_code=402, detail=detail)
    except app.HTTPException:
        raise
    except Exception:  # noqa: BLE001 — 会员引擎异常 fail-open
        return {"mode": "local", "store": None, "remaining": None}


def _member_quota_count(request, gate: dict, n: int = 1) -> dict | None:
    """任务成功创建后的计数（V1：成功创建才计费）。失败只记日志，不影响已建任务。

    匿名（回派收到 NO_AUTH）落本机全局匿名共享池 —— 与 user_membership 的
    「匿名回退 app.member_store」同一份，保证预检与计数落在同一个池。
    """
    if n <= 0:
        return None
    try:
        if gate.get("mode") == "relay":
            res = _relay_member_quota(request, {"resource": "download", "n": n})
            if isinstance(res, dict) and res.get("ok") is False and res.get("code") == "NO_AUTH":
                return app.member_store.use_daily("download", n=n)
            return res
        store = gate.get("store") or app.current_member_store(request)
        return store.use_daily("download", n=n)
    except Exception as e:  # noqa: BLE001 — 计数失败绝不回滚已创建的任务
        try:
            app.logger.warning("[member-quota] 计数失败（忽略）: %s", str(e)[:160])
        except Exception:  # noqa: BLE001
            pass
        return None


def _mq_public(mq) -> dict | None:
    """把计数结果裁成响应里的公开字段（None/失败 → None，前端不展示）。"""
    if not isinstance(mq, dict) or not mq.get("ok"):
        return None
    return {k: mq[k] for k in ("resource", "used", "remaining") if k in mq}


@router.post('/api/download')
def create_download(payload: app.DownloadRequest, request: app.Request) -> dict:
    app._check_rate_limit(request)
    subscribed, free_used, free_daily = app._check_download_quota(request)
    url, platform = app.parse_source(payload.url)
    if not app.downloader.is_valid_quality(payload.quality):
        raise app.HTTPException(status_code=400, detail='不支持的清晰度选项')
    extract_mode = _valid_extract_mode(payload.extract_script)
    gate = _member_quota_gate(request, need=1)
    task = app.store.create(url=url, title='', platform=platform.name, quality=app.downloader.quality_label(payload.quality), quality_key=payload.quality, extract_mode=extract_mode, concurrent_fragments=payload.concurrent_fragments, downloader_type=payload.downloader, cookie=payload.cookie, proxy=payload.proxy, play_url=payload.play_url, watch_options=payload.watch_options, is_hls=payload.is_hls, device_id=_device_of(request))
    app.scheduler.submit(app.downloader.run_download, task, app.store, payload.quality, payload.cookie, payload.proxy, app.SINGLE_DOWNLOAD_RETRIES, payload.format_id, payload.concurrent_fragments, payload.downloader)
    # V1：任务成功创建才计费（预检在 store.create 之前，计数在其之后）
    member_quota = _mq_public(_member_quota_count(request, gate, n=1))
    return {'task_id': task.id, 'status': task.status, 'quota': {'subscribed': subscribed, 'free_used': free_used, 'free_daily': free_daily}, 'member_quota': member_quota}

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
    # V1 配额墙预检：剩余 0 → 402；剩余不足按剩余数截断（创建到额度耗尽为止）
    gate = _member_quota_gate(request, need=len(urls))
    member_cap = gate.get('remaining') if isinstance(gate.get('remaining'), int) else len(urls)
    if member_cap is not None and member_cap <= 0:
        raise app.HTTPException(status_code=402, detail='今日免费下载次数已用尽，开通下载会员可解锁更多次数')
    if payload.concurrency > 0:
        app.scheduler.set_concurrency(payload.concurrency)
    retries = payload.retries if payload.retries >= 0 else app.BATCH_RETRIES_DEFAULT
    task_ids: list[str] = []
    skipped = 0
    quota_exhausted = False
    for u in urls:
        if member_cap is not None and len(task_ids) >= member_cap:
            quota_exhausted = True
            break
        try:
            app._check_download_quota(request)
        except app.HTTPException as exc:
            if exc.status_code == 402:
                quota_exhausted = True
                break
            raise
        try:
            url, platform = app.parse_source(u)
        except (app.UnsupportedPlatformError, app.LinkError):
            skipped += 1
            continue
        task = app.store.create(url=url, title='', platform=platform.name, quality=app.downloader.quality_label(payload.quality), quality_key=payload.quality, extract_mode=extract_mode, device_id=_device_of(request))
        app.scheduler.submit(app.downloader.run_download, task, app.store, payload.quality, payload.cookie, payload.proxy, retries)
        task_ids.append(task.id)
    if not task_ids:
        if quota_exhausted:
            raise app.HTTPException(status_code=402, detail='今日免费下载次数已用完，订阅可解锁无限下载')
        raise app.HTTPException(status_code=400, detail='链接均无法识别，请确认是视频播放页链接')
    # V1：任务成功创建才计费（按实际创建数，剩余不足时只计创建的那部分）
    member_quota = _mq_public(_member_quota_count(request, gate, n=len(task_ids)))
    return {'task_ids': task_ids, 'count': len(task_ids), 'skipped': skipped, 'quota_exhausted': quota_exhausted, 'member_quota': member_quota}

@router.get('/api/tasks')
def list_tasks(request: app.Request) -> dict:
    """列出当前设备可见的任务（设备隔离：只返回本设备创建 + 系统任务），供前端队列概览。"""
    tasks = [t.to_public_dict() for t in app.store.list_all(device=_device_of(request))]
    stats = {'pending': 0, 'downloading': 0, 'merging': 0, 'completed': 0, 'failed': 0, 'canceled': 0}
    for t in tasks:
        stats[t['status']] = stats.get(t['status'], 0) + 1
    stats['active'] = app.scheduler.active_count()
    return {'tasks': tasks, 'stats': stats, 'concurrency': app.scheduler.concurrency}

@router.post('/api/tasks/{task_id}/retry')
def retry_task(task_id: str, request: app.Request) -> dict:
    task = app._require_task(task_id, _device_of(request))
    # B5：成品已清理的历史条目（completed+file_expired）也允许「重新下载」
    _hist_redownload = task.status == 'completed' and getattr(task, 'file_expired', False)
    if task.status not in ('failed', 'canceled') and not _hist_redownload:
        raise app.HTTPException(status_code=400, detail='仅失败 / 已取消 / 成品已清理的任务可以重试')
    task.cancel_requested = False
    task.cancel_reason = ''
    resume = app.downloader._has_partial(task.workdir) and not _hist_redownload
    app.store.update(task_id, status='pending', error='', hint='', progress=task.progress if resume else 0.0, downloaded_bytes=task.downloaded_bytes if resume else 0, total_bytes=task.total_bytes if resume else 0, speed=0.0, eta=0, filesize=0, filename='', resumable=False, file_expired=False, filepath=None)
    app.scheduler.submit(app.downloader.run_download, task, app.store, task.quality_key, task.cookie, task.proxy, app.BATCH_RETRIES_DEFAULT, '', task.concurrent_fragments, task.downloader_type, resume)
    return {'task_id': task_id, 'status': 'pending', 'resume': resume}

@router.post('/api/tasks/{task_id}/extract-text')
def reextract_text(task_id: str, request: app.Request) -> dict:
    """对已完成任务重新提取文案（如首次语音转写超时，可点重试）。"""
    task = app._require_task(task_id, _device_of(request))
    if not task.extract_mode:
        raise app.HTTPException(status_code=400, detail='该任务未开启文案提取')
    if not task.filepath or not app.Path(task.filepath).exists():
        raise app.HTTPException(status_code=400, detail='任务文件不存在，无法提取文案')
    app.executor.submit(app.downloader._run_extraction, task, app.store, app.Path(task.filepath), None, '', '', mode=task.extract_mode)
    return {'task_id': task_id, 'status': 'running'}

@router.post('/api/tasks/cancel-all')
def cancel_all_tasks(request: app.Request) -> dict:
    """取消当前设备所有进行中 / 排队中的任务；已完成与失败的任务保留（不删文件）。"""
    canceled = 0
    for t in app.store.list_all(device=_device_of(request)):
        if not t.is_finished and app.store.request_cancel(t.id):
            canceled += 1
    return {'canceled': canceled}

@router.get('/api/batch/config')
def batch_config() -> dict:
    return {'concurrency': app.scheduler.concurrency, 'hard_max': app.VDL_BATCH_HARD_MAX, 'retries': app.BATCH_RETRIES_DEFAULT}

@router.get('/api/tasks/{task_id}')
def task_status(task_id: str, request: app.Request) -> dict:
    return app._require_task(task_id, _device_of(request)).to_public_dict()

@router.get('/api/tasks/{task_id}/events')
async def task_events(task_id: str, request: app.Request) -> app.StreamingResponse:
    app._require_task(task_id, _device_of(request))

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
def download_file(task_id: str, request: app.Request, download: int=0) -> app.Response:
    task = app._require_task(task_id, _device_of(request))
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
def cancel_task(task_id: str, request: app.Request) -> dict:
    """进行中的任务 → 请求取消并保留记录；已结束的任务 → 连同文件一起清理。"""
    task = app._require_task(task_id, _device_of(request))
    if task.is_finished:
        app.store.remove(task_id)
        return {'task_id': task_id, 'canceled': False, 'removed': True}
    return {'task_id': task_id, 'canceled': app.store.request_cancel(task_id), 'removed': False}

@router.post('/api/tasks/{task_id}/pause')
def pause_task(task_id: str, request: app.Request) -> dict:
    """暂停正在下载的任务——保留 .part 文件，后续可断点续传。"""
    task = app._require_task(task_id, _device_of(request))
    if task.is_finished:
        return {'task_id': task_id, 'paused': False, 'message': '任务已结束，无法暂停'}
    if task.status == 'paused':
        return {'task_id': task_id, 'paused': True, 'message': '已暂停'}
    task.pause_requested = True
    task.add_step('下载音视频', 'pending', '正在暂停…')
    app.store.update(task.id, status='pausing')
    return {'task_id': task_id, 'paused': True}

@router.post('/api/tasks/{task_id}/resume')
def resume_task(task_id: str, request: app.Request) -> dict:
    """继续被暂停的下载——yt-dlp 自动从已下载的 .part 文件断点续传。"""
    task = app._require_task(task_id, _device_of(request))
    if task.status not in ('paused',):
        return {'task_id': task_id, 'resumed': False, 'message': '任务未处于暂停状态'}
    task.pause_requested = False
    task.add_step('下载音视频', 'running', '继续下载…')
    task.log('用户继续下载（断点续传）')
    app.store.update(task.id, status='downloading')
    app.scheduler.submit(app.downloader.run_download, task, app.store, task.quality_key, '', '', app.SINGLE_DOWNLOAD_RETRIES)
    return {'task_id': task_id, 'resumed': True}


class FeedbackRequest(app.BaseModel):
    content: str = app.Field(default='', max_length=2000)
    contact: str = app.Field(default='', max_length=100)


@router.post('/api/feedback')
def create_feedback(payload: FeedbackRequest, request: app.Request) -> dict:
    """留言反馈：保存到 downloads/.feedback.json（追加，保留最近 500 条）。
    走限流防止刷屏；content 必填，contact 可选。
    """
    app._check_rate_limit(request)
    content = (payload.content or '').strip()
    if not content:
        raise app.HTTPException(status_code=400, detail='反馈内容不能为空')
    record = {
        "ts": int(app.time.time()),
        "device": _device_of(request),
        "contact": (payload.contact or '').strip()[:100],
        "content": content[:2000],
        "ua": (request.headers.get("User-Agent") or "")[:200],
    }
    path = app.DOWNLOAD_DIR / ".feedback.json"
    return _append_feedback(path, record)


# 反馈写入锁（模块级，防止并发写坏 JSON）
_feedback_lock_obj = app.threading.Lock()


def _feedback_lock():
    return _feedback_lock_obj


def _append_feedback(path, record: dict) -> dict:
    with _feedback_lock():
        items = []
        try:
            if path.exists():
                data = app.json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    items = data
        except Exception:
            items = []
        items.append(record)
        items = items[-500:]  # 只保留最近 500 条，防膨胀
        try:
            path.write_text(app.json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass  # 写入失败不阻塞反馈接口
    return {"ok": True, "ts": record["ts"]}


# --------------------------------------------------------------------------- #
# 运维可观测性：事件日志 / 前端错误上报 / 诊断导出
# --------------------------------------------------------------------------- #
ADMIN_KEY = (os.environ.get("VDL_ADMIN_KEY") or "").strip()


def _is_local_request(request) -> bool:
    """桌面端本机 WebView 通过 127.0.0.1 访问，视为可信，免密钥放行运维端点。"""
    try:
        host = request.client.host if request.client else ""
    except Exception:
        host = ""
    return host in ("127.0.0.1", "::1", "localhost", "")


def _require_admin(request):
    """运维端点鉴权：本机（桌面 App）放行；远端必须带正确 X-Admin-Key。
    未配置 VDL_ADMIN_KEY 时远端一律 403，避免误暴露。"""
    if _is_local_request(request):
        return
    key = (request.headers.get("X-Admin-Key") or "").strip()
    if ADMIN_KEY and key == ADMIN_KEY:
        return
    raise app.HTTPException(status_code=403, detail="需要管理员密钥（X-Admin-Key）")


@router.post('/api/client-error')
def client_error(payload: dict, request: app.Request):
    """前端 JS 运行期错误上报（window.onerror / unhandledrejection）。

    让网站前端报错也能在服务端事件日志查到，闭环「用户报问题我们看不到记录」。
    仅记录 message 必填；其余为可选上下文。
    """
    app._check_rate_limit(request)
    msg = (payload.get("message") or "").strip()[:2000]
    if not msg:
        raise app.HTTPException(status_code=400, detail="缺少 message")
    extra = {
        "stack": (payload.get("stack") or "")[:3000],
        "url": (payload.get("url") or "")[:500],
        "line": payload.get("line"),
        "col": payload.get("col"),
        "level": payload.get("level", "error"),
        "category": payload.get("category", "client_js"),
    }
    app.record_event("error", "client_js", msg, request=request, extra=extra)
    return {"ok": True}


@router.get('/api/admin/events')
def admin_events(request: app.Request, limit: int = 200, level: str = "", range: str = ""):
    """读取结构化事件日志（运维视图）。本机免密钥，远端需 X-Admin-Key。
    range 时间窗同 /api/admin/visits；空=全部（兼容 diagnostic 旧调用）。"""
    _require_admin(request)
    from datetime import datetime, timezone, timedelta
    threshold = _ops_range_start(range)
    path = app.EVENT_LOG_PATH
    items = []
    if path.exists():
        try:
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = _json.loads(ln)
                except Exception:
                    continue
                if level and rec.get("level") != level:
                    continue
                if threshold is not None:
                    ts = rec.get("ts")
                    if ts is not None:
                        try:
                            _dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=8)))
                            if _dt < threshold:
                                continue
                        except Exception:
                            pass
                items.append(rec)
        except Exception:
            pass
    items = items[-max(1, min(int(limit), 2000)):]
    return {"count": len(items), "events": items, "range": range or "all", "range_label": _OPS_RANGE_LABELS.get(range, "全部")}


def _collect_local_diag() -> dict:
    """聚合桌面端本机诊断文件（仅桌面 App 本地实例存在；网页版返回空）。"""
    out: dict = {}
    launch = _Path.home() / ".vdl_launch.log"
    if launch.exists():
        try:
            txt = launch.read_text(encoding="utf-8", errors="replace")
            out["launch_log_tail"] = "\n".join(txt.splitlines()[-120:])
            out["launch_log_size"] = launch.stat().st_size
        except Exception:
            pass
    stats = _Path.home() / ".video-downloader" / "stats.json"
    if stats.exists():
        try:
            out["stats"] = _json.loads(stats.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            pass
    return out


@router.get('/api/diagnostic')
def diagnostic(request: app.Request):
    """导出诊断信息包：版本指纹 + 近期事件 + 桌面端本机日志/统计。
    桌面端「导出诊断信息」按钮调用；本机免密钥，远端需 X-Admin-Key。"""
    _require_admin(request)
    data: dict = {"generated_at": int(_time.time())}
    try:
        data["version"] = api_version()
    except Exception as e:
        data["version"] = {"error": str(e)}
    try:
        data["recent_events"] = admin_events(request, limit=50).get("events", [])
    except Exception:
        data["recent_events"] = []
    data["local_diag"] = _collect_local_diag()
    return app.JSONResponse(content=data)


_NGINX_ACCESS_LOG = "/var/log/nginx/access.log"
_STATIC_SUFFIX = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".ico",
                  ".woff", ".woff2", ".ttf", ".svg", ".map", ".json")


def _nginx_log_paths(max_files: int = 40) -> list:
    """当前 + 轮转的历史访问日志路径，**按时间从旧到新**排列。

    🔴 2026-10-03：nginx 每天轮转（access.log 只有当天，昨天的在 access.log.1，
    更早是 .gz）。原来只读 access.log，于是「三天/每周/每月」拿到的都是当天数据
    —— 四个范围看起来一模一样，用户以为筛选没生效。现在把轮转文件一并读入，
    再按 range 的时间窗过滤。
    """
    import pathlib as _pl

    base = _pl.Path(_NGINX_ACCESS_LOG)
    older = []
    idx = 1
    while idx <= max_files:
        gz = base.with_name(base.name + f".{idx}.gz")
        plain = base.with_name(base.name + f".{idx}")
        if gz.exists():
            older.append(gz)
        elif plain.exists():
            older.append(plain)
        else:
            break
        idx += 1
    older.reverse()          # 最旧在前
    return older + ([base] if base.exists() else [])


def _read_nginx_log_lines(per_file_cap: int = 20000) -> list:
    """读全部访问日志行（.gz 自动解压），每文件只取末尾若干行防爆内存。"""
    import gzip as _gz

    out: list = []
    for p in _nginx_log_paths():
        try:
            if p.suffix == ".gz":
                with _gz.open(p, "rt", encoding="utf-8", errors="replace") as f:
                    out.extend(f.readlines()[-per_file_cap:])
            else:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    out.extend(f.readlines()[-per_file_cap:])
        except Exception:
            continue          # 单个文件读不动就跳过，不影响其它
    return out


# 机器/脚本 UA 特征（我们自己 App 的轮询、脚本、扫描器等）
_MACHINE_UA_MARKS = (
    "python-requests", "python-urllib", "curl/", "wget", "Go-http-client",
    "okhttp", "Java/", "l9explore", "axios", "node-fetch", "PostmanRuntime",
    "python-http-client", "aiohttp", "scrapy", "HeadlessChrome", "bot",
)


def _is_machine_ua(ua: str) -> bool:
    """UA 是否属于机器/脚本（不是真人浏览器）。"""
    s = str(ua or "")
    return any(mark in s for mark in _MACHINE_UA_MARKS)


def _is_page_path(path: str) -> bool:
    """是否「页面级」请求：排除接口、网关、内部路径与静态资源。"""
    p = str(path or "")
    if not p or p.startswith("/api/") or p.startswith("/gw/") or p.startswith("/internal"):
        return False
    if p.endswith(_STATIC_SUFFIX):
        return False
    return True


@router.get("/api/admin/visits")
def admin_visits(request: app.Request, limit: int = 100, range: str = ""):
    """网站访客汇总：解析 nginx access.log，统计独立 IP / 状态码 / 热点路径 / 最近访问。

    range 时间窗：day=今天 / 3d=近三天 / week=近七天 / month=近三十天；空=全部（兼容旧调用）。
    这是「网页访问记录」的真正数据源（首页 /api/* 之外的真实外部访问）。
    本机（桌面 App WebView）免密钥，远端必须带正确 X-Admin-Key。
    """
    _require_admin(request)
    from datetime import datetime, timedelta, timezone
    threshold = _ops_range_start(range)
    import re as _re
    from collections import Counter
    line_re = _re.compile(
        r'^(?P<ip>\S+) \S+ \S+ \[(?P<t>[^\]]+)\] "(?P<m>\S+) (?P<p>\S+) [^"]*" '
        r'(?P<s>\d{3}) (?P<sz>\S+) "(?P<ref>[^"]*)" "(?P<ua>[^"]*)"'
    )
    total = 0
    ips: set = set()
    by_status: Counter = Counter()
    by_method: Counter = Counter()
    top_paths: Counter = Counter()
    top_ips: Counter = Counter()
    recent: list = []
    # 真人访客口径（2026-10-03）：原口径统计**所有** HTTP 请求，其中 ~80% 是
    # 接口调用 + 我们自己 App 的轮询（python-requests 等），且独立 IP 里混着
    # 机主自己的浏览器与机器 IP —— 老板看数字无法判断「今天来了几个真人」。
    # 这里另算一套：只认「浏览器 UA + 页面级路径（排除 /api /gw /internal /静态资源）」。
    human_pv = 0
    human_uv: set = set()
    human_ips: set = set()
    human_pages: Counter = Counter()
    machine_requests = 0
    try:
        raw_lines = _read_nginx_log_lines()
    except Exception as e:
        return {"error": f"无法读取访问日志：{e}", "total": 0, "source": _NGINX_ACCESS_LOG}
    if not raw_lines:
        return {"error": "访问日志为空或不可读", "total": 0, "source": _NGINX_ACCESS_LOG}
    for ln in raw_lines:
        m = line_re.search(ln)
        if not m:
            continue
        if threshold is not None:
            try:
                _dt = datetime.strptime(m.group("t"), "%d/%b/%Y:%H:%M:%S %z")
                if _dt < threshold:
                    continue
            except Exception:
                pass
        total += 1
        ip = m.group("ip")
        path = m.group("p")
        status = m.group("s")
        ips.add(ip)
        by_status[status] += 1
        by_method[m.group("m")] += 1
        top_ips[ip] += 1
        if not path.lower().endswith(_STATIC_SUFFIX):
            top_paths[path] += 1
        recent.append({
            "t": m.group("t"),
            "ip": ip,
            "m": m.group("m"),
            "p": path,
            "s": int(status),
            "ua": m.group("ua")[:140],
        })
        # ---- 真人访客口径 ----
        ua = m.group("ua") or ""
        if _is_machine_ua(ua) or "Mozilla" not in ua:
            machine_requests += 1
        elif not _is_page_path(path):
            machine_requests += 1     # 浏览器请求接口/静态资源也算「非真人页面访问」
        else:
            human_pv += 1
            human_ips.add(ip)
            human_uv.add((ip, hash(ua) & 0xFFFF))
            human_pages[path] += 1
    return {
        "total": total,
        "unique_ips": len(ips),
        "by_status": dict(by_status.most_common()),
        "by_method": dict(by_method.most_common()),
        "top_paths": [{"path": k, "count": v} for k, v in top_paths.most_common(20)],
        "top_ips": [{"ip": k, "count": v} for k, v in top_ips.most_common(15)],
        "recent": recent[-max(1, min(int(limit), 200)):],
        "source": _NGINX_ACCESS_LOG,
        "log_files": len(_nginx_log_paths()),
        "human": {
            "pv": human_pv,
            "uv": len(human_uv),
            "ips": len(human_ips),
            "machine_requests": machine_requests,
            "top_pages": [{"path": k, "count": v} for k, v in human_pages.most_common(10)],
            "note": "真人访客 = 浏览器 UA 且访问页面级路径；已排除 /api /gw /internal 接口、静态资源与机器请求（App 轮询/健康检查/脚本）",
        },
        "range": range or "all",
        "range_label": _OPS_RANGE_LABELS.get(range, "全部"),
    }


@router.get("/ops")
def ops_console(request: app.Request):
    """运维控制台页面：并排展示「网站访客」与「错误/异常事件」的可视化。

    页面壳本身不鉴权（只是 HTML）；真正的数据接口 /api/admin/* 仍受 X-Admin-Key 保护，
    没有密钥拉不到任何数据。前端在页面内输入密钥后本地拉取并渲染。
    """
    html_path = _Path(__file__).resolve().parent.parent.parent / "web" / "ops" / "index.html"
    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception as e:
        return app.Response(f"<h1>运维控制台页面缺失</h1><p>{e}</p>", media_type="text/html", status_code=500)
    return app.Response(html, media_type="text/html")


# --------------------------------------------------------------------------- #
# App 内运维看板：本地代理拉取 ECS 数据 + 看板页面（WKWebView 同源加载）
# --------------------------------------------------------------------------- #
_OPS_ADMIN_KEY_FILE = _Path.home() / ".video-downloader" / "ops_admin_key"
_OPS_ECS_BASE = (os.environ.get("VDL_OPS_ECS_BASE") or "http://8.138.223.3:8888").strip()


def _ops_range_start(range_key: str):
    """看板时间窗 key → CST 时区起始 datetime；非法/空返回 None（不过滤，兼容旧调用）。

    day=今天 00:00 CST / 3d=近三天 / week=近七天 / month=近三十天。
    """
    from datetime import datetime, timedelta, timezone
    if not range_key or range_key not in ("day", "3d", "week", "month"):
        return None
    _tz = timezone(timedelta(hours=8))
    now = datetime.now(_tz)
    if range_key == "3d":
        return now - timedelta(days=3)
    if range_key == "week":
        return now - timedelta(days=7)
    if range_key == "month":
        return now - timedelta(days=30)
    # day：今天 00:00 CST
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


_OPS_RANGE_LABELS = {"day": "每天", "3d": "三天", "week": "每周", "month": "每月", "": "全部"}


def _ops_admin_key() -> str:
    """运维（超级管理员）密钥解析优先级：环境变量 > 本机文件 > 空。

    安全约束：二进制/服务端**不内置**任何可用密钥。ECS 侧由 VDL_ADMIN_KEY 环境变量
    持有超级管理员密钥；未配置时远端一律 403，避免误暴露。
    """
    env = (os.environ.get("VDL_ADMIN_KEY") or "").strip()
    if env:
        return env
    try:
        if _OPS_ADMIN_KEY_FILE.exists():
            k = _OPS_ADMIN_KEY_FILE.read_text(encoding="utf-8").strip()
            if k:
                return k
    except Exception:
        pass
    return ""


def _require_local(request):
    """App 内看板代理/页面仅限本机 WebView（127.0.0.1）访问，防远端滥用。"""
    if not _is_local_request(request):
        raise app.HTTPException(status_code=403, detail="仅限本机访问")


@router.get("/api/app/ops-visits")
def app_ops_visits(request: app.Request, limit: int = 100, range: str = ""):
    """App 内看板代理：本机放行，带密钥去 ECS 拉「网站访客」汇总并转发给前端。

    数据仍在 ECS（nginx 日志），App 不落盘、不缓存——纯代理展示，不会撑爆 App。
    """
    _require_local(request)
    try:
        resp = app.requests.get(
            f"{_OPS_ECS_BASE}/api/admin/visits",
            params={"limit": max(1, min(int(limit), 200)), "range": range},
            headers={"X-Admin-Key": _ops_admin_key()},
            timeout=15,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"拉取 ECS 访客数据失败：{e}")


@router.get("/api/app/ops-events")
def app_ops_events(request: app.Request, limit: int = 200, level: str = "", range: str = ""):
    """App 内看板代理：本机放行，带密钥去 ECS 拉「错误/异常事件」并转发给前端。"""
    _require_local(request)
    try:
        resp = app.requests.get(
            f"{_OPS_ECS_BASE}/api/admin/events",
            params={"limit": max(1, min(int(limit), 2000)), "level": level, "range": range},
            headers={"X-Admin-Key": _ops_admin_key()},
            timeout=15,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"拉取 ECS 事件数据失败：{e}")


# ── 授权中心告警/对账转发（2026-10-03）：桌面 App /api/app/license-* 的 ECS 侧落点 ──
# 链路：App 本机 /api/app/license-*（X-Admin-Key）→ 本文件 /api/license-*
#       → 127.0.0.1:8902 /api/license/{alerts|alerts_ack|recon}（body token 门禁）。
# 此前 ECS 一直没有这三个路由，App 运维监控的「异常告警/对账」恒报 Not Found。

def _license_admin_token() -> str:
    return (os.environ.get("VDL_LICENSE_ADMIN_TOKEN") or "").strip()


def _license_base() -> str:
    return (os.environ.get("VDL_LICENSE_BASE") or "http://127.0.0.1:8902").strip().rstrip("/")


@router.get("/api/license-alerts")
def ecs_license_alerts(request: app.Request, since: float = 0.0,
                       unseen_only: bool = False, limit: int = 100):
    """授权中心异常告警转发：桌面 App 带 X-Admin-Key 来查 8902 的 alerts。"""
    _require_admin(request)
    try:
        resp = app.requests.post(
            f"{_license_base()}/api/license/alerts",
            json={"token": _license_admin_token(), "since": since,
                  "unseen_only": bool(unseen_only),
                  "limit": max(1, min(int(limit), 200))},
            timeout=12,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"拉取授权中心告警失败：{e}")


@router.post("/api/license-alerts/ack")
def ecs_license_alerts_ack(payload: dict, request: app.Request):
    """确认（已读）授权中心告警。ids 为空数组 = 全部确认。"""
    _require_admin(request)
    try:
        resp = app.requests.post(
            f"{_license_base()}/api/license/alerts_ack",
            json={"token": _license_admin_token(), "ids": list(payload.get("ids") or [])},
            timeout=12,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"确认告警失败：{e}")


@router.post("/api/license-recon")
def ecs_license_recon(payload: dict, request: app.Request):
    """每日入账/充值对账报告转发（超管专用）。"""
    _require_admin(request)
    try:
        days = int(payload.get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    try:
        resp = app.requests.post(
            f"{_license_base()}/api/license/recon",
            json={"token": _license_admin_token(), "days": max(1, min(days, 60))},
            timeout=15,
        )
        return app.JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        raise app.HTTPException(status_code=502, detail=f"拉取对账报告失败：{e}")


@router.get("/ops-board")
def ops_board(request: app.Request):
    """App 内运维看板页面：WKWebView 同源加载本机 /ops-board，fetch /api/app/*。

    页面壳不鉴权（本机已 _require_local）；真正数据走 /api/app/ops-*（带密钥代理 ECS）。
    """
    _require_local(request)
    html_path = _Path(__file__).resolve().parent.parent.parent / "web" / "ops" / "board.html"
    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception as e:
        return app.Response(f"<h1>运维看板页面缺失</h1><p>{e}</p>", media_type="text/html", status_code=500)
    return app.Response(html, media_type="text/html")
