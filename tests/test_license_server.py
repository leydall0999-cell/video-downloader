"""license_server 卡密逻辑单测（纯函数级，不起 HTTP）。"""
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
NOW = 1700000000.0


@pytest.fixture()
def rng(monkeypatch):
    """确定性随机源：token_hex 固定序列，保证卡密可复现。"""
    class FakeRng:
        seq = iter(["a1b2c3d4e5", "f6a7b8c9d0", "0123456789"])

        def token_hex(self, n):
            return next(self.seq)
    return FakeRng()


def _new_state():
    return {}


def test_gen_and_verify_roundtrip(rng):
    code = license_server.gen_code("download_year", SECRET, rng)
    assert code.startswith("VDL-DLY-")
    assert license_server.verify_code(code, SECRET) == "DLY"


def test_verify_rejects_tampered_signature():
    code = "VDL-DLY-a1b2c3d4e5-00000000"
    with pytest.raises(license_server.ApiError) as ei:
        license_server.verify_code(code, SECRET)
    assert ei.value.code == "BAD_CODE"


def test_verify_rejects_wrong_secret():
    code = license_server.gen_code("ai_15000", SECRET)
    with pytest.raises(license_server.ApiError):
        license_server.verify_code(code, "other-secret")


def test_redeem_bind_then_reject_other_device(rng):
    st = _new_state()
    code = license_server.gen_code("download_year", SECRET, rng)
    r1 = license_server.redeem_impl(st, code, "a" * 32, NOW, SECRET)
    assert r1["ok"] and not r1["idempotent"] and r1["plan_code"] == "download_year"
    # 同卡同机 → 幂等成功（网络重试不二扣）
    r2 = license_server.redeem_impl(st, code, "a" * 32, NOW + 5, SECRET)
    assert r2["ok"] and r2["idempotent"]
    # 同卡异机 → ALREADY_BOUND（一机一码强制点）
    with pytest.raises(license_server.ApiError) as ei:
        license_server.redeem_impl(st, code, "b" * 32, NOW + 10, SECRET)
    assert ei.value.code == "ALREADY_BOUND"


def test_redeem_requires_valid_signature_before_binding():
    st = _new_state()
    with pytest.raises(license_server.ApiError):
        license_server.redeem_impl(st, "VDL-DLY-deadbeef00-00000000", "a" * 32, NOW, SECRET)
    assert st.get("cards", {}) == {}


def test_revoked_card_rejected(rng):
    st = _new_state()
    code = license_server.gen_code("credits_5000", SECRET, rng)
    license_server.redeem_impl(st, code, "a" * 32, NOW, SECRET)
    license_server.revoke_impl(st, code, NOW + 1)
    with pytest.raises(license_server.ApiError) as ei:
        license_server.redeem_impl(st, code, "a" * 32, NOW + 2, SECRET)
    assert ei.value.code == "REVOKED"


def test_check_reports_revocation_and_binding(rng):
    st = _new_state()
    code = license_server.gen_code("download_month", SECRET, rng)
    assert license_server.check_impl(st, code, "a" * 32)["known"] is False
    license_server.redeem_impl(st, code, "a" * 32, NOW, SECRET)
    ok = license_server.check_impl(st, code, "a" * 32)
    assert ok["known"] and ok["matches"] and ok["status"] == "used"
    license_server.revoke_impl(st, code, NOW + 1)
    bad = license_server.check_impl(st, code, "a" * 32)
    assert bad["status"] == "revoked"


def test_throttle_window():
    license_server._THROTTLE.clear()
    ip = "1.2.3.4"
    for _ in range(license_server._THROTTLE_LIMIT):
        assert not license_server._throttled(ip, 1000.0)
    assert license_server._throttled(ip, 1000.0)               # 第 61 次被限
    assert not license_server._throttled(ip, 1000.0 + license_server._THROTTLE_WINDOW + 1)  # 窗口过后放行


def test_redeem_without_secret_fails():
    with pytest.raises(license_server.ApiError) as ei:
        license_server.redeem_impl({}, "VDL-DLY-a1b2c3d4e5-00000000", "a" * 32, NOW, "")
    assert ei.value.code == "NO_SECRET"
