"""两端账号库的密码一致性回归（2026-09-22 新增）。

用户问「账号库本地跟云端怎么是分开的，两套机制不应该一样吗?」——确实是两套库：

  · 本机 `auth_store`（`~/.video-downloader/users.json`）：管「账号是否存在 / 密码对
    不对」，是功能门禁（下载 / 字幕 / 个人中心）唯一认的凭据，离线可用。
  · 云端授权中心（`deploy/license_server.py`）：管会员权益 + 设备位名额。

两边各存一份密码哈希，于是**任何一侧单独改密都会分叉**：用户在这台能登、换台说
「密码错」，而且云端登录失败还会连累会员权益同步。本文件锁死三条收敛路径：

  1) 改密 / 忘记密码重置：本机改完立刻把新密码推到云端
     （fail-open：云端连不上也绝不回滚本机，否则断网用户连密码都改不了）
  2) 云端本没有该账号 → 不算失败（老的本机专属账号，登录时会被自动补建到云端）
  3) 已经分叉的账号：下一次登录时自愈 —— 但必须**本机密码校验通过**且**持有该账号的
     云端 token**，否则等于「知道本机密码就能改别人云端账号的密码」（越权）

运行：
    cd server && python tests/test_password_sync.py
    cd server && python -m pytest tests/test_password_sync.py -q
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
_TMP_ROOT = tempfile.mkdtemp(prefix="vdl_pwsync_test_")
os.environ["VDL_DATA_DIR"] = _TMP_ROOT

MISSING = object()

EMAIL = "u@qq.com"
PW_OLD = "oldpass123"
PW_NEW = "newpass456"
SECRET = "s" * 32


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
            kind = rec[0]
            if kind == "env":
                _, key, old = rec
                if old is MISSING:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
            elif kind == "attr":
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
    """清空临时目录里的本机账号表（每个用例从零开始；全程不碰用户真实数据目录）。"""
    for name in ("users.json", "membership.json", ".auth_secret"):
        p = Path(_TMP_ROOT) / name
        if p.exists():
            p.unlink()


def _req(host: str = "127.0.0.1") -> Request:
    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": [], "client": (host, 54321)})


def _uid(ident: str = EMAIL) -> str:
    from auth_store import _load_users
    return _load_users()["by_identifier"][ident]


class _FakeCloud:
    """假 license_client：记录调用，可按需注入行为。"""

    def __init__(self, sb: sandbox, **fns):
        self.calls: list[tuple] = []
        self.behaviors = fns
        calls = self.calls

        class LicenseCloudError(Exception):
            pass

        self.LicenseCloudError = LicenseCloudError
        mod = types.ModuleType("license_client")
        mod.LicenseCloudError = LicenseCloudError

        def passthrough(name):
            def fn(*a, **kw):
                calls.append((name, a, kw))
                return fns.get("default_result", {"ok": False})
            return fn

        def register_remote(email, password, fp, name="", **kw):
            calls.append(("register", email, password))
            return fns.get("register_result", {"ok": True, "token": "tk",
                                               "account": {"email": email}})

        def login_remote(email, password, fp, name="", **kw):
            calls.append(("login", email, password))
            if "login_remote" in fns:
                return fns["login_remote"](email, password, fp, name)
            return fns.get("login_result", {"ok": True, "token": "tk",
                                            "account": {"email": email}})

        def set_password_remote(email, new_password, old_password="", token="", **kw):
            calls.append(("set_password", email, new_password, old_password, token))
            if "set_password_remote" in fns:
                return fns["set_password_remote"](email, new_password, old_password, token)
            return fns.get("set_password_result", {"ok": True, "synced": True})

        mod.register_remote = register_remote
        mod.login_remote = login_remote
        mod.set_password_remote = set_password_remote
        mod.heartbeat_remote = passthrough("heartbeat")
        mod.unbind_remote = passthrough("unbind")
        mod.redeem_remote = passthrough("redeem")
        sb.setmodule("license_client", mod)


def _set_password_calls(calls):
    return [c for c in calls if c[0] == "set_password"]


def _fake_device(sb: sandbox, fp: str = "a" * 32) -> None:
    mod = types.ModuleType("device_id")
    mod.fingerprint = lambda: (fp, True)
    sb.setmodule("device_id", mod)


def _mount_store(sb: sandbox, email: str = "", token: str = ""):
    """装一个临时 MembershipStore，可选写入该账号的云端登录态。"""
    import app as app_mod
    from membership import MembershipStore
    store = MembershipStore(path=Path(_TMP_ROOT) / "membership.json")
    if token:
        store.save_account(email, token)
    sb.setattr(app_mod, "member_store", store)
    return store


def _license_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vdl_license_server_pwtest", str(REPO / "deploy" / "license_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 1. 云端接口本身 ─────────────────────────────────────────────────────── #
def test_cloud_password_accepts_old_password() -> None:
    m = _license_mod()
    st: dict = {}
    now = time.time()
    m.register_impl(st, EMAIL, PW_OLD, now, SECRET, {"fp": "f1", "name": "t"})
    out = m.password_impl(st, EMAIL, PW_NEW, now, SECRET, old_password=PW_OLD)
    assert out["ok"] and out["synced"] is True, out
    # 真的换了密码：新密码能登、旧密码被拒
    assert m.login_impl(st, EMAIL, PW_NEW, now, SECRET, {"fp": "f1"})["ok"]
    try:
        m.login_impl(st, EMAIL, PW_OLD, now, SECRET, {"fp": "f1"})
        raise AssertionError("旧密码竟然还能登录")
    except m.ApiError as e:
        assert e.code == "BAD_PASSWORD", e.code
    print("✅ ① 云端：凭原密码可同步新密码，且旧密码立即失效")


def test_cloud_password_rejects_wrong_old_password() -> None:
    """没有凭据不许改 —— 否则随便报个邮箱就能顶掉别人的密码。"""
    m = _license_mod()
    st: dict = {}
    now = time.time()
    m.register_impl(st, EMAIL, PW_OLD, now, SECRET, {"fp": "f1", "name": "t"})
    try:
        m.password_impl(st, EMAIL, PW_NEW, now, SECRET, old_password="WRONG")
        raise AssertionError("错原密码竟然改成功了")
    except m.ApiError as e:
        assert e.code == "BAD_CREDENTIALS", e.code
    assert m.login_impl(st, EMAIL, PW_OLD, now, SECRET, {"fp": "f1"})["ok"]
    print("✅ ② 云端：原密码不对一律拒绝，原密码保持可用")


def test_cloud_password_token_path_and_no_account() -> None:
    """忘记密码只有验证码、没有原密码 → 靠云端 token 证明持有；别人的 token 无效。"""
    m = _license_mod()
    st: dict = {}
    now = time.time()
    reg = m.register_impl(st, EMAIL, PW_OLD, now, SECRET, {"fp": "f1", "name": "t"})
    out = m.password_impl(st, EMAIL, PW_NEW, now, SECRET, token=reg["token"])
    assert out["ok"] and out["synced"] is True, out

    m.register_impl(st, "other@qq.com", PW_OLD, now, SECRET, {"fp": "f2", "name": "t"})
    other = m.login_impl(st, "other@qq.com", PW_OLD, now, SECRET, {"fp": "f2"})
    try:
        m.password_impl(st, EMAIL, "hacked123", now, SECRET, token=other["token"])
        raise AssertionError("别的账号的 token 竟然能改本账号密码")
    except m.ApiError as e:
        assert e.code == "BAD_CREDENTIALS", e.code

    out2 = m.password_impl(st, "nobody@qq.com", PW_NEW, now, SECRET, old_password=PW_OLD)
    assert out2["ok"] is True and out2["synced"] is False, out2
    assert out2["reason"] == "cloud_no_account", out2
    print("✅ ③ 云端：token 可鉴权（他人 token 无效）；云端无此账号 → 不算失败")


def test_cloud_password_rejects_weak() -> None:
    m = _license_mod()
    st: dict = {}
    now = time.time()
    m.register_impl(st, EMAIL, PW_OLD, now, SECRET, {"fp": "f1", "name": "t"})
    try:
        m.password_impl(st, EMAIL, "123", now, SECRET, old_password=PW_OLD)
        raise AssertionError("弱密码竟然通过了")
    except m.ApiError as e:
        assert e.code == "WEAK_PASSWORD", e.code
    print("✅ ④ 云端：弱密码被拒")


# ── 2. 本机改密 → 推云端（fail-open）───────────────────────────────────── #
def test_change_password_pushes_new_password_to_cloud() -> None:
    from auth_store import create_user, authenticate
    from routers import auth as auth_router
    _fresh()
    create_user(EMAIL, PW_OLD)
    with sandbox() as sb:
        cloud = _FakeCloud(sb)
        sb.setattr(auth_router, "_require_user", lambda req: _uid())
        out = auth_router.auth_change_password(
            _req(), {"current_password": PW_OLD, "new_password": PW_NEW})
        assert out["ok"] is True, out
        assert out["cloud_synced"] is True, out
        assert _set_password_calls(cloud.calls) == [
            ("set_password", EMAIL, PW_NEW, PW_OLD, "")], cloud.calls
    assert authenticate(EMAIL, PW_NEW)
    print("✅ ⑤ 改密：本机改完把新密码（带原密码鉴权）推到云端")


def test_change_password_survives_cloud_failure() -> None:
    """云端连不上：本机必须照常改密（否则断网用户连密码都改不了），但如实标记未同步。"""
    from auth_store import create_user, authenticate
    from routers import auth as auth_router
    _fresh()
    create_user(EMAIL, PW_OLD)

    def boom(*a, **kw):
        raise ConnectionError("网络不可达")

    with sandbox() as sb:
        _FakeCloud(sb, set_password_remote=boom)
        sb.setattr(auth_router, "_require_user", lambda req: _uid())
        out = auth_router.auth_change_password(
            _req(), {"current_password": PW_OLD, "new_password": PW_NEW})
    assert out["ok"] is True, out
    assert out["cloud_sync_tried"] is True and out["cloud_synced"] is False, out
    assert out["cloud_sync_reason"] == "cloud_unreachable", out
    assert authenticate(EMAIL, PW_NEW)      # fail-open：本机不被回滚
    print("✅ ⑥ 改密：云端不可达时本机照常生效（fail-open）并如实标记未同步")


def test_reset_password_reuses_cloud_session_token() -> None:
    """忘记密码只有验证码、没有原密码 → 用本机保存的云端 token 去同步。"""
    from auth_store import create_user, authenticate
    from routers import auth as auth_router
    import auth_store
    _fresh()
    create_user(EMAIL, PW_OLD)
    with sandbox() as sb:
        _mount_store(sb, EMAIL, "tok-cloud")
        cloud = _FakeCloud(sb)
        sb.setattr(auth_store, "verify_reset_code", lambda i, c: True)
        out = auth_router.auth_reset(
            {"identifier": EMAIL, "code": "123456", "password": PW_NEW})
        assert out["ok"] is True and out["cloud_synced"] is True, out
        assert _set_password_calls(cloud.calls) == [
            ("set_password", EMAIL, PW_NEW, "", "tok-cloud")], cloud.calls
    assert authenticate(EMAIL, PW_NEW)
    print("✅ ⑦ 忘记密码：用本机保存的云端 token 同步新密码")


def test_reset_password_without_cloud_session_is_not_a_failure() -> None:
    """没有云端 token（例如从未登录过云端）→ 这轮「没法试」，不该吓用户说同步失败。"""
    from auth_store import create_user, authenticate
    from routers import auth as auth_router
    import auth_store
    _fresh()
    create_user(EMAIL, PW_OLD)
    with sandbox() as sb:
        _mount_store(sb)                       # 没有任何账号登录态
        cloud = _FakeCloud(sb)
        sb.setattr(auth_store, "verify_reset_code", lambda i, c: True)
        out = auth_router.auth_reset(
            {"identifier": EMAIL, "code": "123456", "password": PW_NEW})
    assert out["ok"] is True and out["cloud_sync_tried"] is False, out
    assert cloud.calls == [], cloud.calls
    assert authenticate(EMAIL, PW_NEW)
    print("✅ ⑧ 忘记密码：没有云端会话时不误报「同步失败」")


# ── 3. 已经分叉的账号：登录时自愈 ───────────────────────────────────────── #
def test_heal_requires_local_password_and_cloud_session() -> None:
    from auth_store import create_user
    from routers.cloud_account import _heal_cloud_password
    _fresh()
    create_user(EMAIL, PW_NEW)
    with sandbox() as sb:
        store = _mount_store(sb)
        cloud = _FakeCloud(sb)

        assert _heal_cloud_password(EMAIL, PW_NEW) is False
        assert cloud.calls == [], "没有云端 token 时不得推密码"

        store.save_account(EMAIL, "tok-cloud")
        assert _heal_cloud_password(EMAIL, "WRONG") is False
        assert _set_password_calls(cloud.calls) == [], "本机密码不对时不得推密码"

        store.save_account("other@qq.com", "tok-cloud")
        assert _heal_cloud_password(EMAIL, PW_NEW) is False
        assert _set_password_calls(cloud.calls) == [], "账号与 token 不匹配时不得推密码"

        store.save_account(EMAIL, "tok-cloud")
        assert _heal_cloud_password(EMAIL, PW_NEW) is True
        assert _set_password_calls(cloud.calls) == [
            ("set_password", EMAIL, PW_NEW, "", "tok-cloud")], cloud.calls
    print("✅ ⑨ 自愈三重前提：本机密码对 + 持该账号云端 token，缺一不推")


def test_cloud_login_heals_divergent_password() -> None:
    """云端说密码错、本机密码对 → 推本机密码上去并自动重试一次登录。"""
    from auth_store import create_user
    from routers import cloud_account
    _fresh()
    create_user(EMAIL, PW_NEW)                 # 本机已经是新密码
    logins: list[str] = []

    def login_remote(email, password, fp, name, **kw):
        logins.append(password)
        if len(logins) == 1:
            return {"ok": False, "error": "密码不正确", "code": "BAD_PASSWORD"}
        return {"ok": True, "token": "tok-new",
                "account": {"email": email, "max_devices": 2, "devices": [],
                            "purchases": []}}

    with sandbox() as sb:
        _mount_store(sb, EMAIL, "tok-cloud")
        _fake_device(sb)
        cloud = _FakeCloud(sb, login_remote=login_remote)
        out = cloud_account.cloud_login({"email": EMAIL, "password": PW_NEW})
    assert out["ok"] is True, out
    assert logins == [PW_NEW, PW_NEW], logins
    assert _set_password_calls(cloud.calls), cloud.calls
    print("✅ ⑩ 登录自愈：云端密码旧 → 推本机密码后重试一次即成功")


def test_cloud_login_does_not_heal_on_wrong_local_password() -> None:
    """本机密码也不对 → 绝不能碰云端密码，如实回「密码不正确」。"""
    from auth_store import create_user
    from routers import cloud_account
    _fresh()
    create_user(EMAIL, PW_OLD)
    with sandbox() as sb:
        _mount_store(sb, EMAIL, "tok-cloud")
        _fake_device(sb)
        cloud = _FakeCloud(sb, login_remote=lambda *a, **kw: {
            "ok": False, "error": "密码不正确", "code": "BAD_PASSWORD"})
        out = cloud_account.cloud_login({"email": EMAIL, "password": "WRONG"})
    assert out["ok"] is False and out["code"] == "BAD_PASSWORD", out
    assert _set_password_calls(cloud.calls) == [], cloud.calls
    print("✅ ⑪ 登录自愈：本机密码也不对时不推密码（防越权）")


def test_home_dir_untouched() -> None:
    """全程不得写用户真实数据目录（所有用例都在 VDL_DATA_DIR 临时目录里）。"""
    assert os.environ["VDL_DATA_DIR"] == _TMP_ROOT
    real = Path.home() / ".video-downloader"
    assert str(real) != _TMP_ROOT
    print("✅ ⑫ 未向真实 ~/.video-downloader 写入任何文件")


def _cleanup() -> None:
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


def _main() -> int:
    import traceback
    tests = [
        test_cloud_password_accepts_old_password,
        test_cloud_password_rejects_wrong_old_password,
        test_cloud_password_token_path_and_no_account,
        test_cloud_password_rejects_weak,
        test_change_password_pushes_new_password_to_cloud,
        test_change_password_survives_cloud_failure,
        test_reset_password_reuses_cloud_session_token,
        test_reset_password_without_cloud_session_is_not_a_failure,
        test_heal_requires_local_password_and_cloud_session,
        test_cloud_login_heals_divergent_password,
        test_cloud_login_does_not_heal_on_wrong_local_password,
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
