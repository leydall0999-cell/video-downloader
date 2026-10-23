"""金牌影院一族（chrqj.com / nmwchxx.com）Cookie 池链路守卫。

背景（2026-10-21）：用户第三次报金牌影院解析问题，排查中发现 chrqj.com
只是老域名（首页 302 → nmwchxx.com），同一套 mw-movie API / 签名密钥 /
视频 ID。收编解析器时顺带发现 Cookie 池链路也只认老域名：

  - _COOKIE_HARDENED_DOMAINS 只有 chrqj.com ⇒ 新域名登录态进不了公共池、
    桌面端也不会自动读浏览器 Cookie；
  - verify_cookie 的派发写死 d == "chrqj.com" ⇒ 新域名只能走通用验真兜底；
  - _TEST_URLS 没有新域名条目。

本测试锁死这三个落点，防止下次再加域名时又只改一处。
"""
import os
import sys
import types

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVER)

PASS = 0
FAIL = 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def main() -> int:
    import downloader as D
    import cookie_pool as CP

    print("[1] hardened 清单：两个域名都要在（决定能否进池 + 桌面端自动读）")
    for d in ("chrqj.com", "nmwchxx.com"):
        check(f"hardened 含 {d}", d in D._COOKIE_HARDENED_DOMAINS)
    check("www 子域也命中 hardened", D.is_cookie_hardened_host("www.nmwchxx.com"))
    check("非本站不进 hardened", not D.is_cookie_hardened_host("nmwchxx.com.evil.example"))

    print("[2] 池白名单由 hardened 派生 ⇒ 新域名自动放行")
    roots = CP._root_domains()
    check("白名单含 nmwchxx.com", "nmwchxx.com" in roots)
    check("白名单含 chrqj.com", "chrqj.com" in roots)

    print("[3] 验真派发：一族两域都走签名验真（不打通用兜底）")
    calls = []

    def fake_verify(header, domain="chrqj.com"):
        calls.append(domain)
        return True

    orig = CP.verify_chrqj
    CP.verify_chrqj = fake_verify
    try:
        for dom in ("chrqj.com", "www.chrqj.com", "nmwchxx.com", "www.nmwchxx.com"):
            calls.clear()
            r = CP.verify_cookie(dom, "sessionid=x")
            check(f"{dom} → 签名验真且传对域", r is True and calls == [dom.replace("www.", "", 1)],
                  f"calls={calls}")
        calls.clear()
        r = CP.verify_cookie("bilibili.com", "SESSDATA=x")
        check("B 站不被误派发到金牌验真", calls == [])
    finally:
        CP.verify_chrqj = orig

    print("[4] 签名验真按域取 API 主机与 Referer（不写死老域名）")
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"code": 200, "data": {"list": [{"url": "https://cdn.example/a.m3u8"}]}}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        return _Resp()

    class _FakeRequests:
        @staticmethod
        def get(url, **kw):
            return fake_get(url, **kw)

    orig_req = CP.requests
    CP.requests = _FakeRequests
    try:
        for dom in ("chrqj.com", "nmwchxx.com"):
            seen.clear()
            r = CP.verify_chrqj("sessionid=x", dom)
            check(f"{dom} → 验真通过", r is True)
            check(f"{dom} → API 打在 www.{dom}", f"https://www.{dom}/mw-movie/" in seen.get("url", ""),
                  seen.get("url", ""))
            check(f"{dom} → Referer 用 www.{dom}", seen["headers"].get("Referer") == f"https://www.{dom}/")
    finally:
        CP.requests = orig_req

    print("[5] _TEST_URLS：新域名有兜底测试 URL（通用验真用）")
    check("nmwchxx.com 有测试 URL", "nmwchxx.com" in CP._TEST_URLS)
    check("测试 URL 是站内标准形态", "/vod/play/116537/sid/877419" in CP._TEST_URLS.get("nmwchxx.com", ""))
    check("按域取测试 URL", CP._test_url("www.nmwchxx.com") == CP._TEST_URLS["nmwchxx.com"])

    print("[6] 签名密钥一族共用（解析器与验真同一把 key）")
    check("cookie_pool 与 downloader 同 key",
          CP._CHRQJ_SIGN_KEY == D._NMW_SIGN_KEY)

    print()
    print(f"通过: {PASS}   失败: {FAIL}")
    if FAIL == 0:
        print("🎉 金牌影院 Cookie 池一族链路全部通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
