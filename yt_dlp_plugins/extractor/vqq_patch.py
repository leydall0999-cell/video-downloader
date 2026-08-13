"""腾讯视频（v.qq.com）yt-dlp 提取器补丁。

背景：
  腾讯视频新版 SPA 页面把视频元数据从旧版 ``window.__pinia__`` 改为运行时 JS 填充，
  yt-dlp 内置 ``VQQVideoIE`` 仅从页面 pinia / OG meta 取标题与时长，拿不到后
  title 变为占位符 ``vqq-video video #...``、``duration`` 为 ``None``，
  进而被误判为「会员/受限」内容而拒绝下载。

  但 getvinfo API 的响应 ``vl.vi[0]`` 本身就带有完整元数据：
    - ``ti`` = 真实标题
    - ``td`` = 时长（秒，字符串如 "215.96"）
    - ``vw`` / ``vh`` = 宽高

  本插件以**同名类**覆盖内置 ``VQQVideoIE``（插件加载机制会把同名类前置到提取器链），
  ``_VALID_URL`` / ``IE_NAME`` 保持一致，仅重写元数据提取逻辑，从 API 直接取标题/时长。

支持 URL 格式：
  https://v.qq.com/x/page/<vid>.html
  https://v.qq.com/x/cover/<series_id>/<vid>.html
  https://v.qq.com/x/m/play?vid=<vid>   （由 platforms.py 归一化后走本提取器）
"""
from yt_dlp.extractor.tencent import VQQVideoIE as _BuiltinVQQVideoIE
from yt_dlp.utils import float_or_none, traverse_obj


class VQQVideoIE(_BuiltinVQQVideoIE):
    IE_NAME = 'vqq:video'
    # 保持与内置一致的 URL 匹配规则（不覆盖，直接继承）
    _VALID_URL = _BuiltinVQQVideoIE._VALID_URL

    def _real_extract(self, url):
        video_id, series_id = self._match_valid_url(url).group('id', 'series_id')
        webpage = self._download_webpage(url, video_id)
        webpage_metadata = self._get_webpage_metadata(webpage, video_id)

        formats, subtitles = self._extract_all_video_formats_and_subtitles(url, video_id, series_id)

        # 单独再取一次 API 拿标题/时长（新版 SPA 页面 pinia 已失效，但 API 自带）
        # 包一层 try：即便接口异常也不应阻断下载，title 回退到 OG/pinia 旧逻辑
        video_info = {}
        try:
            meta_resp = self._get_video_api_response(url, video_id, series_id, 'srt', 'hls', 'hd')
            video_info = traverse_obj(meta_resp, ('vl', 'vi', 0), default={}) or {}
        except Exception:
            video_info = {}

        title = (self._get_clean_title(video_info.get('ti') or video_info.get('nm'))
                 or self._og_search_title(webpage, default=None)
                 or traverse_obj(webpage_metadata, ('global', 'videoInfo', 'title')))

        return {
            'id': video_id,
            'title': title,
            'description': (self._og_search_description(webpage, default=None)
                            or traverse_obj(webpage_metadata, ('global', 'videoInfo', 'desc'))),
            'formats': formats,
            'subtitles': subtitles,
            'duration': float_or_none(video_info.get('td')),
            'thumbnail': (self._og_search_thumbnail(webpage, default=None)
                          or video_info.get('pic')
                          or traverse_obj(webpage_metadata, ('global', 'videoInfo', 'pic160x90'))),
            'series': traverse_obj(webpage_metadata, ('global', 'coverInfo', 'title')),
        }
