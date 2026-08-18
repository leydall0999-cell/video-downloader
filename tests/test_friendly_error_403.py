"""_friendly_error 的 403 根因分层单元测试（纯逻辑，无网络 / 无 yt-dlp 真实请求）。

覆盖：
  A. 强反爬站（douyin/v.qq.com 等）无 Cookie  → category=cookie_required
  B. 强反爬站 有 Cookie 但被拒        → category=cookie_invalid_or_expired
  C. 非 hardened 站（YouTube/bilibili）→ category=cdn_forbidden
  D. 非 403 错误（private/geo）        → 走既有规则，category 默认 unknown
"""
import os
import sys

# 让 import downloader 能找到同目录的 server 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import downloader  # noqa: E402


class _FakeExc(Exception):
    pass


def _ctx403(host: str, *, cookie: str = "", with_options_cookie: bool = False, proxy: str = "http://127.0.0.1:9"):
    """构造一个 403 异常 + 对应诊断上下文（proxy 占位避免触发真实代理探测）。"""
    exc = _FakeExc("ERROR: HTTP Error 403: Forbidden")
    options = None
    if with_options_cookie:
        options = {"http_headers": {"Cookie": cookie or "SESSDATA=demo"}}
    return exc, downloader._build_diag_context(
        f"https://{host}/some/path", cookie=cookie, proxy=proxy, options=options
    )


def test_hardened_no_cookie_is_cookie_required():
    exc, ctx = _ctx403("www.douyin.com")
    assert ctx["is_hardened"] is True
    assert ctx["cookie_present"] is False
    err = downloader._friendly_error(exc, ctx)
    assert err.category == "cookie_required"
    assert "Cookie" in err.message


def test_hardened_with_cookie_is_invalid_or_expired():
    exc, ctx = _ctx403("www.douyin.com", cookie="SESSDATA=demo", with_options_cookie=True)
    assert ctx["is_hardened"] is True
    assert ctx["cookie_present"] is True
    err = downloader._friendly_error(exc, ctx)
    assert err.category == "cookie_invalid_or_expired"


def test_vqq_hardened_no_cookie_is_cookie_required():
    exc, ctx = _ctx403("v.qq.com")
    assert ctx["is_hardened"] is True
    err = downloader._friendly_error(exc, ctx)
    assert err.category == "cookie_required"


def test_youtube_403_is_cdn_forbidden():
    exc, ctx = _ctx403("www.youtube.com")
    assert ctx["is_hardened"] is False
    err = downloader._friendly_error(exc, ctx)
    assert err.category == "cdn_forbidden"


def test_bilibili_403_is_cdn_forbidden():
    # B 站公开视频一般不强制登录，403 多来自 CDN 防盗链/地区，当前按通用分支处理；
    # 但文案需追加 B站 专属提示（高画质/会员常需登录态 Cookie）
    exc, ctx = _ctx403("www.bilibili.com")
    assert ctx["is_hardened"] is False
    err = downloader._friendly_error(exc, ctx)
    assert err.category == "cdn_forbidden"
    assert "B站" in err.hint and "登录态 Cookie" in err.hint


def test_bilibili_short_link_403_has_bilibili_hint():
    # 短链 b23.tv 也应命中 B站 专属提示
    exc, ctx = _ctx403("b23.tv")
    assert ctx["is_hardened"] is False
    err = downloader._friendly_error(exc, ctx)
    assert err.category == "cdn_forbidden"
    assert "B站" in err.hint


def test_non_403_private_video_keeps_unknown_category():
    exc = _FakeExc("This video is private")
    err = downloader._friendly_error(exc)
    assert err.category == "unknown"
    assert "私密" in err.message or "登录" in err.message


def test_no_context_returns_cdn_forbidden_fallback_for_403():
    # 兼容旧调用点（未传 context）不抛异常，且 403 仍给通用分支
    exc = _FakeExc("HTTP Error 403: Forbidden")
    err = downloader._friendly_error(exc)
    assert err.category == "cdn_forbidden"


if __name__ == "__main__":
    test_hardened_no_cookie_is_cookie_required()
    test_hardened_with_cookie_is_invalid_or_expired()
    test_vqq_hardened_no_cookie_is_cookie_required()
    test_youtube_403_is_cdn_forbidden()
    test_bilibili_403_is_cdn_forbidden()
    test_bilibili_short_link_403_has_bilibili_hint()
    test_non_403_private_video_keeps_unknown_category()
    test_no_context_returns_cdn_forbidden_fallback_for_403()
    print("✅ 全部 403 诊断分层测试通过")
