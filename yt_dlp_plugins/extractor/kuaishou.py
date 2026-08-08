"""快手（kuaishou.com）yt-dlp 提取器。

页面通过 window.__INIT_STATE__ 内嵌视频数据，本提取器解析该 JSON 获取无水印视频地址。

支持 URL 格式：
  https://www.kuaishou.com/short-video/<id>
  https://www.kuaishou.com/f/<分享码>
  https://v.kuaishou.com/<短码>
"""
import json
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError


class KuaishouIE(InfoExtractor):
    IE_NAME = "kuaishou"
    _VALID_URL = r"https?://(?:(?:www|m)\.kuaishou\.com/(?:short-video|f)/(?P<id>[\w-]+)|v\.kuaishou\.com/(?P<short>[\w-]+))"

    _TESTS = [{
        "url": "https://www.kuaishou.com/short-video/3xabcdef1234",
        "only_matching": True,
    }]

    _UA = (
        "Mozilla/5.0 (Linux; Android 12; SM-G9910) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    )

    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        video_id = mobj.group("id") or mobj.group("short")

        webpage = self._download_webpage(
            url, video_id,
            headers={"User-Agent": self._UA},
            note="正在下载快手页面…",
        )

        # 尝试从 __INIT_STATE__ 提取（主要来源）
        init_state = self._search_json(
            r"window\.__INIT_STATE__\s*=\s*",
            webpage, "INIT_STATE", video_id,
            fatal=False,
        )
        if not init_state:
            init_state = self._search_json(
                r"window\.__APOLLO_STATE__\s*=\s*",
                webpage, "APOLLO_STATE", video_id,
                fatal=False,
            )
        if init_state:
            return self._parse_init_state(video_id, init_state)

        # 兜底：尝试从 <video> 标签提取
        video_url = self._html_search_regex(
            r'<video[^>]+src=["\']([^"\']+)["\']',
            webpage, "video src", default=None,
        )
        if video_url:
            title = self._html_search_regex(
                r"<title>([^<]+)</title>",
                webpage, "title", default=video_id,
            )
            return {
                "id": video_id,
                "title": title.strip(),
                "formats": [{"url": video_url, "ext": "mp4"}],
            }

        raise ExtractorError(
            "无法从快手页面提取视频。可能是页面结构已变更或需要登录（请在前端「高级选项」注入 Cookie）",
            expected=True,
        )

    def _parse_init_state(self, video_id, state):
        """从 __INIT_STATE__ 中提取视频信息。"""
        # 视频数据可能在多个层级，递归查找 mainMvUrls
        video_info = self._find_video(state)
        if not video_info:
            raise ExtractorError("INIT_STATE 中未找到视频数据", expected=True)

        # 提取视频播放地址
        formats = []
        mv_urls = video_info.get("mainMvUrls") or []
        if isinstance(mv_urls, list):
            for url in mv_urls:
                if not url or not isinstance(url, str):
                    continue
                formats.append({
                    "url": url,
                    "ext": "mp4",
                    "format_id": "default",
                })

        # 备用：manifest 中的 HLS 流
        manifest = video_info.get("manifest") or {}
        if isinstance(manifest, dict):
            adaptation_set = (
                manifest.get("adaptationSet") or []
                if isinstance(manifest.get("adaptationSet"), list)
                else [manifest.get("adaptationSet")] if manifest.get("adaptationSet") else []
            )
            for adp in adaptation_set:
                if not isinstance(adp, dict):
                    continue
                representations = adp.get("representation") or []
                if isinstance(representations, dict):
                    representations = [representations]
                for rep in representations:
                    rep_url = rep.get("url") if isinstance(rep, dict) else None
                    if rep_url:
                        formats.append({
                            "url": rep_url,
                            "ext": "mp4",
                            "format_id": str(rep.get("id", "")),
                            "format_note": rep.get("name", ""),
                            "height": rep.get("height"),
                        })

        if not formats:
            raise ExtractorError("未找到可播放的视频地址", expected=True)

        # 标题
        title = (
            video_info.get("caption")
            or video_info.get("title")
            or video_info.get("name")
            or video_id
        )

        # 作者
        uploader = video_info.get("userName") or video_info.get("authorName") or ""
        uploader_id = str(video_info.get("userId") or "")

        # 描述
        description = video_info.get("description") or video_info.get("caption") or ""

        # 缩略图
        thumbnail = video_info.get("coverUrl") or video_info.get("poster") or ""

        # 时长（毫秒 → 秒）
        duration = 0
        raw_dur = video_info.get("duration")
        if raw_dur:
            try:
                duration = float(raw_dur)
                if duration > 1000:
                    duration /= 1000  # 毫秒
            except (TypeError, ValueError):
                pass

        return {
            "id": video_id,
            "title": str(title)[:200],
            "formats": formats,
            "duration": duration or None,
            "uploader": str(uploader) if uploader else None,
            "uploader_id": str(uploader_id) if uploader_id else None,
            "description": str(description)[:500] if description else None,
            "thumbnail": str(thumbnail) if thumbnail else None,
        }

    def _find_video(self, obj, depth=0):
        """递归查找包含 mainMvUrls 的子对象。"""
        if depth > 10 or obj is None:
            return None
        if isinstance(obj, dict):
            if "mainMvUrls" in obj:
                return obj
            if "video" in obj and isinstance(obj["video"], dict):
                return self._find_video(obj["video"], depth + 1)
            for v in obj.values():
                result = self._find_video(v, depth + 1)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = self._find_video(item, depth + 1)
                if result:
                    return result
        return None
