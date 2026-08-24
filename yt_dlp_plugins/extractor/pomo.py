"""pomo.mom（第三方视频解析/聚合站）yt-dlp 提取器。

pomo 是「解析站」：后台管理员添加视频源（手动模式 gid=XXX）或自动搜索
（auto_search / auto_detail），播放器（hls.js + plyr）通过
`/content/plugins/plyr_player/api.php?type=parse&url=<src>` 把任意视频 URL
解析成 m3u8 直链。

流程：
  1) 打开 `?plugin=plyr_player&gid=XXX` 播放页；
  2) 若页面 `hasDbContent=true`（后台手动添加）：解析 `route1Data`/`route2Data`
     （JSON 数组，元素含 name/url），对每个 url 调 parse API 拿 m3u8；
  3) 若 `hasDbContent=false`：解析页面里的 `searchKeyword`，走
     `auto_search?wd=` → `auto_detail?site=&id=` 自动搜索拿到视频源，
     再 parse 出 m3u8；
  4) parse API 返回 JSON：data[源名][i].play_url 为 `名称$m3u8` 或
     `名称$url$$$名称$m3u8` 混合串，取最后一个 m3u8（HD 直链）。

注意：pomo 后台手动添加的 gid 内容可能过期（hasDbContent=false 且搜索也
无结果时抛错），这是站点侧内容问题，非解析器缺陷。
"""
import json
import re
import urllib.parse

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError

_API = "/content/plugins/plyr_player/api.php"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


class PomoIE(InfoExtractor):
    IE_NAME = "pomo"
    _VALID_URL = r"https?://(?:www\.)?pomo\.mom/?(?:\?.*)?$"

    def _real_extract(self, url):
        html = self._download_webpage(url, "pomo", headers={"User-Agent": _UA})
        base = "https://pomo.mom"
        title = (re.search(r"<title>([^<]+)</title>", html) or [None, "pomo 视频"])[1]
        title = re.sub(r"\s*-\s*在线播放$", "", title).strip() or "pomo 视频"

        # 1) 后台手动添加模式：route1Data / route2Data（JSON 数组）
        routes = self._extract_routes(html)
        if not routes:
            # 2) 自动搜索模式：用 searchKeyword 搜索
            kw = self._extract_search_keyword(html)
            if kw:
                routes = self._search_routes(base, kw)
        if not routes:
            raise ExtractorError(
                "pomo: 该页面没有可用视频源（后台手动内容可能已下线，且自动搜索无结果）")

        formats = []
        entries_urls = []
        seen = set()
        for name, src in routes:
            # 已是 m3u8 直链直接用；否则走 parse API 兜底
            play_url = src if ".m3u8" in src else self._parse_src(base, src)
            if play_url and play_url not in seen:
                seen.add(play_url)
                formats.append({
                    "url": play_url,
                    "ext": "mp4",
                    "protocol": "m3u8_native",
                    "format_id": f"pomo-{len(formats)}",
                    "format_note": name,
                })
            entries_urls.append((name, src, play_url))

        if not formats:
            raise ExtractorError("pomo: 解析 API 未返回视频直链")

        return {
            "id": re.search(r"[?&]gid=(\d+)", url).group(1) if re.search(r"[?&]gid=(\d+)", url) else "pomo",
            "title": title,
            "webpage_url": url,
            "extractor_key": self.IE_NAME,
            "extractor": self.IE_NAME,
            "formats": formats,
            "_pomo_routes": entries_urls,
        }

    # ---- 工具 ----
    def _extract_routes(self, html: str) -> list[tuple[str, str]]:
        """解析 hasDbContent 手动模式下的 route1Data/route2Data（元素含 name/url）。"""
        routes: list[tuple[str, str]] = []
        for var in ("route1Data", "route2Data"):
            m = re.search(var + r"\s*=\s*(\[[\s\S]*?\])\s*;", html)
            if not m:
                continue
            try:
                items = json.loads(m.group(1))
            except Exception:
                continue
            for it in items:
                name = it.get("name") or ""
                u = it.get("url") or ""
                if u:
                    routes.append((name, u))
        return routes

    def _extract_search_keyword(self, html: str) -> str:
        m = re.search(r'const searchKeyword\s*=\s*"([^"]*)"', html)
        if not m:
            return ""
        raw = m.group(1)
        # 页面里是 JS unicode 转义（如 \u73a9\u5177...），json.loads 可解成中文
        try:
            return json.loads('"' + raw + '"')
        except Exception:
            return raw

    def _api_json(self, base: str, query: str) -> dict:
        resp = self._download_webpage(
            base + _API + "?" + query, "pomo-api",
            headers={"User-Agent": _UA, "X-Requested-With": "XMLHttpRequest"},
        )
        try:
            return json.loads(resp)
        except Exception as e:
            raise ExtractorError(f"pomo: API 响应非 JSON: {str(e)[:80]}") from e

    def _search_routes(self, base: str, keyword: str) -> list[tuple[str, str]]:
        """自动搜索：auto_search 结果自带 play_url（含 m3u8），直接提取；
        无 m3u8 时用 auto_detail 拿剧集 → parse API 兜底。"""
        routes: list[tuple[str, str]] = []
        data = self._api_json(base, "type=auto_search&wd=" + urllib.parse.quote(keyword))
        sources = (data.get("data") or {}) if isinstance(data.get("data"), dict) else {}
        for site_name, items in sources.items():
            if not isinstance(items, list):
                continue
            for it in items:
                label = (it.get("name") or "").strip() or str(site_name)
                pu = it.get("play_url") or ""
                # play_url 形如 "HD中字$https://...m3u8" 或 "HD$url$$$HD$url"；取最后一个 m3u8
                m3u8s = re.findall(r"https?://[^\s$]+\.m3u8[^\s$]*", pu)
                if m3u8s:
                    routes.append((f"{site_name}·{label}", m3u8s[-1]))
                    continue
                # 无 m3u8：走 auto_detail + parse 兜底
                site_id = it.get("id")
                if not site_id:
                    continue
                try:
                    detail = self._api_json(
                        base, "type=auto_detail&site=" + urllib.parse.quote(str(site_name))
                        + "&id=" + urllib.parse.quote(str(site_id)))
                    detail_data = detail.get("data") or {}
                    routes_data = detail_data.get("routes") or []
                    for rt in routes_data:
                        for ep in (rt.get("episodes") or []):
                            u = ep.get("url") or ""
                            if u:
                                routes.append((f"{site_name}·{ep.get('name') or ''}", u))
                except Exception:
                    continue
        return routes

    def _parse_src(self, base: str, src: str) -> str:
        """调 parse API 把视频源 URL 解析成 m3u8 直链。"""
        try:
            data = self._api_json(base, "type=parse&url=" + urllib.parse.quote(src))
        except Exception:
            return ""
        d = data.get("data")
        if isinstance(d, dict):
            for site, items in d.items():
                if not isinstance(items, list):
                    continue
                for it in items:
                    pu = it.get("play_url") or ""
                    # play_url 形如 "HD中字$https://...m3u8" 或 "HD$url$$$HD$url"；取最后一个 m3u8
                    parts = re.findall(r"https?://[^\s$]+\.m3u8[^\s$]*", pu)
                    if parts:
                        return parts[-1]
        return ""
