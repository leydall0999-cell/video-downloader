"""B 站 requests 补丁：Cookie / UA / 代理必须「按每次请求刷新」（2026-10-18）。

真实故障：补丁是全局单例，旧实现把**首次请求**的 cookie/proxy/ua 冻结在闭包里。
服务启动后第一次 B 站解析若没带 Cookie，之后每次请求都裸奔 ⇒ B 站对无 Cookie 的
HTML 一律 412（实测：同 IP 同 UA，无 Cookie 412 / 带池内 Cookie 200），于是永远走
yt-dlp 兜底，看起来像「cookie 池没用」。

覆盖：
  A. 第二次调用能把状态刷新（不再冻结首次参数）；
  B. 本次没 Cookie 时用公共池兜底；
  C. python-requests / curl 之类脚本 UA 会被换成 Chrome（B 站对脚本 UA 直接 412）。
全程离线：requests.get 被替换成假响应，不碰网络。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_SERVER = str(Path(__file__).resolve().parents[1])   # server/tests -> server
sys.path.insert(0, _SERVER)

import downloader  # noqa: E402


class _FakeResp:
    status_code = 200
    url = "https://www.bilibili.com/video/BV1GJ411x7h7/"
    headers = {"Content-Type": "text/html; charset=utf-8"}
    content = b"<html><title>ok</title></html>"
    text = "<html><title>ok</title></html>"

    def raise_for_status(self) -> None:
        return None


def _call_patch(mp, cookie_in_state: str = "", ua_in_state: str = ""):
    """调用已打补丁的 _download_webpage_handle，返回实际发出的请求头。"""
    from yt_dlp.extractor.bilibili import BiliBiliIE

    downloader._BILIBILI_PATCH_STATE.update(
        {"cookie": cookie_in_state, "proxy": "", "ua": ua_in_state})
    downloader._patch_bilibili_webpage_download(
        proxy="", cookie=cookie_in_state, ua=ua_in_state)

    seen: dict = {}

    def fake_get(url, headers=None, **kwargs):
        seen["url"] = url
        seen["headers"] = dict(headers or {})
        return _FakeResp()

    import requests
    mp.setattr(requests, "get", fake_get)
    inst = object.__new__(BiliBiliIE)
    BiliBiliIE._download_webpage_handle(
        inst, "https://www.bilibili.com/video/BV1GJ411x7h7", "BV1GJ411x7h7")
    return seen


def test_state_is_not_frozen(monkeypatch) -> None:
    """第二次调用必须覆盖第一次的 cookie/ua（旧实现会永远停在第一次）。"""
    seen1 = _call_patch(monkeypatch, cookie_in_state="SESSDATA=FIRST", ua_in_state="")
    seen2 = _call_patch(monkeypatch, cookie_in_state="SESSDATA=SECOND", ua_in_state="")
    assert seen1["headers"].get("Cookie") == "SESSDATA=FIRST"
    assert seen2["headers"].get("Cookie") == "SESSDATA=SECOND", "补丁冻结了首次请求的 Cookie"


def test_pool_cookie_used_when_missing(monkeypatch) -> None:
    """本次请求没 Cookie → 用公共池兜底，否则 B 站恒 412。"""
    import cookie_pool
    monkeypatch.setattr(cookie_pool, "get_cookie", lambda domain: "SESSDATA=FROM_POOL")
    seen = _call_patch(monkeypatch, cookie_in_state="", ua_in_state="")
    assert seen["headers"].get("Cookie") == "SESSDATA=FROM_POOL"


def test_script_ua_is_replaced(monkeypatch) -> None:
    """脚本类 UA 必须换成 Chrome，否则 412。"""
    for bad in ("python-requests/2.31.0", "curl/8.4.0", "Wget/1.21", ""):
        seen = _call_patch(monkeypatch, cookie_in_state="SESSDATA=x", ua_in_state=bad)
        ua = seen["headers"].get("User-Agent", "")
        assert "Mozilla/5.0" in ua and "Chrome/" in ua, "未替换脚本 UA: %r -> %r" % (bad, ua)


def test_browser_ua_preserved() -> None:
    chrome = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
    assert downloader._bili_patch_ua(chrome) == chrome
    assert "Chrome/" in downloader._bili_patch_ua("python-requests/2.31.0")


def test_patch_adds_trailing_slash(monkeypatch) -> None:
    """B 站对 /video/BVxxx（无尾斜杠）直接回 412，补丁必须先补斜杠再请求。

    实测：同 Cookie 同 UA，无斜杠 412、有斜杠 200，两次复现。
    """
    seen = _call_patch(monkeypatch, cookie_in_state="SESSDATA=x", ua_in_state="")
    assert seen["url"].endswith("/"), "补丁未补尾斜杠，会让 B 站直接 412: %r" % seen["url"]


def _run_all() -> int:
    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            old = getattr(obj, name)
            self._undo.append((obj, name, old))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo.clear()

    failed = 0
    for fn in (test_state_is_not_frozen, test_pool_cookie_used_when_missing,
               test_script_ua_is_replaced, test_browser_ua_preserved,
               test_patch_adds_trailing_slash):
        mp = _MP()
        try:
            if fn is test_browser_ua_preserved:
                fn()
            else:
                fn(mp)
            print("  ✓ " + fn.__name__)
        except AssertionError as e:
            failed += 1
            print("  ❌ %s: %s" % (fn.__name__, e))
        finally:
            mp.undo()
    return failed


if __name__ == "__main__":
    raise SystemExit(_run_all())
