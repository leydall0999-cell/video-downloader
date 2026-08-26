#!/usr/bin/env python3
"""本地用 Playwright 真实扫码登录优酷，自动导出登录态 Cookie。

为什么需要这个：
  优酷会员/受限视频解析报 -3007「请先登录」，根因是共享池缺少一条
  *有效登录态* 的优酷 cookie（核心标识 P__yk__uck）。这条 cookie 只能从
  你本人已登录的浏览器 session 来——脚本用扫码登录真实拿到，再导出。

方案：扫码登录（最稳，不碰密码）。
  1. 打开优酷登录页，切到二维码
  2. 终端打印二维码（或输出 URL 供手机扫）
  3. 你用手机优酷/支付宝/淘宝 App 扫码确认
  4. 脚本轮询直到检测到 P__yk__uck（登录态落地）
  5. 导出 Netscape cookie 文件 + 贡献用的 header 字符串
  6. 顺带用 UPS API 验一次 user.login 非空，确保 -3007 能消

前置（本机一次）：
  /Users/suixindelang/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
    -m venv /Users/suixindelang/WorkBuddy/video-downloader/.build_venv   # 已存在则跳过
  .build_venv/bin/pip install playwright
  .build_venv/bin/python -m playwright install chromium

用法：
  .build_venv/bin/python youku_login.py                 # 扫码登录 + 导出 + 校验
  .build_venv/bin/python youku_login.py --headless      # 无头模式（需 VNC 看码时不用）
  .build_venv/bin/python youku_login.py --verify        # 仅用已有 cookie 跑 UPS 校验
  .build_venv/bin/python youku_login.py --header-only   # 只打印贡献用 header 字符串

导出位置：
  cookies/youku.com.txt      (Netscape，供 check_youku_cookie.py 用)
  cookies/youku.com.header   (单行 header 字符串，可直接 POST /api/cookie/contribute)
"""
import argparse
import os
import sys
import time
import json
import urllib.request
import urllib.error
from pathlib import Path

# 贡献到线上共享池（自动覆盖 youku.com.json 无效那条）
CONTRIBUTE_URL = "https://hanyuxz.top/api/cookie/contribute"
CONTRIBUTE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HERE = Path(__file__).resolve().parent
PROFILE = str(HERE / "chrome_profile_youku")
COOKIE_DIR = HERE / "cookies"
NETSCAPE_OUT = COOKIE_DIR / "youku.com.txt"
HEADER_OUT = COOKIE_DIR / "youku.com.header"
LOGIN_URL = "https://www.youku.com"
QR_URL_CACHE = COOKIE_DIR / "youku_qr_url.txt"

# 你之前那条会员 vid，用于 UPS 校验
TEST_VID = "XMjQ0ODk0Njg0"


def _need_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        print("❌ 未安装 playwright。请先执行：")
        print("   .build_venv/bin/pip install playwright && .build_venv/bin/python -m playwright install chromium")
        sys.exit(1)


def launch(headless: bool):
    from playwright.sync_api import sync_playwright
    Path(PROFILE).mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    # mac13-arm64 上 playwright 不提供独立 chromium 下载，改用本机已装 Google Chrome
    extra_args = {
        "channel": "chrome",
    }
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE,
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        **extra_args,
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return pw, context.browser, context


def _find_qr(page):
    """尝试抓取登录二维码图片 URL 或 base64。"""
    for sel in [
        "img[class*=qrcode]", "img[class*=qr]", "img[alt*=二维码]",
        "img[src*=qr]", ".login-qrcode img", "#qrCode img",
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                return el
        except Exception:
            pass
    return None


def _print_qr_ascii(page):
    """尽量把二维码画出来：优先用终端二维码库，否则打印图片路径。"""
    try:
        import qrcode  # type: ignore
    except Exception:
        qrcode = None
    # 抓二维码图片 src
    el = _find_qr(page)
    src = None
    if el is not None:
        try:
            src = el.get_attribute("src")
        except Exception:
            pass
    if src:
        QR_URL_CACHE.write_text(src, encoding="utf-8")
        print(f"📱 二维码地址已缓存：{QR_URL_CACHE}")
        print(f"   浏览器打开以下任一方式扫码：")
        print(f"   - 复制此 URL 到浏览器看大图：{src[:120]}{'...' if len(src) > 120 else ''}")
    else:
        print("⚠️ 未能自动定位二维码图片，请手动在弹出的浏览器窗口里扫码。")
    return src


def stage_login(headless: bool, contribute: bool = False):
    pw, browser, context = launch(headless)
    page = context.new_page()
    try:
        print("🌐 打开优酷首页 / 登录页 ...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        # 触发登录弹层：点「登录」按钮（优酷首页右上角）
        for sel in ["text=登录", "button:has-text('登录')", "a:has-text('登录')"]:
            try:
                loc = page.get_by_text("登录", exact=False).first
                if loc.is_visible(timeout=2000):
                    loc.click()
                    break
            except Exception:
                pass
        time.sleep(2)

        # 切到二维码登录
        switched = False
        for sel in ["text=扫码登录", "text=二维码登录", "text=扫码", "[class*=qrcode]"]:
            try:
                loc = page.get_by_text("扫码", exact=False).first
                if loc.is_visible(timeout=2000):
                    loc.click()
                    switched = True
                    break
            except Exception:
                pass
        time.sleep(2)
        print("🔄 已尝试切换到二维码登录。" if switched else "ℹ️ 未显式切到扫码，若页面已是扫码态可忽略。")

        src = _print_qr_ascii(page)
        # 兜底：二维码定位不到时截图，便于手动看码
        try:
            page.screenshot(path=str(COOKIE_DIR / "youku_login_page.png"))
        except Exception:
            pass
        if not headless:
            print("👉 请在弹出的浏览器窗口中用手机优酷/淘宝/支付宝 App 扫码并确认登录。")
        else:
            print("👉 无头模式下请打开缓存的二维码地址或 youku_login_page.png 扫码。")

        # 轮询登录态：优酷改版后不落地 P__yk__uck，改用 cnpassport 票据
        # 改用「登录按钮消失」作为主判据，辅以关键票据名
        LOGIN_MARKERS = ("P__yk__uck", "SESSIONID", "last_ud_youku", "_uab_collina", "ykus_utid")
        print("⏳ 等待扫码登录（最多 150s），请在 App 内确认登录...")
        deadline = time.time() + 150
        logged = False
        last_login_button = True
        while time.time() < deadline:
            try:
                still_login = page.get_by_text("登录", exact=False).first.is_visible(timeout=600)
            except Exception:
                still_login = False
            cookies = context.cookies()
            yk = [c for c in cookies if "youku.com" in c.get("domain", "")]
            has_marker = any(c.get("name") in LOGIN_MARKERS for c in yk)
            if not still_login:
                # 登录按钮消失 + 有 youku cookie => 视为已登录
                if yk:
                    logged = True
                    print("   ✅ 登录按钮消失且存在 youku cookie，判定已登录。")
                    break
                else:
                    if last_login_button:
                        print("   ℹ️ 登录按钮已消失，等待 cookie 落地...")
            elif has_marker:
                logged = True
                print(f"   ✅ 检测到登录票据（{[c['name'] for c in yk if c['name'] in LOGIN_MARKERS]}）。")
                break
            last_login_button = still_login
            time.sleep(1)

        if not logged:
            print("❌ 150s 内未检测到登录态，登录可能未完成或被风控。")
            print("   请确认手机已扫码并在 App 内点了「确认登录」。可重跑本脚本。")
            return

        # 登录成功后主动访问播放页，触发票据完整落地
        print("🌐 访问播放页触发票据落地...")
        try:
            page.goto(f"https://v.youku.com/v_show/id_{TEST_VID}.html", wait_until="domcontentloaded", timeout=30000)
            time.sleep(4)
        except Exception:
            pass
        # 再访问 cnpassport 域
        try:
            page.goto("https://cnpassport.youku.com/", wait_until="domcontentloaded", timeout=15000)
            time.sleep(2)
        except Exception:
            pass
        time.sleep(2)
        cookies = context.cookies()
        n = _export(cookies)
        print(f"✅ 已检测到登录态，导出 {n} 条 Cookie。")
        _ups_verify()
        if contribute:
            stage_contribute(HEADER_OUT.read_text(encoding="utf-8").strip())
    finally:
        browser.close()
        pw.stop()


def _export(cookies):
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    # Netscape 格式
    lines = ["# Netscape HTTP Cookie File", "# Generated by youku_login.py", ""]
    for c in cookies:
        domain = c.get("domain", "")
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expiry = int(c.get("expires", 0)) if c.get("expires") else 0
        name = c.get("name", "")
        value = c.get("value", "")
        lines.append("\t".join([domain, include_sub, path, secure, str(expiry), name, value]))
    NETSCAPE_OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # header 单行字符串（按域名过滤 youku，保持原顺序）
    yk = [c for c in cookies if "youku.com" in c.get("domain", "")]
    header = "; ".join(f"{c['name']}={c['value']}" for c in yk)
    HEADER_OUT.write_text(header, encoding="utf-8")

    print(f"   Netscape : {NETSCAPE_OUT}")
    print(f"   Header   : {HEADER_OUT}")
    return len(cookies)


def _ups_verify():
    """用导出的 header 直连 UPS，确认 user.login 非空（-3007 可消）。"""
    if not HEADER_OUT.exists():
        print("⚠️ 无 header 文件，跳过 UPS 校验。")
        return
    header = HEADER_OUT.read_text(encoding="utf-8").strip()
    if not header:
        print("⚠️ header 为空，跳过 UPS 校验。")
        return
    try:
        import urllib.request
        import urllib.error
        cna = ""
        for kv in header.split(";"):
            kv = kv.strip()
            if kv.startswith("cna="):
                cna = kv[4:]
                break
        url = f"https://ups.youku.com/ups/get.json?vid={TEST_VID}&ccode=0564&utid={cna}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://v.youku.com/",
            "Cookie": header,
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        user = data.get("data", {}).get("user", {})
        login = user.get("login")
        if login:
            print(f"✅ UPS 校验通过：user.login={login!r}，-3007 可消。")
        else:
            print(f"⚠️ UPS user.login 仍为空，可能该账号无此片权限或 cookie 不全。")
    except Exception as e:
        print(f"⚠️ UPS 校验请求失败：{str(e)[:160]}")


def stage_contribute(header: str):
    """把导出的 youku header 贡献到线上共享池（覆盖 youku.com.json）。"""
    if not header:
        print("❌ 无 header 可贡献。")
        return
    if "P__yk__uck" not in header:
        print("❌ 贡献中止：header 不含 P__yk__uck，贡献了也消不掉 -3007。")
        return
    payload = json.dumps({"host": "youku.com", "cookie": header}).encode("utf-8")
    req = urllib.request.Request(
        CONTRIBUTE_URL, data=payload, method="POST", headers={
            "User-Agent": CONTRIBUTE_UA,
            "Content-Type": "application/json",
            "Origin": "https://hanyuxz.top",
            "Referer": "https://hanyuxz.top/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "ignore")
        print(f"✅ 已贡献到共享池：{CONTRIBUTE_URL}")
        print(f"   响应：{body[:300]}")
    except Exception as e:
        print(f"⚠️ 贡献请求失败：{str(e)[:200]}")
        print("   可稍后手动执行：.build_venv/bin/python youku_login.py --header-only")


def stage_verify_only():
    if not HEADER_OUT.exists():
        print(f"❌ 未找到 {HEADER_OUT}，请先运行登录。")
        sys.exit(1)
    _ups_verify()


def stage_header_only():
    if not HEADER_OUT.exists():
        print(f"❌ 未找到 {HEADER_OUT}，请先运行登录。")
        sys.exit(1)
    header = HEADER_OUT.read_text(encoding="utf-8").strip()
    print("=== 贡献用 Cookie Header（复制发给动哥 / 直接 POST /api/cookie/contribute）===")
    print(header)
    print("=== 校验 P__yk__uck 是否在其中 ===")
    print("YES" if "P__yk__uck" in header else "NO ❌")


def main():
    _need_playwright()
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true", help="无头模式（默认有头，方便扫码）")
    ap.add_argument("--verify", action="store_true", help="仅用已有 cookie 跑 UPS 校验")
    ap.add_argument("--header-only", action="store_true", help="只打印贡献用 header 字符串")
    ap.add_argument("--contribute", action="store_true", help="登录成功后自动贡献到共享池")
    args = ap.parse_args()

    if args.verify:
        stage_verify_only()
    elif args.header_only:
        stage_header_only()
    else:
        stage_login(args.headless, contribute=args.contribute)


if __name__ == "__main__":
    main()
