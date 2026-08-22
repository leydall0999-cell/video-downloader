"""百视TV（bestv.com.cn）视频解析 —— VPS Playwright worker。

百视TV 播放页（/web/play/{vid}）是 Next.js SPA，web 端播放地址由内部
wasm 函数 window.makepreviewquery(vid) 生成签名参数 s，再请求
  /api/source/preview.m3u8?s={s}
返回真实 HLS（m3u8）流。纯 requests 无法复现 wasm 签名，故走 Playwright：
打开播放页 → 等 window.makepreviewquery 就绪 → 调它拿 s → 拼 m3u8 URL。

注：百视 web 端仅提供预览/正片片段（完整内容在 APP），preview.m3u8 即 web 可播流。
标题另从 /api/v/{vid}（无签名，纯 JSON）获取。
"""
import json
import re
import sys
import time
import urllib.request

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

_REFERER = "https://www.bestv.com.cn/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _pick_vid(url: str):
    m = re.search(r"/play/(\d+)", url) or re.search(r"vid=?(\d+)", url, re.I)
    return m.group(1) if m else ""


def _fetch_title(vid: str) -> str:
    try:
        api = f"https://www.bestv.com.cn/api/v/{vid}"
        req = urllib.request.Request(
            api, headers={"User-Agent": _UA, "Referer": _REFERER}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        data = d.get("Data") or {}
        return (data.get("Title") or "")[:150]
    except Exception:
        return ""


def resolve(url: str, timeout: int = 60) -> dict:
    if sync_playwright is None:
        return {"ok": False, "error": "Playwright 未安装"}

    vid = _pick_vid(url)
    if not vid:
        return {"ok": False, "error": "无法从链接提取百视TV视频ID"}

    m3u8_url = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        try:
            page.goto(
                f"https://www.bestv.com.cn/web/play/{vid}",
                wait_until="domcontentloaded",
                timeout=timeout * 1000,
            )
        except Exception:
            pass

        # 等 window.makepreviewquery 就绪（wasm 注入）
        try:
            page.wait_for_function(
                "typeof window.makepreviewquery === 'function'", timeout=20000
            )
        except Exception:
            browser.close()
            return {"ok": False, "error": "百视TV播放器未初始化（wasm 未加载）"}

        try:
            s = page.evaluate("() => window.makepreviewquery('" + vid + "')")
            if s:
                m3u8_url = f"https://www.bestv.com.cn/api/source/preview.m3u8?s={s}"
        except Exception as e:
            browser.close()
            return {"ok": False, "error": f"生成播放签名失败：{e}"}

        browser.close()

    if not m3u8_url:
        return {"ok": False, "error": "未能生成百视TV播放地址"}

    title = _fetch_title(vid) or "百视TV"

    # 试看检测（2026-08-22 实测）：preview.m3u8 是 web 端试看流——《上海滩往事》
    # 只返回 1 个 9s 分片。若用户下载到这种文件会误以为是 bug，直接明确提示。
    # 拉取清单统计 EXTINF 总时长，< 60s 判为试看片段。
    try:
        req = urllib.request.Request(m3u8_url, headers={"User-Agent": _UA, "Referer": _REFERER})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
        infs = re.findall(r"#EXTINF:\s*([0-9.]+)", body)
        total = sum(float(x) for x in infs)
        if total and total < 60:
            return {
                "ok": False,
                "error": (
                    f"百视TV 网页端仅提供试看片段（约 {int(total)} 秒），"
                    "完整版需在百视TV APP 或登录后观看"
                ),
            }
    except Exception:
        pass  # 清单探测失败不阻断，保持原行为

    return {
        "ok": True,
        "video_id": vid,
        "title": title,
        "uploader": "",
        "duration": None,
        "thumbnail": "",
        "webpage_url": url,
        "video_url": m3u8_url,
        "ext": "m3u8",
        "is_live": False,
    }


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    print(json.dumps(resolve(u), ensure_ascii=False, indent=2))
