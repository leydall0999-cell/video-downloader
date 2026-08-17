"""chrqj.com (影视聚合站) 专用 yt-dlp 提取器。

该站为 Next.js 应用，播放地址通过签名接口返回：
  GET https://www.chrqj.com/mw-movie/anonymous/v2/video/episode/url
      ?clientType=1&id=<影片id>&nid=<选集nid>
请求头需带 sign / t / deviceId / authorization，其中：
  sign = SHA1( MD5("clientType=1&id=..&nid=..&key=<SIGN_KEY>&t=<timestamp_ms>") )
接口返回 data.list[]，每项含 m3u8 播放地址与清晰度。
"""

import hashlib
import os
import time
import uuid

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError


class ChrqjIE(InfoExtractor):
    IE_NAME = 'chrqj'
    _VALID_URL = r'https?://(?:www|m)\.chrqj\.com/vod/play/(?P<id>\d+)/\d+/(?P<nid>\d+)'

    _TESTS = [{
        'url': 'https://www.chrqj.com/vod/play/116537/1/877419',
        'only_matching': True,
    }]

    _SIGN_KEY = 'cb808529bae6b6be45ecfab29a4889bc'
    _API = 'https://www.chrqj.com/mw-movie/anonymous/v2/video/episode/url'
    _WEB_HOST = 'https://www.chrqj.com/'
    _UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

    def _load_cached_cookie(self, default='') -> str:
        """兜底读取本机 Cookie 缓存（~/.videodownloader/cookies/<host>.json）。

        当全局 http_headers 里没有 Cookie（例如 Cookie 仅在 yt-dlp 的浏览器
        jar 里、未透传进 http_headers）时，直接从缓存文件取，确保视频流带登录态。
        """
        # 部署平台（如 Railway）通过环境变量注入登录态，优先级最高，
        # 且不怕容器重建（缓存文件会随容器销毁而丢失）。格式为纯 Cookie 值
        # （如 "PHPSESSID=abc; uid=123"），不带 "Cookie: " 前缀。
        env_cookie = os.environ.get('CHRQJ_COOKIE')
        if env_cookie:
            return env_cookie

        try:
            import json
            from pathlib import Path
            cache_dir = Path.home() / ".videodownloader" / "cookies"
            for host in ("www.chrqj.com", "chrqj.com", "m.chrqj.com"):
                f = cache_dir / f"{host}.json"
                if f.exists():
                    data = json.loads(f.read_text())
                    h = data.get("header") or ""
                    if h:
                        return h
        except Exception:
            pass
        return default

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        vid = mobj.group('id')
        nid = mobj.group('nid')
        params = {'clientType': '1', 'id': vid, 'nid': nid}
        t = str(int(time.time() * 1000))
        # 与前端签名逻辑一致：参数按 key 排序 -> k=v&k=v
        g = '&'.join('%s=%s' % (k, params[k]) for k in sorted(params))
        h = '%s&key=%s&t=%s' % (g, self._SIGN_KEY, t)
        sign = hashlib.sha1(
            hashlib.md5(h.encode('utf-8')).hexdigest().encode('utf-8')
        ).hexdigest()

        headers = {
            'User-Agent': self._UA,
            'Referer': self._WEB_HOST,
            'Accept': 'application/json',
            'sign': sign,
            't': t,
            'deviceId': str(uuid.uuid4()),
            'authorization': '',
        }

        data = self._download_json(self._API, vid, query=params, headers=headers)
        if not isinstance(data, dict) or data.get('code') != 200:
            raise ExtractorError(
                'chrqj 接口返回异常: %s' % (data.get('msg') if isinstance(data, dict) else data))

        item_list = (data.get('data') or {}).get('list') or []

        # 标题优先取播放页 <title>
        title = nid
        try:
            webpage = self._download_webpage(url, vid, fatal=False) or ''
            title = (self._og_search_title(webpage, default=None)
                     or self._html_search_regex(r'<title>(.*?)</title>', webpage,
                                                'title', default=nid))
        except Exception:
            pass

        # 自动获取登录态 Cookie 并透传到视频流请求头：
        # ① 优先取全局 http_headers（用户粘贴 或 _base_options 已从本机浏览器解密注入）；
        # ② 兜底直接读本地 Cookie 缓存文件（~/.videodownloader/cookies/<host>.json），
        #    覆盖「Cookie 只在 yt-dlp jar 里、未进 http_headers」的情况。
        # 源站（m3u8/ts CDN）会校验播放页下发的会话 Cookie，缺它直接 403/拒绝。
        # 注意：yt-dlp 的 format 级 http_headers 会覆盖全局，必须显式合并进来，
        # 否则全局注入的 Cookie 在真正下载流时被丢掉。
        global_headers = (self._downloader.params.get('http_headers') or {}) if self._downloader else {}
        stream_cookie = global_headers.get('Cookie') or ''
        if not stream_cookie:
            stream_cookie = self._load_cached_cookie(vid) or ''

        formats = []
        for item in item_list:
            play_url = item.get('url')
            if not play_url:
                continue
            resolution = item.get('resolution') or 0
            # 接口可能返回数字或字符串，统一转成 int 供清晰度选择器识别
            try:
                resolution_int = int(resolution)
            except (TypeError, ValueError):
                resolution_int = 0
            name = item.get('resolutionName') or ('%sp' % resolution)
            need_login = bool(item.get('needLogin')) and not item.get('flag')
            note = name + ('（需登录）' if need_login else '')
            fmt_headers = {
                'Referer': self._WEB_HOST,
                'User-Agent': self._UA,
            }
            if stream_cookie:
                fmt_headers['Cookie'] = stream_cookie
            formats.append({
                'url': play_url,
                'format_id': '%s-%s' % (resolution, name),
                'format_note': note,
                'ext': 'mp4',
                'protocol': 'm3u8_native',  # 走 python 原生 HLS 下载器，避开 ffmpeg（沙盒偶发 SIGXCPU 强杀）
                'vcodec': 'h264',
                'acodec': 'aac',
                'height': resolution_int if resolution_int > 0 else None,
                # 免登录清晰度优先，避免默认选到需登录的高清导致下载失败
                'preference': 1 if not need_login else -1,
                'http_headers': fmt_headers,
            })

        if not formats:
            raise ExtractorError('未找到可播放的视频地址（该清晰度可能需登录）')

        return {
            'id': nid,
            'title': title,
            'formats': formats,
            'http_headers': {
                'Referer': self._WEB_HOST,
                'User-Agent': self._UA,
            },
        }
