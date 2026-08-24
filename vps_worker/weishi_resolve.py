#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器 + H5 JSON 接口解析微视（weishi.qq.com）视频。

背景：微视分享页是 SPA（纯 requests 拿到的是壳 HTML，Railway 海外节点也无法直连
腾讯系反爬站点），且微视原本依赖 App 端签名（wskey/rticket）才能拿到播放地址。
但腾讯提供了公开的 H5 接口 ``WSH5GetPlayPage``（多个来源一致确认），游客态大多
可直接返回**无水印**视频直链 ``data.feeds[0].video_url``。

本脚本解析策略（双保险，优先快路径）：
  1) HTTP 优先：从分享链接提取 feedid → GET
     ``https://h5.weishi.qq.com/webapp/json/weishi/WSH5GetPlayPage?feedid=<id>``
     游客态带 UA/Referer/cookie 请求；ret==0 且 video_url 有效即返回（无水印直链，
     最快、最省 VPS 资源）。
  2) Playwright 兜底：接口失效（404/ret!=0/无 video_url）时，用无头 Chromium 开
     ``h5.weishi.qq.com`` 播放页，等 <video> 真实 src/currentSrc（排除广告/封面
     假直链），返回可播放流（有水印，但可用）。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

PROFILE = "/opt/vdl-worker/weishi_profile"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# feedid 提取：微视分享链接形如
#   https://h5.weishi.qq.com/weishi/feed/<feedid>/wsfeed?...&id=<feedid>
#   https://weishi.qq.com/.../feed/<feedid>...
# feedid 通常为 15~20 位大小写字母+数字组合。
_FEED_RE = re.compile(r"feed(?:/|=)([A-Za-z0-9]{8,})")


def _pick_feedid(url: str) -> str:
    m = _FEED_RE.search(url)
    if m:
        return m.group(1)
    # 退路：?id=xxx 或 ?feedid=xxx
    q = urllib.parse.urlparse(url).query
    for key in ("id", "feedid", "feed_id"):
        mv = re.search(rf"{key}=([A-Za-z0-9_]+)", q)
        if mv:
            return mv.group(1)
    return ""


def _http_get(url: str, headers: dict, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _try_h5_api(feedid: str) -> dict | None:
    """尝试 WSH5GetPlayPage 接口拿到无水印直链。成功返回 dict，失败返回 None。"""
    if not feedid:
        return None
    api = (
        "https://h5.weishi.qq.com/webapp/json/weishi/WSH5GetPlayPage"
        "?feedid=" + urllib.parse.quote(feedid, safe="")
    )
    headers = {
        "User-Agent": MOBILE_UA,
        "Referer": "https://h5.weishi.qq.com/",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json,text/plain,*/*",
    }
    try:
        body = _http_get(api, headers, timeout=12)
    except Exception as e:
        # 404 / 网络错误：接口路径失效或网络不通，转 Playwright 兜底
        print(f"[weishi] H5 API request failed: {e!r}", file=sys.stderr)
        return None
    try:
        data = json.loads(body)
    except Exception:
        return None
    if data.get("ret") != 0:
        return None
    feeds = (data.get("data") or {}).get("feeds") or []
    if not feeds:
        return None
    feed = feeds[0]
    video_url = feed.get("video_url") or ""
    if not video_url.startswith("http"):
        return None
    title = (feed.get("feed_desc") or feed.get("feed_desc_withat")
             or "微视视频")
    title = re.sub(r"\s*@\S+", "", str(title)).strip() or "微视视频"
    poster = (feed.get("poster") or {})
    author = poster.get("nick") if isinstance(poster, dict) else ""
    cover = feed.get("material_thumburl") or feed.get("images", [{}])[0].get("url", "") if feed.get("images") else ""
    return {
        "ok": True,
        "title": title,
        "author": author,
        "duration": feed.get("video_time"),
        "video_id": feedid,
        "video_url": video_url,
        "webpage_url": "",
        "thumbnail": cover,
        "ext": "mp4",
        "watermark": False,  # H5 接口返回无水印直链
        "source": "h5_api",
    }


def resolve(url, timeout=60):
    """解析微视视频，返回 dict（成功）或抛 RuntimeError（失败）。"""
    feedid = _pick_feedid(url)

    # 快路径：H5 接口（无水印直链）
    api_result = _try_h5_api(feedid)
    if api_result is not None:
        api_result["webpage_url"] = url
        return api_result

    # 兜底：Playwright 真实浏览器抓播放页 <video> 真实流
    from playwright.sync_api import sync_playwright

    os.makedirs(PROFILE, exist_ok=True)
    pw = sync_playwright().start()
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
            ],
            user_agent=MOBILE_UA,
            viewport={"width": 390, "height": 844},
            locale="zh-CN",
        )
        try:
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            # 优先开 h5 播放页（移动 UA），失败再试桌面 weishi.qq.com
            target = url
            if "h5.weishi.qq.com" not in url and "weishi.qq.com" in url:
                target = url.replace("weishi.qq.com", "h5.weishi.qq.com", 1)
            page.goto(target, wait_until="domcontentloaded", timeout=30000)

            src = ""
            title = ""
            deadline = time.time() + 35
            while time.time() < deadline:
                # 真实视频流：优先匹配微视/腾讯视频 CDN 且非广告/封面资源
                try:
                    src = page.evaluate(
                        "() => { const vs = Array.from(document.querySelectorAll('video')); "
                        "for (const v of vs) { "
                        "  const s = v.src || v.currentSrc || ''; "
                        "  if (s && s.startsWith('http') && !s.includes('cover') && !s.includes('poster')) return s; "
                        "}"
                        "return ''; }"
                    ) or ""
                except Exception:
                    src = ""
                if src.startswith("http"):
                    break
                if not title:
                    try:
                        t = page.title() or ""
                        if t and "微视" not in t:
                            title = t
                    except Exception:
                        pass
                try:
                    page.mouse.wheel(0, 400)
                except Exception:
                    pass
                time.sleep(1.5)

            if not src.startswith("http"):
                raise RuntimeError(
                    "未能从微视播放页捕获视频流（页面未渲染 video 或链接已失效）。"
                    "微视已收缩运营，部分分享链接会跳 404；若原视频也发在微信/QQ，"
                    "可改用腾讯视频（v.qq.com）链接解析。"
                )

            if not title:
                try:
                    title = (page.title() or "").replace(" - 微视", "").strip()
                except Exception:
                    pass
            if not title:
                title = "微视视频"

            return {
                "ok": True,
                "title": title,
                "author": "",
                "duration": None,
                "video_id": feedid,
                "video_url": src,
                "webpage_url": page.url or url,
                "thumbnail": "",
                "ext": "mp4",
                "watermark": True,  # 播放页流通常带微视水印
                "source": "playwright",
            }
        finally:
            context.close()
    finally:
        pw.stop()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python weishi_resolve.py <微视分享链接>")
        sys.exit(1)
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
