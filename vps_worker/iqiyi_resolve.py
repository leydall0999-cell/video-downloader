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
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PROFILE = "/opt/vdl-worker/iqiyi_profile"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 简易诊断日志：写 daemon.log 方便 Railway 端排查 worker 真实状态
LOG_PATH = "/opt/vdl-worker/iqiyi_resolve.log"

# 浏览器 stealth 指纹伪装（2026-08-20 实测必要）：
# 爱奇艺反爬会检测 headless 特征（navigator.webdriver/plugins/chrome 对象缺失、
# --enable-automation 标志），识别出 Playwright 后拒绝返回 m3u8（跳 error.html）。
# 这套伪装在 VPS 上实测拿到 m3u8（test_stealth.py CAUGHT=1）。
_STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
window.chrome = window.chrome || { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {isInstalled: false}, webstore: {onInstallStageChanged: {}} };
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 5});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
"""

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
    """解析爱奇艺视频。成功返回 dict，失败抛 RuntimeError。

    策略（2026-08-22 实测 VPS 有效）：不自己调 tmts API 拼 video_id——
    页面 JS 加载后播放器会主动请求流（VIP/部分内容走 meta-cdn.video.iqiyi.com/*.m3u8
    HLS 清单；免费/低清内容走 *.inter.71edge.com/videos/...f4v 完整 FLV 直链，
    2026-08 实测改版后免费电影已无 m3u8 请求），用 Playwright 监听网络请求捕获，
    最稳。标题等 page.title 从通用壳（"爱奇艺-在线视频网站..."）变成真实剧名后再取。
    """
    from playwright.sync_api import sync_playwright

    os.makedirs(PROFILE, exist_ok=True)
    pw = sync_playwright().start()
    try:
        # 用 ephemeral context（不传 user_data_dir）—— 避免持久 profile 被爱奇艺
        # 风控打标后跨调用污染（实测：持久 profile 多次失败后即使清 cookie 也跳
        # error.html，ephemeral 每次干净 + 先访问主页建会话能稳定拿到 m3u8）。
        browser = pw.chromium.launch(
            headless=True,
            ignore_default_args=["--enable-automation"],  # 关键：去掉自动化标志
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--no-zygote",
                "--single-process",
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        try:
            context.add_init_script(_STEALTH_JS)
            page = context.new_page()

            # 捕获播放器发出的流请求（2026-08 实测：免费视频走 F4V 直链，
            # VIP/部分内容仍走 m3u8，两者都要监听）。
            #   - m3u8：meta-cdn.video.iqiyi.com/*.m3u8（HLS 清单）
            #   - f4v：inter.71edge.com/videos/...f4v 完整文件（FLV 容器直链）；
            #     data.video.iqiyi.com / pcw-data.video.iqiyi.com 的 *.f4v 是 JSON
            #     重定向代理（返回 {"t":..,"l":真实CDN}），不是真实媒体，需排除。
            caught: list[str] = []
            caught_f4v: list[str] = []

            def _on_request(req):
                u = req.url or ""
                low = u.lower()
                if ".m3u8" in low and "iqiyi" in low:
                    caught.append(u)
                elif ".f4v" in low and not any(
                    h in low for h in ("data.video.iqiyi.com", "pcw-data.video.iqiyi.com")
                ):
                    # data/pcw-data.video.iqiyi.com 的 *.f4v 是 JSON 重定向代理
                    # （返回 {"t":..,"l":真实CDN}），不是真实媒体；其余（如
                    # *.inter.71edge.com）是完整 FLV 直链（首字节 FLV 头）
                    caught_f4v.append(u)

            page.on("request", _on_request)

            # 关键：先访问 iqiyi.com 主页建立真实会话 cookie，再访问分享页
            # 否则爱奇艺反爬会把首次直访 share URL 的请求跳到 video/error.html
            # （实测：iQIYI 自有 Playwright 检测会拒绝未建立会话的 share 访问）
            try:
                page.goto("https://www.iqiyi.com/", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(5000)
            except Exception:
                pass  # 主页失败不影响后续，best-effort

            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 等 m3u8/f4v 请求 + 真实标题（最多 50s，分享页 JS 解析较慢）
            title = ""
            deadline = time.time() + 50
            while time.time() < deadline:
                if not caught and not caught_f4v:
                    try:
                        page.mouse.wheel(0, 600)
                    except Exception:
                        pass
                    time.sleep(1.5)
                    continue
                try:
                    t = page.title() or ""
                    # 通用壳标题（"爱奇艺-在线视频网站-海量正版高清视频在线观看"）不算数
                    if t and "爱奇艺-在线视频网站" not in t:
                        title = t
                        break
                except Exception:
                    pass
                time.sleep(1.5)

            if not caught and not caught_f4v:
                # 失败诊断：写 daemon.log 看到底什么状态
                try:
                    with open(LOG_PATH, "a", encoding="utf-8") as f:
                        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] FAIL url={url[:120]} "
                                f"final_url={(page.url or '')[:160]} "
                                f"title={(page.title() or '')[:120]}\n")
                except Exception:
                    pass
                raise RuntimeError(
                    "未捕获到爱奇艺视频流（m3u8/f4v 请求未发出，"
                    "可能是付费/VIP 专享、链接失效或页面未加载）"
                )

            if not title:
                try:
                    title = page.title() or ""
                except Exception:
                    pass
            # 清理标题站点后缀："活佛济公3-电视剧全集-完整版视频在线观看-爱奇艺" → "活佛济公3"
            title = re.sub(r"\s*[-_|｜].*?(爱奇艺|iqiyi).*$", "", title).strip()
            if not title:
                title = "爱奇艺视频"

            # 流地址选择：优先 m3u8（VIP/HLS 路径）；否则取完整 f4v（FLV 直链）。
            # f4v 可能有多个（广告/不同清晰度），取最后一个（正片主流的规律）。
            if caught:
                video_url = caught[0]
                ext = "mp4"
            else:
                video_url = caught_f4v[-1]
                ext = "flv"  # f4v 容器即 FLV（H.264 + AAC），HttpFD 原样下载出 .flv

            return {
                "ok": True,
                "title": title,
                "duration": None,
                "video_id": "",
                "tvid": "",
                "video_url": video_url,
                "quality": "",
                "webpage_url": page.url or url,
                "thumbnail": "",
                "ext": ext,
            }
        finally:
            context.close()
            browser.close()
            # 强杀残留 chromium 子进程——VPS 1.6GB 内存吃紧，Playwright 的 close()
            # 有时漏掉 chromium_headless_shell 等子进程，累积占用致后续启动失败。
            # 用 ps -eo 精确挑 chromium 子进程 PID（避免 pkill -f chromium 误杀本进程，
            # 因为本进程命令行本身不含 chromium，但 pkill -f 在 shell 包装下会匹配命令行）。
            # best-effort，失败不影响 resolve 结果。
            try:
                out = subprocess.run(
                    ["ps", "-eo", "pid=,comm="],
                    capture_output=True, text=True, timeout=5,
                ).stdout
                for line in out.splitlines():
                    parts = line.strip().split(None, 1)
                    if len(parts) != 2 or "chrom" not in parts[1].lower():
                        continue
                    pid = int(parts[0])
                    if pid > 0 and pid != os.getpid():
                        try:
                            os.kill(pid, 9)
                        except ProcessLookupError:
                            pass
            except Exception:
                pass
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
