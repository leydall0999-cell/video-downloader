#!/usr/bin/env python3
"""从「Copy as cURL」贡献优酷 Cookie + ckey 到线上公共池。

用法：
  python3 tools/contribute_youku_curl.py < youku_ups.curl
  python3 tools/contribute_youku_curl.py youku_ups.curl

参数：
  cURL 文本来自：已登录优酷的浏览器 → F12 → Network → 过滤 ups → 刷新播放页
  → 找到 ups/get.json 请求 → 右键 Copy as cURL (bash) → 粘贴到文件或 stdin。

说明：
  优酷 -3007 的根因是 yt-dlp 内置 YoukuIE 不生成播放签名 ckey。本工具把浏览器
  实时生成的 ckey（含 Cookie）贡献到共享池，VDL 解析优酷时走专用 UPS 通道直接拿 m3u8。
  ckey 有时效（数小时~1天），过期后需重新贡献一次。
"""
import re
import sys
import json
import ssl
import urllib.parse
import urllib.request

CONTRIBUTE_URL = "https://hanyuxz.top/api/cookie/contribute"


def extract(curl_text: str):
    m = re.search(r"ckey=([^&'\s]+)", curl_text)
    ckey = urllib.parse.unquote(m.group(1)) if m else ""
    bm = re.search(r"-\s*b\s+'([^']+)'", curl_text)
    cookie = bm.group(1) if bm else ""
    vm = re.search(r"vid=(\w+)", curl_text)
    vid = vm.group(1) if vm else ""
    return ckey, cookie, vid


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            curl_text = f.read()
    else:
        curl_text = sys.stdin.read()

    ckey, cookie, vid = extract(curl_text)
    if not ckey or not cookie:
        print("ERROR: 未能从 cURL 中解析出 ckey 或 cookie，请确认复制的是 ups/get.json 的完整请求")
        sys.exit(1)

    url = f"https://v.youku.com/v_show/id_{vid}.html" if vid else "https://v.youku.com/v_show/id_XMjQ0ODk0Njg0.html"
    payload = json.dumps({"url": url, "cookie": cookie, "ckey": ckey}).encode()
    req = urllib.request.Request(
        CONTRIBUTE_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        resp = urllib.request.urlopen(req, timeout=20, context=ctx)
        print("CONTRIBUTE RESP:", resp.read().decode()[:300])
    except Exception as e:
        print("CONTRIBUTE ERR:", repr(e)[:300])
        sys.exit(1)


if __name__ == "__main__":
    main()
