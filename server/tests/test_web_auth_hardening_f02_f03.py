#!/usr/bin/env python3
"""网页版账号体系加固回归（安全审计 2026-10-13，F-02 / F-03）。

背景：2026-10-11 的桌面端修复**未同步到网页版仓库**，web 侧存在两条高危：
  · F-02：`verify_token` 无 token_version / 不查账号状态；`authenticate` 不拒
    `disabled`；改密 / 注销 / 后台禁用均不撤销既有 token（TTL 30 天）⇒
    后台「封禁」在 web 端完全无效，被盗 token 也切不断。
  · F-03：`ensure_superusers()` 无 loopback 守卫 ⇒ 无管理员名单时**最早注册的
    账号自动成为超管**，可拿 `/api/admin/*` 全部权限。

本守卫全部在隔离 `VDL_DATA_DIR=/tmp/...` 下跑，不碰真实账号表与生产数据。
运行：python3 tests/test_web_auth_hardening_f02_f03.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SERVER = _HERE.parent
_TMP = Path(tempfile.mkdtemp(prefix="vdl_web_auth_"))
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
# 必须在 import 业务模块前设置（auth_store 在 import 时定下 users.json 路径）
os.environ["VDL_DATA_DIR"] = str(_TMP)
os.environ["HOME"] = str(_TMP)

import auth_store  # noqa: E402
import admin_store  # noqa: E402

_PASS = _FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {detail}")


def _req(host: str, headers: dict | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        headers=dict(headers or {}),
        client=types.SimpleNamespace(host=host),
    )


# ── F-02：token 撤销 ─────────────────────────────────────────────────────── #
def test_disable_revokes_existing_token() -> None:
    """后台禁用 ⇒ 该账号已签发的 token 立即失效（此前完全无效）。"""
    uid = auth_store.create_user("dis_1@vdl.test", "passw0rd")
    assert uid
    tok = auth_store.issue_token(uid)
    assert auth_store.verify_token(tok) == uid, "刚签发的 token 应有效"

    r = admin_store.set_user_disabled(uid, True)
    assert r.get("ok") is True, r
    check("禁用后旧 token 失效（此前仍可用）", auth_store.verify_token(tok) is None)
    check("禁用后不得再登录换新 token（authenticate 拒 disabled）",
          auth_store.authenticate("dis_1@vdl.test", "passw0rd") is None)

    # 解禁 → 恢复（幂等，不误伤）
    admin_store.set_user_disabled(uid, False)
    check("解禁后可重新登录", auth_store.authenticate("dis_1@vdl.test", "passw0rd") == uid)


def test_password_reset_revokes_token() -> None:
    """改密 ⇒ 旧 token 失效（此前改密不 bump，旧 bearer 在 TTL 内继续可用）。"""
    uid = auth_store.create_user("pw_1@vdl.test", "passw0rd")
    tok = auth_store.issue_token(uid)
    assert auth_store.verify_token(tok) == uid
    assert auth_store.reset_password("pw_1@vdl.test", "newpass1")
    check("改密后旧 token 失效", auth_store.verify_token(tok) is None)
    tok2 = auth_store.issue_token(uid)
    check("改密后新 token 可用", auth_store.verify_token(tok2) == uid)


def test_deactivate_revokes_token() -> None:
    """注销 ⇒ 旧 token 失效。"""
    uid = auth_store.create_user("del_1@vdl.test", "passw0rd")
    tok = auth_store.issue_token(uid)
    assert auth_store.deactivate_user(uid).get("ok") is True
    check("注销后旧 token 失效", auth_store.verify_token(tok) is None)


def test_old_format_token_not_mass_logged_out() -> None:
    """兼容：payload 只有 3 段的旧 token 按版本 0 处理，历史账号缺省也是 0
    ⇒ 升级后不会把在线用户集体踢下线（直到该账号发生一次改密/禁用/注销）。"""
    uid = auth_store.create_user("old_1@vdl.test", "passw0rd")
    import base64
    import hashlib
    import hmac as _hmac
    iat = int(__import__("time").time())
    payload = auth_store._b64url(f"{uid}.{iat}.{iat + 99999}".encode())
    sig = _hmac.new(auth_store._SECRET, f"{uid}.{payload}".encode("utf-8"),
                    hashlib.sha256).hexdigest()
    legacy = f"{uid}.{payload}.{sig}"
    check("旧格式（无版本号）token 仍可用（不集体踢下线）",
          auth_store.verify_token(legacy) == uid)


# ── F-03：超管引导仅本机 ─────────────────────────────────────────────────── #
def test_bootstrap_only_from_loopback() -> None:
    """无管理员名单时，兜底提权**仅本机直连**允许；公网首注册不得成超管。"""
    # 公网（远端 IP）注册的账号 → 不得被提权
    remote_uid = auth_store.create_user("first@vdl.test", "passw0rd", is_admin=False)
    assert remote_uid
    auth_store.ensure_superusers(allow_bootstrap=False)   # 模拟 app.py 启动钩子 / 公网调用
    check("公网首注册账号不被自动提权（此前会）",
          auth_store.user_is_admin(remote_uid) is False)

    # 本机直连登录 → 允许兜底提升最早注册者（桌面端单机即机主本人）
    auth_store.ensure_superusers(allow_bootstrap=True)
    check("本机直连时兜底提升最早注册者",
          auth_store.user_is_admin(remote_uid) is True)

    # 带上代理头（XFF 自称本机）⇒ 不得判为本机直连
    from routers.auth import _is_loopback
    check("`_is_loopback` 对带 XFF 的本机请求判 False",
          _is_loopback(_req("127.0.0.1", {"x-forwarded-for": "127.0.0.1"})) is False)
    check("`_is_loopback` 对干净的本机请求判 True",
          _is_loopback(_req("127.0.0.1")) is True)


def test_explicit_admin_list_still_works() -> None:
    """显式名单（VDL_ADMIN_IDENTIFIER）不受本机限制 —— 公网也能正确提权。"""
    # 复位「已有超管 60s 节流」并清掉既有超管，模拟「全新部署、只靠显式名单」
    auth_store._SUPERUSER_CACHE["ts"] = 0.0
    with auth_store.users_mutation():
        _d = auth_store._load_users()
        for _u in _d["users"]:
            _u["is_admin"] = False
        auth_store._save_users(_d)

    uid = auth_store.create_user("listed@vdl.test", "passw0rd")
    os.environ["VDL_ADMIN_IDENTIFIER"] = "listed@vdl.test"
    try:
        # allow_bootstrap=False ⇒ 只有显式名单这条路能提权
        auth_store.ensure_superusers(allow_bootstrap=False)
        check("显式名单内账号在公网调用下也被提权",
              auth_store.user_is_admin(uid) is True)
    finally:
        os.environ.pop("VDL_ADMIN_IDENTIFIER", None)


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1 if (failed or _FAIL) else 0)
