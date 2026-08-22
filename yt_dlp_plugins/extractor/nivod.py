"""nivod.vip（泥视频·海外华人在线影院）yt-dlp 提取器。

nivod 是苹果CMS（maccms）架构的影视聚合站：
- 详情页 `/nivod/{vid}/`：列出所有剧集播放链接 `/niplay/{vid}-{sid}-{nid}/`
- 播放页 `/niplay/{vid}-{sid}-{nid}/`：HTML 内嵌 `var player_aaaa = {...}`，
  其中 `url`/`url_next` 就是该集视频的 m3u8 直链（m3u8.vhmzy.com 等 CDN），
  无需登录、无需 JS 渲染，纯 requests 即可拿到。

流程：
  1) 接受详情页 / 播放页两种 URL（播放页直接解析；详情页取第一个播放链接）；
  2) 平衡括号稳健提取 player_aaaa JSON（内部字符串可能含 `{`/`}`/`;`）；
  3) 从 `url`（m3u8）构造 formats；vod_name + 集数做标题；
  4) `url_next` 提示下一集（供合集场景使用，此处仅取当前集）。
"""
import json
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, int_or_none


class NivodIE(InfoExtractor):
    IE_NAME = "nivod"
    _VALID_URL = r"https?://(?:www\.)?nivod\.vip/(?:nivod|niplay)/(\d+)(?:-(\d+)-(\d+))?/?"

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        vid = mobj.group(1)
        sid = mobj.group(2)
        nid = mobj.group(3)
        video_id = f"{vid}-{sid}-{nid}" if sid and nid else vid

        page_url = url
        if not (sid and nid):
            # 详情页：取第一个剧集播放链接
            html = self._download_webpage(url, video_id)
            m = re.search(r'href="(/niplay/' + vid + r'-\d+-\d+/)"', html)
            if not m:
                raise ExtractorError("nivod: 未找到剧集播放链接（详情页可能为空）")
            page_url = "https://www.nivod.vip" + m.group(1)
            sid_nid = re.search(r"niplay/\d+-(\d+)-(\d+)/", m.group(1))
            video_id = f"{vid}-{sid_nid.group(1)}-{sid_nid.group(2)}"

        html = self._download_webpage(page_url, video_id)
        data = self._extract_player_data(html)
        if not data:
            raise ExtractorError("nivod: 未解析到 player_aaaa 播放数据（页面结构可能已变）")

        video_url = data.get("url") or ""
        if not video_url:
            raise ExtractorError("nivod: 播放数据中没有视频直链")
        video_url = video_url.replace("\\/", "/")

        vod_data = data.get("vod_data") or {}
        title = vod_data.get("vod_name") or "nivod 视频"
        # 集数信息（从 link_next / link 提取当前第几集）
        ep_match = re.search(r"-(\d+)/?$", (data.get("link") or ""))
        ep = ep_match.group(1) if ep_match else ""
        if ep:
            title = f"{title} 第{int(ep)}集"

        formats = [{
            "url": video_url,
            "ext": "mp4",
            "protocol": "m3u8_native",
            "format_id": "m3u8",
            "format_note": "m3u8 直链",
        }]

        return {
            "id": video_id,
            "title": title,
            "webpage_url": page_url,
            "extractor_key": self.IE_NAME,
            "extractor": self.IE_NAME,
            "formats": formats,
            "thumbnail": (vod_data.get("vod_pic") or "").replace("\\/", "/") or None,
            "duration": int_or_none(vod_data.get("vod_duration")) if vod_data.get("vod_duration") else None,
            "uploader": vod_data.get("vod_actor") or None,
            "series": vod_data.get("vod_name") or None,
            "url_next": (data.get("url_next") or "").replace("\\/", "/") or None,
        }

    def _extract_player_data(self, html: str) -> dict:
        """平衡括号提取 var player_aaaa = {...} JSON（兼容字符串内 {} 和 ;）。"""
        m = re.search(r"player_aaaa\s*=\s*(\{)", html)
        if not m:
            return {}
        start = m.start(1)
        brace = 0
        in_str = False
        esc = False
        end = None
        for i in range(start, len(html)):
            ch = html[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                brace += 1
            elif ch == "}":
                brace -= 1
                if brace == 0:
                    end = i + 1
                    break
        if not end:
            return {}
        try:
            return json.loads(html[start:end])
        except Exception:
            return {}


class NivodPlaylistIE(InfoExtractor):
    """nivod 剧集合集：从详情页 /nivod/{vid}/ 提取全部剧集（供合集批量下载）。"""

    IE_NAME = "nivod:playlist"
    _VALID_URL = r"https?://(?:www\.)?nivod\.vip/nivod/(\d+)/?$"

    def _real_extract(self, url):
        vid = self._match_valid_url(url).group(1)
        html = self._download_webpage(url, vid)
        entries = []
        seen = set()
        for m in re.finditer(r'href="(/niplay/' + vid + r'-(\d+)-(\d+)/)"[^>]*>\s*([^<]{0,40})', html):
            path, sid, nid, label = m.group(1), m.group(2), m.group(3), (m.group(4) or "").strip()
            if path in seen:
                continue
            seen.add(path)
            entries.append({
                "_type": "url",
                "url": "https://www.nivod.vip" + path,
                "ie_key": NivodIE.IE_NAME,
                "title": f"{label or '第' + str(int(nid)) + '集'}",
            })
        if not entries:
            raise ExtractorError("nivod: 详情页没有剧集列表")
        return {
            "_type": "playlist",
            "id": vid,
            "title": f"nivod {vid} 合集",
            "entries": entries,
        }
