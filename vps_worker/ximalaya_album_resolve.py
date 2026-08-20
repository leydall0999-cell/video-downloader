#!/usr/bin/env python3
"""VPS 上用 Playwright（游客态）解析喜马拉雅专辑，返回全部剧集列表。

背景：yt-dlp XimalayaAlbumIE 失效（revision/album/v1/getTracksList 需登录），
但 revision/album/getTracksList（新路径）在带游客 cookie 的浏览器上下文里可访问，
返回 trackTotalCount + tracks[]（含 trackId/title/duration/isPaid）。

对外暴露 resolve_album(url, max_items=500) -> dict
"""
import json
import math
import re
import sys
import time

PROFILE = "/opt/vdl-worker/douyin_resolve_profile"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_ALBUM_RE = re.compile(r"/album/(\d+)")


def _album_id(url: str) -> str:
    m = _ALBUM_RE.search(url)
    return m.group(1) if m else ""


def resolve_album(url, max_items=500, timeout=60):
    """解析喜马拉雅专辑，返回 {ok,title,count,items:[{index,title,duration,url,is_paid}]}"""
    from playwright.sync_api import sync_playwright

    album_id = _album_id(url)
    if not album_id:
        raise RuntimeError("链接不是喜马拉雅专辑（需包含 /album/<id>）")

    pw = sync_playwright().start()
    try:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
            user_agent=UA, locale="zh-CN")
        try:
            page = ctx.new_page()
            # 先打开专辑页建立游客态
            page.goto("https://www.ximalaya.com/album/%s" % album_id,
                      wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            title = ""
            # 专辑名优先从 simple API 拿
            try:
                rs = page.request.get(
                    "https://www.ximalaya.com/revision/album/v1/simple",
                    params={"albumId": album_id},
                    headers={"Referer": "https://www.ximalaya.com/album/%s" % album_id})
                sd = rs.json()
                main = (sd.get("data") or {}).get("albumPageMainInfo") or {}
                title = main.get("albumTitle") or main.get("title") or ""
            except Exception:
                pass
            if not title:
                m = re.search(r"<title>([^<]+)", page.content())
                if m:
                    title = m.group(1).split("_")[0].strip()

            # 分页拉取剧集
            page_size = 30
            page_num = 1
            all_tracks = []
            total = 0
            while page_num * page_size <= max_items + page_size:
                r = page.request.get(
                    "https://www.ximalaya.com/revision/album/getTracksList",
                    params={"albumId": album_id, "pageNum": str(page_num), "pageSize": str(page_size)},
                    headers={"Referer": "https://www.ximalaya.com/album/%s" % album_id})
                d = r.json()
                data = d.get("data") or {}
                if d.get("ret") != 200 or not data:
                    break
                tracks = data.get("tracks") or []
                total = data.get("trackTotalCount") or len(all_tracks)
                if not tracks:
                    break
                all_tracks.extend(tracks)
                if len(all_tracks) >= total or len(all_tracks) >= max_items:
                    break
                page_num += 1
                time.sleep(0.5)

            items = []
            for t in all_tracks[:max_items]:
                tid = t.get("trackId")
                if not tid:
                    continue
                items.append({
                    "index": t.get("index") or len(items) + 1,
                    "title": t.get("title") or "",
                    "duration": t.get("duration"),
                    "url": "https://www.ximalaya.com/sound/%s" % tid,
                    "is_paid": bool(t.get("isPaid")),
                })

            if not items:
                raise RuntimeError("专辑剧集列表为空（可能需登录或专辑不存在）")

            return {
                "ok": True,
                "title": title or ("喜马拉雅专辑 %s" % album_id),
                "count": total or len(items),
                "items": items,
            }
        finally:
            ctx.close()
    finally:
        pw.stop()


if __name__ == "__main__":
    u = sys.argv[1] if len(sys.argv) > 1 else ""
    if not u:
        print("用法: python ximalaya_album_resolve.py <专辑链接>")
        sys.exit(1)
    try:
        r = resolve_album(u)
        print(json.dumps(r, ensure_ascii=False)[:2000])
        print("...共 %d 集" % len(r.get("items", [])))
    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(2)
