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

运行：
    cd server && python tests/test_login_error_message.py
    cd server && python -m pytest tests/test_login_error_message.py -q
"""
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

from starlette.requests import Request

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

REPO = Path(_SERVER_DIR).parent
# 🔴 必须在 import app / auth_store **之前**设好，否则会落到真实数据目录
_TMP_ROOT = tempfile.mkdtemp(prefix="vdl_login_msg_test_")
os.environ["VDL_DATA_DIR"] = _TMP_ROOT

MISSING = object()
PHONE = "15014313254"
PW_GOOD = "rightpass123"
PW_BAD = "asdfghjkl"


class sandbox:
    """极简 monkeypatch：环境变量 / 模块属性 / sys.modules 三件事，退出即还原。"""

    def __init__(self):
        self._undo = []

    def setenv(self, key, value):
        old = os.environ.get(key, MISSING)
        os.environ[key] = value
        self._undo.append(("env", key, old))

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
            if rec[0] == "env":
                _, key, old = rec
                if old is MISSING:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
            elif rec[0] == "attr":
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


def _fresh() -> None:
    """清空临时目录里的本机账号表（每个用例从零开始，不碰用户真实数据）。"""
    for name in ("users.json", ".auth_secret"):
        p = Path(_TMP_ROOT) / name
        if p.exists():
            p.unlink()


def _req(host: str = "127.0.0.1") -> Request:
    """构造一个最小 Request，仅用于 _is_loopback 判定。"""
    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": [], "client": (host, 54321)})


# ── 1. user_exists ──────────────────────────────────────────────────────── #
def test_user_exists_matches_local_account_table() -> None:
    from auth_store import create_user, user_exists
    _fresh()
    assert create_user(PHONE, PW_GOOD)
    assert user_exists(PHONE) is True
    assert user_exists(f"  {PHONE} ") is True        # 空白容错
    assert user_exists("13800138000") is False       # 没注册过
    assert user_exists("") is False
    print("✅ ① user_exists 与本机账号表一致（含空白容错）")


def test_user_exists_returns_false_after_deactivate() -> None:
    """注销后的账号不算存在（否则会把「已注销」误报成「密码不正确」）。"""
    from auth_store import create_user, user_exists, deactivate_user, _load_users
    _fresh()
    create_user(PHONE, PW_GOOD)
    uid = _load_users()["by_identifier"][PHONE]
    deactivate_user(uid)
    assert user_exists(PHONE) is False
    print("✅ ①b 已注销账号不算「存在」")


# ── 2. 登录失败文案：本机区分，公网模糊 ─────────────────────────────────── #
def test_loopback_login_wrong_password_says_password_not_account() -> None:
    """本机回环：账号在、密码错 → 必须说「密码不正确」，不能说「账号不存在」。"""
    from auth_store import create_user
    from routers.auth import auth_login
    _fresh()
    create_user(PHONE, PW_GOOD)
    r = auth_login(_req("127.0.0.1"), {"identifier": PHONE, "password": PW_BAD})
    assert r["ok"] is False, r
    assert r["code"] == "BAD_PASSWORD", r
    assert "密码不正确" in r["error"], r
    assert "账号不存在" not in r["error"], r
    print("✅ ② 本机：账号在、密码错 → 「密码不正确」（不再张冠李戴）")


def test_loopback_login_unknown_account_says_no_account() -> None:
    from routers.auth import auth_login
    _fresh()
    r = auth_login(_req("127.0.0.1"), {"identifier": "13800138000", "password": PW_BAD})
    assert r["ok"] is False and r["code"] == "NO_ACCOUNT", r
    print("✅ ③ 本机：账号确实不在 → 「账号不存在」")


def test_loopback_login_success_still_works() -> None:
    from auth_store import create_user
    from routers.auth import auth_login
    _fresh()
    create_user(PHONE, PW_GOOD)
    r = auth_login(_req("127.0.0.1"), {"identifier": PHONE, "password": PW_GOOD})
    assert r["ok"] is True and r["token"], r
    print("✅ ④ 本机：密码正确照常登录")


def test_public_login_stays_ambiguous() -> None:
    """公网（网页版）不得区分账号是否存在——那等于开放账号枚举。"""
    from auth_store import create_user
    from routers.auth import auth_login
    _fresh()
    create_user(PHONE, PW_GOOD)
    for ident, pw in ((PHONE, PW_BAD), ("13800138000", PW_BAD)):
        r = auth_login(_req("203.0.113.7"), {"identifier": ident, "password": pw})
        assert r["ok"] is False, r
        assert r["error"] == "账号或密码错误", r
        assert r["code"] == "BAD_CREDENTIALS", r
    print("✅ ⑤ 公网：两种失败文案完全一致（防账号枚举）")


# ── 3. 手机号老账号自愈 ─────────────────────────────────────────────────── #
def _fake_license(sb: sandbox, boom: bool = False):
    calls: list[tuple] = []

    class LicenseCloudError(Exception):
        pass

    mod = types.ModuleType("license_client")
    mod.LicenseCloudError = LicenseCloudError

    def register_remote(email, password, fp, name="", **kw):
        if boom:
            raise LicenseCloudError("网络不可达")
        calls.append((email, password, fp, name))
        return {"ok": True, "token": "tk", "account": {"email": email}}

    def set_password_remote(email, new_password, old_password="", token="", **kw):
        calls.append(("set_password", email, new_password))
        return {"ok": True, "synced": True}

    mod.register_remote = register_remote
    mod.set_password_remote = set_password_remote
    sb.setmodule("license_client", mod)
    return calls


def test_adopt_only_when_local_password_matches() -> None:
    from auth_store import create_user
    from routers.cloud_account import _adopt_local_account
    _fresh()
    create_user(PHONE, PW_GOOD)
    with sandbox() as sb:
        calls = _fake_license(sb)
        assert _adopt_local_account(PHONE, PW_GOOD, "fp1", "Mac") is True
        assert len(calls) == 1, calls              # 只有密码对才建号
        assert calls[0][0] == PHONE
        assert _adopt_local_account(PHONE, PW_BAD, "fp1", "Mac") is False
        assert _adopt_local_account("13800138000", PW_GOOD, "fp1", "Mac") is False
        assert len(calls) == 1, "失败路径不得调用云端建号"
    print("✅ ⑥ 手机号老账号自愈：仅本机密码正确时补建到云端")


def test_adopt_survives_cloud_failure() -> None:
    """云端不可达时自愈必须安静失败（返回 False），不能抛异常打断登录流程。"""
    from auth_store import create_user
    _fresh()
    create_user(PHONE, PW_GOOD)
    with sandbox() as sb:
        _fake_license(sb, boom=True)
        from routers.cloud_account import _adopt_local_account
        assert _adopt_local_account(PHONE, PW_GOOD, "fp1", "Mac") is False
    print("✅ ⑦ 云端不可达时自愈安静失败（不打断登录）")


# ── 4. 忘记密码：手机号必须能自助（否则用户被永久卡在登录页） ────────────── #
def test_phone_reset_code_works_on_loopback() -> None:
    """手机号 + SMTP 发不出短信 → 本机回环必须退回自助回显，不能报「发送失败」。

    否则手机号用户一旦忘记密码就再也进不来（本次报障用户正是这种账号形态）。
    """
    from auth_store import create_user
    from routers.auth import auth_reset_code
    _fresh()
    create_user(PHONE, PW_GOOD)
    with sandbox() as sb:
        sb.setenv("VDL_SEND_MODE", "smtp")   # 复刻本机现状：只配了邮箱通道
        r = auth_reset_code(_req("127.0.0.1"), {"identifier": PHONE})
    assert r["ok"] is True, r
    assert r.get("dev_code"), f"本机应能自助拿到验证码，实际 {r}"
    # self_serve 让前端把措辞从「测试模式」改成「手机号暂不支持短信接收」，
    # 别让用户以为这是个没做完的功能
    assert r.get("self_serve") is True, f"手机号自助应标记 self_serve，实际 {r}"
    print("✅ ⑧ 手机号忘记密码：本机自助回显验证码（self_serve）")


def test_phone_reset_code_never_leaks_on_public() -> None:
    """公网绝不回显验证码（回显＝任意账号改密权）。"""
    from auth_store import create_user
    from routers.auth import auth_reset_code
    _fresh()
    create_user("13800138001", PW_GOOD)
    with sandbox() as sb:
        sb.setenv("VDL_SEND_MODE", "smtp")
        r = auth_reset_code(_req("203.0.113.7"), {"identifier": "13800138001"})
    assert not r.get("dev_code"), f"公网不得回显验证码，实际 {r}"
    print("✅ ⑨ 公网：手机号也不回显验证码")


def test_email_reset_code_still_reports_delivery_failure() -> None:
    """邮箱走 SMTP；投递失败仍如实报错（不要被手机号那条分支顺手放过）。"""
    from auth_store import create_user
    from routers.auth import auth_reset_code
    _fresh()
    create_user("someone@qq.com", PW_GOOD)
    with sandbox() as sb:
        sb.setenv("VDL_SEND_MODE", "smtp")
        r = auth_reset_code(_req("127.0.0.1"), {"identifier": "someone@qq.com"})
    assert r["ok"] is False and "发送失败" in r["error"], r
    print("✅ ⑩ 邮箱服务配置坏了照样如实报错（不被手机号分支放过）")


# ── 5. 云端授权中心的账号主键口径 ────────────────────────────────────────── #
def _license_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vdl_license_server_msgtest", str(REPO / "deploy" / "license_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cloud_register_accepts_phone_and_email() -> None:
    cases = [
        ("15014313254", True),       # 中国大陆手机号（本次报障的账号形态）
        ("+8615014313254", True),    # E.164
        ("user@qq.com", True),       # 邮箱
        ("12345", False),            # 太短
        ("abcdefg", False),          # 既非邮箱也非手机号
        ("not-an-email@", False),    # 半个邮箱
        ("", False),
    ]
    m = _license_mod()
    for ident, should_pass in cases:
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
    print("✅ ⑪ 云端账号主键口径：手机号与邮箱一视同仁")


def test_cloud_login_wrong_password_message() -> None:
    """云端的密码错文案同样不能含糊成「账号不存在」。"""
    m = _license_mod()
    st: dict = {}
    sec = "s" * 32
    m.register_impl(st, PHONE, PW_GOOD, time.time(), sec, {"fp": "f1", "name": "t"})
    try:
        m.login_impl(st, PHONE, PW_BAD, time.time(), sec, {"fp": "f1", "name": "t"})
        raise AssertionError("错密码竟然登录成功")
    except m.ApiError as e:
        assert e.code == "BAD_PASSWORD", e.code
    try:
        m.login_impl(st, "13800138000", PW_GOOD, time.time(), sec,
                     {"fp": "f1", "name": "t"})
        raise AssertionError("不存在的账号竟然登录成功")
    except m.ApiError as e:
        assert e.code == "NO_ACCOUNT", e.code
    print("✅ ⑫ 云端：密码错 ≠ 账号不存在")


def test_home_dir_untouched() -> None:
    assert os.environ["VDL_DATA_DIR"] == _TMP_ROOT
    assert str(Path.home() / ".video-downloader") != _TMP_ROOT
    print("✅ ⑬ 未向真实 ~/.video-downloader 写入任何文件")


def _cleanup() -> None:
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


def _main() -> int:
    import traceback
    tests = [
        test_user_exists_matches_local_account_table,
        test_user_exists_returns_false_after_deactivate,
        test_loopback_login_wrong_password_says_password_not_account,
        test_loopback_login_unknown_account_says_no_account,
        test_loopback_login_success_still_works,
        test_public_login_stays_ambiguous,
        test_adopt_only_when_local_password_matches,
        test_adopt_survives_cloud_failure,
        test_phone_reset_code_works_on_loopback,
        test_phone_reset_code_never_leaks_on_public,
        test_email_reset_code_still_reports_delivery_failure,
        test_cloud_register_accepts_phone_and_email,
        test_cloud_login_wrong_password_message,
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
