#!/usr/bin/env python3
"""
一键重启 Chrome 并带远程调试端口（9222），用 AppleScript 自动化，用户无需手动敲命令。
重启后用 CDP 接管已登录会话抓优酷流。

用法：
  .build_venv/bin/python youku_cdp_launch.py
然后按提示在弹出的 Chrome 里登录/确认优酷可播放，再跑 youku_cdp_capture.py
"""
import subprocess
import time
import sys
import urllib.request
import json

CDP_PORT = 9222
DEBUG_URL = f"http://127.0.0.1:{CDP_PORT}/json/version"


def quit_chrome():
    print("🔻 正在退出 Chrome...")
    subprocess.run(["osascript", "-e", 'quit app "Google Chrome"'], check=False)
    time.sleep(3)


def launch_chrome_debug():
    print("🚀 以远程调试端口启动 Chrome...")
    # 直接调用二进制，带 --remote-debugging-port
    subprocess.Popen(
        ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
         f"--remote-debugging-port={CDP_PORT}",
         "--no-first-run", "--no-default-browser-check"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def wait_cdp(timeout=20):
    print(f"⏳ 等待 CDP 端口 {CDP_PORT} 就绪（最多 {timeout}s）...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(DEBUG_URL, timeout=3) as r:
                data = json.loads(r.read().decode())
                print(f"✅ CDP 已就绪: {data.get('Browser')}")
                return True
        except Exception:
            time.sleep(1)
    return False


def main():
    quit_chrome()
    launch_chrome_debug()
    if wait_cdp():
        print("\n✅ Chrome 已带调试端口启动。")
        print("👉 现在请在弹出的 Chrome 窗口里：")
        print("   1. 打开 https://www.youku.com 并登录（若未登录）")
        print("   2. 打开你要下载的优酷视频，确认能播放")
        print("   3. 完成后回来告诉我，我跑 youku_cdp_capture.py 抓流")
        sys.exit(0)
    else:
        print("❌ CDP 端口未就绪，启动可能失败。请检查 Chrome 是否正常运行。")
        sys.exit(1)


if __name__ == "__main__":
    main()
