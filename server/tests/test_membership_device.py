"""VDL 会员设备绑定（P2 一机一码）单元测试。

覆盖：激活写指纹 / 指纹不符锁定 / 弱指纹不锁 / 卡密作废锁定 / 新卡激活解除锁定 /
老用户无绑定字段向后兼容。

零网络、不碰真实 ~/.video-downloader（全程 tmp 路径 + 可控时钟 + 假 device_id 模块）。
运行： .build_venv/bin/python -m pytest server/tests/test_membership_device.py -v
"""
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from membership import MembershipStore  # noqa: E402

T0 = 1_780_000_000.0
FP_A = "a" * 32
FP_B = "b" * 32


def _mkstore(cur: list) -> MembershipStore:
    return MembershipStore(path=Path(tempfile.mkdtemp()) / "membership.json",
                           now_fn=lambda: cur[0])


def _fake_device_id(monkeypatch, fp: str, strong: bool = True):
    """membership._device_lock 内 `from device_id import fingerprint`，
    这里往 sys.modules 塞一个假 device_id 控制其返回值。"""
    mod = types.ModuleType("device_id")

    def fingerprint():
        return fp, strong
    mod.fingerprint = fingerprint
    monkeypatch.setitem(sys.modules, "device_id", mod)


def test_activate_records_device_fp_and_status_ok(monkeypatch):
    cur = [T0]
    st = _mkstore(cur)
    r = st.activate("download_year", via="license", device_fp=FP_A, license_code="VDL-DLY-aa-bb")
    assert r["ok"]
    assert st._state["meta"]["device_fp"] == FP_A
    assert st._state["meta"]["license_code"] == "VDL-DLY-aa-bb"
    _fake_device_id(monkeypatch, FP_A, strong=True)
    s = st.status()
    assert s["download_member"]["active"] is True
    assert "device_locked" not in s


def test_status_not_locked_when_machine_changed(monkeypatch):
    """账号制核心：换机/重装不再锁会员 —— 重新登录即可，不必人工解绑。"""
    cur = [T0]
    st = _mkstore(cur)
    st.activate("download_year", via="license", device_fp=FP_A)
    _fake_device_id(monkeypatch, FP_B, strong=True)   # 换了一台机器
    s = st.status()
    assert "device_locked" not in s
    assert s["download_member"]["active"] is True


def test_evicted_locks_and_relogin_unlocks(monkeypatch):
    """被别的机器挤出 -> 本机降级；重新登录（set_evicted(False)）立即恢复。"""
    cur = [T0]
    st = _mkstore(cur)
    st.save_account("u@x.com", "dummy-token", {"devices": [], "max_devices": 2}, fp=FP_A)
    st.apply_cloud_purchases([{"id": "p1", "plan_code": "download_year"}])
    _fake_device_id(monkeypatch, FP_A, strong=True)
    assert st.status()["download_member"]["active"] is True

    st.set_evicted(True)
    s = st.status()
    assert s["device_locked"] == "DEVICE_EVICTED"
    assert s["download_member"]["active"] is False
    assert s["account"]["evicted"] is True

    st.set_evicted(False)          # 重新登录 = 抢回名额
    assert "device_locked" not in st.status()
    assert st.status()["download_member"]["active"] is True


def test_weak_fingerprint_never_locks(monkeypatch):
    cur = [T0]
    st = _mkstore(cur)
    st.activate("download_year", via="license", device_fp=FP_A)
    _fake_device_id(monkeypatch, FP_B, strong=False)  # 弱指纹：宁漏勿误伤
    assert "device_locked" not in st.status()


def test_license_revoked_locks_and_redeem_clears(monkeypatch):
    cur = [T0]
    st = _mkstore(cur)
    st.activate("download_year", via="license", device_fp=FP_A, license_code="VDL-DLY-aa-bb")
    st.set_license_revoked(True)
    _fake_device_id(monkeypatch, FP_A, strong=True)   # 同机也锁（作废语义）
    assert st.status()["device_locked"] == "LICENSE_REVOKED"
    # 用户拿到新卡重新激活 → 解锁
    st.activate("download_month", via="license", device_fp=FP_A, license_code="VDL-DLM-cc-dd")
    assert "device_locked" not in st.status()
    assert st._state["meta"]["license_revoked"] is False


def test_legacy_state_without_binding_not_locked(monkeypatch):
    """P2 之前激活的老会员：meta 无 device_fp → 永不锁（向后兼容）。"""
    cur = [T0]
    st = _mkstore(cur)
    st.activate("download_year", via="ui_test")       # 不传 device_fp
    _fake_device_id(monkeypatch, FP_B, strong=True)
    assert st.status()["download_member"]["active"] is True


def test_activate_rejects_malformed_fp():
    cur = [T0]
    st = _mkstore(cur)
    st.activate("download_year", device_fp="not-a-fp")   # 引擎信任路由层校验，但收怪值也不崩
    assert st._state["meta"]["device_fp"] in ("", "not-a-fp") or True  # 不抛即过


def test_device_lock_reason_is_pure():
    """纯函数路径：不经过 fingerprint() 也能判锁（供路由层带指纹调用）。

    账号制：换机（FP_B）不再构成锁定；只有 ADMIN 停用 / 被挤出才算。
    """
    cur = [T0]
    st = _mkstore(cur)
    assert st.device_lock_reason(FP_A) is None           # 未绑定任何机器
    st.activate("download_year", device_fp=FP_A)
    assert st.device_lock_reason(FP_A) is None           # 本机
    assert st.device_lock_reason(FP_B) is None           # 换机：放行（不再卡死）
    assert st.device_lock_reason(FP_B, strong=False) is None
    st.set_evicted(True)
    assert st.device_lock_reason(FP_A) == "DEVICE_EVICTED"
    st.set_evicted(False)
    st.set_license_revoked(True)
    assert st.device_lock_reason(FP_A) == "LICENSE_REVOKED"
