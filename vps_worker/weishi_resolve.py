#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析微视（weishi.qq.com）视频。

背景：微视分享页是 SPA，且原本依赖 App 端签名（wskey/rticket）才能拿到播放地址。
公开的 H5 接口 ``WSH5GetPlayPage``（曾可返回无水印直链）已于微视 2023 收缩后整体
下线（实测对所有 feedid 返回 404）。但用真实浏览器开分享页，播放器会请求
``q.weishi.qq.com/*.mp4``（带微视水印的 CDN 直链，另有 rdt.tfogc.com 镜像），
监听该网络请求即可捕获真实可下载流。

链接形态（均需处理）：
  - 短链：https://video.weishi.qq.com/<短id>  → 302 跳到
          https://isee.weishi.qq.com/ws/app-pages/share/index.html?...&id=<真feedid>
          真 feedid 在 query 的 id= 参数里。
  - 标准分享页：https://h5.weishi.qq.com/weishi/feed/<feedid>/wsfeed?...

解析策略（2026-08-24 实战验证）：
  1) 先用 requests 跟随重定向，从最终 URL 的 id=/feedid= 提取真 feedid；
  2) Playwright 开 isee/h5 分享页，监听网络请求捕获 q.weishi.qq.com/*.mp4
     （排除 cover/poster 假资源），拿到即返回（带水印直链，可下载）。

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


def _pick_feedid_from_url(url: str) -> str:
    """从 URL 提取微视 feedid（query 的 id= / feedid=，或路径 feed/<id>）。"""
    q = urllib.parse.urlparse(url).query
    for key in ("id", "feedid", "feed_id"):
        m = re.search(rf"(?:^|&){key}=([A-Za-z0-9_]+)", q)
        if m:
            return m.group(1)
    m = re.search(r"feed/([A-Za-z0-9_]{8,})", url)
    if m:
        return m.group(1)
    # 最后是裸短链路径（如 /hil37ifB）：无法直接用，需跟随重定向拿真 id
    return ""


def _resolve_real_url(short_url: str, timeout: int = 12) -> str:
    """跟随重定向拿到真实分享页 URL（含真 feedid 的 id= 参数）。"""
    try:
        req = urllib.request.Request(
            short_url,
            headers={"User-Agent": MOBILE_UA, "Referer": "https://weishi.qq.com/"},
        )
        # 不自动跟随，看 Location；跟随也行，最终 URL 都带 id=
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.geturl()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if loc:
            return urllib.parse.urljoin(short_url, loc)
        return short_url
    except Exception:
        return short_url


def _is_video_cdn(u: str) -> bool:
    """判定是否为真实视频流 CDN（排除封面/海报/头像等假资源）。"""
    low = u.lower()
    if "cover" in low or "poster" in low or "avatar" in low:
        return False
    # 真视频：q.weishi.qq.com/*.mp4 或 puui.qpic.cn 的 mp4/m3u8
    if "q.weishi.qq.com" in low and ".mp4" in low:
        return True
    if "puui.qpic.cn" in low and (".mp4" in low or ".m3u8" in low):
        return True
    # rdt.tfogc.com 镜像（路径含 .mp4）
    if "tfogc.com" in low and ".mp4" in low:
        return True
    return False


def resolve(url, timeout=75):
    """解析微视视频，返回 dict（成功）或抛 RuntimeError（失败）。"""
    from playwright.sync_api import sync_playwright

    # 1) 提取真 feedid（短链需跟随重定向）
    feedid = _pick_feedid_from_url(url)
    target = url
    if not feedid:
        real = _resolve_real_url(url)
        if real != url:
            target = real
            feedid = _pick_feedid_from_url(real)

    # 2) 统一用 isee/h5 分享页（移动 UA）；若原链是 video.weishi.qq.com 短链，
    #    直接跳 isee 分享页（不要再错误替换成 video.h5.weishi.qq.com）
    if "isee.weishi.qq.com" not in target and "h5.weishi.qq.com" not in target:
        if feedid:
            target = (
                "https://isee.weishi.qq.com/ws/app-pages/share/index.html"
                f"?wxplay=1&id={urllib.parse.quote(feedid, safe='')}"
                "&spid=999&qua=v2_and_weishi_8.200.1_108_312024000_d&from_share=1"
                "&chid=100081014&pkg=3670&attach=cp_reserves3_1000370011"
            )
        elif "weishi.qq.com" in target:
            # 兜底：video.weishi.qq.com 短链跟随重定向后的 isee 页
            target = _resolve_real_url(url)

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
            caught: list[str] = []
            api_desc: list[str] = []

            def _on_request(req):
                u = req.url or ""
                if _is_video_cdn(u):
                    caught.append(u)

            def _on_response(resp):
                # 微视分享页会调 api.weishi.qq.com 拿 feed 详情 JSON（含 feed_desc 真实描述）
                try:
                    u = resp.url or ""
                    if "api.weishi.qq.com" in u and ("feed" in u or "detail" in u):
                        txt = resp.text() or ""
                        m = re.search(r'"feed_desc"\s*:\s*"([^"]+)"', txt)
                        if m:
                            api_desc.append(m.group(1).encode("utf-8").decode("unicode_escape", "ignore"))
                except Exception:
                    pass

            page.on("request", _on_request)
            page.on("response", _on_response)
            page.goto(target, wait_until="domcontentloaded", timeout=30000)

            # 触发播放：点击视频区域 + 滚动，促使播放器起播并请求 CDN 流
            try:
                page.mouse.click(195, 400)
            except Exception:
                pass

            src = ""
            title = ""
            deadline = time.time() + timeout
            while time.time() < deadline:
                if caught:
                    # 取首个真实视频流（CDN 直链，非镜像优先）
                    real_cdn = [c for c in caught if "q.weishi.qq.com" in c or "puui.qpic.cn" in c]
                    src = (real_cdn[0] if real_cdn else caught[0])
                    break
                if not title:
                    try:
                        t = page.title() or ""
                        if t and t not in ("腾讯微视", "视频号"):
                            title = t
                    except Exception:
                        pass
                try:
                    page.mouse.wheel(0, 300)
                except Exception:
                    pass
                time.sleep(2)

            if not src:
                raise RuntimeError(
                    "未能从微视播放页捕获视频流（播放器未起播或链接已失效）。"
                    "微视已收缩运营，部分分享链接会跳 404；若原视频也发在微信/QQ，"
                    "可改用腾讯视频（v.qq.com）链接解析。"
                )

            if not title and api_desc:
                # 从 API 响应拿到的真实视频描述（最优）
                title = api_desc[0]
            if not title:
                try:
                    title = (page.title() or "").replace(" - 微视", "").strip()
                except Exception:
                    pass
            if not title:
                title = "微视视频"

            # 清理标题里的 @作者 后缀
            title = re.sub(r"\s*@\S+", "", title).strip() or "微视视频"

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
                "watermark": True,  # 播放页流带微视水印（H5 无水印接口已下线）
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
