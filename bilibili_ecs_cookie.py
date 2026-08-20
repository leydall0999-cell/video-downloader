#!/usr/bin/env python3
"""ECS（VPS）上用 Playwright 无头浏览器过 B站 412 风控，导出 Cookie 推送到网页版公共池。

为什么需要它：
  B站 视频页对无头/海外/无登录环境返回 412 验证页（security.bilibili.com/412.js），
  该页需要真实浏览器执行 JS 才能完成验证。纯 requests/curl 拿不到「验证通过」的 Cookie，
  所以网页版（Railway 海外 IP）即使用 VDL_PROXY_CN 走了国内出口，仍被 412 拦。
  本脚本在【国内 VPS】用无头 Chromium 真实访问 B站、自动完成 412 验证，
  导出匿名初始化 Cookie（buvid3/buvid4/b_nut 等），推送到 Railway 公共池。
  网页端（Railway）借 VDL_PROXY_CN 走这台 VPS 的国内出口 + 这枚 Cookie，即可绕过 412。

与抖音脚本 douyin_ecs_login.py 的区别：
  抖音需要「登录」（手机号+验证码），B站 只需「过匿名风控」，无需登录、无需交互。
  （可选）若提供 --sessdata，则注入登录态导出完整 Cookie，能下更高清晰度。

前置（ECS 上一次）：
  cd /opt/vdl-worker
  .venv/bin/pip install playwright
  .venv/bin/python -m playwright install chromium

用法：
  # 单次（默认匿名过风控）
  .venv/bin/python bilibili_ecs_cookie.py --url https://hanyuxz.top --token <云端同步令牌>
  # 注入登录态（更高画质，token 见桌面端 cloud_sync.json 或 Railway VDL_COOKIE_SYNC_TOKEN）
  .venv/bin/python bilibili_ecs_cookie.py --url https://hanyuxz.top --token <令牌> --sessdata <你的SESSDATA>
  # 循环刷新（建议用 cron 或 nohup，Cookie 约 30 天 TTL，每天刷一次足够）
  .venv/bin/python bilibili_ecs_cookie.py --url https://hanyuxz.top --token <令牌> --loop --interval 86400

令牌(url/token)也可走环境变量 VDL_COOKIE_SYNC_URL / VDL_COOKIE_SYNC_TOKEN，
与桌面端 cloud_sync.json 保持一致即可。
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path

PROFILE = "/opt/vdl-worker/bili_profile"
# 用一个公开视频页触发并渡过 412；BV 号随意，仅作「访问视频页」的载体
_TRIGGER_VIDEO = "https://www.bilibili.com/video/BV1gtgE6AEmZ"


def _need_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        print("❌ 未安装 playwright。请先执行：")
        print("   cd /opt/vdl-worker && .venv/bin/pip install playwright "
              "&& .venv/bin/python -m playwright install chromium")
        sys.exit(1)


def launch(sessdata: str = ""):
    from playwright.sync_api import sync_playwright

    Path(PROFILE).mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    # user_data_dir 持久化登录态（注入 SESSDATA 后下次无需再注入）
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE,
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
    )
    # 抹掉 webdriver 标记，降低被识别为自动化的概率
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    if sessdata:
        # 访问 bilibili 域后注入 SESSDATA，使后续请求带登录态
        page = context.new_page()
        page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=30000)
        context.add_cookies([{
            "name": "SESSDATA",
            "value": sessdata,
            "domain": ".bilibili.com",
            "path": "/",
        }])
        page.close()
    return pw, context


def _collect_cookies(sessdata: str = ""):
    pw, context = launch(sessdata)
    try:
        page = context.new_page()
        # 1) 先访问首页，初始化 buvid3/buvid4/b_nut 等匿名 Cookie
        page.goto("https://www.bilibili.com/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        # 2) 再访问视频页，触发并渡过 412 验证页（JS 自动完成，国内 IP 通常无滑块）
        page.goto(_TRIGGER_VIDEO, wait_until="domcontentloaded", timeout=30000)
        passed = False
        hit_slider = False
        for _ in range(25):
            time.sleep(1)
            try:
                if page.locator("video, .bpx-player-video-wrap, #bilibili-player").first.is_visible(timeout=400):
                    passed = True
                    break
            except Exception:
                pass
            try:
                if page.locator("text=安全验证, text=滑动验证, text=请滑动").first.is_visible(timeout=400):
                    hit_slider = True
                    break
            except Exception:
                pass
        cookies = context.cookies()
        header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        return header, passed, hit_slider, cookies
    finally:
        context.browser.close()
        pw.stop()


def _push_to_cloud(url: str, token: str, header: str) -> dict:
    import json
    import urllib.request

    target = url.rstrip("/") + "/api/cookie/sync"
    body = json.dumps({"token": token, "domain": "bilibili.com", "cookie": header}).encode()
    req = urllib.request.Request(
        target,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def main():
    _need_playwright()
    ap = argparse.ArgumentParser(description="ECS 无头过 B站 412 并推送 Cookie 到网页版公共池")
    ap.add_argument("--url", default=os.environ.get("VDL_COOKIE_SYNC_URL", ""),
                    help="网页版地址，如 https://hanyuxz.top")
    ap.add_argument("--token", default=os.environ.get("VDL_COOKIE_SYNC_TOKEN", ""),
                    help="云端同步令牌（与桌面端一致）")
    ap.add_argument("--sessdata", default="", help="可选：注入 SESSDATA 导出完整登录态 Cookie")
    ap.add_argument("--loop", action="store_true", help="循环刷新")
    ap.add_argument("--interval", type=int, default=86400, help="循环间隔（秒）")
    args = ap.parse_args()

    if not args.url or not args.token:
        print("❌ 需要 --url 和 --token（或设置环境变量 VDL_COOKIE_SYNC_URL / VDL_COOKIE_SYNC_TOKEN）")
        sys.exit(1)

    while True:
        header, passed, hit_slider, cookies = _collect_cookies(args.sessdata)
        print(f"✅ 收集到 {len(cookies)} 条 Cookie；验证页通过={'是' if passed else '否'}"
              f"{'；⚠️命中滑块需 VNC 手动过一次' if hit_slider else ''}")
        if hit_slider:
            print("   本次未成功过风控，Cookie 可能仍带 412；稍后重试或改用 --sessdata 注入登录态。")
        try:
            resp = _push_to_cloud(args.url, args.token, header)
            print("   推送结果：", resp)
            # 打印完整 Cookie 字符串，供用户手动粘贴到网页端「会话 Cookie」框应急验证。
            # 注意：不脱敏——因为本就用于用户自己粘贴到自己的浏览器；测试完建议重跑脚本轮换 SESSDATA。
            print("   [DEBUG] 复制下面这整行，粘贴到网页端「会话 Cookie」框（含登录态）：")
            print("   " + header)
            if args.sessdata:
                if "SESSDATA=" in header:
                    print("   ✅ 已采集到 SESSDATA 登录态 Cookie")
                else:
                    print("   ⚠️ 未采集到 SESSDATA，注入可能失败：请检查 SESSDATA 值是否正确、是否已过期")
        except Exception as e:
            print("   ❌ 推送失败：", str(e)[:200])
        if not args.loop:
            break
        print(f"   等待 {args.interval}s 后刷新...")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
