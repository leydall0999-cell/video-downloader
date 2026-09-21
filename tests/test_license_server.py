"""license_server 账号制单测（纯函数级，不起 HTTP）。

覆盖 2026-09-22 的新语义：账号密码登录 + 设备配额（默认 2，超限挤掉最旧的），
卡密改为绑账号而非绑机器。
"""
import importlib.util
import sys
import time
from pathlib import Path

import pytest

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "license_server", _here.parent / "deploy" / "license_server.py")
license_server = importlib.util.module_from_spec(_spec)
sys.modules["license_server"] = license_server
_spec.loader.exec_module(license_server)

SECRET = "unit-test-secret"
# 基准时间用真实时钟：部分 impl（devices/unbind）内部取 time.time() 校验 token 有效期，
# 若这里写死一个 2023 的常量会造成「假过期」。相对偏移（NOW + n）保持语义不变。
NOW = time.time()
FP_A = "a" * 32
FP_B = "b" * 32
FP_C = "c" * 32


@pytest.fixture()
def rng():
    class FakeRng:
        seq = iter(["a1b2c3d4e5", "f6a7b8c9d0", "0123456789", "aaaabbbbcc",
                    "ddddeeeeff", "1111222233"])

        def token_hex(self, n):
            return next(self.seq)
    return FakeRng()


def _card(plan="download_year", secret=SECRET):
    return license_server.gen_code(plan, secret)


def _register(state, email="u@x.com", password="secret123", now=NOW, device=None):
    return license_server.register_impl(state, email, password, now, SECRET,
                                        device or {"fp": FP_A, "name": "Mac-A"})


# ── 注册 / 登录 ────────────────────────────────────────────────────────────── #
def test_register_returns_token_and_device():
    st = {}
    r = _register(st)
    assert r["ok"] and r["token"]
    acc = r["account"]
    assert acc["email"] == "u@x.com"
    assert [d["fp"] for d in acc["devices"]] == [FP_A]
    assert acc["max_devices"] == 2


def test_duplicate_register_rejected():
    st = {}
    _register(st)
    with pytest.raises(license_server.ApiError) as e:
        _register(st)
    assert e.value.code == "EXISTS"


def test_weak_password_rejected():
    st = {}
    with pytest.raises(license_server.ApiError) as e:
        license_server.register_impl(st, "a@b.com", "123", NOW, SECRET)
    assert e.value.code == "WEAK_PASSWORD"


def test_login_wrong_password_rejected():
    st = {}
    _register(st)
    with pytest.raises(license_server.ApiError) as e:
        license_server.login_impl(st, "u@x.com", "wrong-pass", NOW, SECRET,
                                  {"fp": FP_A})
    assert e.value.code == "BAD_PASSWORD"


def test_login_unknown_account_rejected():
    st = {}
    with pytest.raises(license_server.ApiError) as e:
        license_server.login_impl(st, "nobody@x.com", "whatever1", NOW, SECRET,
                                  {"fp": FP_A})
    assert e.value.code == "NO_ACCOUNT"


# ── 设备配额（核心：不再卡死换机）──────────────────────────────────────────── #
def test_second_device_allowed():
    st = {}
    _register(st)
    r = license_server.login_impl(st, "u@x.com", "secret123", NOW + 1, SECRET,
                                  {"fp": FP_B, "name": "Mac-B"})
    assert r["ok"] and not r.get("evicted")
    assert len(r["account"]["devices"]) == 2


def test_third_device_evicts_oldest_not_rejected():
    """第 3 台登录：不拒绝，而是挤掉最久没活动的那台。"""
    st = {}
    _register(st)
    license_server.login_impl(st, "u@x.com", "secret123", NOW + 10, SECRET,
                              {"fp": FP_B, "name": "Mac-B"})
    license_server.heartbeat_impl(st, license_server.login_impl(
        st, "u@x.com", "secret123", NOW + 20, SECRET,
        {"fp": FP_A, "name": "Mac-A"})["token"], FP_A, NOW + 30, SECRET)
    r = license_server.login_impl(st, "u@x.com", "secret123", NOW + 40, SECRET,
                                  {"fp": FP_C, "name": "Mac-C"})
    assert r["ok"]
    fps = [d["fp"] for d in r["account"]["devices"]]
    assert sorted(fps) == sorted([FP_A, FP_C])   # B 最久没活动 → 被挤掉，不是拒绝 C
    assert r.get("evicted") and r["evicted"][0]["fp"] == FP_B


def test_evicted_device_gets_device_evicted_on_heartbeat():
    """A 最早登录 → B、C 陆续登录后 A 被挤掉 → A 心跳应拿到 DEVICE_EVICTED。"""
    st = {}
    tok_a = _register(st)["token"]                                    # A @ NOW
    license_server.login_impl(st, "u@x.com", "secret123", NOW + 5, SECRET,
                              {"fp": FP_B, "name": "Mac-B"})
    license_server.login_impl(st, "u@x.com", "secret123", NOW + 9, SECRET,
                              {"fp": FP_C, "name": "Mac-C"})
    fps = [d["fp"] for d in st["users"]["u@x.com"]["devices"]]
    assert FP_A not in fps and sorted(fps) == sorted([FP_B, FP_C])
    r = license_server.heartbeat_impl(st, tok_a, FP_A, NOW + 20, SECRET)
    assert not r["ok"] and r["code"] == "DEVICE_EVICTED"


def test_relogin_recovers_device_slot():
    """被挤掉后重新登录 = 抢回名额（不卡死）。"""
    st = {}
    _register(st)
    license_server.login_impl(st, "u@x.com", "secret123", NOW + 5, SECRET,
                              {"fp": FP_B, "name": "Mac-B"})
    license_server.login_impl(st, "u@x.com", "secret123", NOW + 9, SECRET,
                              {"fp": FP_C, "name": "Mac-C"})
    r = license_server.login_impl(st, "u@x.com", "secret123", NOW + 12, SECRET,
                                  {"fp": FP_A, "name": "Mac-A"})
    assert r["ok"]
    assert FP_A in [d["fp"] for d in r["account"]["devices"]]


# ── token ──────────────────────────────────────────────────────────────────── #
def test_token_tampered_rejected():
    st = {}
    tok = _register(st)["token"]
    bad = tok[:-4] + "AAAA"
    with pytest.raises(license_server.ApiError) as e:
        license_server.redeem_impl(st, bad, _card(), NOW, SECRET)
    assert e.value.code == "BAD_TOKEN"


def test_token_expired_rejected():
    st = {}
    tok = _register(st)["token"]
    with pytest.raises(license_server.ApiError) as e:
        license_server.parse_token(tok, SECRET, NOW + 40 * 86400)
    assert e.value.code == "TOKEN_EXPIRED"


# ── 卡密：绑账号，不绑机器 ──────────────────────────────────────────────────── #
def test_redeem_binds_to_account():
    st = {}
    tok = _register(st)["token"]
    code = _card()
    r = license_server.redeem_impl(st, tok, code, NOW, SECRET)
    assert r["ok"] and r["plan_code"] == "download_year"
    pur = st["users"]["u@x.com"]["purchases"]
    assert [p["plan_code"] for p in pur] == ["download_year"]
    assert st["cards"][code]["status"] == "used"
    assert st["cards"][code]["bound_user"] == "u@x.com"


def test_same_card_twice_rejected():
    st = {}
    tok = _register(st)["token"]
    code = _card()
    license_server.redeem_impl(st, tok, code, NOW, SECRET)
    with pytest.raises(license_server.ApiError) as e:
        license_server.redeem_impl(st, tok, code, NOW + 1, SECRET)
    assert e.value.code == "USED"


def test_card_follows_account_to_another_machine():
    """换机（不同 fp）也能享受已充值的权益 —— 这是去掉一机一码的根本诉求。"""
    st = {}
    tok = _register(st)["token"]
    code = _card("download_year")
    license_server.redeem_impl(st, tok, code, NOW, SECRET)
    r = license_server.login_impl(st, "u@x.com", "secret123", NOW + 5, SECRET,
                                  {"fp": FP_B, "name": "New-Mac"})
    assert r["ok"]
    assert [p["plan_code"] for p in r["account"]["purchases"]] == ["download_year"]


def test_bad_signature_rejected():
    st = {}
    tok = _register(st)["token"]
    good = _card()
    bad = good[:-8] + "00000000"
    with pytest.raises(license_server.ApiError) as e:
        license_server.redeem_impl(st, tok, bad, NOW, SECRET)
    assert e.value.code == "BAD_CODE"


def test_revoked_card_rejected():
    st = {}
    tok = _register(st)["token"]
    code = _card()
    # 真实链路里卡密由管理端 gen 先落库；这里模拟这一步再作废
    st.setdefault("cards", {})[code] = {"plan": "DLY", "plan_code": "download_year",
                                        "status": "unused", "bound_user": ""}
    license_server.revoke_impl(st, code, NOW)
    with pytest.raises(license_server.ApiError) as e:
        license_server.redeem_impl(st, tok, code, NOW + 1, SECRET)
    assert e.value.code == "REVOKED"


# ── 设备管理 & 管理端 ──────────────────────────────────────────────────────── #
def test_unbind_frees_slot():
    st = {}
    tok = _register(st)["token"]
    license_server.login_impl(st, "u@x.com", "secret123", NOW + 1, SECRET,
                              {"fp": FP_B, "name": "Mac-B"})
    r = license_server.unbind_impl(st, tok, FP_B, SECRET)
    assert r["ok"] and [d["fp"] for d in r["account"]["devices"]] == [FP_A]


def test_unbind_unknown_device_404():
    st = {}
    tok = _register(st)["token"]
    with pytest.raises(license_server.ApiError) as e:
        license_server.unbind_impl(st, tok, FP_C, SECRET)
    assert e.value.code == "NOT_FOUND"


def test_grant_without_card_and_without_existing_user():
    st = {}
    r = license_server.grant_impl(st, "new@example.com", "DLY", NOW, "补偿")
    assert r["ok"] and r["plan_code"] == "download_year"
    assert st["users"]["new@example.com"]["purchases"][0]["plan_code"] == "download_year"


def test_users_listing():
    st = {}
    _register(st)
    license_server.grant_impl(st, "u@x.com", "DLM", NOW)
    r = license_server.users_impl(st)
    assert r["count"] == 1 and r["users"][0]["email"] == "u@x.com"


def test_devices_endpoint():
    st = {}
    tok = _register(st)["token"]
    r = license_server.devices_impl(st, tok, SECRET)
    assert r["ok"] and r["account"]["devices"][0]["current"] in (True, False)


def test_check_impl_legacy_shape():
    st = {}
    r = license_server.check_impl(st, "VDL-XXX-0000000000-00000000")
    assert r["known"] is False and r["status"] == "unknown"


def test_gen_and_revoke_admin():
    st = {}
    codes = ["VDL-DLY-" + s + "-" + license_server._sign(
        "DLY", s, SECRET) for s in ("aa11bb22cc", "dd33ee44ff")]
    for c in codes:
        st.setdefault("cards", {})[c] = {"plan": "DLY", "plan_code": "download_year",
                                         "status": "unused"}
    out = license_server.revoke_impl(st, codes[0], NOW)
    assert out["status"] == "revoked"
    assert license_server.verify_code(codes[1], SECRET) == "DLY"
