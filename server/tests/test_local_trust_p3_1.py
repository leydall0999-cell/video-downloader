"""「本机直连」判定不可伪造 回归守卫（安全审计 2026-10-11，P3-1）

发现的链路（验证轮新发现，不在初版审计里）：
  uvicorn 默认开启 `--proxy-headers`（Procfile 里没有关掉），此时 `scope["client"]`
  已被 `ProxyHeadersMiddleware` 用 `X-Forwarded-For` **重写**，不再是 socket 对端。
  uvicorn 0.52 的取法是「从右往左取第一个**不受信**的 host；全部受信则回退最左」
  （`_TrustedHosts.get_trusted_client_address`，受信集默认 `{"127.0.0.1"}`）。
  于是链路里只要存在环回跳（CF 命名隧道下 cloudflared 与 nginx 同机，nginx 的
  `$remote_addr` 恰为 127.0.0.1），客户端自填 `X-Forwarded-For: 127.0.0.1` 就能让
  推导结果落在受信集内 → `request.client.host` 变成 127.0.0.1 → 被当成「本机」。

  受影响的「本机免鉴权」分支（都是高危）：
    · `routers/core.py::_is_local_request` → `_require_admin`（运维端点免密钥）
      与 `_require_local`
    · `routers/auth.py::_is_loopback` → dev 模式**回传密码重置验证码**（账号接管）、
      `ensure_superusers(allow_bootstrap=...)`（公网首用户自动提权）、账号存在性提示
    · `app.cookie_sync_from_local`（读本机浏览器登录态并上报公共池）
    · `routers/system.py::_is_local_direct`（改全局网络代理配置）

  修复：全仓统一走 `app._is_local_direct` —— 要求 socket 对端环回 **且未携带任何代理头**。
  桌面 App 的 WebView 直连 127.0.0.1、不带代理头，故不影响桌面体验。

设计约束：纯离线，用最小请求替身，不联网。
"""
import sys
import types
from pathlib import Path

_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from routers import auth as auth_router  # noqa: E402
from routers import core as core_router  # noqa: E402
from routers import system as system_router  # noqa: E402


def _req(host="127.0.0.1", headers=None):
    return types.SimpleNamespace(headers=dict(headers or {}),
                                 client=types.SimpleNamespace(host=host),
                                 query_params={},
                                 url=types.SimpleNamespace(hostname=host),
                                 base_url="http://127.0.0.1:8321/")


# ---------------------------------------------------------------- 代理头识别

def test_every_proxy_header_marks_request_as_proxied():
    for name in server_app._PROXY_HEADER_NAMES:
        assert server_app._has_proxy_headers(_req(headers={name: "x"})) is True, name
        # 空值不算（某些反代会传空串）
        assert server_app._has_proxy_headers(_req(headers={name: "  "})) is False, name


def test_no_headers_means_not_proxied():
    assert server_app._has_proxy_headers(_req()) is False


def test_headers_unavailable_is_fail_closed():
    """取不到 headers 时按「经过代理」从严处理。"""
    bad = types.SimpleNamespace()   # 既无 headers 也无 client
    assert server_app._has_proxy_headers(bad) is True
    assert server_app._is_local_direct(bad) is False


# ---------------------------------------------------------------- 真源判定

def test_local_direct_requires_loopback_and_no_proxy_headers():
    # 桌面 App 直连：环回 + 无代理头 → 本机
    assert server_app._is_local_direct(_req(host="127.0.0.1")) is True
    assert server_app._is_local_direct(_req(host="::1")) is True
    assert server_app._is_local_direct(_req(host="localhost")) is True
    # 公网来源 → 不是本机
    assert server_app._is_local_direct(_req(host="203.0.113.7")) is False
    # 环回 + 任意代理头 → 不是本机（伪造入口）
    for name in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip",
                 "forwarded", "true-client-ip", "x-forwarded-proto"):
        assert server_app._is_local_direct(_req(headers={name: "127.0.0.1"})) is False, name


def test_client_none_is_not_local():
    req = types.SimpleNamespace(headers={}, client=None)
    assert server_app._is_local_direct(req) is False


# ---------------------------------------------------------------- 各调用点

def test_core_local_request_and_admin_gate():
    """`_require_admin` / `_require_local` 不得被伪造的 XFF 骗过。"""
    # 桌面直连：免密钥放行
    core_router._require_admin(_req())
    core_router._require_local(_req())
    # 伪造 XFF：不再视为本机 → 未配置密钥时 fail-closed（503）
    with_key = "s3cret"
    old = core_router._ops_admin_key
    core_router._ops_admin_key = lambda: with_key
    try:
        try:
            core_router._require_admin(_req(headers={"x-forwarded-for": "127.0.0.1"}))
            raise AssertionError("伪造 XFF 的请求不应免密钥通过 _require_admin")
        except server_app.HTTPException as e:
            assert e.status_code == 403, e.status_code
    finally:
        core_router._ops_admin_key = old
    try:
        core_router._require_local(_req(headers={"cf-connecting-ip": "127.0.0.1"}))
        raise AssertionError("伪造代理头的请求不应通过 _require_local")
    except server_app.HTTPException as e:
        assert e.status_code == 403, e.status_code


def test_auth_is_loopback_not_spoofable():
    """`_is_loopback` 下游含「回传重置验证码」「首用户自动提权」，必须不可伪造。"""
    assert auth_router._is_loopback(_req()) is True
    for name in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip"):
        assert auth_router._is_loopback(_req(headers={name: "127.0.0.1"})) is False, name
    assert auth_router._is_loopback(_req(host="203.0.113.7")) is False


def test_system_proxy_settings_local_gate():
    assert system_router._is_local_direct(_req()) is True
    assert system_router._is_local_direct(_req(headers={"x-real-ip": "127.0.0.1"})) is False
    assert system_router._is_local_direct(_req(headers={"x-forwarded-for": "127.0.0.1"})) is False


def test_cookie_sync_from_local_rejects_spoofed_headers():
    """`from-local` 会去读**本机浏览器登录态**并上报公共池 —— 绝不能被骗过。"""
    try:
        server_app.cookie_sync_from_local({"domain": "example.com"},
                                         _req(headers={"x-forwarded-for": "127.0.0.1"}))
        raise AssertionError("伪造代理头的请求不应进入本机 Cookie 上报流程")
    except server_app.HTTPException as e:
        assert e.status_code == 403, e.status_code


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
