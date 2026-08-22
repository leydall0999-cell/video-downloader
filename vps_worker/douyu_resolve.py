#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析斗鱼视频/直播流。

背景：yt-dlp 内置 DouyuTVIE 用旧正则 `$ROOM.room_id = N` 提取 room_id，
2026 斗鱼新版页面改用 JSON 格式（"roomInfo":{"room":{"room_id":601979），
导致 "Unable to extract room id"。且回放提取器 DouyuShowIE 依赖过时的
PhantomJS。用真实浏览器渲染最稳：打开页面后监听 m3u8/flv 流请求即可拿到
播放地址（直播返回当前流，回放返回回放流）。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import json
import os
import re
import sys
import time

PROFILE = "/opt/vdl-worker/douyu_profile"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_ROOM_RE = re.compile(r"douyu\.com/(?:topic/\w+\?rid=|(?:[^/]+/))*([A-Za-z0-9]+)")
_STREAM_RE = re.compile(r"\.m3u8|\.flv|\.mp4|playlist")


def _pick_id(url: str) -> str:
    m = _ROOM_RE.search(url)
    return m.group(1) if m else ""


def resolve(url, timeout=40):
    """解析斗鱼直播间/回放，返回 dict（成功）或抛 RuntimeError（失败）。

    直播间：返回当前直播 m3u8 流（含标题/主播/房间号）。
    回放页：返回回放 m3u8 流。
    """
    from playwright.sync_api import sync_playwright

    streams = []
    page_meta = {"title": "", "author": ""}

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

        # 监听流请求
        def _on_response(resp):
            try:
                u = resp.url
                if _STREAM_RE.search(u) and ("douyu" in u or "txp2p" in u or "akm" in u or "m3u8" in u or "flv" in u):
                    if u not in streams:
                        streams.append(u)
            except Exception:
                pass

        page.on("response", _on_response)
        page.on("request", lambda req: None)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except Exception as e:
            # 页面超时不一定失败，等一会继续
            pass

        # 等页面加载/流出现
        deadline = time.time() + timeout
        while time.time() < deadline and not streams:
            try:
                # 提取标题/主播
                meta = page.evaluate(
                    "() => {"
                    " const title = document.querySelector('.Title-text, .title-text, "
                    "  [class*=title] h1, [class*=Title] span, .room-title, h1') "
                    "  ? document.querySelector('.Title-text, .title-text, [class*=title] h1, "
                    "    [class*=Title] span, .room-title, h1').textContent.trim() : '';"
                    " const author = document.querySelector('[class*=anchor] [class*=name], "
                    "  [class*=owner] [class*=name], .anchor-name, [class*=nickname]') "
                    "  ? document.querySelector('[class*=anchor] [class*=name], "
                    "    [class*=owner] [class*=name], .anchor-name, [class*=nickname]')"
                    "    .textContent.trim() : '';"
                    " return {title: title, author: author}; }"
                ) or {}
                if meta.get("title"):
                    page_meta["title"] = str(meta["title"])[:120]
                if meta.get("author"):
                    page_meta["author"] = str(meta["author"])[:60]
            except Exception:
                pass
            # 滚动触发加载
            try:
                page.mouse.wheel(0, 300)
            except Exception:
                pass
            time.sleep(2)

        browser.close()

    # 没有抓到流请求：尝试从页面 JS 数据提取
    if not streams:
        # 回放页有时流地址在 __NEXT_DATA__ 或 window.$DATA
        raise RuntimeError(
            f"未捕获到斗鱼视频流（page={url}）。直播间可能未开播，或回放链接失效。"
            f"title={page_meta.get('title') or '?'}"
        )

    stream_url = streams[-1]
    ext = "m3u8" if ".m3u8" in stream_url else ("flv" if ".flv" in stream_url else "mp4")
    return {
        "video_id": _pick_id(url) or "",
        "title": page_meta.get("title") or "斗鱼直播",
        "uploader": page_meta.get("author") or "",
        "duration": None,
        "thumbnail": "",
        "webpage_url": url,
        "video_url": stream_url,
        "ext": ext,
        "is_live": True if "m3u8" not in stream_url or ".flv" in stream_url else False,
    }


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "https://www.douyu.com/601979"
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
