#!/usr/bin/env python3
"""爱奇艺分享页调试：dump 页面真实内容，定位 tvid/videoid 到底存在哪。

用法（VPS 上）: python3 debug_iqiyi.py '<分享链接>'
"""
import json
import sys
import time

URL = sys.argv[1] if len(sys.argv) > 1 else (
    "https://www.iqiyi.com/playShare.html?shareId=NTA0MTMxMTU0Mzg5MDcwMA%3D%3D"
)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir="/tmp/iqiyi_debug_profile",
        headless=True,
        args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
        user_agent=UA,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
    )
    page = ctx.new_page()
    print("GO:", URL[:120])
    try:
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print("GOTO ERR:", str(e)[:200])
    for wait in (3, 6, 10):
        time.sleep(wait if wait == 3 else (wait - 3))
        print(f"--- after ~{wait}s ---")
        print("FINAL URL:", (page.url or "")[:160])
        print("TITLE:", (page.title() or "")[:120])
        # window 全局变量中与视频相关的
        try:
            ks = page.evaluate(
                "() => Object.keys(window).filter(k => /tvid|video|acc|play|qy|player/i.test(k)).slice(0, 40)"
            )
            print("WINDOW KEYS:", ks)
        except Exception as e:
            print("WINDOW KEYS ERR:", str(e)[:100])
        try:
            p = page.evaluate(
                "() => { try { const s = window.playbackPageStageStatus; "
                "return s ? JSON.stringify({_tvid: s._tvid, playInfo: s.playInfo, _url: s._url}).slice(0,400) : 'NONE'; }"
                "catch(e) { return 'ERR ' + e.message; } }"
            )
            print("playbackPageStageStatus:", p)
        except Exception as e:
            print("playback ERR:", str(e)[:100])
        try:
            a = page.evaluate(
                "() => { try { const d = window._accData; "
                "return d ? JSON.stringify(d).slice(0,600) : 'NONE'; }"
                "catch(e) { return 'ERR ' + e.message; } }"
            )
            print("_accData:", a)
        except Exception as e:
            print("_accData ERR:", str(e)[:100])
        # HTML 中 tvId 附近内容
        try:
            t = page.evaluate(
                "() => { const h = document.documentElement.outerHTML; "
                "const i = h.indexOf('tvId'); "
                "return i >= 0 ? h.slice(Math.max(0, i - 150), i + 250) : 'NO tvId in HTML'; }"
            )
            print("HTML tvId ctx:", t[:450])
        except Exception as e:
            print("HTML ERR:", str(e)[:100])
    ctx.close()
