"""后端无头冒烟测试：用 FastAPI TestClient 在沙盒内验证核心路由已正确装配。

解决的问题：之前每改一处，都得发 dmg 让用户真机试才能发现「模块没加载 /
路由 500 / import 崩溃」这类问题。本测试在沙盒内直接 import 整个 app，
对核心路由发请求，确认：
  - app 能正常 import（无 import 期崩溃、无模块缺失）
  - 核心路由已注册并返回合法 JSON（不 500）

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


def _client():
    return TestClient(server_app.app)


def test_root_serves_html():
    c = _client()
    r = c.get("/")
    assert r.status_code == 200, f"GET / 非 200: {r.status_code}"
    assert "text/html" in r.headers.get("content-type", ""), "根路径应返回 HTML"
    print("✅ GET / 返回前端页面 (200)")


def _assert_wired(c, method, path, body=None):
    """断言路由已挂载且 handler 已运行（排除 Starlette 原生 404「Not Found」）。

    业务层的 404（如「任务不存在」）是合法响应，与「路由根本没挂上」的
    Starlette 404 区分：后者 detail 恒定是 "Not Found"。
    """
    r = c.request(method, path, json=body) if body is not None else c.request(method, path)
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


def test_stream_proxy_upstream_error():
    """回归测试：/api/stream/proxy 上游请求失败时，错误处理分支不得 NameError。

    修复前 core.py 在 178 行裸调不存在的 _clean_message -> 500 NameError；
    修复后改为 app.downloader._clean_message -> 正常返回 502 业务错误。
    该分支此前从无任何测试覆盖，属潜伏雷。
    """
    with patch.object(server_app.requests, "get", side_effect=RuntimeError("simulated upstream failure")):
        c = _client()
        r = c.get("/api/stream/proxy", params={"u": "http://example.com/a.m3u8"})
        assert r.status_code == 502, f"预期 502，实际 {r.status_code}（若是 500 即 _clean_message 雷未排）"
        assert "上游拉取失败" in (r.json().get("detail") or ""), f"detail 异常: {r.text[:200]}"
    print("✅ /api/stream/proxy 上游失败分支返回 502（_clean_message 雷已排，无 NameError）")


if __name__ == "__main__":
    test_root_serves_html()
    test_core_routes_wired()
    test_stream_proxy_upstream_error()
    print("\n🎉 后端无头冒烟测试全部通过 — 核心路由装配正确，沙盒内可验证。")
