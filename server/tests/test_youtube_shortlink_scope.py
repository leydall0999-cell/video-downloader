#!/usr/bin/env python3
"""YouTube 短链（youtu.be / shorts / m. / music.）必须归一化成长链的回归测试（2026-09-29）。

背景（香港节点实测定位，用户截图为证）
------------------------------------
用户在网页版粘 `https://youtu.be/Rpv74_GxY6s?si=...`，得到红色横幅
「YouTube 需要登录 Cookie 才能解析」——但公共池里**当时就有有效 Cookie**，
重粘 Cookie 永远修不好。实测矩阵（同一视频、同一份公共池 Cookie、同一 web_safari）：

    youtu.be/Rpv74_GxY6s                  → bot 拦截（失败）
    m.youtube.com/watch?v=Rpv74_GxY6s     → bot 拦截（失败）
    www.youtube.com/watch?v=Rpv74_GxY6s   → 成功，11 个格式
    youtu.be/Rpv74_GxY6s + cookiefile     → 成功

根因：yt-dlp 通过 `http_headers["Cookie"]` 注入的 Cookie 会被**限定在输入 URL 的域**
（yt-dlp 原话 `they will be scoped to the domain of the downloaded urls`）。
短链把 Cookie 挂到 `youtu.be`，而真正取播放信息的 innertube 请求打到
`www.youtube.com/youtubei/v1/player` 时**不带 Cookie** → 被判 bot。

契约（本测试钉住）
  1. 各种等价形态一律归一化为 `https://www.youtube.com/watch?v=<id>`：
     youtu.be/<id>、m./music./裸域的 /watch?v=、/shorts/、/live/、/embed/、/v/、
     youtube-nocookie.com/embed/；
  2. `www.youtube.com/watch?v=<id>` 原样通过（保留 list / index / t 等有语义参数）；
  3. 非 YouTube 链接（B站/抖音/X）与非法形态（ID 非 11 位、playlist、频道页）
     一律原样返回——不猜、不改；
  4. **`_resolve_youtube` 绝不把短链交给 yt-dlp**：即使调用方漏了归一化，
     函数入口也会兜一层（这是本次 bug 的根防线）；
  5. 复制丢字的 ID（≠11 位）在打 yt-dlp 之前就报「视频链接不完整」（category=bad_url），
     不再让它流进 bot 检测、变成误导性的「需要登录 Cookie」。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import downloader as dl  # noqa: E402

_VID = "Rpv74_GxY6s"
_WANT = f"https://www.youtube.com/watch?v={_VID}"


# --------------------------------------------------------------------------- #
# 1. 归一化形态表（全部必须落到 www.youtube.com 长链）
# --------------------------------------------------------------------------- #
def test_short_forms_all_normalized():
    cases = [
        f"https://youtu.be/{_VID}?si=u1h2E430EY2cjFOy",
        f"https://youtu.be/{_VID}",
        f"https://youtu.be/{_VID}/",
        f"https://www.youtu.be/{_VID}",
        f"https://m.youtube.com/watch?v={_VID}",
        f"https://music.youtube.com/watch?v={_VID}",
        f"https://youtube.com/watch?v={_VID}",
        f"https://www.youtube.com/shorts/{_VID}",
        f"https://m.youtube.com/shorts/{_VID}",
        f"https://www.youtube.com/live/{_VID}?feature=share",
        f"https://www.youtube.com/embed/{_VID}",
        f"https://www.youtube.com/v/{_VID}",
        f"https://www.youtube-nocookie.com/embed/{_VID}",
    ]
    for u in cases:
        got = dl._normalize_share_url(u)
        assert got == _WANT, f"{u}\n  应归一化为 {_WANT}\n  实际为     {got}"
        # 幂等：归一化结果再跑一遍不应变形
        assert dl._normalize_share_url(got) == _WANT, f"归一化不幂等：{got}"
    print(f"✅ 短链/子域/嵌入页 {len(cases)} 种形态全部归一化为 www.youtube.com 长链")


def test_resolve_entry_normalizes_before_ytdlp():
    """根防线：短链绝不能进 yt-dlp（否则 Cookie 被域作用域挡住 → 假 bot 拦截）。"""
    seen = []

    class _CapturingYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            hdrs = dict(self.opts.get("http_headers") or {})
            seen.append((url, "Cookie" in hdrs))
            if "Cookie" not in hdrs:
                # 免 Cookie 路径：模拟数据中心 IP 被 bot 拦
                raise dl.ExtractorError(
                    "ERROR: [youtube] abc: Sign in to confirm you're not a bot."
                )
            return {"id": "abc", "title": "ok", "formats": [{}], "webpage_url": url}

    _saved_ydl = dl._YoutubeDL
    _saved_vd = dl._fetch_youtube_visitor_data
    _saved_proxy = dl._resolve_proxy
    _saved_cands = dl._youtube_cookie_candidates
    _saved_evict = dl._evict_youtube_cookie_cache
    try:
        dl._YoutubeDL = _CapturingYDL
        dl._fetch_youtube_visitor_data = lambda proxy="": ""
        dl._resolve_proxy = lambda host: ""
        dl._youtube_cookie_candidates = lambda user_cookie: [("pool", "LOGIN_INFO=x")]
        dl._evict_youtube_cookie_cache = lambda reason="": None

        info = dl._resolve_youtube(f"https://youtu.be/{_VID}?si=tracking", "", "")
        assert info, "有有效 Cookie 源时短链也应解析成功"
    finally:
        dl._YoutubeDL = _saved_ydl
        dl._fetch_youtube_visitor_data = _saved_vd
        dl._resolve_proxy = _saved_proxy
        dl._youtube_cookie_candidates = _saved_cands
        dl._evict_youtube_cookie_cache = _saved_evict

    assert seen, "yt-dlp 没有被调用，测试自身失效"
    for url, _has_ck in seen:
        assert "youtu.be" not in url, f"短链漏进了 yt-dlp（Cookie 会被域作用域挡住）：{url}"
        assert url == _WANT, f"交给 yt-dlp 的 URL 应为长链，实际：{url}"
    # 免 Cookie 路径失败 → 带 Cookie 的源成功，证明「换成长链后 Cookie 真生效」
    assert any(has_ck for _u, has_ck in seen), "带 Cookie 的尝试没有发生"
    assert seen[-1][1] is True, "最后一次（成功那次）应带 Cookie"
    print(f"✅ 短链在进入 yt-dlp 前已归一化（共 {len(seen)} 次调用，末次带 Cookie 成功）")


# --------------------------------------------------------------------------- #
# 2. 复制丢字的 ID：给「链接不完整」，别流入 bot/Cookie 兜底报误导性错误
# --------------------------------------------------------------------------- #
def test_validate_truncated_id_reports_clear_error():
    # 11 位合法 ID 与非 YouTube 链接：不得报错
    for u in (_WANT, f"https://youtu.be/{_VID}", f"https://www.youtube.com/shorts/{_VID}",
              "https://www.bilibili.com/video/BV1xx411c7mD"):
        dl.validate_youtube_id(u)  # 不抛即通过

    # 10 位（复制丢字，app 端真实案例 youtu.be/mGBQMAUayc）与过短 ID：必须报明确错
    for u in ("https://youtu.be/mGBQMAUayc", "https://www.youtube.com/watch?v=abc"):
        try:
            dl.validate_youtube_id(u)
        except dl.ResolveError as exc:
            assert exc.message == "视频链接不完整", f"实际标题：{exc.message}"
            assert getattr(exc, "category", "") == "bad_url", "前端据此显示「链接不完整」而非贴 Cookie"
            assert "11 位" in (exc.hint or ""), f"提示应说明应为 11 位：{exc.hint}"
        else:
            raise AssertionError(f"{u} 应报「视频链接不完整」，实际没报错")

    # 入口契约：截断 ID 在调用 yt-dlp 之前就被拦下（一次都不该打出去）
    calls = []

    class _NoCallYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):  # pragma: no cover
            calls.append(url)
            raise AssertionError("截断 ID 不该走到 yt-dlp")

    _saved = (dl._YoutubeDL, dl._fetch_youtube_visitor_data, dl._resolve_proxy)
    try:
        dl._YoutubeDL = _NoCallYDL
        dl._fetch_youtube_visitor_data = lambda proxy="": ""
        dl._resolve_proxy = lambda host: ""
        try:
            dl._resolve_youtube("https://youtu.be/mGBQMAUayc", "", "")
        except dl.ResolveError as exc:
            assert exc.message == "视频链接不完整", f"实际：{exc.message}"
        else:
            raise AssertionError("截断 ID 应抛 ResolveError")
    finally:
        dl._YoutubeDL, dl._fetch_youtube_visitor_data, dl._resolve_proxy = _saved
    assert not calls, "截断 ID 不该触发任何 yt-dlp 请求"
    print("✅ 截断 ID（10 位）：解析前明确报「视频链接不完整」，零次 yt-dlp 请求")


# --------------------------------------------------------------------------- #
# 2b. 标准长链 / 非视频页原样通过
# --------------------------------------------------------------------------- #
def test_canonical_and_non_video_passthrough():
    same = [
        f"https://www.youtube.com/watch?v={_VID}",
        f"https://www.youtube.com/watch?v={_VID}&list=PLabc&index=3",
        "https://www.youtube.com/playlist?list=PLabc",
        "https://www.youtube.com/watch?list=PLabc",          # 无 v，别乱造
        "https://www.youtube.com/@somechannel",
        "https://www.youtube.com/results?search_query=abc",
    ]
    for u in same:
        assert dl._normalize_share_url(u) == u, f"应原样返回，实际被改成 {dl._normalize_share_url(u)}"
    print(f"✅ 标准长链/播放列表/频道页 {len(same)} 例原样通过（不误伤 list 等参数）")


# --------------------------------------------------------------------------- #
# 3. 非法 ID 与非 YouTube 链接：不猜、不改
# --------------------------------------------------------------------------- #
def test_invalid_and_other_platforms_untouched():
    same = [
        "https://youtu.be/too-short",           # ID 非 11 位
        "https://youtu.be/",
        "https://youtu.be/abcdefghijk?list=PL",  # 合法 ID 但纯 watch 语义 → 允许改写
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1xx411c7mD?p=2&t=3",
        "https://b23.tv/abc123",
        "https://v.douyin.com/abc/",
        "https://x.com/someone/status/123",
    ]
    for u in same:
        got = dl._normalize_share_url(u)
        if u.startswith("https://youtu.be/abcdefghijk"):
            assert got == "https://www.youtube.com/watch?v=abcdefghijk", f"实际：{got}"
            continue
        assert got == u, f"{u} 不应被改动，实际：{got}"
    # 直接调 helper：非 YouTube host 一律原样
    assert dl._normalize_youtube_url("https://x.com/a") == "https://x.com/a"
    assert dl._normalize_youtube_url("https://v.douyin.com/abc/") == "https://v.douyin.com/abc/"
    print("✅ 非法形态/其他平台原样返回（B站、抖音短链逻辑不受影响）")


if __name__ == "__main__":
    test_short_forms_all_normalized()
    test_validate_truncated_id_reports_clear_error()
    test_resolve_entry_normalizes_before_ytdlp()
    test_canonical_and_non_video_passthrough()
    test_invalid_and_other_platforms_untouched()
    print("\n🎉 YouTube 短链归一化测试全部通过")
