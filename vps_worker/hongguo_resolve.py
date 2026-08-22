#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析红果短剧（hongguoduanju.com）视频流。

红果短剧是字节系短剧平台，详情页（/detail?series_id=...）为 SPA，播放地址来自
字节 CDN（*.qznovelvod.com 的 /video/tos/cn/... 链接），URL 无 .mp4 扩展名，但
本身即为可直接下载的渐进式流。纯 requests 拿不到签名，需真实浏览器点开播放器、
监听媒体响应来捕获真实流地址。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。返回的 ext 固定为
mp4（URL 虽无扩展名，但属 mp4 类渐进流），配合 downloader.py 的 _detect_direct_url
对 ext 的兜底匹配，使直链透传生效。
"""
import json
import re
import sys
import time

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REFERER = "https://www.hongguoduanju.com/"

# 字节系 CDN：*.qznovelvod.com 的 /video/tos/cn/ 链接；也兜底 .mp4/.m3u8
_STREAM_RE = re.compile(
    r"qznovelvod\.com|/video/tos/|\.mp4(\?|$)|/play/\w+\.m3u8|\.m3u8(\?|$)"
)
# 详情页 series_id / 单集 id（episode_id）提取
_SERIES_RE = re.compile(r"series_id=(\d+)", re.I)
_EPISODE_RE = re.compile(r"episode_id=(\d+)", re.I)


def _pick_id(url: str) -> str:
    m = _SERIES_RE.search(url) or _EPISODE_RE.search(url)
    return m.group(1) if m else ""


def resolve(url, timeout=45):
    """解析红果短剧详情页，返回 dict（成功）或抛 RuntimeError（失败）。"""
    from playwright.sync_api import sync_playwright

    streams = []
    page_meta = {"title": ""}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        page = context.new_page()

        def _on_response(resp):
            try:
                u = resp.url
                if _STREAM_RE.search(u) and resp.status < 400:
                    if u not in streams:
                        streams.append(u)
            except Exception:
                pass

        page.on("response", _on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000, referer=REFERER)
        except Exception:
            pass

        # SPA 初始化 + 尝试点开播放器（红果需用户交互才起流；debug 验证
        # [class*='play'] 任意元素选择器能稳定触发起流，优先用它）
        time.sleep(4)
        for sel in (
            "video",
            ".play",
            "[class*='play']",
            "[class*='Play']",
            "button",
        ):
            try:
                el = page.query_selector(sel)
                if el:
                    el.click(timeout=3000)
                    break
            except Exception:
                continue
        # 兜底：点一下页面中央（很多 SPA 播放器点屏幕即播）
        try:
            page.mouse.click(640, 400)
        except Exception:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline and not streams:
            # 周期性重试点播，部分剧集需二次交互才起流
            try:
                el = page.query_selector("[class*='play']") or page.query_selector("video")
                if el:
                    el.click(timeout=2000)
            except Exception:
                pass
            try:
                page.mouse.click(640, 400)
            except Exception:
                pass
            try:
                meta = page.evaluate(
                    "() => {"
                    " const og = document.querySelector('meta[property=\"og:title\"]');"
                    " return {title: og ? og.content : (document.title || '')}; }"
                ) or {}
                if meta.get("title"):
                    page_meta["title"] = str(meta["title"])[:150]
            except Exception:
                pass
            try:
                page.mouse.move(640, 400)
                page.mouse.wheel(0, 200)
            except Exception:
                pass
            time.sleep(2)
            # 还没出流时再点一次，有些剧集需二次交互
            if not streams:
                try:
                    page.mouse.click(640, 400)
                except Exception:
                    pass

        browser.close()

    if not streams:
        raise RuntimeError(
            f"未捕获到红果短剧视频流（page={url}）。视频可能需会员/已下线/地区限制。"
            f"title={page_meta.get('title') or '?'}"
        )

    # 优先 qznovelvod.com 的真流，否则取最后一个捕获的媒体
    hg = [u for u in streams if "qznovelvod.com" in u]
    stream_url = (hg or streams)[-1]
    ext = "m3u8" if ".m3u8" in stream_url else "mp4"
    return {
        "ok": True,
        "video_id": _pick_id(url) or "",
        "title": page_meta.get("title") or "红果短剧",
        "uploader": "",
        "duration": None,
        "thumbnail": "",
        "webpage_url": url,
        "video_url": stream_url,
        "ext": ext,
        "is_live": False,
    }


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "https://hongguoduanju.com/detail?series_id=7503745441910017049"
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
