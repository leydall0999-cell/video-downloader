"""登录错误提示的准确性 + 手机号账号的云端建号（2026-09-22）。

用户实测报障：桌面 App 用手机号登录、密码打错，界面却提示「账号不存在，请先注册」。
事实链条（都已实测确认）：
  · 本机账号表里 `15014313254` **存在**（2026-09-08 注册），只是密码不匹配
  · 云端授权中心没有该账号——`register_impl` 当年硬判 `"@" in uid`，手机号注册被拒
  · 而前端注册流程**静默吞掉**了这个云端失败、继续本地注册成功
  · 登录时前端又把**云端**的 NO_ACCOUNT 当结论显示，覆盖了本地的真实结论

本文件锁死四件事，避免这几处再被改回去：
  1) 本机账号表决定「账号是否存在」：本机回环时区分 BAD_PASSWORD / NO_ACCOUNT
  2) 公网调用方仍返回模糊文案（防账号枚举）
  3) 手机号老账号自愈：本机有该账号且**密码正确**时才补建到云端
  4) 云端授权中心的账号主键口径与 App 一致（邮箱 或 手机号）
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

import pytest
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]          # server/
PROJ = ROOT.parent                                  # 仓库根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(PROJ))

PHONE = "15014313254"
PW_GOOD = "rightpass123"
PW_BAD = "asdfghjkl"


def _req(host: str = "127.0.0.1") -> Request:
    """构造一个最小 Request，仅用于 _is_loopback 判定。"""
    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": [], "client": (host, 54321)})


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VDL_DATA_DIR", str(tmp_path))
    return tmp_path


# ── 1. user_exists ──────────────────────────────────────────────────────── #
def test_user_exists_matches_local_account_table(data_dir):
    from auth_store import create_user, user_exists
    assert create_user(PHONE, PW_GOOD)
    assert user_exists(PHONE) is True
    assert user_exists(f"  {PHONE} ") is True        # 空白容错
    assert user_exists("13800138000") is False       # 没注册过
    assert user_exists("") is False


def test_user_exists_returns_false_after_deactivate(data_dir):
    """注销后的账号不算存在（否则会把「已注销」误报成「密码不正确」）。"""
    from auth_store import create_user, user_exists, deactivate_user, _load_users
    create_user(PHONE, PW_GOOD)
    uid = _load_users()["by_identifier"][PHONE]
    deactivate_user(uid)
    assert user_exists(PHONE) is False


# ── 2. 登录失败文案：本机区分，公网模糊 ─────────────────────────────────── #
def test_loopback_login_wrong_password_says_password_not_account(data_dir):
    """本机回环：账号在、密码错 → 必须说「密码不正确」，不能说「账号不存在」。"""
    from auth_store import create_user
    from routers.auth import auth_login
    create_user(PHONE, PW_GOOD)
    r = auth_login(_req("127.0.0.1"), {"identifier": PHONE, "password": PW_BAD})
    assert r["ok"] is False
    assert r["code"] == "BAD_PASSWORD"
    assert "密码不正确" in r["error"]
    assert "账号不存在" not in r["error"]


def test_loopback_login_unknown_account_says_no_account(data_dir):
    from routers.auth import auth_login
    r = auth_login(_req("127.0.0.1"), {"identifier": "13800138000", "password": PW_BAD})
    assert r["ok"] is False and r["code"] == "NO_ACCOUNT"


def test_loopback_login_success_still_works(data_dir):
    from auth_store import create_user
    from routers.auth import auth_login
    create_user(PHONE, PW_GOOD)
    r = auth_login(_req("127.0.0.1"), {"identifier": PHONE, "password": PW_GOOD})
    assert r["ok"] is True and r["token"]


def test_public_login_stays_ambiguous(data_dir):
    """公网（网页版）不得区分账号是否存在——那等于开放账号枚举。"""
    from auth_store import create_user
    from routers.auth import auth_login
    create_user(PHONE, PW_GOOD)
    for ident, pw in ((PHONE, PW_BAD), ("13800138000", PW_BAD)):
        r = auth_login(_req("203.0.113.7"), {"identifier": ident, "password": pw})
        assert r["ok"] is False
        assert r["error"] == "账号或密码错误"
        assert r["code"] == "BAD_CREDENTIALS"


# ── 3. 手机号老账号自愈 ─────────────────────────────────────────────────── #
@pytest.fixture()
def fake_license(monkeypatch):
    """假 license_client：记录 register_remote 调用。"""
    calls: list[tuple] = []

    class LicenseCloudError(Exception):
        pass

    mod = types.ModuleType("license_client")
    mod.LicenseCloudError = LicenseCloudError

    def register_remote(email, password, fp, name="", **kw):
        calls.append((email, password, fp, name))
        return {"ok": True, "token": "tk", "account": {"email": email}}

    mod.register_remote = register_remote
    monkeypatch.setitem(sys.modules, "license_client", mod)
    return calls


def test_adopt_only_when_local_password_matches(data_dir, fake_license):
    from auth_store import create_user
    from routers.cloud_account import _adopt_local_account
    create_user(PHONE, PW_GOOD)

    assert _adopt_local_account(PHONE, PW_GOOD, "fp1", "Mac") is True
    assert len(fake_license) == 1                     # 只有密码对才建号
    assert fake_license[0][0] == PHONE

    assert _adopt_local_account(PHONE, PW_BAD, "fp1", "Mac") is False
    assert _adopt_local_account("13800138000", PW_GOOD, "fp1", "Mac") is False
    assert len(fake_license) == 1                     # 失败路径不得调用云端建号


def test_adopt_survives_cloud_failure(data_dir, monkeypatch):
    """云端不可达时自愈必须安静失败（返回 False），不能抛异常打断登录流程。"""
    from auth_store import create_user
    create_user(PHONE, PW_GOOD)

    class LicenseCloudError(Exception):
        pass

    mod = types.ModuleType("license_client")
    mod.LicenseCloudError = LicenseCloudError

    def boom(*a, **kw):
        raise LicenseCloudError("网络不可达")

    mod.register_remote = boom
    monkeypatch.setitem(sys.modules, "license_client", mod)

    from routers.cloud_account import _adopt_local_account
    assert _adopt_local_account(PHONE, PW_GOOD, "fp1", "Mac") is False


# ── 4. 忘记密码：手机号必须能自助（否则用户被永久卡在登录页） ────────────── #
@pytest.fixture()
def smtp_no_channel(tmp_path, monkeypatch):
    """复刻本机现状：配了 smtp（只发邮箱），短信网关（sms）尚未接入。"""
    monkeypatch.setenv("VDL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VDL_SEND_MODE", "smtp")
    return tmp_path


def test_phone_reset_code_works_on_loopback(smtp_no_channel):
    """手机号 + SMTP 发不出短信 → 本机回环必须退回自助回显，不能报「发送失败」。

    否则手机号用户一旦忘记密码就再也进不来（本次报障用户正是这种账号形态）。
    """
    from auth_store import create_user
    from routers.auth import auth_reset_code
    create_user(PHONE, PW_GOOD)
    r = auth_reset_code(_req("127.0.0.1"), {"identifier": PHONE})
    assert r["ok"] is True
    assert r.get("dev_code"), f"本机应能自助拿到验证码，实际 {r}"


def test_phone_reset_code_never_leaks_on_public(smtp_no_channel):
    """公网绝不回显验证码（回显＝任意账号改密权）。"""
    from auth_store import create_user
    from routers.auth import auth_reset_code
    create_user("13800138001", PW_GOOD)
    r = auth_reset_code(_req("203.0.113.7"), {"identifier": "13800138001"})
    assert not r.get("dev_code"), f"公网不得回显验证码，实际 {r}"


def test_email_reset_code_still_reports_delivery_failure(smtp_no_channel):
    """邮箱走 SMTP；投递失败仍如实报错（不要被手机号那条分支顺手放过）。"""
    from auth_store import create_user
    from routers.auth import auth_reset_code
    create_user("someone@qq.com", PW_GOOD)
    r = auth_reset_code(_req("127.0.0.1"), {"identifier": "someone@qq.com"})
    assert r["ok"] is False and "发送失败" in r["error"]


# ── 5. 云端授权中心的账号主键口径 ────────────────────────────────────────── #
def _license_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vdl_license_server", str(PROJ / "deploy" / "license_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("ident,should_pass", [
    ("15014313254", True),          # 中国大陆手机号（本次报障的账号形态）
    ("+8615014313254", True),       # E.164
    ("user@qq.com", True),          # 邮箱
    ("12345", False),               # 太短
    ("abcdefg", False),             # 既非邮箱也非手机号
    ("not-an-email@", False),       # 半个邮箱
    ("", False),
])
def test_cloud_register_accepts_phone_and_email(ident, should_pass):
    m = _license_mod()
    st: dict = {}
    try:
        m.register_impl(st, ident, PW_GOOD, time.time(), "s" * 32,
                        {"fp": "f1", "name": "t"})
        got = True
    except m.ApiError as e:
        got = e.code
    if should_pass:
        assert got is True, f"{ident} 应可注册，实际 {got}"
    else:
        assert got == "BAD_ACCOUNT", f"{ident} 应被拒，实际 {got}"


def test_cloud_login_wrong_password_message():
    """云端的密码错文案同样不能含糊成「账号不存在」。"""
    m = _license_mod()
    st: dict = {}
    sec = "s" * 32
    m.register_impl(st, PHONE, PW_GOOD, time.time(), sec, {"fp": "f1", "name": "t"})
    with pytest.raises(m.ApiError) as ei:
        m.login_impl(st, PHONE, PW_BAD, time.time(), sec, {"fp": "f1", "name": "t"})
    assert ei.value.code == "BAD_PASSWORD"
    with pytest.raises(m.ApiError) as ei2:
        m.login_impl(st, "13800138000", PW_GOOD, time.time(), sec, {"fp": "f1", "name": "t"})
    assert ei2.value.code == "NO_ACCOUNT"
