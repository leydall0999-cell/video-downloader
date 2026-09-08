"""VDL 后台管理面板数据层（server/admin_store.py）。

职责：
  - 管理员口令校验 / 改密 / admin token 签发与校验（独立于用户账号体系）。
  - 用户管理：列表（含会员态、禁用标记）、禁用/启用、重置密码。
  - 会员管理：列表、按 code 赠送会员、按 delta 调整积分。
  - 使用统计：聚合 stats.json。
  - 系统配置：套餐/成本/SMTP 状态（不暴露任何密码/密钥）。

纯本机、零外部依赖。管理员口令默认取环境变量 VDL_ADMIN_PASSWORD，未设置则
回退内置默认值（首次启动写入 ~/.video-downloader/admin.json）。建议正式使用前
在面板里修改口令，或部署时通过 VDL_ADMIN_PASSWORD 注入。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _base_dir() -> Path:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    else:
        base = Path.home() / ".video-downloader"
    return base


def _admin_path() -> Path:
    return _base_dir() / "admin.json"


def _admin_secret_path() -> Path:
    return _base_dir() / ".admin_secret"


def _users_path() -> Path:
    return _base_dir() / "users.json"


# --------------------------------------------------------------------------- #
# 管理员签名密钥（独立于用户 auth secret）
# --------------------------------------------------------------------------- #
def _load_admin_secret() -> bytes:
    p = _admin_secret_path()
    if p.exists():
        try:
            return p.read_bytes()
        except OSError:
            pass
    secret = secrets.token_bytes(32)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(secret)
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return secret


_ADMIN_SECRET = _load_admin_secret()
ADMIN_TOKEN_TTL = 60 * 60 * 12  # 12 小时


# --------------------------------------------------------------------------- #
# 口令哈希（复用 auth_store 的 PBKDF2 方案）
# --------------------------------------------------------------------------- #
def _hash_password(password: str) -> tuple[str, str]:
    from auth_store import hash_password
    return hash_password(password)


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    from auth_store import verify_password
    return verify_password(password, salt_hex, hash_hex)


def _default_admin_password() -> str:
    return os.environ.get("VDL_ADMIN_PASSWORD") or "admin123"


def _load_admin() -> dict[str, Any]:
    p = _admin_path()
    if not p.exists():
        return {"salt": None, "pw_hash": None, "updated_at": 0.0, "init": False}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {"salt": None, "pw_hash": None, "updated_at": 0.0, "init": False}
    data.setdefault("salt", None)
    data.setdefault("pw_hash", None)
    data.setdefault("updated_at", 0.0)
    data.setdefault("init", False)
    return data


def _save_admin(data: dict[str, Any]) -> None:
    p = _admin_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def _ensure_initialized() -> dict[str, Any]:
    """首次启动：若未初始化，用默认口令（或环境变量）落库。"""
    data = _load_admin()
    if not data.get("init") or not data.get("salt") or not data.get("pw_hash"):
        pw = _default_admin_password()
        salt, pw_hash = _hash_password(pw)
        data = {"salt": salt, "pw_hash": pw_hash,
                "updated_at": time.time(), "init": True,
                "from_env": bool(os.environ.get("VDL_ADMIN_PASSWORD"))}
        _save_admin(data)
    return data


def verify_admin_password(password: str) -> bool:
    data = _ensure_initialized()
    if not password:
        return False
    return _verify_password(password, data["salt"], data["pw_hash"])


def change_admin_password(old_password: str, new_password: str) -> dict[str, Any]:
    """改密：校验旧口令，新口令至少 6 位。成功返回 ok=True。"""
    if not verify_admin_password(old_password):
        return {"ok": False, "error": "原口令错误"}
    if not new_password or len(new_password) < 6:
        return {"ok": False, "error": "新口令至少 6 位"}
    salt, pw_hash = _hash_password(new_password)
    data = _load_admin()
    data.update({"salt": salt, "pw_hash": pw_hash, "updated_at": time.time(), "init": True})
    _save_admin(data)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Admin token（HMAC 签名，无状态；前缀 'admin' 与用户 token 区分）
# --------------------------------------------------------------------------- #
def _b64url(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_admin_token() -> str:
    iat = int(time.time())
    exp = iat + ADMIN_TOKEN_TTL
    payload = _b64url(f"admin.{iat}.{exp}".encode("utf-8"))
    sig = hmac.new(_ADMIN_SECRET, f"admin.{payload}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"admin.{payload}.{sig}"


def verify_admin_token(token: str) -> bool:
    if not token or token.count(".") != 2:
        return False
    marker, payload, sig = token.split(".")
    if marker != "admin":
        return False
    expected = hmac.new(_ADMIN_SECRET, f"admin.{payload}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False
    try:
        decoded = _b64d(payload).decode("utf-8")
        exp = int(decoded.split(".")[2])
    except Exception:
        return False
    if int(time.time()) > exp:
        return False
    return True


# --------------------------------------------------------------------------- #
# 用户管理
# --------------------------------------------------------------------------- #
def _load_users() -> dict[str, Any]:
    p = _users_path()
    if not p.exists():
        return {"users": [], "by_identifier": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {"users": [], "by_identifier": {}}
    data.setdefault("users", [])
    data.setdefault("by_identifier", {})
    return data


def _save_users(data: dict[str, Any]) -> None:
    p = _users_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def list_users() -> list[dict[str, Any]]:
    """返回用户列表，含会员态与禁用标记。"""
    from user_membership import get_user_store
    data = _load_users()
    out = []
    for u in data.get("users", []):
        uid = u.get("user_id")
        rec: dict[str, Any] = {
            "user_id": uid,
            "identifier": u.get("identifier"),
            "created_at": u.get("created_at"),
            "disabled": bool(u.get("disabled", False)),
        }
        try:
            st = get_user_store(uid).status()
            rec["membership"] = {
                "download_active": st["download_member"]["active"],
                "ai_active": st["ai_member"]["active"],
                "credits_total": st["credits_total"],
            }
        except Exception:
            rec["membership"] = None
        out.append(rec)
    # 按注册时间倒序（新注册在前）
    out.sort(key=lambda r: (r.get("created_at") or 0), reverse=True)
    return out


def set_user_disabled(user_id: str, disabled: bool) -> dict[str, Any]:
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    user["disabled"] = bool(disabled)
    user["updated_at"] = int(time.time())
    _save_users(data)
    return {"ok": True, "disabled": bool(disabled)}


def reset_user_password(user_id: str, new_password: str) -> dict[str, Any]:
    if not new_password or len(new_password) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    from auth_store import reset_password
    ok = reset_password(user["identifier"], new_password)
    if not ok:
        return {"ok": False, "error": "重置失败（账号异常）"}
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 会员管理
# --------------------------------------------------------------------------- #
def list_memberships() -> list[dict[str, Any]]:
    """遍历所有 per-user 会员状态文件。"""
    from user_membership import get_user_store
    data = _load_users()
    out = []
    for u in data.get("users", []):
        uid = u.get("user_id")
        try:
            st = get_user_store(uid).status()
        except Exception:
            continue
        out.append({
            "user_id": uid,
            "identifier": u.get("identifier"),
            "download_active": st["download_member"]["active"],
            "download_plan": st["download_member"]["plan"],
            "download_expire_at": st["download_member"]["expire_at"],
            "ai_active": st["ai_member"]["active"],
            "ai_plan": st["ai_member"]["plan"],
            "ai_credits_left": st["ai_member"]["credits_left"],
            "permanent_credits": st["permanent_credits"],
            "credits_total": st["credits_total"],
            "disabled": bool(u.get("disabled", False)),
        })
    out.sort(key=lambda r: (r.get("download_active") or r.get("ai_active") or False), reverse=True)
    return out


def grant_membership(user_id: str, code: str) -> dict[str, Any]:
    from user_membership import get_user_store
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    try:
        res = get_user_store(user_id).activate(code)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"激活失败：{e}"}
    return res


def adjust_credits(user_id: str, delta: int) -> dict[str, Any]:
    from user_membership import get_user_store
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return {"ok": False, "error": "delta 必须为整数"}
    try:
        res = get_user_store(user_id).add_credits(delta)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"调分失败：{e}"}
    return res


# --------------------------------------------------------------------------- #
# 使用统计 / 系统配置
# --------------------------------------------------------------------------- #
def usage_stats() -> dict[str, Any]:
    from stats import get_stats
    return get_stats()


def system_config() -> dict[str, Any]:
    """套餐、成本、SMTP 状态（不暴露密码/密钥）。"""
    from membership import MembershipStore, MATTING_CLOUD_CREDIT_COST
    from auth_store import _smtp_accounts
    cfg: dict[str, Any] = {
        "plans": MembershipStore().plans(),
        "credit_costs": {"matting_cloud": MATTING_CLOUD_CREDIT_COST},
        "smtp": {"configured": False, "accounts": []},
        "admin_default_password_set": (not bool(os.environ.get("VDL_ADMIN_PASSWORD"))),
    }
    try:
        accounts = _smtp_accounts() or []
        cfg["smtp"]["configured"] = len(accounts) > 0
        cfg["smtp"]["accounts"] = [
            {"host": a.get("host"), "user": a.get("user"),
             "from_name": a.get("from_name"), "match_domains": a.get("match_domains")}
            for a in accounts
        ]
    except Exception:
        pass
    return cfg


def reset_stats(path: Optional[Path] = None) -> dict[str, Any]:
    from stats import reset_stats as _reset
    _reset(path)
    return {"ok": True}
