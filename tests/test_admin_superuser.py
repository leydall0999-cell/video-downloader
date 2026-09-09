"""超级用户（is_admin）与后台管理面板鉴权测试。

覆盖 server/auth_store 的超级用户体系 + server/routers/admin 的 require_admin 守卫：

  1. 首个注册账号在无显式名单时自动成为超级用户（bootstrap）
  2. 环境变量 VDL_ADMIN_IDENTIFIER 名单可显式指定超管
  3. set_user_admin 可提权 / 降权
  4. 注册 / 登录 / me 三个接口均返回 is_admin
  5. /api/admin/* 仅超管可访问：普通用户 401、无 token 401、伪造 token 401
  6. 旧管理员口令端点（/api/admin/login、/api/admin/change-password）已移除
  7. system_config 返回 has_superuser，不再返回 admin_default_password_set

运行：PYTHONPATH=server:tests <venv>/python -m pytest tests/test_admin_superuser.py -q
"""
import os

import pytest
from fastapi.testclient import TestClient

import admin_store
import app as m
import auth_store

client = TestClient(m.app)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """把 auth_store / admin_store 的数据目录重定向到临时目录，避免污染真实数据。"""
    base = tmp_path / ".video-downloader"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(auth_store, "_base_dir", lambda: base)
    monkeypatch.setattr(admin_store, "_base_dir", lambda: base)
    monkeypatch.delenv("VDL_ADMIN_IDENTIFIER", raising=False)
    # ensure_superusers 带 60s 缓存，每个用例前清空避免串扰
    auth_store._SUPERUSER_CACHE["ts"] = 0.0
    yield
    auth_store._SUPERUSER_CACHE["ts"] = 0.0


def _register(identifier, password="secret123"):
    return client.post("/api/auth/register",
                       json={"identifier": identifier, "password": password})


# --------------------------------------------------------------------------- #
# 1. bootstrap：首个账号自动成为超管
# --------------------------------------------------------------------------- #
def test_first_registered_user_becomes_superuser():
    r1 = _register("owner@example.com")
    assert r1.status_code == 200 and r1.json()["ok"] is True
    assert r1.json()["is_admin"] is True

    r2 = _register("normal@example.com")
    assert r2.status_code == 200 and r2.json()["ok"] is True
    assert r2.json()["is_admin"] is False


def test_bootstrap_is_idempotent_after_admin_exists():
    r1 = _register("owner@example.com")
    owner_uid = r1.json()["user_id"]
    _register("normal@example.com")
    # 第二次 bootstrap 不应改变既有超管归属
    auth_store.ensure_superusers()
    assert auth_store.user_is_admin(owner_uid) is True
    admins = [u for u in auth_store._load_users()["users"] if u.get("is_admin")]
    assert len(admins) == 1


# --------------------------------------------------------------------------- #
# 2. 显式名单（环境变量）
# --------------------------------------------------------------------------- #
def test_env_identifier_promotes_listed_user(monkeypatch):
    _register("owner@example.com")
    r2 = _register("boss@example.com")
    boss_uid = r2.json()["user_id"]
    assert r2.json()["is_admin"] is False  # 名单尚未生效（首个账号已是超管）

    monkeypatch.setenv("VDL_ADMIN_IDENTIFIER", "boss@example.com")
    auth_store._SUPERUSER_CACHE["ts"] = 0.0
    auth_store.ensure_superusers()
    assert auth_store.user_is_admin(boss_uid) is True


def test_env_identifier_accepts_comma_separated(monkeypatch):
    _register("a@example.com")
    b = _register("b@example.com").json()["user_id"]
    c = _register("c@example.com").json()["user_id"]
    monkeypatch.setenv("VDL_ADMIN_IDENTIFIER", "b@example.com, c@example.com")
    auth_store._SUPERUSER_CACHE["ts"] = 0.0
    auth_store.ensure_superusers()
    assert auth_store.user_is_admin(b) is True
    assert auth_store.user_is_admin(c) is True


# --------------------------------------------------------------------------- #
# 3. set_user_admin 提权 / 降权
# --------------------------------------------------------------------------- #
def test_set_user_admin_promote_and_demote():
    owner = _register("owner@example.com").json()
    uid = _register("normal@example.com").json()["user_id"]

    r = client.post(f"/api/admin/users/{uid}/set-admin",
                    json={"is_admin": True},
                    headers={"Authorization": "Bearer " + owner["token"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["is_admin"] is True
    assert auth_store.user_is_admin(uid) is True

    r = client.post(f"/api/admin/users/{uid}/set-admin",
                    json={"is_admin": False},
                    headers={"Authorization": "Bearer " + owner["token"]})
    assert r.json()["ok"] is True and r.json()["is_admin"] is False
    assert auth_store.user_is_admin(uid) is False


def test_set_user_admin_unknown_user():
    owner = _register("owner@example.com").json()
    r = client.post("/api/admin/users/nope/set-admin",
                    json={"is_admin": True},
                    headers={"Authorization": "Bearer " + owner["token"]})
    assert r.json()["ok"] is False


def test_set_user_admin_requires_superuser():
    _register("owner@example.com")
    normal = _register("normal@example.com").json()
    other = _register("other@example.com").json()["user_id"]
    r = client.post(f"/api/admin/users/{other}/set-admin",
                    json={"is_admin": True},
                    headers={"Authorization": "Bearer " + normal["token"]})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# 4. 注册 / 登录 / me 返回 is_admin
# --------------------------------------------------------------------------- #
def test_login_and_me_return_is_admin():
    r = _register("owner@example.com", "pw123456")
    assert r.json()["is_admin"] is True

    r = client.post("/api/auth/login",
                    json={"identifier": "owner@example.com", "password": "pw123456"})
    assert r.status_code == 200 and r.json()["is_admin"] is True
    tok = r.json()["token"]

    r = client.get("/api/auth/me", headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200 and r.json()["is_admin"] is True


def test_me_reports_false_for_normal_user():
    _register("owner@example.com")
    n = _register("normal@example.com", "pw123456").json()
    r = client.get("/api/auth/me", headers={"Authorization": "Bearer " + n["token"]})
    assert r.json()["is_admin"] is False


# --------------------------------------------------------------------------- #
# 5. /api/admin/* 守卫
# --------------------------------------------------------------------------- #
ADMIN_ENDPOINTS_GET = ["/api/admin/users", "/api/admin/memberships",
                       "/api/admin/stats", "/api/admin/config"]


@pytest.mark.parametrize("path", ADMIN_ENDPOINTS_GET)
def test_admin_endpoints_open_to_superuser(path):
    owner = _register("owner@example.com").json()
    r = client.get(path, headers={"Authorization": "Bearer " + owner["token"]})
    assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.parametrize("path", ADMIN_ENDPOINTS_GET)
def test_admin_endpoints_reject_normal_user(path):
    _register("owner@example.com")
    normal = _register("normal@example.com").json()
    r = client.get(path, headers={"Authorization": "Bearer " + normal["token"]})
    assert r.status_code == 401


@pytest.mark.parametrize("path", ADMIN_ENDPOINTS_GET)
def test_admin_endpoints_reject_anonymous(path):
    _register("owner@example.com")
    r = client.get(path)
    assert r.status_code == 401


def test_admin_endpoints_reject_forged_token():
    _register("owner@example.com")
    r = client.get("/api/admin/users", headers={"Authorization": "Bearer forged.token.sig"})
    assert r.status_code == 401


def test_admin_user_list_includes_is_admin():
    owner = _register("owner@example.com").json()
    _register("normal@example.com")
    r = client.get("/api/admin/users",
                   headers={"Authorization": "Bearer " + owner["token"]})
    users = r.json()["users"]
    assert len(users) == 2
    assert all("is_admin" in u for u in users)
    owner_rec = next(u for u in users if u["identifier"] == "owner@example.com")
    assert owner_rec["is_admin"] is True


# --------------------------------------------------------------------------- #
# 6. 旧口令端点已移除
# --------------------------------------------------------------------------- #
def test_legacy_admin_login_endpoint_removed():
    r = client.post("/api/admin/login", json={"password": "admin123"})
    assert r.status_code != 200


def test_legacy_admin_change_password_endpoint_removed():
    owner = _register("owner@example.com").json()
    r = client.post("/api/admin/change-password",
                    json={"old_password": "admin123", "new_password": "admin456"},
                    headers={"Authorization": "Bearer " + owner["token"]})
    assert r.status_code != 200


# --------------------------------------------------------------------------- #
# 7. system_config
# --------------------------------------------------------------------------- #
def test_system_config_reports_has_superuser():
    owner = _register("owner@example.com").json()
    r = client.get("/api/admin/config",
                   headers={"Authorization": "Bearer " + owner["token"]})
    cfg = r.json()
    assert cfg["has_superuser"] is True
    assert "admin_default_password_set" not in cfg
    assert "plans" in cfg and "smtp" in cfg


def test_system_config_has_superuser_false_when_no_admin():
    """无 is_admin 账号时（绕过 bootstrap）应返回 False。"""
    _register("owner@example.com")
    for u in auth_store._load_users()["users"]:
        auth_store.set_user_admin(u["user_id"], False)
    cfg = admin_store.system_config()
    assert cfg["has_superuser"] is False
