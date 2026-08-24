"""斗鱼直播提取器兼容补丁。

yt-dlp 内置 DouyuTVIE 用旧正则提取 room_id（旧页面为 $ROOM.room_id = N 形式），
但 2026 斗鱼新版页面改用 JSON 格式（"roomInfo":{"room":{"room_id":601979），
导致 "Unable to extract room id"。本插件继承 DouyuTVIE，仅重写 _real_extract
的字段提取部分（兼容新旧两种格式），签名/流格式等逻辑复用父类。
"""

import hashlib
import time
import urllib.parse

from yt_dlp.extractor.douyutv import DouyuTVIE, DouyuBaseIE
from yt_dlp.utils import UserNotLive, traverse_obj


class DouyuCompatIE(DouyuBaseIE):
    # 与内置 DouyuTVIE 同名（yt-dlp 插件同名覆盖内置提取器）
    IE_NAME = 'DouyuTV'
    IE_DESC = '斗鱼直播 (compat)'
    _VALID_URL = DouyuTVIE._VALID_URL

    _TESTS = []

    def _real_extract(self, url):
        video_id = self._match_id(url)

        webpage = self._download_webpage(url, video_id)

        # 新版：JSON 转义格式 "room_id":601979 / \"room_id\":601979 / room_id\":601979
        # 旧版：$ROOM.room_id = 601979
        room_id = self._search_regex(
            r'(?:\$ROOM\.room_id\s*=\s*|\\?"room_id\\?"\s*:\s*|room_id\\?"?\s*:\s*)(\d+)',
            webpage, 'room id')

        # videoLoop / show_status 同样兼容新旧格式
        if self._search_regex(
                r'(?:videoLoop\\?"?\s*:\s*|\$ROOM\.videoLoop\s*=\s*)(\d+)',
                webpage, 'loop', default='') == '1':
            raise UserNotLive('The channel is auto-playing VODs', video_id=video_id)
        if self._search_regex(
                r'(?:\\?"show_status\\?"\s*:\s*|\$ROOM\.show_status\s*=\s*)(\d+)',
                webpage, 'status', default='') == '2':
            raise UserNotLive(video_id=video_id)

        # Grab metadata from API
        params = {
            'aid': 'wp',
            'client_sys': 'wp',
            'time': int(time.time()),
        }
        params['auth'] = hashlib.md5(
            f'room/{room_id}?{urllib.parse.urlencode(params)}zNzMV1y4EMxOHS6I5WKm'.encode()).hexdigest()
        room = traverse_obj(self._download_json(
            f'http://www.douyutv.com/api/v1/room/{room_id}', video_id,
            note='Downloading room info', query=params, fatal=False), 'data')

        # 1 = live, 2 = offline
        if traverse_obj(room, 'show_status') == '2':
            raise UserNotLive(video_id=video_id)

        js_sign_func = self._search_js_sign_func(webpage, fatal=False) or self._get_sign_func(room_id, video_id)
        form_data = {
            'rate': 0,
            **self._calc_sign(js_sign_func, video_id, room_id),
        }
        stream_formats = [self._download_json(
            f'https://www.douyu.com/lapi/live/getH5Play/{room_id}',
            video_id, note='Downloading livestream format',
            data=urllib.parse.urlencode(form_data).encode())]

        for rate_id in traverse_obj(stream_formats[0], ('data', 'multirates', ..., 'rate')):
            if rate_id != traverse_obj(stream_formats[0], ('data', 'rate')):
                form_data['rate'] = rate_id
                stream_formats.append(self._download_json(
                    f'https://www.douyu.com/lapi/live/getH5Play/{room_id}',
                    video_id, note=f'Downloading livestream format {rate_id}',
                    data=urllib.parse.urlencode(form_data).encode()))

        return {
            'id': room_id,
            'formats': self._extract_stream_formats(stream_formats),
            'is_live': True,
            **traverse_obj(room, {
                'display_id': ('url', {str}, {lambda i: i[1:]}),
                'title': ('room_name', {str}),
                'description': ('show_details', {str}),
                'uploader': ('nickname', {str}),
                'thumbnail': ('room_src', {str}),
            }),
        }
