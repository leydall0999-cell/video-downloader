#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 真实浏览器解析小红书视频笔记，提取真实视频流 URL。

背景：小红书网页版是 JS 渲染 SPA + 游客态弹登录墙，纯 requests 拿不到视频流；
yt-dlp 无 XiaohongshuIE。真实浏览器执行 JS 后，笔记数据在 window.__INITIAL_STATE__
里（noteDetailMap → note.video.media.stream.h264/h265 → masterUrl）。

支持 URL：
  - https://www.xiaohongshu.com/explore/<id>?...
  - https://www.xiaohongshu.com/discovery/item/<id>?...
  - https://xhslink.com/o/xxx / xhslink.cn 短链（浏览器自动 302 跟随）
  - 用户直接粘贴的分享文案（含链接）由后端先抽出 URL

图文笔记（无视频）明确报错，不误报解析失败。
对外暴露 resolve(url) 供守护进程 vdl_cookie_daemon.py 复用。
"""
import os
import re
import sys
import time

PROFILE = "/opt/vdl-worker/xhs_profile"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-fA-F]+)")

# 递归扫描 __INITIAL_STATE__：取第一个像视频直链的字符串。
# 不依赖具体层级（小红书前端结构常变），只认 URL 特征。
_SCAN_JS = r"""
() => {
  const hits = [];
  const seen = new Set();
  const looksLikeVideo = (s) =>
    typeof s === 'string' &&
    s.startsWith('http') &&
    (/(sns-video[^.]*\.xhscdn\.com|sns-video\.hicdn\.on|xhscdn\.com\/sns_video)/.test(s) ||
     /sns-video/.test(s));
  const walk = (node, depth) => {
    if (!node || depth > 12) return;
    if (typeof node === 'string') {
      if (looksLikeVideo(node)) hits.push(node);
      return;
    }
    if (typeof node !== 'object' || seen.has(node)) return;
    seen.add(node);
    // masterUrl 优先：先看当前层是否有该键
    if (typeof node.masterUrl === 'string' && node.masterUrl.startsWith('http')) {
      hits.push(node.masterUrl);
    }
    for (const k in node) {
      try { walk(node[k], depth + 1); } catch (e) {}
    }
  };
  const st = window.__INITIAL_STATE__ || null;
  if (st) walk(st, 0);
  return hits.slice(0, 5);
}
"""


def _pick_note_id(url: str) -> str:
    m = _NOTE_ID_RE.search(url or "")
    return m.group(1) if m else ""


def resolve(url, timeout=45):
    """解析小红书视频笔记，返回 dict（成功）或抛 RuntimeError（失败）。"""
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
                "--mute-audio",
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
            # 网络层兜底：监听所有响应，抓 xhscdn 视频流直链（INITIAL_STATE 拿不到时用）
            net_hits = []

            def _on_response(resp):
                try:
                    u = resp.url or ""
                    if "sns-video" in u and u.startswith("http") and u not in net_hits:
                        net_hits.append(u)
                except Exception:
                    pass

            page.on("response", _on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            video_url = ""
            is_image_note = False
            title = ""
            deadline = time.time() + 30
            while time.time() < deadline:
                # 1) __INITIAL_STATE__ 递归扫描
                try:
                    hits = page.evaluate(_SCAN_JS) or []
                except Exception:
                    hits = []
                # 2) video 标签 src
                if not hits:
                    try:
                        vsrc = page.evaluate(
                            "() => { const v = document.querySelector('video');"
                            " return v ? (v.src || v.currentSrc || '') : ''; }"
                        ) or ""
                        if vsrc.startswith("http"):
                            hits = [vsrc]
                    except Exception:
                        pass
                # 3) 网络监听兜底
                if not hits and net_hits:
                    hits = list(net_hits)
                if hits:
                    video_url = hits[0]
                    break
                # 图文笔记判定：页面已加载且明确是 normal（无视频）类型 → 提前终止
                try:
                    st = page.evaluate(
                        "() => { try { const s = window.__INITIAL_STATE__;"
                        " if (!s) return null;"
                        " const nd = (s.note && s.note.firstNoteId) ?"
                        " (s.note.noteDetailMap || {})[s.note.firstNoteId] :"
                        " (s.noteData || null);"
                        " if (!nd) return null;"
                        " const n = nd.note || nd;"
                        " return JSON.stringify({t: n.type || '', w: !!n.video});"
                        " } catch (e) { return null; } }"
                    )
                    if st:
                        info = __import__("json").loads(st)
                        if info.get("t") in ("normal", "img") or (info.get("t") and not info.get("w")):
                            is_image_note = True
                            break
                except Exception:
                    pass
                try:
                    # 关掉可能的登录弹窗 + 滚动触发懒加载
                    page.evaluate(
                        "() => { const b = document.querySelector('.close-button,"
                        " .login-modal .close, [class*=close]'); if (b) b.click(); }"
                    )
                    page.mouse.wheel(0, 600)
                except Exception:
                    pass
                time.sleep(1.5)

            if not video_url and is_image_note:
                raise RuntimeError("该笔记是图文笔记（无视频），不支持下载")
            if not video_url:
                raise RuntimeError(
                    "未解析到小红书视频流（可能需要登录/笔记已删/仅图片）。"
                    "可稍后重试或换一条视频笔记链接"
                )

            # 标题：og:title → page.title → note id
            try:
                title = page.evaluate(
                    "() => { const m = document.querySelector('meta[property=\"og:title\"]');"
                    " return m ? m.content : ''; }"
                ) or ""
            except Exception:
                title = ""
            if not title:
                title = (page.title() or "").strip()
            title = re.sub(r"\s*-\s*小红书\s*$", "", title).strip()
            if not title:
                title = "小红书视频"

            vid = _pick_note_id(page.url or "") or _pick_note_id(url)

            return {
                "ok": True,
                "title": title,
                "duration": None,
                "video_id": vid,
                "video_url": video_url,
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
        print("用法: python xiaohongshu_resolve.py <小红书笔记链接>")
        sys.exit(1)
    import json
    try:
        r = resolve(u)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
