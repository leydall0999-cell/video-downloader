"""桌面端：浏览器/缓存里已有百度登录态时，**自动**加入共享池（2026-10-25 用户定档）。

用户要求：百度 cookies 主要由管理员提供、用户自己填的入口隐藏；但用户浏览器里
本来就有百度登录态的，我们自动取到后自动进共享池（网页版与别人也能用上）。

钉住的契约：
  1. 手动保存的那份仍然优先（用户显式指定的账号盖过自动抓到的）；
  2. 从缓存命中 ⇒ 自动入本地池 + 推云端，两步都做；
  3. 从浏览器解密命中 ⇒ 同上；
  4. 自动贡献失败**不能**抛（不能因为共享池挂了影响本次下载）；
  5. 共享池兜底仍然排最后（自己的账号永远优先于别人的）。

全程离线：cookie_cache / cookie_pool / app 的云端推送都被替换成假实现。
运行：cd server && python tests/test_cloud_drive_auto_contrib.py
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest import mock

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_auto_contrib")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

import routers.cloud_drive as rt  # noqa: E402


def _fake_cache(peek: str = "", browser: str = ""):
    return types.SimpleNamespace(
        peek_cached_cookie_header=lambda host: peek if host == "pan.baidu.com" else "",
        get_cached_cookie_header=lambda host: browser if host == "pan.baidu.com" else "",
    )


def test_manual_bduss_wins_and_is_not_auto_contributed():
    """手动保存的那份优先，且**不**自动入池（它是显式指定的账号）。"""
    added: list = []
    pushed: list = []
    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache(peek="BDUSS=from-cache")}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value="BDUSS=manual"), \
         mock.patch("cookie_pool.add_cookie",
                    side_effect=lambda d, h, source="sync": added.append((d, h, source)) or True), \
         mock.patch("app._cloud_sync_config", return_value=("http://cloud", "tok")), \
         mock.patch("app._push_cookie_to_cloud",
                    side_effect=lambda d, h, u, t: pushed.append((d, h)) or {"ok": True}):
        got = rt._baidu_login_cookie()
    assert got == "BDUSS=manual", got
    assert not added, f"手动那份不应自动入池：{added}"
    assert not pushed, f"手动那份不应自动推云端：{pushed}"
    print("✅ 手动保存的登录态仍优先，且不会被自动共享")


def test_cache_hit_auto_contributes_to_pool_and_cloud():
    added: list = []
    pushed: list = []
    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache(peek="BDUSS=from-cache")}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_pool.add_cookie",
                    side_effect=lambda d, h, source="sync": added.append((d, h, source)) or True), \
         mock.patch("app._cloud_sync_config", return_value=("http://cloud", "tok")), \
         mock.patch("app._push_cookie_to_cloud",
                    side_effect=lambda d, h, u, t: pushed.append((d, h)) or {"ok": True}):
        got = rt._baidu_login_cookie()
    assert got == "BDUSS=from-cache", got
    assert added, "缓存里取到的登录态应自动入本地池"
    assert added[0][0] == "baidu.com", added
    assert pushed, "自动入池的同时应推云端（网页版与别人才能用上）"
    assert pushed[0][0] == "baidu.com", pushed
    print("✅ 缓存里的百度登录态会自动入本地池并推云端")


def test_browser_hit_auto_contributes():
    added: list = []
    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache(browser="BDUSS=from-browser")}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_pool.add_cookie",
                    side_effect=lambda d, h, source="sync": added.append((d, h, source)) or True), \
         mock.patch("app._cloud_sync_config", return_value=("", "")), \
         mock.patch("app._push_cookie_to_cloud", return_value={"ok": True}):
        got = rt._baidu_login_cookie()
    assert got == "BDUSS=from-browser", got
    assert added and added[0][0] == "baidu.com", added
    print("✅ 浏览器里解出来的登录态也会自动入池")


def test_auto_contrib_failure_does_not_break_download():
    """共享池挂了不能影响本次下载（自动贡献必须吞掉所有异常）。"""
    def _boom(*_a, **_k):
        raise RuntimeError("pool on fire")

    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache(peek="BDUSS=from-cache")}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_pool.add_cookie", side_effect=_boom), \
         mock.patch("app._cloud_sync_config", side_effect=_boom), \
         mock.patch("app._push_cookie_to_cloud", side_effect=_boom):
        got = rt._baidu_login_cookie()
    assert got == "BDUSS=from-cache", got
    print("✅ 共享池故障不影响本次取用登录态")


def test_pool_fallback_is_last():
    """共享池兜底排最后：自己的账号优先于别人的。"""
    with mock.patch.dict(sys.modules, {"cookie_cache": _fake_cache()}), \
         mock.patch.object(rt, "get_baidu_bduss", return_value=""), \
         mock.patch("cookie_pool.get_cookie", return_value="BDUSS=from-pool"):
        got = rt._baidu_login_cookie()
    assert got == "BDUSS=from-pool", got
    print("✅ 共享池兜底仍然排最后")


if __name__ == "__main__":
    for fn in (
        test_manual_bduss_wins_and_is_not_auto_contributed,
        test_cache_hit_auto_contributes_to_pool_and_cloud,
        test_browser_hit_auto_contributes,
        test_auto_contrib_failure_does_not_break_download,
        test_pool_fallback_is_last,
    ):
        fn()
    print("\n🎉 百度登录态自动共享池测试全部通过")
