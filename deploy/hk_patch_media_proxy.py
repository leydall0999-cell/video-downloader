#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给香港节点（/opt/vdl，更老分支）定点追加「下载用媒体中继」端点 /api/media/proxy。

为什么必须补：
  网页版解析海外站靠香港，但下载分片的中继端点只在 ECS 上。前端 `_dlRunHls` 的
  「对端→主站回落」因此会把海外 HLS 的分片全推给 ECS 中继 —— 而 ECS 出海是"看 CDN 运气"：
  实测取 mux 清单 200、取 cdn.jwplayer.com 清单 **502**。凡 ECS 连不上的海外 CDN，
  「浏览器内合成」直接挂。香港直连同一地址 200/0.37s。

  补完后浏览器直连香港拿分片 ⇒ 覆盖度补齐，且 ECS 出网归零。

设计约束（老分支，风险要压到最低）：
  1. 只**追加**一个新端点到 core.py，不改任何既有函数；
  2. 端点自包含：不依赖新分支特有符号（_MEDIA_EXT_BY_TYPE 在老分支没有，就地定义）；
  3. 保留 `_assert_safe_url` SSRF 校验（绝不能做成任意内网代理）；
  4. 代理分流交给既有 `_resolve_proxy`（香港侧：海外直连、国内走 VDL_PROXY_CN）；
  5. 幂等：已存在则跳过；锚点命中数必须恰好 1，否则拒绝写入。
"""
import io
import os
import shutil
import sys
import time

CORE = '/opt/vdl/server/routers/core.py'
APPPY = '/opt/vdl/server/app.py'
BACKUP_DIR = '/opt/vdl/backup'

ANCHOR_CORE = "@router.get('/api/cookie/status')"
ANCHOR_CORS = 'allow_headers=["Content-Type", "X-Subscription-Key", "X-Api-Key", "X-Device-Id"]'

NEW_ENDPOINT = '''
# ── 下载用媒体中继（2026-09-27 定点追加；与 /api/stream/proxy 分工不同，勿合并）──
_PROXY_DL_EXT = {
    'video/mp4': '.mp4', 'video/webm': '.webm', 'video/quicktime': '.mov',
    'video/x-matroska': '.mkv', 'video/mpeg': '.mpg', 'video/x-flv': '.flv',
    'video/mp2t': '.ts', 'video/x-mpegurl': '.m3u8',
    'application/vnd.apple.mpegurl': '.m3u8', 'application/octet-stream': '.bin',
    'audio/mpeg': '.mp3', 'audio/mp4': '.m4a', 'audio/aac': '.aac',
    'audio/ogg': '.ogg', 'audio/wav': '.wav', 'audio/x-wav': '.wav',
}


@router.get('/api/media/proxy')
def media_proxy(u: str='', cookie: str='', dl: str='', request: app.Request=None):
    """下载用媒体中继：把源站响应连同 Range 语义**原样透传**，供前端做分片并发下载与
    「浏览器内 HLS 合成」。

    为什么下载必须走服务端中继：浏览器 fetch 跨域拿不到 CDN 的 Content-Length /
    Content-Range（CDN 不回 Access-Control-Expose-Headers），没有总长度就无法分片、
    也无法画进度条，只能退化成一个整文件请求。

    与 /api/stream/proxy 的分工（互不影响，别合并）：
      - /api/stream/proxy 服务于 <video> 在线播放，会改写 m3u8 内部 URL、强制带 Range；
      - 本端点只做字节透传，不改写任何内容，专供下载。
    只透传不改写，是为了让 Content-Length 与 Content-Range 保持源站原值，
    前端才能据此切分片；一旦改写（如 gzip）长度就对不上，分片必然错位。
    """
    if not u:
        raise app.HTTPException(status_code=400, detail='缺少 u 参数')
    app._assert_safe_url(u)
    host = app._host_of(u)
    _proxies = None
    try:
        # 两地分流交给 _resolve_proxy：本节点在香港 ⇒ 海外站直连、国内站走 VDL_PROXY_CN。
        _proxy_url = app.downloader._resolve_proxy(host)
        if _proxy_url:
            _proxies = {'http': _proxy_url, 'https': _proxy_url}
    except Exception:
        _proxies = None
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
    _client_range = None
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
        _ext = _PROXY_DL_EXT.get(_base, '')
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


'''


def backup(path):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, os.path.basename(path) + '.pre-media-proxy.' + time.strftime('%Y%m%d-%H%M%S'))
    shutil.copy2(path, dst)
    print('  备份 →', dst)


def patch_core():
    src = io.open(CORE, encoding='utf-8').read()
    if '/api/media/proxy' in src:
        print('[core.py] 已存在 /api/media/proxy，跳过')
        return False
    n = src.count(ANCHOR_CORE)
    if n != 1:
        raise SystemExit(f'[core.py] 锚点命中 {n} 次（期望 1），拒绝写入：{ANCHOR_CORE}')
    backup(CORE)
    src = src.replace(ANCHOR_CORE, NEW_ENDPOINT.lstrip('\n') + ANCHOR_CORE)
    io.open(CORE, 'w', encoding='utf-8').write(src)
    print('[core.py] 已追加 media_proxy 端点')
    return True


def patch_app():
    src = io.open(APPPY, encoding='utf-8').read()
    if ANCHOR_CORS not in src:
        print('[app.py] 未找到 CORS allow_headers 锚点，跳过（需人工确认）')
        return False
    target = 'allow_headers=["Content-Type", "X-Subscription-Key", "X-Api-Key", "X-Device-Id", "Range"]'
    if target in src:
        print('[app.py] allow_headers 已含 Range，跳过')
        return False
    n = src.count(ANCHOR_CORS)
    if n != 1:
        raise SystemExit(f'[app.py] 锚点命中 {n} 次（期望 1），拒绝写入')
    backup(APPPY)
    src = src.replace(ANCHOR_CORS, target)
    io.open(APPPY, 'w', encoding='utf-8').write(src)
    print('[app.py] CORS allow_headers 已放行 Range（分片并发下载需要带 Range 头）')
    return True


def main():
    print('== 香港节点 /api/media/proxy 定点补丁 ==')
    before = len(io.open(CORE, encoding='utf-8').read().splitlines())
    a = patch_core()
    b = patch_app()
    after = len(io.open(CORE, encoding='utf-8').read().splitlines())
    print(f'core.py 行数 {before} → {after} (+{after - before})')
    if a or b:
        print('== 语法自检 ==')
        import py_compile
        for p in (CORE, APPPY):
            try:
                py_compile.compile(p, doraise=True)
                print('  OK', p)
            except Exception as exc:
                raise SystemExit(f'  语法错误 {p}: {exc}')
    print('== 完成 ==')


if __name__ == '__main__':
    main()
