#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 解析爱奇艺（iqiyi.com）视频，提取真实 m3u8 流。

背景：爱奇艺分享页 playShare.html?shareId=X 是纯 JS SPA——服务端 requests 拿到的
是通用壳 HTML（"爱奇艺-在线视频网站-海量正版高清视频在线观看"），真实 tvid / video_id
由浏览器执行 JS 后写入 DOM（data-player-tvid / data-player-videoid）。
yt-dlp 的 IqiyiIE 对这些分享链接无能为力（提取不到 tvid → "Can't find any video"）。

本脚本用无头 Chromium 打开链接（分享页或 v_xxx.html 均可），等待 JS 渲染出
data-player-tvid 与 data-player-videoid，然后复刻 yt-dlp IqiyiIE.get_raw_data
的签名逻辑调 cache.m.iqiyi.com/jp/tmts/{tvid}/{vid}/ 拿 m3u8 直链（走 VPS 国内
网络，无需代理）。iQIYI m3u8 为音视频合一清单，返回直链后由下游 yt-dlp 分段下载。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

PROFILE = "/opt/vdl-worker/iqiyi_profile"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 与 yt-dlp IqiyiIE.get_raw_data 一致的签名常量（2026-08 实测仍有效）
_IQIYI_CDN_KEY = "d5fb4bd9d50c4be6948c97edd7254b0e"
_IQIYI_SRC = "76f90cbd92f94a2e925d83e8ccd22cb7"

# 从页面提取 tvid / videoid / 标题：
#   1) DOM data-player-tvid/videoid 属性（部分版本）
#   2) window.playbackPageStageStatus._tvid / playInfo.bid（iQIYI 播放器全局状态）
#   3) window._accData.videoInfo.tvId / .bid（页面数据对象）
#   4) 整页 HTML 正则兜底（属性写在 script 模板里的场景）
_JS_EXTRACT = (
    "() => {"
    "  const out = { t: '', v: '', title: '' };"
    "  // 1) DOM 属性"
    "  let el = document.querySelector('[data-player-tvid],[data-shareplattrigger-tvid]');"
    "  if (el) out.t = el.getAttribute('data-player-tvid') || el.getAttribute('data-shareplattrigger-tvid') || el.dataset.playerTvid || el.dataset.shareplattriggerTvid || '';"
    "  let el2 = document.querySelector('[data-player-videoid],[data-shareplattrigger-videoid]');"
    "  if (el2) out.v = el2.getAttribute('data-player-videoid') || el2.getAttribute('data-shareplattrigger-videoid') || el2.dataset.playerVideoid || el2.dataset.shareplattriggerVideoid || '';"
    "  // 2) iQIYI 播放器全局状态"
    "  if (!out.t && window.playbackPageStageStatus) {"
    "    out.t = window.playbackPageStageStatus._tvid || '';"
    "    if (!out.v && window.playbackPageStageStatus.playInfo) {"
    "      out.v = window.playbackPageStageStatus.playInfo.bid || '';"
    "    }"
    "  }"
    "  // 3) _accData 视频对象（playShare.html 解析分享后会写入）"
    "  if (!out.t && window._accData && window._accData.videoInfo) {"
    "    const vi = window._accData.videoInfo;"
    "    out.t = vi.tvId || vi.tvid || '';"
    "    if (!out.v) out.v = vi.bid || vi.vid || vi.videoid || '';"
    "  }"
    "  // 4) 整页正则兜底"
    "  if (!out.t || !out.v) {"
    "    const html = document.documentElement.outerHTML;"
    "    const mt = html.match(/data-(?:player|shareplattrigger)-tvid=[\"'](\\d+)/);"
    "    if (mt && !out.t) out.t = mt[1];"
    "    const mv = html.match(/data-(?:player|shareplattrigger)-videoid=[\"']([a-f\\d]+)/);"
    "    if (mv && !out.v) out.v = mv[1];"
    "  }"
    "  // 5) 标题"
    "  const tt = document.querySelector('#widget-videotitle');"
    "  if (tt) out.title = tt.textContent.trim();"
    "  return out;"
    "}"
)


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _get_raw_data(tvid: str, video_id: str) -> dict:
    """复刻 yt-dlp IqiyiIE.get_raw_data：md5 签名后调 tmts API 拿 m3u8 清单。"""
    tm = int(time.time() * 1000)
    sc = _md5(str(tm) + _IQIYI_CDN_KEY + tvid)
    url = (
        f"http://cache.m.iqiyi.com/jp/tmts/{tvid}/{video_id}/"
        + "?" + urllib.parse.urlencode({
            "tvid": tvid, "vid": video_id, "src": _IQIYI_SRC, "sc": sc, "t": tm,
        })
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": "https://www.iqiyi.com/",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read().decode("utf-8", "replace")
    if body.startswith("var tvInfoJs="):
        body = body[len("var tvInfoJs="):]
    return json.loads(body)


def resolve(url, timeout=60):
    """解析爱奇艺视频。成功返回 dict，失败抛 RuntimeError。"""
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

            # 等待分享页 JS 跳转 + 播放器数据注入（tvid / videoid 同时出现才视为成功）
            tvid, videoid, title = "", "", ""
            # 分享页需 JS 解析 shareId 写入 _accData.videoInfo.tvId，可能较慢
            deadline = time.time() + 50
            while time.time() < deadline:
                try:
                    d = page.evaluate(_JS_EXTRACT) or {}
                    tvid = (d.get("t") or "").strip()
                    videoid = (d.get("v") or "").strip()
                    title = (d.get("title") or "").strip()
                except Exception:
                    pass
                if tvid and videoid:
                    break
                try:
                    page.mouse.wheel(0, 800)
                except Exception:
                    pass
                time.sleep(1.5)

            if not tvid or not videoid:
                raise RuntimeError(
                    "未解析到爱奇艺视频（data-player-tvid/videoid 为空，"
                    "可能是付费/VIP 专享、链接失效或页面改版）"
                )

            if not title:
                try:
                    title = (page.title() or "").strip()
                    title = re.sub(r"[-_|｜].*?(爱奇艺|iqiyi).*$", "", title).strip()
                except Exception:
                    pass
            if not title:
                title = tvid

            # 调 tmts API 拿 m3u8（选最高 vd 码率）
            raw = _get_raw_data(tvid, videoid)
            if raw.get("code") != "A00000":
                raise RuntimeError("爱奇艺流获取失败: " + str(raw.get("code")))
            d2 = raw.get("data") or {}
            vidl = d2.get("vidl") or []
            streams = [s for s in vidl if s.get("m3utx")]
            if not streams:
                raise RuntimeError("未找到可下载的视频流（可能为付费/VIP 专享）")
            best = max(streams, key=lambda s: int(s.get("vd") or 0))

            return {
                "ok": True,
                "title": title,
                "duration": d2.get("dt") or None,
                "video_id": videoid,
                "tvid": tvid,
                "video_url": best.get("m3utx"),
                "quality": str(best.get("vd") or ""),
                "webpage_url": page.url or url,
                "thumbnail": d2.get("pic") or "",
                "ext": "mp4",
            }
        finally:
            context.close()
    finally:
        pw.stop()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python iqiyi_resolve.py <爱奇艺链接>")
        sys.exit(1)
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
