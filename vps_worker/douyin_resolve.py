#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析抖音视频，提取真实视频/音频流 URL。

背景：yt-dlp 抖音提取器在抖音 2026 初反爬升级后失效（aweme_detail API 需要
a_bogus 签名，yt-dlp 未实现，报 "Fresh cookies needed"），即使带完整登录态
cookie 也无解。但真实浏览器执行 JS 后能拿到真实视频流 URL。

本脚本用无头 Chromium 打开抖音视频页（游客态，无需登录），提取：
  - 标题（document.title 去掉 " - 抖音" 后缀）
  - video_url：视频轨（media-video-avc1）
  - audio_url：音频轨（media-audio-und-mp4a，抖音 PC 网页音视频分离）
  - 时长 / 视频 ID

短链 v.douyin.com/xxx 会被浏览器自动 302 展开；无效短链会落到首页（无
/video/ 数字ID 或 modal_id），据此判定失败。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import os
import re
import sys
import time

PROFILE = "/opt/vdl-worker/douyin_resolve_profile"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_VID_RE = re.compile(r"/video/(\d{15,})")
_MODAL_RE = re.compile(r"[?&]modal_id=(\d{15,})")
_NOTE_RE = re.compile(r"/note/(\d{15,})")

# 收集所有 video 标签的 src + 时长（真实视频在 douyinvod.com，静态占位在 douyinstatic.com）
_JS_COLLECT = (
    "() => Array.from(document.querySelectorAll('video'))"
    ".map(v => ({src: v.src || v.currentSrc || '', dur: v.duration || 0}))"
    ".filter(x => x.src.startsWith('http'))"
)
# 触发真实视频加载：滚动 + 对 douyinvod 视频静音播放
_JS_PLAY_ALL = (
    "() => Array.from(document.querySelectorAll('video')).forEach(v => { "
    "if (v.src && v.src.indexOf('douyinvod') >= 0) { v.muted = true; "
    "if (v.play) v.play().catch(() => {}); } })"
)


def _pick_video_id(url: str) -> str:
    m = _VID_RE.search(url) or _MODAL_RE.search(url)
    return m.group(1) if m else ""


def _classify(urls):
    """把捕获到的 douyinvod.com URL 分成 视频轨 / 音频轨。"""
    video, audio = [], []
    for u in urls:
        if "media-video-avc1" in u:
            video.append(u)
        elif "media-audio-und-mp4a" in u or "media-audio" in u:
            audio.append(u)
    return video, audio


def _normalize_douyin_url(url: str) -> str:
    """抖音链接归一化：v.douyin.com 短链 / iesdouyin.com/xg/video/ID
    统一转为 https://www.douyin.com/video/<ID>，解决 iesdouyin 域名下
    Playwright 拿不到真实视频流的问题。"""
    # 1) 长链里直接有 video_id（含西瓜 ixigua.com/<ID>，字节系视频ID互通）
    m = re.search(r"(?:iesdouyin\.com/xg/video/|douyin\.com/video/|ixigua\.com/(?:video/|i)?|modal_id=)(\d{15,})", url)
    if m:
        return "https://www.douyin.com/video/%s" % m.group(1)
    # 2) v.douyin.com 短链：用 urllib 跟随 302 展开
    if "v.douyin.com" in url:
        try:
            import urllib.request
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
                    "Accept": "text/html,*/*",
                },
            )
            final = urllib.request.urlopen(req, timeout=15).geturl()
            m2 = re.search(r"(?:iesdouyin\.com/xg/video/|douyin\.com/video/|modal_id=)(\d{15,})", final)
            if m2:
                return "https://www.douyin.com/video/%s" % m2.group(1)
        except Exception:
            pass
    return url


def resolve(url, timeout=30):
    """解析抖音视频，返回 dict（成功）或抛 RuntimeError（失败）。"""
    url = _normalize_douyin_url(url)
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
            captured = []

            def _on_resp(resp):
                u = resp.url
                if "douyinvod.com" in u and ("video" in u or "mime_type=video" in u):
                    captured.append(u)

            page.on("response", _on_resp)

            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 轮询：滚动 + 触发播放，挑真实视频流（douyinvod.com，排除静态占位）
            chosen = None
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    page.mouse.wheel(0, 600)
                except Exception:
                    pass
                try:
                    page.evaluate(_JS_PLAY_ALL)
                except Exception:
                    pass
                try:
                    items = page.evaluate(_JS_COLLECT) or []
                except Exception:
                    items = []
                vod = [x for x in items if "douyinvod.com" in x["src"]]
                if vod:
                    chosen = vod[0]
                    if chosen["src"]:
                        break
                time.sleep(1.0)

            # 分类捕获的 URL：视频轨 / 音频轨
            c_video, c_audio = _classify(captured)

            # 视频轨：优先 video 标签 src，兜底捕获
            video_url = chosen["src"] if chosen else ""
            if not video_url.startswith("http") and c_video:
                video_url = c_video[0]
            # 音频轨：从捕获里取（video 标签不含音频轨）
            audio_url = c_audio[0] if c_audio else ""

            final_url = page.url
            vid = _pick_video_id(final_url) or _pick_video_id(url)
            if not video_url.startswith("http") or not vid:
                # 拿不到 video_id 且无视频流 → 短链失效 / 视频被删除 / 私密 / 短链过期
                # final_url 通常会落到首页/jingxuan 精选页 / 抖音返回未登录提示
                raise RuntimeError(
                    "抖音链接无效或视频不存在（final_url=%s, streams=%d）。"
                    "可能原因：视频已删除/作者设为私密/仅好友可见/短链已过期。请换一个视频链接试"
                    % (final_url[:80], len(captured))
                )

            # 标题：抖音页面 title 可能异步写入，等 1-2s 直到非空
            title = ""
            for _ in range(6):
                title = (page.title() or "").replace(" - 抖音", "").strip()
                if title:
                    break
                time.sleep(0.5)
            if not title:
                title = vid

            duration = None
            if chosen and chosen.get("dur"):
                try:
                    duration = int(chosen["dur"])
                except Exception:
                    duration = None

            width = int(chosen.get("vw") or 0) if chosen else 0
            height = int(chosen.get("vh") or 0) if chosen else 0

            return {
                "ok": True,
                "title": title,
                "duration": duration,
                "video_id": vid,
                "video_url": video_url,
                "audio_url": audio_url,
                "width": width,
                "height": height,
                "webpage_url": "https://www.douyin.com/video/%s" % vid,
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
        print("用法: python douyin_resolve.py <抖音链接>")
        sys.exit(1)
    import json
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
