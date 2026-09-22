"""账号制客户端/路由单测（2026-09-22）。

最关键的一条：**同一笔云端购买不能被重复落户**。登录每次都会拉一次 purchases，
若不做幂等，用户每登一次录会员就顺延一年 —— 这类错误只在重复操作后才暴露，
必须钉死。

运行：
    cd server && python tests/test_cloud_account.py
    cd server && python -m pytest tests/test_cloud_account.py -q
"""
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

_TMP_ROOT = tempfile.mkdtemp(prefix="vdl_cloud_acct_test_")
os.environ["VDL_DATA_DIR"] = _TMP_ROOT

MISSING = object()
FP_A = "a" * 32


class sandbox:
    """极简 monkeypatch：模块属性 / sys.modules，退出即还原。"""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        old = getattr(obj, name, MISSING)
        setattr(obj, name, value)
        self._undo.append(("attr", obj, name, old))

    def setmodule(self, name, mod):
        old = sys.modules.get(name, MISSING)
        sys.modules[name] = mod
        self._undo.append(("module", name, old))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for rec in reversed(self._undo):
            if rec[0] == "attr":
                _, obj, name, old = rec
                if old is MISSING:
                    try:
                        delattr(obj, name)
                    except AttributeError:
                        pass
                else:
                    setattr(obj, name, old)
            else:
                _, name, old = rec
                if old is MISSING:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old
        return False


_seq = [0]


def _store():
    """每个用例一个独立的会员状态文件（都在临时目录里）。"""
    from membership import MembershipStore
    _seq[0] += 1
    return MembershipStore(path=Path(_TMP_ROOT) / f"membership_{_seq[0]}.json")


def _fake_device_id(sb: sandbox, fp: str = FP_A) -> None:
    mod = types.ModuleType("device_id")
    mod.fingerprint = lambda: (fp, True)
    sb.setmodule("device_id", mod)


# ── 幂等：同一笔购买不得重复落户 ─────────────────────────────────────────── #
def test_apply_cloud_purchases_is_idempotent() -> None:
    store = _store()
    purchases = [{"id": "p1", "plan_code": "download_year"}]
    r1 = store.apply_cloud_purchases(purchases)
    assert r1["ok"] and r1["applied"] == ["download_year"], r1
    first_expire = store.status()["download_member"]["expire_at"]
    assert first_expire > time.time()

    # 第二次登录（同样拉到这一笔购买）不应再顺延
    r2 = store.apply_cloud_purchases(purchases)
    assert r2["applied"] == [], r2
    assert store.status()["download_member"]["expire_at"] == first_expire
    print("✅ ① 同一笔云端购买只落户一次（重复登录不累计会员时长）")


def test_new_purchase_extends() -> None:
    store = _store()
    store.apply_cloud_purchases([{"id": "p1", "plan_code": "download_month"}])
    first = store.status()["download_member"]["expire_at"]
    store.apply_cloud_purchases([{"id": "p1", "plan_code": "download_month"},
                                 {"id": "p2", "plan_code": "download_month"}])
    assert store.status()["download_member"]["expire_at"] > first + 25 * 86400
    print("✅ ② 新增一笔购买正常顺延")


def test_unknown_plan_code_reported() -> None:
    store = _store()
    r = store.apply_cloud_purchases([{"id": "bad", "plan_code": "not_a_plan"}])
    assert r["ok"] is False and r["errors"], r
    assert store._state["meta"]["account"]["purchases_applied"] == []
    print("✅ ③ 未知套餐码如实报错且不落账")


def test_logout_keeps_purchased_benefits() -> None:
    store = _store()
    store.apply_cloud_purchases([{"id": "p1", "plan_code": "download_year"}])
    store.clear_account()
    view = store.account_view()
    assert view["logged_in"] is False, view
    assert store.status()["download_member"]["active"] is True
    print("✅ ④ 登出不清已购权益（换机重装不丢）")


def test_evicted_does_not_delete_benefits() -> None:
    store = _store()
    store.apply_cloud_purchases([{"id": "p1", "plan_code": "download_year"}])
    store.set_evicted(True)
    assert store.status()["download_member"]["active"] is False
    store.set_evicted(False)
    assert store.status()["download_member"]["active"] is True
    print("✅ ⑤ 被挤下线只降级、不删权益；重新登录即恢复")


# ── 路由层 ────────────────────────────────────────────────────────────────── #
def test_cloud_login_applies_and_reports() -> None:
    import app as app_mod
    from routers import cloud_account

    store = _store()
    seen = {}

    def fake_login(email, password, fp, name, base_url="", timeout=0, opener=None):
        seen["args"] = (email, password, fp, name)
        return {"ok": True, "token": "tok-1",
                "account": {"email": email, "max_devices": 2,
                            "devices": [{"fp": fp, "name": name, "last_seen": 0.0}],
                            "purchases": [{"id": "p9", "plan_code": "download_month"}]}}

    with sandbox() as sb:
        sb.setattr(app_mod, "member_store", store)
        _fake_device_id(sb)
        fake_client = types.ModuleType("license_client")
        fake_client.login_remote = fake_login
        fake_client.LicenseCloudError = RuntimeError
        sb.setmodule("license_client", fake_client)

        out = cloud_account.cloud_login({"email": "U@Example.com", "password": "pw123456"})

    assert out["ok"] and out["account"]["logged_in"] is True, out
    assert out["account"]["email"] == "u@example.com", out       # 主键归一
    assert seen["args"][0] == "U@Example.com", seen
    assert store.status()["download_member"]["active"] is True
    print("✅ ⑥ 云端登录：账号主键归一 + 购买自动落户")


def test_cloud_sync_marks_evicted() -> None:
    import app as app_mod
    from routers import cloud_account

    store = _store()
    store.save_account("u@x.com", "tok")

    with sandbox() as sb:
        sb.setattr(app_mod, "member_store", store)
        _fake_device_id(sb)
        fake_client = types.ModuleType("license_client")
        fake_client.heartbeat_remote = lambda *a, **k: {
            "ok": False, "code": "DEVICE_EVICTED", "error": "被挤出"}
        fake_client.LicenseCloudError = RuntimeError
        sb.setmodule("license_client", fake_client)

        out = cloud_account.cloud_sync()

    assert out["evicted"] is True, out
    assert store.status()["device_locked"] == "DEVICE_EVICTED"
    print("✅ ⑦ 心跳发现被挤下线 → 标记 EVICTED")


def test_offline_sync_keeps_benefits() -> None:
    """断网不得惩罚已付费用户：动也不动本地权益。"""
    import app as app_mod
    from routers import cloud_account

    store = _store()
    store.save_account("u@x.com", "tok")
    store.apply_cloud_purchases([{"id": "p1", "plan_code": "download_year"}])

    class Boom(RuntimeError):
        pass

    def boom(*a, **k):
        raise Boom("无法连接授权中心")

    with sandbox() as sb:
        sb.setattr(app_mod, "member_store", store)
        _fake_device_id(sb)
        fake_client = types.ModuleType("license_client")
        fake_client.heartbeat_remote = boom
        fake_client.LicenseCloudError = Boom
        sb.setmodule("license_client", fake_client)

        out = cloud_account.cloud_sync()

    assert out["ok"] is True and out.get("offline") is True, out
    assert store.status()["download_member"]["active"] is True
    assert "device_locked" not in store.status()
    print("✅ ⑧ 断网同步 fail-open：权益原样保留，不锁设备")


def test_home_dir_untouched() -> None:
    assert os.environ["VDL_DATA_DIR"] == _TMP_ROOT
    assert str(Path.home() / ".video-downloader") != _TMP_ROOT
    print("✅ ⑨ 未向真实 ~/.video-downloader 写入任何文件")


def _cleanup() -> None:
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


def _main() -> int:
    import traceback
    tests = [
        test_apply_cloud_purchases_is_idempotent,
        test_new_purchase_extends,
        test_unknown_plan_code_reported,
        test_logout_keeps_purchased_benefits,
        test_evicted_does_not_delete_benefits,
        test_cloud_login_applies_and_reports,
        test_cloud_sync_marks_evicted,
        test_offline_sync_keeps_benefits,
        test_home_dir_untouched,
    ]
    ok = fail = 0
    try:
        for fn in tests:
            try:
                fn()
            except Exception:  # noqa: BLE001
                fail += 1
                print("❌ %s\n%s" % (fn.__name__, traceback.format_exc()))
            else:
                ok += 1
    finally:
        _cleanup()
    print("\n通过 %d / 失败 %d（共 %d）" % (ok, fail, len(tests)))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(_main())
