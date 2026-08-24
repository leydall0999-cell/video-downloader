#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析微信视频号（Channels / finder）视频。

背景：微信视频号的播放地址由微信客户端/网页登录态签名生成，游客态（无微信会话
Cookie）打开分享链接只会看到 "请在微信中打开" 的壳，拿不到真实视频流。必须用带
微信登录态的浏览器会话才能加载播放器并请求 CDN 流。

链接形态：
  - 短链：https://weixin.qq.com/sph/<sphid>  → 302 跳到
          https://channels.weixin.qq.com/finder-preview/pages/sph?id=<sphid>
  - 直链：https://channels.weixin.qq.com/finder-preview/pages/sph?id=<sphid>
  sphid 即 finder feed 的唯一 id（query 的 id= 或路径 /sph/<id>）。

解析策略：
  1) 跟随重定向拿 sphid；
  2) 加载本地微信登录态 Cookie（cookies/weixin.txt，Netscape 格式或 "k=v; ..." 文本），
     注入到 channels.weixin.qq.com 会话；若无 Cookie 则诚实报错，不伪装成功；
  3) Playwright 开视频号页，监听网络请求捕获真实视频流 CDN：
        - finder.video.qq.com/*.mp4（视频号主 CDN）
        - wsd.vqucache.com / vqucache.com 的 .mp4 / .m3u8
        - shp.qpic.cn 的 mp4（部分场景）
     排除 cover/poster/avatar 等假资源；
  4) 拿到首个真实流即返回。

注意：微信有强风控，登录态 Cookie 可能过期或被风控挑战；本解析器只负责用给定
登录态抓流，Cookie 的获取/刷新由外部（用户粘贴或扫码）负责。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

PROFILE = "/opt/vdl-worker/finder_profile"
COOKIE_FILE = "/opt/vdl-worker/cookies/weixin.txt"
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"
)


def _pick_sphid(url: str) -> str:
    """从 URL 提取视频号 sphid（query 的 id=，或路径 /sph/<id>）。"""
    q = urllib.parse.urlparse(url).query
    m = re.search(r"(?:^|&)id=([A-Za-z0-9_-]+)", q)
    if m:
        return m.group(1)
    m = re.search(r"/sph/([A-Za-z0-9_-]+)", url)
    if m:
        return m.group(1)
    return ""


def _resolve_real_url(short_url: str, timeout: int = 12) -> str:
    """跟随重定向拿到真实视频号页 URL（含 sphid 的 id= 参数）。"""
    try:
        req = urllib.request.Request(
            short_url,
            headers={"User-Agent": MOBILE_UA, "Referer": "https://weixin.qq.com/"},
        )
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.geturl()
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location")
        if loc:
            return urllib.parse.urljoin(short_url, loc)
        return short_url
    except Exception:
        return short_url


def _load_cookies() -> list[dict]:
    """读取本地微信登录态 Cookie 文件，转成 Playwright 可用的 dict 列表。

    支持两种格式：
      - Netscape cookie jar（# Netscape HTTP Cookie File 开头，每行 domain\tflag\tpath\tsecure\texp\tname\tvalue）
      - 纯文本 "k1=v1; k2=v2; ..."（从浏览器开发者工具复制的 Cookie 字符串）
    返回空列表表示无 Cookie（调用方应据此诚实报错）。
    """
    if not os.path.exists(COOKIE_FILE):
        return []
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except Exception:
        return []
    if not raw:
        return []

    cookies: list[dict] = []

    # 1) Netscape 格式
    if raw.startswith("# Netscape") or "\t" in raw.splitlines()[0]:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                domain, _, path, secure, exp, name, value = parts[:7]
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain.lstrip("."),
                    "path": path or "/",
                    "secure": secure.lower() == "true",
                    "expires": int(exp) if exp and exp.isdigit() else None,
                })
        if cookies:
            return cookies

    # 2) 纯文本 "k=v; k2=v2"
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        cookies.append({
            "name": k,
            "value": v,
            "domain": ".weixin.qq.com",
            "path": "/",
            "secure": True,
            "expires": None,
        })
    return cookies


def _is_video_cdn(u: str) -> bool:
    """判定是否为视频号真实视频流 CDN（排除封面/海报/头像等假资源）。"""
    low = u.lower()
    if any(x in low for x in ("cover", "poster", "avatar", "headimg", "thumb", "pic_")):
        return False
    # 视频号主 CDN
    if "finder.video.qq.com" in low and (".mp4" in low or ".m3u8" in low):
        return True
    # vqucache 镜像
    if "vqucache.com" in low and (".mp4" in low or ".m3u8" in low):
        return True
    # qpic 视频流（部分场景 shp.qpic.cn）
    if "shp.qpic.cn" in low and ".mp4" in low:
        return True
    # 通用兜底：明确是视频扩展名且来自微信系域名
    if ".mp4" in low and ("weixin.qq.com" in low or "qq.com" in low) and "cover" not in low:
        return True
    return False


def resolve(url, timeout=75):
    """解析微信视频号视频，返回 dict（成功）或抛 RuntimeError（失败）。

    失败原因可能为：无微信登录态、Cookie 过期/被风控、链接失效/视频已删。
    """
    from playwright.sync_api import sync_playwright

    # 1) 提取 sphid（短链需跟随重定向）
    sphid = _pick_sphid(url)
    target = url
    if not sphid:
        real = _resolve_real_url(url)
        if real != url:
            target = real
            sphid = _pick_sphid(real)
    if not sphid:
        raise RuntimeError(
            "无法从链接提取视频号 id（应为 weixin.qq.com/sph/<id> 或 "
            "channels.weixin.qq.com/.../sph?id=<id> 形态）。"
        )

    # 2) 加载微信登录态 Cookie
    cookies = _load_cookies()
    if not cookies:
        raise RuntimeError(
            "微信视频号需要微信登录态才能解析，当前 VPS 未配置微信 Cookie。\n"
            "请在已登录微信的浏览器中打开 channels.weixin.qq.com，复制该域名的 "
            "Cookie 字符串，粘贴给我写入 VPS（cookies/weixin.txt）后重试。\n"
            "无登录态时视频号只返回「请在微信中打开」的壳，无法提取视频流。"
        )

    # 3) 统一用 channels.weixin.qq.com 视频号页
    if "channels.weixin.qq.com" not in target:
        target = (
            "https://channels.weixin.qq.com/finder-preview/pages/sph"
            f"?id={urllib.parse.quote(sphid, safe='')}"
        )

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
            # 注入微信登录态 Cookie（限定微信系域名，避免污染）
            try:
                context.add_cookies(cookies)
            except Exception as e:
                raise RuntimeError(
                    "注入微信 Cookie 失败：%s。请确认粘贴的是有效微信登录态 Cookie。"
                    % str(e)[:160]
                )

            page = context.new_page()
            caught: list[str] = []
            desc_hint: list[str] = []

            def _on_request(req):
                u = req.url or ""
                if _is_video_cdn(u):
                    caught.append(u)

            def _on_response(resp):
                # 视频号页面可能调 finder 接口返回 feed 详情（含描述/作者），
                # 尝试从中提取标题（非阻塞，拿不到就用页面标题兜底）
                try:
                    u = resp.url or ""
                    if "finder" in u and ("feed" in u or "object" in u or "detail" in u):
                        txt = resp.text() or ""
                        m = re.search(r'"desc"\s*:\s*"([^"]+)"', txt)
                        if m:
                            desc_hint.append(
                                m.group(1).encode("utf-8").decode("unicode_escape", "ignore")
                            )
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
            logged_in = True
            while time.time() < deadline:
                if caught:
                    # 优先取微信系主 CDN，其次镜像
                    primary = [c for c in caught if "finder.video.qq.com" in c or "shp.qpic.cn" in c]
                    src = (primary[0] if primary else caught[0])
                    break
                # 检测是否停在「请在微信中打开」壳（无登录态的信号）
                if not logged_in:
                    pass
                if not title:
                    try:
                        t = page.title() or ""
                        if t and t not in ("视频号", "微信"):
                            title = t
                    except Exception:
                        pass
                # 若页面提示需微信打开，提前失败，避免空等
                try:
                    body_txt = page.evaluate(
                        "() => document.body ? document.body.innerText.slice(0, 200) : ''"
                    ) or ""
                    if "请在微信" in body_txt or "在微信中打开" in body_txt:
                        raise RuntimeError(
                            "微信登录态无效或已过期：页面仍提示「请在微信中打开」。\n"
                            "请重新复制有效的微信 Cookie（channels.weixin.qq.com 域名）"
                            "粘贴给我后重试。"
                        )
                except RuntimeError:
                    raise
                except Exception:
                    pass
                try:
                    page.mouse.wheel(0, 300)
                except Exception:
                    pass
                time.sleep(2)

            if not src:
                raise RuntimeError(
                    "未能从视频号播放页捕获视频流。可能原因：① 微信登录态已失效"
                    "（需重新粘贴 Cookie）；② 微信风控拦截（频率过高/异地）；"
                    "③ 该视频已删除或设为私密。可在微信内打开确认视频仍可播放。"
                )

            if not title and desc_hint:
                title = desc_hint[0]
            if not title:
                try:
                    title = (page.title() or "").strip()
                except Exception:
                    pass
            if not title or title in ("视频号", "微信"):
                title = "微信视频号视频"

            title = re.sub(r"\s+", " ", title).strip() or "微信视频号视频"

            return {
                "ok": True,
                "title": title,
                "author": "",
                "duration": None,
                "video_id": sphid,
                "video_url": src,
                "webpage_url": page.url or url,
                "thumbnail": "",
                "ext": "mp4",
                "watermark": False,  # 视频号播放流通常不加水印（取决于原作者设置）
                "source": "playwright",
                "needs_login": False,
            }
        finally:
            context.close()
    finally:
        pw.stop()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python finder_resolve.py <微信视频号分享链接>")
        sys.exit(1)
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
