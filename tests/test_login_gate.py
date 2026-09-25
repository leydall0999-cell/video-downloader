"""全功能登录门禁测试（2026-09-26 用户要求「所有功能必须登录才能使用」）。

契约（server/app.py `_login_gate` + web/app.js `_LOGIN_GATED_ACTIONS`）：
  1. 未登录调用能力型 POST 入口 → 401 + code=NO_AUTH
  2. 已登录 → 放行（交回正常路由，不再被门禁拦）
  3. GET 状态查询 / 文件读取、以及登录、账号、会员、管理、支付等公开前缀 → 不拦

不依赖任何外部算力：门禁在路由之前生效，命中即 401；放行分支只断言「不是 401」，
不假设具体路由实现（测试环境用 fake 模块屏蔽真实解码/模型）。

运行：cd <repo> && .build_venv/bin/python -m pytest tests/test_login_gate.py -q
"""

import pytest
from fastapi.testclient import TestClient

import app as m

client = TestClient(m.app)


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    """本文件默认打开门禁（conftest 为其它测试整体关掉了它）。"""
    monkeypatch.setattr(m, "LOGIN_GATE_ENABLED", True)
    yield


# --------------------------------------------------------------------------- #
# 1) 未登录：能力型入口一律 401 + NO_AUTH
# --------------------------------------------------------------------------- #
GATED_POSTS = [
    ("/api/resolve", {"url": "https://example.com/v"}),
    ("/api/download", {}),
    ("/api/batch", {}),
    ("/api/convert", {}),
    ("/api/convert/local", {}),
    ("/api/upload-convert", {}),
    ("/api/upload-chunk", {}),
    ("/api/concat", {}),
    ("/api/concat/local", {}),
    ("/api/compress/local", {}),
    ("/api/sr/local", {}),
    ("/api/sr/video/local", {}),
    ("/api/matting/image", {}),
    ("/api/dw/image", {}),
    ("/api/dw/pdf", {}),
    ("/api/dw/video", {}),
    ("/api/subtitle/extract", {}),
    ("/api/subtitles/extract", {}),
    ("/api/subtitles/burn", {}),
    ("/api/subtitles/translate", {}),
    ("/api/commentary", {}),
    ("/api/commentary/script-only", {}),
    ("/api/commentary/render/j1", {}),
    ("/api/subscriptions", {}),
    ("/api/process/run", {}),
    ("/api/retention/run", {}),
    ("/api/torrents/add", {}),
    ("/api/share/upload_path", {}),
]


@pytest.mark.parametrize("path,payload", GATED_POSTS)
def test_gated_post_requires_login(path, payload):
    r = client.post(path, json=payload)
    assert r.status_code == 401, f"{path} 未被登录门禁拦截（实际 {r.status_code}）"
    assert r.json().get("code") == "NO_AUTH", path


def test_get_requests_not_gated():
    """状态查询 / 文件读取类 GET 不能被门禁拦（否则页面初始化就崩）。"""
    r = client.get("/api/system/info")
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# 2) 已登录：放行
# --------------------------------------------------------------------------- #
def test_logged_in_is_allowed(monkeypatch):
    monkeypatch.setattr("user_membership.get_current_user_id", lambda request: "u-test")
    # 空 body 会走参数校验（422），关键是**不能再是 401**
    r = client.post("/api/convert/local", json={})
    assert r.status_code != 401, "已登录仍被登录门禁拦截"


# --------------------------------------------------------------------------- #
# 3) 名单本身：不能误伤账号 / 会员 / 管理 / 支付 / 只读接口
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    "/api/resolve",
    "/api/download",
    "/api/batch",
    "/api/convert",
    "/api/convert/local",
    "/api/upload-convert",
    "/api/upload-chunk/finish",
    "/api/concat",
    "/api/compress/local",
    "/api/sr/local",
    "/api/matting/image",
    "/api/dw/image",
    "/api/dw/video",
    "/api/subtitle/extract",
    "/api/subtitles/burn",
    "/api/commentary/script-only",
    "/api/commentary/render/abc",
    "/api/subscriptions",
    "/api/process/run",
    "/api/retention/run",
    "/api/torrents/add",
    "/api/share/upload_path",
])
def test_path_is_gated(path):
    assert m._login_gated_path(path), path


@pytest.mark.parametrize("path", [
    "/api/system/info",
    "/api/member/status",
    "/api/auth/login",
    "/api/auth/register",
    "/api/admin/stats",
    "/api/cloud/pay/create",
    "/api/commentary/list",
    "/api/commentary/config",
    "/api/commentary/voice-library",
    "/api/dw/video/j1/cancel",       # 取消在跑的任务，不该因未登录而卡死
    "/api/library",
    "/ops-board",
    "/",
])
def test_path_not_gated(path):
    assert not m._login_gated_path(path), f"{path} 不该被登录门禁拦截"
