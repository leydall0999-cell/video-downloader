"""优酷零门槛解析引擎：用已登录的真实浏览器拦截 ups 响应，自动拿 ckey + m3u8。

为什么必须走真实浏览器：
- 优酷 UPS 播放签名 ckey 的算法在优酷前端 JS 里混淆，服务端无法复刻。
- 但**优酷播放页自己会发** `ups.youku.com/ups/get.json?...&ckey=XXX`（页面 JS 算好 ckey）。
- 所以我们用 Playwright 起一个「已登录优酷」的浏览器加载播放页，拦截这个请求的
  **响应**，里面就有完整 stream + m3u8_url —— ckey 由真实浏览器替我们算，零人工参与。

这就是用户「粘贴链接直接出结果」零门槛的关键：解析时实时起浏览器拦截，
不需要人去抓 ckey / 装书签 / 发 cURL。

登录态来源：
- profile_dir 指向一个已用 youku_login.py 扫码登录过的 Chrome 用户目录
  （默认项目内 chrome_profile_youku）。profile 有效期间全自动；过期需重扫一次。

并发控制：
- 浏览器很重，进程内同一时刻只允许一个解析在跑（_LOCK）。其余请求排队等待。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_LAST_LAUNCH = 0.0  # 防止过于频繁起浏览器


def _profile_default() -> str:
    """默认 profile 路径。

    优先级：
    1. 环境变量 VDL_YOUKU_PROFILE（显式指定）
    2. 桌面端：用户目录下的 VDL/youku_profile（打包后无项目根）
    3. 回退：项目根下的 chrome_profile_youku（开发/服务端用）
    """
    import os
    env = os.environ.get("VDL_YOUKU_PROFILE", "").strip()
    if env:
        return env
    home = Path.home()
    desktop_profile = home / "Library" / "Application Support" / "VDL" / "youku_profile"
    if desktop_profile.exists():
        return str(desktop_profile)
    here = Path(__file__).resolve().parent.parent
    return str(here / "chrome_profile_youku")


def _youku_vid(url: str) -> str:
    m = re.search(r"id_([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"vid=([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    return ""


def _title_from_html(page) -> str:
    try:
        mt = re.search(r"<title>(.*?)</title>", page.content(), re.S)
        if mt:
            return re.sub(r"[_-]?优酷.*$", "", mt.group(1)).strip()
    except Exception:
        pass
    return ""


def resolve_via_browser(vid_or_url: str, profile_dir: str = "", proxy: str = "") -> dict:
    """用已登录浏览器拦截 ups 响应，零门槛解析优酷。

    返回 yt-dlp 兼容 info dict（含 m3u8 url）。失败抛 RuntimeError（含可读原因）。
    """
    vid = _youku_vid(vid_or_url) if "http" in vid_or_url else vid_or_url
    if not vid:
        raise RuntimeError("无法从链接识别优酷视频 ID")
    profile_dir = profile_dir or _profile_default()
    if not Path(profile_dir).exists():
        raise RuntimeError(
            "未找到优酷登录 profile（%s）。请先运行 youku_login.py 扫码登录一次。"
            % profile_dir
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("服务端未安装 playwright，无法走浏览器解析通道")

    global _LAST_LAUNCH
    with _LOCK:
        # 两次浏览器启动至少间隔 2s，避免资源抖动
        _wait = 2.0 - (time.time() - _LAST_LAUNCH)
        if _wait > 0:
            time.sleep(_wait)
        _LAST_LAUNCH = time.time()

        captured = {}
        err_box = {}

        pw = None
        browser = None
        try:
            pw = sync_playwright().start()
            launch_kwargs = {
                "user_data_dir": profile_dir,
                "headless": True,
                "args": [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--mute-audio",
                ],
            }
            # mac 上 playwright 没自带 chromium 时用本机 Chrome
            try:
                browser = pw.chromium.launch_persistent_context(**launch_kwargs)
            except Exception:
                launch_kwargs["channel"] = "chrome"
                browser = pw.chromium.launch_persistent_context(**launch_kwargs)

            page = browser.new_page()
            # 拦截 ups 响应：页面自己发的请求带真实 ckey，响应里就有 stream
            def _on_response(resp):
                try:
                    if "ups.youku.com/ups/get.json" in resp.url:
                        data = resp.json()
                        d = data.get("data") or {}
                        # 新版优酷：完整流在 data.stream（单个对象）；data.streams 常为空数组
                        streams = d.get("streams") or []
                        if not streams and isinstance(d.get("stream"), dict) and d["stream"].get("m3u8_url"):
                            streams = [d["stream"]]
                        if not streams and isinstance(d.get("pay"), dict) and d["pay"].get("can_play") is False:
                            err_box["need_vip"] = True
                            return
                        if streams:
                            captured["streams"] = streams
                            captured["raw"] = d
                except Exception:
                    pass

            page.on("response", _on_response)

            play_url = f"https://v.youku.com/v_show/id_{vid}.html"
            page.goto(play_url, wait_until="domcontentloaded", timeout=45000)
            # 等待播放器发起 ups 请求（最多 25s）
            deadline = time.time() + 25
            while time.time() < deadline:
                if captured.get("streams") or err_box.get("need_vip"):
                    break
                time.sleep(0.5)

            if err_box.get("need_vip"):
                raise RuntimeError("该优酷视频为 VIP/付费专享，当前登录账号无播放权限（需会员账号）")
            streams = captured.get("streams") or []
            if not streams:
                raise RuntimeError(
                    "浏览器拦截未拿到 ups 流（可能登录态失效或页面未触发播放）。"
                    "请确认优酷登录 profile 仍有效，或重跑 youku_login.py 扫码。"
                )

            def _h(s):
                return int(s.get("height") or 0)

            streams_sorted = sorted(streams, key=_h, reverse=True)
            best = streams_sorted[0]
            m3u8 = best.get("m3u8_url") or best.get("playurl") or ""
            if not m3u8:
                for k, v in best.items():
                    if "url" in k.lower() and isinstance(v, str) and v.startswith("http"):
                        m3u8 = v
                        break
            if not m3u8:
                raise RuntimeError("UPS 返回的流缺少 m3u8 地址")

            cookie_header = ""
            try:
                cs = browser.cookies()
                yk = [c for c in cs if "youku.com" in c.get("domain", "")]
                cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in yk)
            except Exception:
                pass

            title = _title_from_html(page) or f"优酷视频_{vid}"
            return {
                "id": vid,
                "title": title,
                "webpage_url": play_url,
                "extractor_key": "YoukuBrowser",
                "extractor": "youku",
                "ext": "mp4",
                "direct": True,
                "url": m3u8,
                "protocol": "m3u8_native",
                "http_headers": {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://m.youku.com/",
                    "Origin": "https://m.youku.com",
                    "Cookie": cookie_header,
                },
                "_youku_m3u8": True,
            }
        finally:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass


def browser_available(profile_dir: str = "") -> bool:
    """快速探测：playwright 是否可用 + profile 是否存在。"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    p = profile_dir or _profile_default()
    return Path(p).exists()
