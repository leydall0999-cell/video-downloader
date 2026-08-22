"""好看视频（haokan.baidu.com）yt-dlp 提取器。

原理：好看视频的播放地址加密在页面 SSR 数据 window.__PRELOADED_STATE__ 的
encrptedVideoMeta 字段里，解密算法：
  1) base64 解码 encrptedVideoMeta；
  2) XOR 解密（key = 'guanghui456'）得到 JSON；
  3) JSON.clarityUrl 数组 = 各清晰度 mp4 直链（从低到高，取最后一个最高清）。

该方案纯 HTTP 即可，无需浏览器渲染（2026-08-22 实测可用）。
"""

import base64
import json
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, traverse_obj

_XOR_KEY = "guanghui456"


class HaokanIE(InfoExtractor):
    IE_NAME = "Haokan"
    IE_DESC = "好看视频"
    _VALID_URL = r"https?://(?:haokan\.baidu\.com|www\.haokan\.com)/v\?(?:[^#]*&)?vid=(?P<id>\d+)(?:&|#|$|.*)"
    _TESTS = []

    @staticmethod
    def _xor_cipher(meta: str, key: str) -> str:
        return "".join(
            chr(ord(meta[i]) ^ ord(key[i % len(key)])) for i in range(len(meta))
        )

    def _real_extract(self, url):
        video_id = self._match_id(url)

        webpage = self._download_webpage(url, video_id, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
            ),
            "Referer": "https://haokan.baidu.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
            # 百度对无 Cookie 的数据中心请求返回简化页（无 encrptedVideoMeta），
            # BIDUPSID/PSTM 是百度系通用访客 Cookie（2026-08 实测 VPS 带此
            # Cookie 即可拿到完整 SSR 数据）
            "Cookie": "BIDUPSID=080A06EA5F585DF4688EF4D0F4152440; PSTM=1710125001",
        })

        # 1) 提取加密 meta
        m = re.search(r'"encrptedVideoMeta"\s*:\s*"(.*?)"', webpage, re.S)
        if not m:
            raise ExtractorError("未找到 encrptedVideoMeta 字段（页面结构可能已变更）", expected=True)
        meta = m.group(1)

        # 2) base64 + XOR 解密
        try:
            decoded = base64.b64decode(meta).decode()
        except Exception as e:
            raise ExtractorError(f"encrptedVideoMeta base64 解码失败: {e}", expected=True)
        try:
            data = json.loads(self._xor_cipher(decoded, _XOR_KEY))
        except Exception as e:
            raise ExtractorError(f"encrptedVideoMeta 解密失败: {e}", expected=True)

        title = str(data.get("title") or "好看视频")
        duration = data.get("duration")
        clarity_urls = data.get("clarityUrl") or []
        if not clarity_urls:
            raise ExtractorError("解密结果中没有清晰度链接（clarityUrl 为空）", expected=True)

        formats = []
        for i, item in enumerate(clarity_urls):
            u = item.get("url") if isinstance(item, dict) else None
            if not u:
                continue
            fmt = {
                "url": u,
                "format_id": f"hk-{i}",
                "ext": "mp4",
                "format_note": item.get("clarity") or "",
            }
            # clarity 字段可能是 360p/720p/1080p 或 cae_h264 等
            note = str(item.get("clarity") or "")
            m_h = re.search(r"(\d{3,4})[pP]", note)
            if m_h:
                fmt["height"] = int(m_h.group(1))
            formats.append(fmt)
        if not formats:
            raise ExtractorError("无有效视频链接", expected=True)
        # 按清晰度升序，前端 best 默认选最后一个（最高清）
        formats.sort(key=lambda f: f.get("height") or 0)

        return {
            "id": video_id,
            "title": title,
            "duration": duration,
            "thumbnail": data.get("poster") or "",
            "formats": formats,
            "extractor_key": "Haokan",
            "extractor": "haokan",
            "webpage_url": url,
        }
