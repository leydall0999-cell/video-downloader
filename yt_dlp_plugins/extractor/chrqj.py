"""chrqj.com (影视聚合站) 专用 yt-dlp 提取器。

该站为 Next.js 应用，播放地址通过签名接口返回：
  GET https://www.chrqj.com/mw-movie/anonymous/v2/video/episode/url
      ?clientType=1&id=<影片id>&nid=<选集nid>
请求头需带 sign / t / deviceId / authorization，其中：
  sign = SHA1( MD5("clientType=1&id=..&nid=..&key=<SIGN_KEY>&t=<timestamp_ms>") )
接口返回 data.list[]，每项含 m3u8 播放地址与清晰度。
"""

import hashlib
import time
import uuid

from yt_dlp.extractor.common import InfoExtractor


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

        formats = []
        for item in item_list:
            play_url = item.get('url')
            if not play_url:
                continue
            resolution = item.get('resolution') or 0
            name = item.get('resolutionName') or ('%sp' % resolution)
            need_login = bool(item.get('needLogin')) and not item.get('flag')
            note = name + ('（需登录）' if need_login else '')
        formats.append({
            'url': play_url,
            'format_id': '%s-%s' % (resolution, name),
            'format_note': note,
            'ext': 'mp4',
            'protocol': 'm3u8_native',  # 走 python 原生 HLS 下载器，避开 ffmpeg（沙盒偶发 SIGXCPU 强杀）
            'vcodec': 'h264',
            'acodec': 'aac',
            'height': resolution if isinstance(resolution, int) else None,
            # 免登录清晰度优先，避免默认选到需登录的高清导致下载失败
            'preference': 1 if not need_login else -1,
            'http_headers': {
                'Referer': self._WEB_HOST,
                'User-Agent': self._UA,
            },
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
