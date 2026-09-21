"""账号制客户端/路由单测（2026-09-22）。

最关键的一条：**同一笔云端购买不能被重复落户**。登录每次都会拉一次 purchases，
若不做幂等，用户每登一次录会员就顺延一年 —— 这类错误只在重复操作后才暴露，
必须钉死。
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from membership import MembershipStore  # noqa: E402

FP_A = "a" * 32


@pytest.fixture()
def store(tmp_path):
    return MembershipStore(path=tmp_path / "membership.json")


def _fake_device_id(monkeypatch, fp=FP_A):
    mod = types.ModuleType("device_id")
    mod.fingerprint = lambda: (fp, True)
    monkeypatch.setitem(sys.modules, "device_id", mod)


def test_apply_cloud_purchases_is_idempotent(store):
    purchases = [{"id": "p1", "plan_code": "download_year"}]
    r1 = store.apply_cloud_purchases(purchases)
    assert r1["ok"] and r1["applied"] == ["download_year"]
    first_expire = store.status()["download_member"]["expire_at"]
    assert first_expire > time.time()

    # 第二次登录（同样拉到这一笔购买）不应再顺延
    r2 = store.apply_cloud_purchases(purchases)
    assert r2["applied"] == []
    assert store.status()["download_member"]["expire_at"] == first_expire


def test_new_purchase_extends(store):
    store.apply_cloud_purchases([{"id": "p1", "plan_code": "download_month"}])
    first = store.status()["download_member"]["expire_at"]
    store.apply_cloud_purchases([{"id": "p1", "plan_code": "download_month"},
                                 {"id": "p2", "plan_code": "download_month"}])
    assert store.status()["download_member"]["expire_at"] > first + 25 * 86400


def test_unknown_plan_code_reported(store):
    r = store.apply_cloud_purchases([{"id": "bad", "plan_code": "not_a_plan"}])
    assert r["ok"] is False and r["errors"]
    assert store._state["meta"]["account"]["purchases_applied"] == []


def test_logout_keeps_purchased_benefits(store):
    store.apply_cloud_purchases([{"id": "p1", "plan_code": "download_year"}])
    store.clear_account()
    view = store.account_view()
    assert view["logged_in"] is False
    assert store.status()["download_member"]["active"] is True


def test_evicted_does_not_delete_benefits(store):
    store.apply_cloud_purchases([{"id": "p1", "plan_code": "download_year"}])
    store.set_evicted(True)
    assert store.status()["download_member"]["active"] is False
    store.set_evicted(False)
    assert store.status()["download_member"]["active"] is True


# ── 路由层 ────────────────────────────────────────────────────────────────── #
def test_cloud_login_applies_and_reports(monkeypatch, store):
    import app as app_mod
    from routers import cloud_account

    monkeypatch.setattr(app_mod, "member_store", store, raising=False)
    _fake_device_id(monkeypatch)

    seen = {}

    def fake_login(email, password, fp, name, base_url="", timeout=0, opener=None):
        seen["args"] = (email, password, fp, name)
        return {"ok": True, "token": "tok-1",
                "account": {"email": email, "max_devices": 2,
                            "devices": [{"fp": fp, "name": name, "last_seen": 0.0}],
                            "purchases": [{"id": "p9", "plan_code": "download_month"}]}}

    fake_client = types.ModuleType("license_client")
    fake_client.login_remote = fake_login
    fake_client.LicenseCloudError = RuntimeError
    monkeypatch.setitem(sys.modules, "license_client", fake_client)

    out = cloud_account.cloud_login({"email": "U@Example.com", "password": "pw123456"})
    assert out["ok"] and out["account"]["logged_in"] is True
    assert out["account"]["email"] == "u@example.com"     # 主键归一
    assert seen["args"][0] == "U@Example.com"
    assert store.status()["download_member"]["active"] is True


def test_cloud_sync_marks_evicted(monkeypatch, store):
    import app as app_mod
    from routers import cloud_account

    monkeypatch.setattr(app_mod, "member_store", store, raising=False)
    _fake_device_id(monkeypatch)
    store.save_account("u@x.com", "tok")

    fake_client = types.ModuleType("license_client")
    fake_client.heartbeat_remote = lambda *a, **k: {
        "ok": False, "code": "DEVICE_EVICTED", "error": "被挤出"}
    fake_client.LicenseCloudError = RuntimeError
    monkeypatch.setitem(sys.modules, "license_client", fake_client)

    out = cloud_account.cloud_sync()
    assert out["evicted"] is True
    assert store.status()["device_locked"] == "DEVICE_EVICTED"


def test_offline_sync_keeps_benefits(monkeypatch, store):
    """断网不得惩罚已付费用户：动也不动本地权益。"""
    import app as app_mod
    from routers import cloud_account

    monkeypatch.setattr(app_mod, "member_store", store, raising=False)
    _fake_device_id(monkeypatch)
    store.save_account("u@x.com", "tok")
    store.apply_cloud_purchases([{"id": "p1", "plan_code": "download_year"}])

    class Boom(RuntimeError):
        pass

    fake_client = types.ModuleType("license_client")

    def boom(*a, **k):
        raise Boom("无法连接授权中心")
    fake_client.heartbeat_remote = boom
    fake_client.LicenseCloudError = Boom
    monkeypatch.setitem(sys.modules, "license_client", fake_client)

    out = cloud_account.cloud_sync()
    assert out["ok"] is True and out.get("offline") is True
    assert store.status()["download_member"]["active"] is True
    assert "device_locked" not in store.status()
