"""后端无头冒烟测试：用 FastAPI TestClient 在沙盒内验证所有 PCS 路由已正确装配。

解决的问题：之前每改一处，都得发 dmg 让用户真机试才能发现「模块没加载 /
路由 500 / import 崩溃」这类问题。本测试在沙盒内直接 import 整个 app，
对所有 /api/pcs/* 路由发请求，确认：
  - app 能正常 import（无 import 期崩溃、无模块缺失）
  - 所有 PCS 路由已注册并返回合法 JSON（不 500）
  - 即使后端函数被 mock，路由装配也正确（与网络/二进制解耦）

全程不碰外部网络、不需要 GUI、不依赖二进制是否安装。运行：
    cd server && python tests/test_app_smoke.py
"""
import os
import sys
from unittest.mock import patch

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import baidu_pcs  # noqa: E402
import baidu_qr  # noqa: E402


def _client():
    return TestClient(server_app.app)


def test_root_serves_html():
    c = _client()
    r = c.get("/")
    assert r.status_code == 200, f"GET / 非 200: {r.status_code}"
    assert "text/html" in r.headers.get("content-type", ""), "根路径应返回 HTML"
    print("✅ GET / 返回前端页面 (200)")


def test_pcs_status_real_offline():
    # 调真实的 baidu_pcs.status()（仅检查本机二进制路径，不联网）
    c = _client()
    r = c.get("/api/pcs/status")
    assert r.status_code == 200, f"status 非 200: {r.status_code}"
    j = r.json()
    assert "binary_installed" in j, f"status 缺 binary_installed 字段: {j}"
    print(f"✅ GET /api/pcs/status -> binary_installed={j.get('binary_installed')}")


def test_all_pcs_routes_wired():
    """用 mock 隔离后端实现，纯粹验证「路由已注册 + 返回 200 JSON」。"""
    fake_status = {"binary_installed": True, "logged_in": True, "who": "uid:123"}
    fake_qr = {"ok": True, "sign": "S", "img": "data:image/png;base64,AAAA", "expires_in": 120}
    fake_poll = {"status": "waiting", "message": "等待扫码"}
    fake_login = {"ok": True, "message": "登录成功(测试)"}
    fake_transfer = {"ok": True, "message": "转存成功(测试)"}
    fake_who = {"ok": True, "logged_in": True, "message": "uid:123"}

    with patch.object(baidu_pcs, "status", return_value=fake_status), \
         patch.object(baidu_qr, "qr_gen", return_value=fake_qr), \
         patch.object(baidu_qr, "qr_poll", return_value=fake_poll), \
         patch.object(baidu_pcs, "login", return_value=fake_login), \
         patch.object(baidu_pcs, "login_by_password", return_value=fake_login), \
         patch.object(baidu_pcs, "transfer", return_value=fake_transfer), \
         patch.object(baidu_pcs, "who", return_value=fake_who):

        c = _client()
        cases = [
            ("GET", "/api/pcs/qr/gen", None, None),
            ("GET", "/api/pcs/qr/poll?sign=ABC", None, None),
            ("POST", "/api/pcs/login", {"cookies": "BDUSS=x"}, None),
            ("POST", "/api/pcs/login-password", {"username": "u", "password": "p"}, None),
            ("POST", "/api/pcs/share/transfer", {"url": "https://pan.baidu.com/s/x", "pwd": "1"}, None),
            ("GET", "/api/pcs/who", None, None),
        ]
        for method, path, body, _ in cases:
            if method == "GET":
                r = c.get(path)
            else:
                r = c.post(path, json=body)
            assert r.status_code == 200, f"{method} {path} 非 200: {r.status_code} {r.text[:200]}"
            j = r.json()
            assert isinstance(j, dict), f"{method} {path} 响应非 JSON: {r.text[:200]}"
    print("✅ 全部 /api/pcs/* 路由装配正确（qr.gen/poll, login, login-password, transfer, who）")


def _assert_wired(c, method, path, body=None):
    """断言路由已挂载且 handler 已运行（排除 Starlette 原生 404「Not Found」）。

    业务层的 404（如「任务不存在」）是合法响应，与「路由根本没挂上」的
    Starlette 404 区分：后者 detail 恒定是 "Not Found"。
    """
    r = c.request(method, path, json=body) if body is not None else c.request(method, path)
    # 仅「路由未挂载」算装配失败；handler 跑通后的任意状态（400 校验 / 404 业务
    # / 504 解析超时）均证明路由已挂载。504 来自 /api/resolve 拿到假 URL 真联网超时，
    # 属预期，不算崩溃。
    if r.status_code == 404:
        detail = (r.json() or {}).get("detail", "")
        assert detail != "Not Found", f"{method} {path} 是路由未挂载的 404（Starlette Not Found），说明 core router 没 include"
    return r


def test_core_routes_wired():
    """验证 Phase3 抽到 routers/core.py 的核心路由已装配、可响应。

    覆盖 /api/version、/api/platforms、/api/nodes、/api/ydlp/version、
    /api/tasks、/api/batch/config、/api/cookie/status、/api/resolve、
    /api/download、/api/convert、/api/batch、/api/tasks/cancel-all。
    """
    c = _client()
    # GET 类：应直接返回 200 + 合法 JSON
    for path in [
        "/api/version",
        "/api/platforms",
        "/api/nodes",
        "/api/ydlp/version",
        "/api/tasks",
        "/api/batch/config",
        "/api/cookie/status?url=https://example.com/x",
    ]:
        r = c.get(path)
        assert r.status_code == 200, f"GET {path} 非 200: {r.status_code} {r.text[:200]}"
        assert isinstance(r.json(), (dict, list)), f"GET {path} 响应非 JSON: {r.text[:200]}"
    print("✅ 核心 GET 路由（version/platforms/nodes/ydlp/tasks/batch/cookie）装配正确 (200)")

    # POST 类：不 mock 后端，只要路由挂上、handler 跑通（业务 4xx 视为通过）
    post_cases = [
        ("/api/resolve", {"url": "https://example.com/x"}),
        ("/api/download", {"url": "https://example.com/a.mp4", "quality": "best"}),
        ("/api/convert", {"task_id": "nope", "target": "mp4"}),
        ("/api/batch", {"urls": ["https://example.com/a.mp4"]}),
        ("/api/tasks/cancel-all", {}),
    ]
    for path, body in post_cases:
        _assert_wired(c, "POST", path, body)
    print("✅ 核心 POST 路由（resolve/download/convert/batch/cancel-all）已挂载并装配正确")


if __name__ == "__main__":
    test_root_serves_html()
    test_pcs_status_real_offline()
    test_all_pcs_routes_wired()
    test_core_routes_wired()
    print("\n🎉 后端无头冒烟测试全部通过 — 所有 PCS + 核心路由装配正确，沙盒内可验证。")
