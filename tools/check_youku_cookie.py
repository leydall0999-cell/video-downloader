#!/usr/bin/env python3
"""
优酷 Cookie 自检脚本 —— 在贡献/发给我之前先本地验证。

用法：
  echo "ysestep=1; cna=...; ..." | python3 check_youku_cookie.py
  或
  python3 check_youku_cookie.py  <(echo "ysestep=1; ...")

判定标准：
  1. 必须含 P__yk__uck（优酷登录态核心 uid 票）
  2. 直连 UPS API 时 user.login 必须非空

两项都满足才算有效，否则 -3007 必然还在。
"""
import json
import re
import sys
import urllib.request
import urllib.error

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def extract(cookie: str, name: str) -> str | None:
    m = re.search(r"(?:^|;)\s*" + re.escape(name) + r"=([^;]*)", cookie)
    return m.group(1) if m else None


def ups_probe(cookie: str, vid: str) -> dict:
    cna = extract(cookie, "cna") or ""
    url = f"https://ups.youku.com/ups/get.json?vid={vid}&ccode=0564&utid={cna}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://v.youku.com/",
        "Cookie": cookie,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        return {"ok": False, "err": str(e)[:160]}
    user = data.get("data", {}).get("user", {})
    stream = data.get("data", {}).get("stream", {})
    return {
        "ok": True,
        "code": data.get("code"),
        "msg": data.get("msg"),
        "user_login": user.get("login"),
        "stream_present": bool(stream),
    }


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print("❌ 没读到 cookie 文本（请用 echo ... | 本脚本，或重定向）")
        sys.exit(2)

    # 有些导出会把多条用 .. 或换行拼一起，先取最长一段去重合并
    segs = re.split(r"\s*\.\.\s*|\n+", raw)
    merged = "; ".join(sorted({s.strip().rstrip(";").strip() for s in segs if s.strip()}, key=len, reverse=True))
    # 合并所有段的 key（防止被 .. 截断丢失）
    all_keys = {}
    for s in segs:
        for kv in s.split(";"):
            kv = kv.strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                all_keys[k.strip()] = v.strip()
    union = "; ".join(f"{k}={v}" for k, v in all_keys.items())

    has_uck = "P__yk__uck" in union
    has_session = "SESSIONID" in union
    has_cna = "cna" in all_keys

    vid = "XMjQ0ODk0Njg0"  # 你之前那条会员 vid，用于实测
    ups = ups_probe(union, vid)

    print("=" * 56)
    print("优酷 Cookie 自检结果")
    print("=" * 56)
    print(f"  含 P__yk__uck (登录票): {'✅ YES' if has_uck else '❌ NO'}")
    print(f"  含 SESSIONID        : {'✅ YES' if has_session else '⚠️  NO'}")
    print(f"  含 cna (utid)       : {'✅ YES' if has_cna else '⚠️  NO'}")
    print("-" * 56)
    if ups.get("ok"):
        print(f"  UPS user.login      : {ups.get('user_login')!r}")
        print(f"  UPS stream 存在     : {'✅ YES' if ups['stream_present'] else '❌ NO'}")
        print(f"  UPS code/msg        : {ups.get('code')} / {ups.get('msg')}")
    else:
        print(f"  UPS 直连失败        : {ups.get('err')}")
    print("=" * 56)

    valid = has_uck and ups.get("user_login")
    if valid:
        print("✅ 结论：这是有效登录态 cookie，可以贡献进池，去测 -3007。")
        sys.exit(0)
    else:
        print("❌ 结论：无效。优酷没认出登录态，-3007 必然仍在。")
        print("   原因：浏览器在 youku.com 上并未真正登录主账号（访客态/过期/第三方授权未落地）。")
        print("   解决：开 https://www.youku.com → 确认右上角有头像昵称 →")
        print("         进任意会员片播放页 → Cookie-Editor 导出 youku.com 全部 cookie →")
        print("         重跑本脚本，直到 P__yk__uck=YES 且 user.login 非空。")
        sys.exit(1)


if __name__ == "__main__":
    main()
