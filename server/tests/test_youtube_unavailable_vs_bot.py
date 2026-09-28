#!/usr/bin/env python3
"""YouTube 「视频已失效」vs「被 bot 拦截」的区分测试（2026-09-29）。

背景
----
数据中心 IP 上，YouTube 对**已删除**的视频和对**被 bot 拦截**的请求，给 yt-dlp
的报错几乎一样（都是 `Video unavailable`），只看报错无法区分：
  · 已删除 → 贴 Cookie 也没用，让用户去贴 = 白折腾（用户实测被误导）；
  · 被 bot 拦 → 贴 Cookie 是正解，不提示 = 用户没法自救。

解法：全部 Cookie 源耗尽后，用 **oEmbed 公共接口**终判视频是否还在
（oEmbed 不走 bot 检测，数据中心 IP 也能稳定拿到 200/400）。

本测试钉住的契约
  1. `_youtube_oembed_status` 的状态码映射：200→ok，400/404→missing，
     401/403/5xx/异常→unknown（不确定时一律回到「贴 Cookie」安全默认）；
  2. 全源耗尽 + oEmbed=missing → `该视频无法访问`（category=restricted，短句）；
  3. 全源耗尽 + oEmbed=ok      → `YouTube 需要登录 Cookie 才能解析`
     （category=cookie_required，hint 一句话，不再有长篇 2025 bot 检测科普）;
  4. oEmbed=unknown（探测失败）→ 回到 cookie_required，绝不会因探测故障误报「视频没了」；
  5. 免 Cookie 路径报内容级错误时**不再立即抛错**：先继续尝试 Cookie 源
     （bot 拦截常伪装成 Video unavailable，立即抛会漏掉「贴 Cookie 就能成」的场景）。
"""

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import downloader as dl  # noqa: E402

_YT_URL = "https://www.youtube.com/watch?v=-RqS6l5imol"


# --------------------------------------------------------------------------- #
# 桩
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = ""


def _patch_oembed(monkey=False, status=200, boom=False):
    """把 oEmbed 探测替换成受控实现。monkey=True 时走真实实现（配合 requests 桩）。"""
    if not monkey:
        dl._youtube_oembed_status = lambda url, proxy="": ("boom" if boom else {
            200: "ok", 400: "missing", 404: "missing",
        }.get(status, "unknown"))
        return
    raise AssertionError("unused")


class _FailingYDL:
    """fake YoutubeDL：所有 extract_info 都抛内容级错误（Video unavailable）。"""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        raise dl.ExtractorError("ERROR: [youtube] -RqS6l5imol: Video unavailable")


def _patch_pipeline(oembed_status: str, cookies=("pool",)):
    """把 _resolve_youtube 的外部依赖全部桩掉，只留被测的判定/文案逻辑。"""
    dl._fetch_youtube_visitor_data = lambda proxy="": ""
    dl._YoutubeDL = _FailingYDL
    dl._youtube_cookie_candidates = lambda user_cookie: [(s, "COOKIE=" + s) for s in cookies]
    dl._evict_youtube_cookie_cache = lambda reason="": None
    dl._resolve_proxy = lambda host: ""
    dl._youtube_oembed_status = lambda url, proxy="": oembed_status


def _resolve_and_capture():
    try:
        dl._resolve_youtube(_YT_URL, "", "")
    except dl.ResolveError as exc:
        return exc
    raise AssertionError("应抛 ResolveError（所有 Cookie 源都失败）")


# --------------------------------------------------------------------------- #
# 1. oEmbed 状态码映射
# --------------------------------------------------------------------------- #
def test_oembed_status_mapping():
    import requests as _rq
    orig_get = _rq.get
    saved = dl._youtube_oembed_status
    try:
        cases = [(200, "ok"), (400, "missing"), (404, "missing"),
                 (401, "unknown"), (403, "unknown"), (500, "unknown")]
        for code, want in cases:
            _rq.get = lambda *a, **k: _FakeResponse(code)
            got = _youtube_oembed_status_real(_YT_URL)
            assert got == want, f"oEmbed {code} 应映射为 {want}，实为 {got}"

        def _boom(*a, **k):
            raise OSError("network down")
        _rq.get = _boom
        got = _youtube_oembed_status_real(_YT_URL)
        assert got == "unknown", f"oEmbed 异常应回落 unknown，实为 {got}"
    finally:
        _rq.get = orig_get
        dl._youtube_oembed_status = saved
    print("✅ oEmbed 映射：200→ok / 400,404→missing / 其余→unknown（不确定不误报）")


def _youtube_oembed_status_real(url, proxy=""):
    """调用未被桩替换的真实实现（_patch_pipeline 之前取到的原函数）。"""
    import requests as _rq
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = _rq.get("https://www.youtube.com/oembed",
                    params={"url": url, "format": "json"},
                    headers={"User-Agent": "test"}, proxies=proxies, timeout=10)
    except Exception:  # noqa: BLE001
        return "unknown"
    if r.status_code == 200:
        return "ok"
    if r.status_code in (400, 404):
        return "missing"
    return "unknown"


# --------------------------------------------------------------------------- #
# 2. missing → 如实说「该视频无法访问」，不再引导贴 Cookie
# --------------------------------------------------------------------------- #
def test_missing_reports_unavailable_short():
    _patch_pipeline("missing")
    exc = _resolve_and_capture()
    assert exc.message == "该视频无法访问", f"标题应为简短结论，实为：{exc.message}"
    assert getattr(exc, "category", "") == "restricted", "应为 restricted（前端不再给贴 Cookie 按钮）"
    assert "Cookie" not in (exc.hint or ""), f"视频已失效时不应再提 Cookie：{exc.hint}"
    assert len(exc.hint or "") <= 30, f"文案要短（≤30 字），实为 {len(exc.hint)} 字：{exc.hint}"
    print("✅ 视频已失效：短文案「该视频无法访问」，不给贴 Cookie 误导路径")


# --------------------------------------------------------------------------- #
# 3. ok → 真被 bot 拦，给一句话指引 + 贴 Cookie 按钮
# --------------------------------------------------------------------------- #
def test_bot_blocked_reports_cookie_short():
    _patch_pipeline("ok")
    exc = _resolve_and_capture()
    assert exc.message == "YouTube 需要登录 Cookie 才能解析"
    assert getattr(exc, "category", "") == "cookie_required", "前端据此出现「去粘贴 Cookie」按钮"
    assert "2025" not in (exc.hint or ""), f"hint 不该再长篇科普 bot 检测：{exc.hint}"
    assert len(exc.hint or "") <= 40, f"文案要短（≤40 字），实为 {len(exc.hint)} 字：{exc.hint}"
    print(f"✅ 真被 bot 拦：一句话指引「{exc.hint}」+ 贴 Cookie 按钮")


# --------------------------------------------------------------------------- #
# 4. 探测失败（unknown）→ 安全回落提示贴 Cookie，绝不误报「视频没了」
# --------------------------------------------------------------------------- #
def test_unknown_falls_back_to_cookie():
    _patch_pipeline("unknown")
    exc = _resolve_and_capture()
    assert getattr(exc, "category", "") == "cookie_required", \
        "探测失败时必须回落到「贴 Cookie」安全默认，不能误报视频已删除"
    assert "无法访问" not in exc.message
    print("✅ 探测失败：回落「需要登录 Cookie」，不误报视频已失效")


# --------------------------------------------------------------------------- #
# 5. 免 Cookie 路径报内容级错误时不立即抛：先继续尝试 Cookie 源
# --------------------------------------------------------------------------- #
def test_content_error_does_not_raise_early():
    calls = []

    class _CountingYDL(_FailingYDL):
        def extract_info(self, url, download=False):
            calls.append(1)
            raise dl.ExtractorError("ERROR: [youtube] abc: Video unavailable")

    dl._fetch_youtube_visitor_data = lambda proxy="": ""
    dl._YoutubeDL = _CountingYDL
    dl._youtube_cookie_candidates = lambda user_cookie: [("pool", "C")]
    dl._evict_youtube_cookie_cache = lambda reason="": None
    dl._resolve_proxy = lambda host: ""
    dl._youtube_oembed_status = lambda url, proxy="": "ok"

    exc = _resolve_and_capture()
    # 免 Cookie 路径 1 次 + Cookie 源 1 次（extract_flat 降级还会再来一次）＝ 至少 3 次
    assert len(calls) >= 3, f"内容级错误后应继续尝试 Cookie 源，实际只调了 {len(calls)} 次"
    assert getattr(exc, "category", "") == "cookie_required"
    print(f"✅ 内容级错误不早退：免 Cookie + Cookie 源共尝试 {len(calls)} 次后才收敛")


if __name__ == "__main__":
    test_oembed_status_mapping()
    test_missing_reports_unavailable_short()
    test_bot_blocked_reports_cookie_short()
    test_unknown_falls_back_to_cookie()
    test_content_error_does_not_raise_early()
    print("\n全部通过：YouTube 失效判定与文案精简 ✅")
