"""P0-5 回归测试：注销 / 禁用 / 改密 必须让已签发的 token 立即失效。

背景（安全审计 2026-10-11，缺陷 P0-5「注销/禁用/改密不撤销 token」）：
  · `verify_token` 只校验 HMAC 签名 + `exp`，**完全不查**账号是否还存在、
    是否已被后台禁用、是否已注销，也不感知密码是否已变更；
  · 而 `TOKEN_TTL = 60*60*24*365*10`（10 年）。
  ⇒ 任何一次 token 泄漏都等于**永久后门**：用户改密、注销、被管理员禁用，
    攻击者手里的 bearer 依然畅通无阻。

修复口径（见 auth_store.py）：
  · payload 增加第 4 段 `token_version`；改密/注销/禁用/删除时 +1；
  · `verify_token` 追加两道闸门：账号状态（不存在/disabled/deleted_at → 拒）
    与 token_version 比对；
  · 旧格式 token（3 段 payload，无版本号）按版本 0 处理，与历史账号
    缺省的 token_version=0 一致 ⇒ 升级后**不会**把在线用户集体踢下线；
  · `authenticate` 同时拒绝已禁用账号登录（否则禁用可被「重新登录」绕过）；
  · TTL 可用 VDL_TOKEN_TTL_SECONDS 收紧（公网部署），桌面默认保持不变。

设计约束：VDL_DATA_DIR 指向临时目录，绝不碰真实 ~/.video-downloader/users.json。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

# 必须在 import auth_store 之前隔离数据目录（_base_dir() 每次调用读 env）
_TMP_DATA = tempfile.mkdtemp(prefix="vdl_p0_5_")
os.environ["VDL_DATA_DIR"] = _TMP_DATA

_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import auth_store as au  # noqa: E402
import admin_store as ads  # noqa: E402


def _new_account(tag: str) -> tuple[str, str]:
    ident = f"{tag}@example.com"
    uid = au.create_user(ident, "Pw#123456")
    assert uid, f"注册失败：{ident}"
    return ident, uid


def _legacy_token(uid: str, ttl: int = 3600) -> str:
    """构造**旧格式** token（payload 仅 3 段，无 token_version），模拟升级前签发的 token。"""
    iat = int(time.time())
    exp = iat + ttl
    payload = au._b64url(f"{uid}.{iat}.{exp}".encode("utf-8"))
    sig = au._hmac_sig(uid, payload)
    return f"{uid}.{payload}.{sig}"


# ---------------------------------------------------------------- 基线

def test_issue_and_verify_roundtrip():
    _, uid = _new_account("base")
    tok = au.issue_token(uid)
    assert au.verify_token(tok) == uid


def test_tampered_signature_rejected():
    _, uid = _new_account("tamper")
    tok = au.issue_token(uid)
    head, payload, _sig = tok.split(".")
    assert au.verify_token(f"{head}.{payload}.{'0' * 64}") is None


def test_expired_token_rejected():
    _, uid = _new_account("expired")
    iat = int(time.time()) - 100
    exp = iat - 1                      # 已过期
    payload = au._b64url(f"{uid}.{iat}.{exp}.0".encode("utf-8"))
    tok = f"{uid}.{payload}.{au._hmac_sig(uid, payload)}"
    assert au.verify_token(tok) is None


def test_unknown_user_token_rejected():
    """签名合法但账号不存在（伪造/已物理删除）→ 必须拒绝。"""
    payload = au._b64url(f"u_ghost.{int(time.time())}.{int(time.time()) + 999}.0".encode("utf-8"))
    tok = f"u_ghost.{payload}.{au._hmac_sig('u_ghost', payload)}"
    assert au.verify_token(tok) is None


# ---------------------------------------------------------------- 撤销

def test_password_change_revokes_old_token():
    """改密后：旧 token 立即失效，新 token 正常。"""
    ident, uid = _new_account("pwchange")
    old = au.issue_token(uid)
    assert au.verify_token(old) == uid

    assert au.reset_password(ident, "NewPw#654321") is True

    assert au.verify_token(old) is None, "改密后旧 token 仍然有效 —— 撤销失败"
    assert au.verify_token(au.issue_token(uid)) == uid


def test_admin_password_reset_also_revokes():
    """后台重置密码（admin_store 路径）同样撤销旧 token。"""
    _, uid = _new_account("adminreset")
    old = au.issue_token(uid)
    r = ads.reset_user_password(uid, "AdminNew#999")
    assert r.get("ok"), r
    assert au.verify_token(old) is None


def test_deactivate_revokes_token():
    """自助注销（软删除）后旧 token 立即失效。"""
    _, uid = _new_account("deact")
    old = au.issue_token(uid)
    assert au.deactivate_user(uid).get("ok")
    assert au.verify_token(old) is None, "注销后旧 token 仍然有效"


def test_admin_disable_revokes_token_and_blocks_login():
    """后台禁用：旧 token 失效 **且** 不能再登录换新 token。"""
    ident, uid = _new_account("disabled")
    old = au.issue_token(uid)
    assert ads.set_user_disabled(uid, True).get("ok")

    assert au.verify_token(old) is None, "禁用后旧 token 仍然有效"
    assert au.authenticate(ident, "Pw#123456") is None, "禁用账号仍能登录"

    # 解禁后可以重新登录，但旧 token 仍无效（版本已递增）
    assert ads.set_user_disabled(uid, False).get("ok")
    new_uid = au.authenticate(ident, "Pw#123456")
    assert new_uid == uid
    assert au.verify_token(old) is None
    assert au.verify_token(au.issue_token(uid)) == uid


def test_disable_is_idempotent_for_active_accounts():
    """对本来就正常的账号调用「解禁」不得递增版本（否则无故踢掉在线用户）。"""
    _, uid = _new_account("noop")
    tok = au.issue_token(uid)
    assert ads.set_user_disabled(uid, False).get("ok")
    assert au.verify_token(tok) == uid, "无状态变化的调用不应使 token 失效"


def test_admin_delete_revokes_token():
    _, uid = _new_account("admindelete")
    old = au.issue_token(uid)
    assert ads.delete_user(uid).get("ok")
    assert au.verify_token(old) is None


# ---------------------------------------------------------------- 兼容性

def test_legacy_token_without_version_still_accepted():
    """升级前签发的旧格式 token：账号状态未变时必须继续可用（不集体踢下线）。"""
    _, uid = _new_account("legacy")
    tok = _legacy_token(uid)
    assert au.verify_token(tok) == uid

    # 一旦该账号发生改密，旧格式 token 也随之失效（版本从 0 → 1）
    au.reset_password(f"legacy@example.com", "Changed#123")
    assert au.verify_token(tok) is None


# ---------------------------------------------------------------- TTL

def test_token_ttl_env_override():
    assert au._resolve_token_ttl() == au.DEFAULT_TOKEN_TTL      # 未设置 → 默认
    os.environ["VDL_TOKEN_TTL_SECONDS"] = "120"
    try:
        assert au._resolve_token_ttl() == 120
        os.environ["VDL_TOKEN_TTL_SECONDS"] = "0"               # 非法（<60）→ 回落默认
        assert au._resolve_token_ttl() == au.DEFAULT_TOKEN_TTL
        os.environ["VDL_TOKEN_TTL_SECONDS"] = "abc"             # 非法 → 回落默认
        assert au._resolve_token_ttl() == au.DEFAULT_TOKEN_TTL
    finally:
        os.environ.pop("VDL_TOKEN_TTL_SECONDS", None)


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
