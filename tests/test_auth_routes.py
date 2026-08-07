"""API Token 鉴权路由测试（可选开关 VDL_API_TOKEN）。

不依赖任何外部算力：仅验证中间件对 /api/* 的拦截逻辑。
运行：PYTHONPATH=server:tests <venv>/python -m pytest tests/test_auth_routes.py -q
"""

import pytest
from fastapi.testclient import TestClient

import app as m

client = TestClient(m.app)


@pytest.fixture(autouse=True)
def _enable_auth():
    # 强制开启鉴权，避免依赖导入顺序；结束复位，避免污染其它测试文件。
    m.API_TOKEN = "test-secret-token"
    m.AUTH_REQUIRED = True
    yield
    m.API_TOKEN = ""
    m.AUTH_REQUIRED = False


def test_nodes_is_public_and_reports_auth():
    r = client.get("/api/nodes")
    assert r.status_code == 200
    assert r.json()["authRequired"] is True


def test_unauthenticated_request_rejected():
    r = client.get("/api/platforms")
    assert r.status_code == 401
    assert "Token" in (r.json().get("error") or "")


def test_bearer_token_accepted():
    r = client.get("/api/platforms", headers={"Authorization": "Bearer test-secret-token"})
    assert r.status_code == 200


def test_x_api_key_accepted():
    r = client.get("/api/platforms", headers={"X-Api-Key": "test-secret-token"})
    assert r.status_code == 200


def test_wrong_token_rejected():
    r = client.get("/api/platforms", headers={"X-Api-Key": "wrong"})
    assert r.status_code == 401


def test_root_static_exempt():
    # 静态资源（SPA 入口）不应被鉴权拦截，否则前端都加载不了
    r = client.get("/")
    assert r.status_code != 401


def test_no_auth_when_token_unset():
    m.API_TOKEN = ""
    m.AUTH_REQUIRED = False
    try:
        r = client.get("/api/platforms")
        assert r.status_code == 200
    finally:
        m.API_TOKEN = "test-secret-token"
        m.AUTH_REQUIRED = True
