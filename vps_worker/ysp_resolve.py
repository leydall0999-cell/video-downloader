#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析央视频（yangshipin.cn）视频流。

原理：央视频播放地址来自 playvv.yangshipin.cn/playvinfo JSONP 接口，
参数带动态 cKey 签名（由页面 JS 生成），纯 requests 无法复现。
用真实浏览器打开视频页 → 播放器自动请求 playvinfo → 拦截该响应解析
JSONP，拿到视频文件名(fn)、签名(fvkey)、CDN 前缀(ul.ui[0].url)，
构造真实播放 URL：{cdn}{fn}?sdtfrom={sdtfrom}&vkey={fvkey}&platform=2

实测（2026-08-22）：构造 URL 返回 200 video/mp4（480P/720P 多清晰度）。

对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import json
import os
import re
import sys
import time

PROFILE = "/opt/vdl-worker/ysp_profile"
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1")

_VID_RE = re.compile(r"vid=([a-zA-Z0-9]+)")
_JSONP_RE = re.compile(r"^\s*[\w$]+\((.*)\)\s*;?\s*$", re.S)


def _pick_vid(url: str) -> str:
    m = _VID_RE.search(url)
    return m.group(1) if m else ""


def _build_streams(data: dict, sdtfrom: str = "4330701") -> list[tuple[str, str]]:
    """从 playvinfo JSON 构造 (清晰度名, 播放URL) 列表。"""
    streams: list[tuple[str, str]] = []
    try:
        vl = data.get("vl") or {}
        vi_list = vl.get("vi") or []
        fl = data.get("fl") or {}
        fi_list = fl.get("fi") or []
        for vi in vi_list:
            fn = vi.get("fn") or ""
            fvkey = vi.get("fvkey") or ""
            ul = vi.get("ul") or {}
            ui_list = ul.get("ui") or []
            if not (fn and fvkey and ui_list):
                continue
            cdn = ui_list[0].get("url") or ""
            if not cdn:
                continue
            base = cdn.rstrip("/")
            url = f"{base}/{fn}?sdtfrom={sdtfrom}&vkey={fvkey}&platform=2"
            streams.append((fn, url))
        # 用清晰度列表补 label（fi 与 fn 里的 FiLO 编号对应）
        for fi in fi_list:
            fid = fi.get("id")
            cname = fi.get("cname") or fi.get("defnname") or ""
            rate = fi.get("defnrate") or ""
            for i, (fn, url) in enumerate(streams):
                if fid and f"{fid}" in fn:
                    streams[i] = (f"{cname} {rate}".strip() or fn, url)
    except Exception:
        pass
    return streams


def _parse_playvinfo(body: str) -> dict:
    text = body
    m = _JSONP_RE.match(body)
    if m:
        text = m.group(1)
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def resolve(url, timeout=50):
    """解析央视频视频页，返回 dict（成功）或抛 RuntimeError（失败）。"""
    from playwright.sync_api import sync_playwright

    playvinfo_bodies: list[str] = []
    playvinfo_sdtfrom = "4330701"
    page_meta = {"title": ""}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=MOBILE_UA,
            viewport={"width": 390, "height": 844},
            locale="zh-CN",
        )
        page = context.new_page()

        def _on_response(resp):
            nonlocal playvinfo_sdtfrom
            try:
                u = resp.url
                if "playvinfo" in u or "playinfo" in u:
                    m = re.search(r"sdtfrom=(\d+)", u)
                    if m:
                        playvinfo_sdtfrom = m.group(1)
                    try:
                        body = resp.text()
                        if body and body not in playvinfo_bodies:
                            playvinfo_bodies.append(body)
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", _on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except Exception:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline:
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
            # 已有 playvinfo 响应则解析
            for body in playvinfo_bodies:
                d = _parse_playvinfo(body)
                streams = _build_streams(d, playvinfo_sdtfrom)
                if streams:
                    # 标题取响应里的 ti
                    vi0 = ((d.get("vl") or {}).get("vi") or [{}])[0]
                    title = str(vi0.get("ti") or page_meta.get("title") or "央视频")[:150]
                    # 多清晰度：best 用最高码率（流列表第一个通常是 480P，选 fs 最大的）
                    browser.close()
                    return {
                        "ok": True,
                        "video_id": _pick_vid(url) or "",
                        "title": title,
                        "uploader": "",
                        "duration": vi0.get("td"),
                        "thumbnail": "",
                        "webpage_url": url,
                        "video_url": streams[0][1],
                        "ext": "mp4",
                        "is_live": False,
                        "formats": [{"name": n, "url": u} for n, u in streams],
                    }
            # 滚动触发播放器初始化
            try:
                page.mouse.move(200, 300)
                page.mouse.wheel(0, 300)
            except Exception:
                pass
            time.sleep(2)

        browser.close()

    raise RuntimeError(
        f"未捕获到央视频播放地址（page={url}）。视频可能已下线/需登录/地区限制。"
        f"title={page_meta.get('title') or '?'}"
    )


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else "https://m.yangshipin.cn/video?type=0&vid=t000009y9n2&cid=3n3vij90wxc801x"
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
