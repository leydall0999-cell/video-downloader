"""快手（kuaishou.com）yt-dlp 提取器（v3：纯 SSR 解析，无需浏览器）。

快手自 2024 年后改为全客户端渲染（CSR）+ GraphQL + kwpsec 反爬，
旧版依赖的 window.__INIT_STATE__ 已不总是存在，且 GraphQL 接口带滑块验证码，
任何自动化上下文（headless / headless=False）都拿不到主视频的 visionVideoDetail 响应。

关键突破：主视频数据其实内嵌在**初始 SSR HTML 的 window.__APOLLO_STATE__** 里
（caption / photoH265Url 直链 / duration / videoResource 全都在），
用一次普通 HTTP GET（带本机浏览器 Cookie 即可拿到登录态高清档）即可解析，
根本不需要 Playwright / 浏览器渲染。

策略：
  1) 用 yt-dlp 自带 HTTP 栈下载页面（会自动带上 downloader 注入的浏览器 Cookie / 手动 Cookie / 代理）；
  2) 平衡括号稳健抽取 window.__APOLLO_STATE__（JSON 可能含字符串内括号，需逐字符解析）；
  3) 递归定位主视频 photo 对象（按 URL 中的 photoId 精确匹配，SSR 里通常只有主视频一个）；
  4) 从 photoH265Url / photoH264Url 直链 + videoResource.json 的 HLS 多清晰度流构造 formats；
  5) 旧版 window.__INIT_STATE__ 作兜底（极老链接 / 特殊环境）。
"""
import json
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError


class KuaishouIE(InfoExtractor):
    IE_NAME = "kuaishou"
    _VALID_URL = r"https?://(?:(?:www|m)\.kuaishou\.com/(?:short-video|f|photo)/(?P<id>[\w-]+)|v\.kuaishou\.com/(?P<short>[\w-]+))"

    _TESTS = [{
        "url": "https://www.kuaishou.com/short-video/3xabcdef1234",
        "only_matching": True,
    }]

    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    # ── 主入口 ──────────────────────────────────────────────
    def _real_extract(self, url):
        mobj = self._match_valid_url(url)
        video_id = mobj.group("id") or mobj.group("short")

        webpage = self._download_webpage(
            url, video_id,
            headers={
                "User-Agent": self._UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.kuaishou.com/",
            },
            note="正在下载快手页面…",
        )

        # ── 诊断日志（写临时文件，打包后可读）──
        diag_path = None
        try:
            import tempfile, os
            diag_path = os.path.join(tempfile.gettempdir(), "vdl_ks_diag.txt")
            has_apollo = "__APOLLO_STATE__" in webpage
            has_init = "__INIT_STATE__" in webpage
            state_blob = self._extract_state_blob(webpage, "__APOLLO_STATE__")
            state_size = len(json.dumps(state_blob)) if state_blob else 0
            photo_found = bool(state_blob and self._find_photo(state_blob, video_id))
            # 检查是否有 Cookie（通过页面内容推断）
            has_login_hint = "passToken" in webpage or "kwscode" in webpage

            with open(diag_path, "w") as f:
                f.write(f"url={url}\n")
                f.write(f"video_id={video_id}\n")
                f.write(f"webpage_len={len(webpage)}\n")
                f.write(f"has_APOLLO_STATE={has_apollo}\n")
                f.write(f"has_INIT_STATE={has_init}\n")
                f.write(f"state_blob_size={state_size}\n")
                f.write(f"photo_found={photo_found}\n")
                f.write(f"has_login_cookie_hints={has_login_hint}\n")
                f.write(f"webpage_first300={webpage[:300]!r}\n")
                f.write(f"webpage_last300={webpage[-300:]!r}\n")
                # 列出所有 window.__ 变量
                import re as _re
                blobs = _re.findall(r'window\.__(\w+)__', webpage)
                f.write(f"window_vars={blobs}\n")
        except Exception:
            pass

        # 1) 新版 SSR：window.__APOLLO_STATE__（主视频数据在这里）
        state = self._extract_state_blob(webpage, "__APOLLO_STATE__")
        if state is not None:
            photo = self._find_photo(state, video_id)
            if photo is None and video_id:
                # 退一步：忽略 photoId 精确匹配，取首个含视频字段的对象
                photo = self._find_photo(state, None)
            if photo is not None:
                return self._build_result(video_id, photo)

        # 2) 旧版兜底：window.__INIT_STATE__
        init = self._extract_state_blob(webpage, "__INIT_STATE__")
        if init is not None:
            return self._parse_init_state(video_id, init)

        raise ExtractorError(
            "未能从快手页面提取到视频数据。可能原因：①页面结构已变化；"
            "②需要登录态（请在常用浏览器登录过快手，或在「高级选项 → Cookie」粘贴快手 Cookie）；"
            "③该视频为私享 / 已删除。",
            expected=True,
        )

    # ── 稳健抽取 window.XXX = { ... } 的 JSON（平衡括号，兼容字符串内括号）──
    @staticmethod
    def _extract_state_blob(webpage, varname):
        m = re.search(r"window\." + re.escape(varname) + r"\s*=\s*", webpage)
        if not m:
            return None
        try:
            start = webpage.index("{", m.end())
        except ValueError:
            return None

        depth = 0
        in_str = False
        escape = False
        i = start
        n = len(webpage)
        while i < n:
            ch = webpage[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        blob = webpage[start:i + 1]
                        try:
                            return json.loads(blob)
                        except ValueError:
                            return None
            i += 1
        return None

    # ── 递归定位主视频 photo 对象 ────────────────────────────
    _VIDEO_KEYS = ("photoH265Url", "photoH264Url", "videoResource", "mainMvUrls")

    def _find_photo(self, obj, target_id, depth=0):
        if depth > 16 or obj is None:
            return None
        if isinstance(obj, dict):
            has_video = any(k in obj for k in self._VIDEO_KEYS)
            if has_video:
                pid = str(obj.get("id") or obj.get("photoId") or "")
                if not target_id or pid == target_id:
                    return obj
            for v in obj.values():
                r = self._find_photo(v, target_id, depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = self._find_photo(item, target_id, depth + 1)
                if r is not None:
                    return r
        return None

    # ── 构造 yt-dlp 结果 ────────────────────────────────────
    def _build_result(self, video_id, photo):
        formats = []

        # 直链 MP4（质量高、无需 ffmpeg remux，最优）
        for key in ("photoH265Url", "photoH264Url", "croppedPhotoH265Url", "croppedPhotoUrl"):
            u = photo.get(key)
            if isinstance(u, str) and u.startswith("http") and self._looks_like_media(u, ".mp4"):
                formats.append({
                    "url": u,
                    "ext": "mp4",
                    "format_id": key,
                    "format_note": key,
                    "preference": 20,
                })

        # videoResource.json：h264/hevc → adaptationSet → representation（HLS m3u8，多清晰度）
        vr = photo.get("videoResource")
        if isinstance(vr, dict):
            j = vr.get("json")
            if isinstance(j, str):
                try:
                    j = json.loads(j)
                except ValueError:
                    j = None
            if isinstance(j, dict):
                for codec, cd in j.items():
                    if not isinstance(cd, dict):
                        continue
                    for adp in (cd.get("adaptationSet") or []):
                        if not isinstance(adp, dict):
                            continue
                        reps = adp.get("representation") or []
                        if isinstance(reps, dict):
                            reps = [reps]
                        for rep in reps:
                            if not isinstance(rep, dict):
                                continue
                            urls = [rep.get("url")] + list(rep.get("backupUrl") or [])
                            for u in urls:
                                if not (isinstance(u, str) and u.startswith("http")):
                                    continue
                                is_hls = self._looks_like_media(u, ".m3u8")
                                formats.append({
                                    "url": u,
                                    "ext": "m3u8" if is_hls else "mp4",
                                    "format_id": f"{codec}_{rep.get('id', '')}",
                                    "format_note": codec,
                                    "height": rep.get("height"),
                                    "width": rep.get("width"),
                                    "vbr": rep.get("maxBitrate") or rep.get("bitrate"),
                                    "preference": 10,
                                })

        # 兜底：递归扫描任意 kwaicdn/oskwai/ksc2 的 mp4/m3u8 直链
        if not formats:
            for u in self._scan_urls(photo):
                is_hls = self._looks_like_media(u, ".m3u8")
                formats.append({
                    "url": u,
                    "ext": "m3u8" if is_hls else "mp4",
                    "format_id": "scan",
                    "preference": 1,
                })

        formats = self._dedupe_formats(formats)
        if not formats:
            raise ExtractorError("未找到可播放的视频地址", expected=True)

        title = str(photo.get("caption") or photo.get("title") or video_id)[:200]

        author = photo.get("author")
        uploader = None
        uploader_id = None
        if isinstance(author, dict):
            uploader = author.get("name")
            uploader_id = str(author.get("id") or "")
        elif isinstance(author, list) and author and isinstance(author[0], dict):
            uploader = author[0].get("name")
            uploader_id = str(author[0].get("id") or "")

        # 优先用 APOLLO_STATE 里真实的 photo id（v.kuaishou.com 短链 token 只是跳转码）
        real_id = str(photo.get("id") or photo.get("photoId") or video_id)

        result = {
            "id": real_id,
            "title": title,
            "formats": formats,
            "duration": self._to_seconds(photo.get("duration")),
            "thumbnail": photo.get("coverUrl") or photo.get("photoUrl"),
            "description": str(photo.get("caption") or "")[:500] or None,
        }
        if uploader:
            result["uploader"] = str(uploader)
        if uploader_id:
            result["uploader_id"] = str(uploader_id)
        return result

    @staticmethod
    def _looks_like_media(url, suffix):
        """判断 URL 路径（去掉查询串）是否以指定后缀结尾。"""
        path = url.split("?", 1)[0].split("#", 1)[0].lower()
        return path.endswith(suffix)

    @staticmethod
    def _dedupe_formats(formats):
        seen = set()
        out = []
        for f in formats:
            u = f.get("url")
            if not u or u in seen:
                continue
            seen.add(u)
            out.append(f)
        return out

    @classmethod
    def _scan_urls(cls, obj, depth=0):
        """递归收集所有 kwaicdn/oskwai/ksc2 的 mp4/m3u8 直链。"""
        out = []
        if depth > 16 or obj is None:
            return out
        if isinstance(obj, str):
            low = obj.lower()
            if ("kwaicdn" in low or "oskwai" in low or "ksc2" in low) and (
                ".mp4" in low or ".m3u8" in low
            ):
                out.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                out.extend(cls._scan_urls(v, depth + 1))
        elif isinstance(obj, list):
            for v in obj:
                out.extend(cls._scan_urls(v, depth + 1))
        return out

    @staticmethod
    def _to_seconds(raw):
        if not raw:
            return None
        try:
            v = float(raw)
            return v / 1000 if v > 1000 else v
        except (TypeError, ValueError):
            return None

    # ── 旧版 INIT_STATE 解析（兜底）─────────────────────────
    def _parse_init_state(self, video_id, state):
        video_info = self._find_video(state)
        if not video_info:
            raise ExtractorError("INIT_STATE 中未找到视频数据", expected=True)

        formats = []
        mv_urls = video_info.get("mainMvUrls") or []
        if isinstance(mv_urls, list):
            for url in mv_urls:
                if url and isinstance(url, str):
                    formats.append({"url": url, "ext": "mp4", "format_id": "default"})

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
                            "url": rep_url, "ext": "mp4",
                            "format_id": str(rep.get("id", "")),
                            "format_note": rep.get("name", ""),
                            "height": rep.get("height"),
                        })

        if not formats:
            raise ExtractorError("未找到可播放的视频地址", expected=True)

        title = (
            video_info.get("caption") or video_info.get("title")
            or video_info.get("name") or video_id
        )
        uploader = video_info.get("userName") or video_info.get("authorName") or ""
        uploader_id = str(video_info.get("userId") or "")
        description = video_info.get("description") or video_info.get("caption") or ""
        thumbnail = video_info.get("coverUrl") or video_info.get("poster") or ""

        duration = 0
        raw_dur = video_info.get("duration")
        if raw_dur:
            try:
                duration = float(raw_dur)
                if duration > 1000:
                    duration /= 1000
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

    @staticmethod
    def _find_video(obj, depth=0):
        if depth > 10 or obj is None:
            return None
        if isinstance(obj, dict):
            if "mainMvUrls" in obj:
                return obj
            if "video" in obj and isinstance(obj["video"], dict):
                return KuaishouIE._find_video(obj["video"], depth + 1)
            for v in obj.values():
                result = KuaishouIE._find_video(v, depth + 1)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = KuaishouIE._find_video(item, depth + 1)
                if result:
                    return result
        return None
