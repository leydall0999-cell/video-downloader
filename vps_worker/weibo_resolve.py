#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析微博视频，提取真实视频流 URL。

背景：微博对非浏览器请求返回 Sina Visitor System 反爬验证页（纯 curl/requests
拿不到真实页面），且 yt-dlp 内置 WeiboIE 也已失效。真实浏览器执行 JS 后能过
验证并拿到 video 标签的真实 src。

本脚本用无头 Chromium 打开微博视频页（游客态、无需登录），提取：
  - 标题（page.title 清理后缀）
  - 真实视频流 URL（f.video.weibocdn.com 的合并 mp4，音视频已合并）
  - 视频 ID

支持 URL：video.weibo.com/show?fid=1034:xxx（会自动 302 到 weibo.com/tv/show/）。
对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import os
import re
import sys
import time

PROFILE = "/opt/vdl-worker/weibo_profile"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_FID_RE = re.compile(r"fid=(\d+:\d+)|/tv/show/(\d+:\d+)|/show\?fid=(\d+:\d+)")


def _pick_video_id(url: str) -> str:
    m = _FID_RE.search(url)
    return (m.group(1) or m.group(2) or m.group(3) or "") if m else ""


def resolve(url, timeout=35):
    """解析微博视频，返回 dict（成功）或抛 RuntimeError（失败）。"""
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
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
        )
        try:
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 轮询等 video.src（微博视频可能需几秒加载）
            src = ""
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    src = page.evaluate(
                        "() => { const v = document.querySelector('video'); "
                        "return v ? (v.src || v.currentSrc || '') : ''; }"
                    ) or ""
                except Exception:
                    src = ""
                if src.startswith("http"):
                    break
                try:
                    page.mouse.wheel(0, 600)
                except Exception:
                    pass
                time.sleep(1.5)

            if not src.startswith("http"):
                raise RuntimeError("未解析到微博视频流（video.src 为空）")

            # 标题：清理 page.title 的后缀
            title = (page.title() or "").replace(" - 微博", "").strip()
            title = re.sub(r"\s*-\s*视频\s*-\s*微博\s*$", "", title).strip()
            if not title:
                title = _pick_video_id(url)

            vid = _pick_video_id(url) or _pick_video_id(page.url)

            return {
                "ok": True,
                "title": title,
                "duration": None,
                "video_id": vid,
                "video_url": src,
                "webpage_url": page.url or url,
                "thumbnail": "",
                "ext": "mp4",
            }
        finally:
            context.close()
    finally:
        pw.stop()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python weibo_resolve.py <微博视频链接>")
        sys.exit(1)
    import json
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
