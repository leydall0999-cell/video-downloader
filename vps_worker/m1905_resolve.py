#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析 1905 电影网（1905.com）视频流。

背景：1905.com/mdb/film/{id}/ 页面是 SSR + 反爬，纯 requests 被 403
（数据中心 IP）。页面 JS 加载后自动请求真实视频流（vodfile.m1905.com 的
mp4/m3u8）。用真实浏览器打开详情页，监听 m3u8/mp4 流请求即可拿到直链。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import json
import os
import re
import sys
import time

PROFILE = "/opt/vdl-worker/m1905_profile"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_ID_RE = re.compile(r"/film/(\d+)")
# 真实视频 CDN：vodfile.m1905.com / vipmp4i.vodfile.m1905.com 的 mp4/m3u8
_STREAM_RE = re.compile(r"vodfile\.m1905\.com/.+\.(mp4|m3u8)|\.m3u8")


def _pick_id(url: str) -> str:
    m = _ID_RE.search(url)
    return m.group(1) if m else ""


def resolve(url, timeout=45):
    """解析 1905 电影详情页，返回 dict（成功）或抛 RuntimeError（失败）。"""
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
                if _STREAM_RE.search(u):
                    if u not in streams:
                        streams.append(u)
            except Exception:
                pass

        page.on("response", _on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except Exception:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline and not streams:
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
                page.mouse.wheel(0, 300)
            except Exception:
                pass
            time.sleep(2)

        browser.close()

    if not streams:
        raise RuntimeError(
            f"未捕获到 1905 视频流（page={url}）。影片可能已下线/需登录/地区限制。"
            f"title={page_meta.get('title') or '?'}"
        )

    mp4s = [u for u in streams if ".mp4" in u]
    m3u8s = [u for u in streams if ".m3u8" in u]
    stream_url = (mp4s or m3u8s or streams)[-1]
    ext = "m3u8" if ".m3u8" in stream_url else "mp4"
    return {
        "ok": True,
        "video_id": _pick_id(url) or "",
        "title": page_meta.get("title") or "1905电影网",
        "uploader": "",
        "duration": None,
        "thumbnail": "",
        "webpage_url": url,
        "video_url": stream_url,
        "ext": ext,
        "is_live": False,
    }


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "https://www.1905.com/mdb/film/2255588/"
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
