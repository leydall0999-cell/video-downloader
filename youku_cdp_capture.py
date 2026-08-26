#!/usr/bin/env python3
"""
通过 CDP 接管用户已登录的 Chrome（--remote-debugging-port=9222），
抓取优酷播放时真实发出的 ups/get.json 响应（含完整 cookie + ckey 签名），
验证会员视频能否拿到 m3u8 流地址。

用法：
  1. 用调试端口重启 Chrome（见下方说明），打开目标优酷 URL 并确认能播
  2. 运行：.build_venv/bin/python youku_cdp_capture.py "<优酷URL>"
"""
import sys
import json
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent
CDP_URL = "http://127.0.0.1:9222"


def connect():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(CDP_URL)
    return pw, browser


def main():
    if len(sys.argv) < 2:
        print("用法: youku_cdp_capture.py <优酷URL>")
        sys.exit(1)
    target_url = sys.argv[1]

    print(f"🔌 连接 CDP: {CDP_URL}")
    pw, browser = connect()
    print(f"✅ 已连接，上下文数: {len(browser.contexts)}")

    # 找已登录的 youku 页面，或新建一个打开目标 URL
    page = None
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "youku.com" in (p.url or ""):
                page = p
                print(f"📄 复用已打开页面: {p.url}")
                break
        if page:
            break

    if page is None:
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        print(f"🌐 打开目标 URL: {target_url}")
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

    # 确保目标 URL 已打开
    if "youku.com" not in (page.url or "") or target_url not in (page.url or ""):
        print(f"🌐 当前页不是目标，跳转: {target_url}")
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

    # 读取完整 cookie（含 httpOnly / 会话态）
    cookies = page.context.cookies()
    youku_cookies = [c for c in cookies if "youku.com" in c.get("domain", "")]
    print(f"🍪 读到 youku 相关 cookie: {len(youku_cookies)} 条")
    has_uck = any(c.get("name") == "P__yk__uck" for c in youku_cookies)
    has_sid = any(c.get("name") == "SESSIONID" for c in youku_cookies)
    print(f"   P__yk__uck = {'YES' if has_uck else 'NO'} | SESSIONID = {'YES' if has_sid else 'NO'}")

    # 抓网络请求里的 ups/get.json 响应
    print("🔍 监听网络中 ups/get.json 请求（最多 25s）...")
    captured = {}

    def on_response(response):
        if "ups.youku.com/ups/get.json" in response.url:
            try:
                body = response.json()
            except Exception:
                return
            data = body.get("data", {})
            streams = data.get("stream", {})
            user = data.get("user", {})
            err = data.get("error")
            captured["url"] = response.url
            captured["login"] = user.get("login")
            captured["stream_bool"] = bool(streams)
            captured["error"] = err
            captured["has_m3u8"] = any(
                s.get("m3u8_url") for s in (streams if isinstance(streams, list) else [])
            )
            print(f"   📡 ups 响应: login={user.get('login')} stream={bool(streams)} err={err}")

    page.on("response", on_response)

    # 触发播放：滚动/点击播放器
    try:
        page.evaluate("() => { const v=document.querySelector('video'); if(v){v.play&&v.play().catch(()=>{});} }")
    except Exception:
        pass
    page.mouse.wheel(0, 300)
    time.sleep(3)

    # 若 5s 内没抓到，重新加载触发请求
    if not captured:
        print("   ⏳ 未捕获，重新加载页面触发 ups 请求...")
        page.reload(wait_until="domcontentloaded")
        time.sleep(8)

    deadline = time.time() + 25
    while not captured and time.time() < deadline:
        time.sleep(1)

    page.remove_listener("response", on_response)

    if not captured:
        print("❌ 未捕获到 ups/get.json 响应。请确认目标视频在浏览器中能正常播放（点一下播放）。")
        pw.stop()
        sys.exit(2)

    print("\n=== 抓包结果 ===")
    print(f"ups url: {captured.get('url','')[:120]}")
    print(f"user.login: {captured.get('login')}")
    print(f"stream 存在: {captured.get('stream_bool')}")
    print(f"含 m3u8: {captured.get('has_m3u8')}")
    print(f"error: {captured.get('error')}")

    if captured.get("stream_bool") and captured.get("has_m3u8"):
        print("\n✅ 成功！会员视频在已登录会话下能拿到流地址。")
        print("   下一步：把这路 cookie + ckey 机制接进 VDL 的 YoukuIE 适配层。")
    elif captured.get("login"):
        print("\n⚠️ 登录态有效但流为空，可能是会员权限/ckey 签名问题。")
    else:
        print("\n❌ 登录态仍未被 UPS 识别（login=None）。")

    pw.stop()


if __name__ == "__main__":
    main()
