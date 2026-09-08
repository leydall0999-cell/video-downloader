"""VDL 本地账号系统（A1：自建轻量账号）。

零外部依赖（仅标准库 hashlib / hmac / secrets），契合桌面端 LGPL/离线约束。
账号以「邮箱或手机号」为标识，密码用 PBKDF2-HMAC-SHA256 加盐哈希；登录态用
HMAC-SHA256 签名的无状态 token（Bearer），由本机 .auth_secret 校验。

存储：~/.video-downloader/users.json（账号表）+ 同目录 .auth_secret（签名密钥，0600）。
会员状态按用户分文件存于 memberships/{user_id}.json（见 user_membership.py，即 B2）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _base_dir() -> Path:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    else:
        base = Path.home() / ".video-downloader"
    return base


def _users_path() -> Path:
    return _base_dir() / "users.json"


def _secret_path() -> Path:
    return _base_dir() / ".auth_secret"


# --------------------------------------------------------------------------- #
# 签名密钥（每机一份，丢失即全部旧 token 失效，但账号仍在）
# --------------------------------------------------------------------------- #
def _load_secret() -> bytes:
    p = _secret_path()
    if p.exists():
        try:
            return p.read_bytes()
        except OSError:
            pass
    # 生成并落盘（仅本机可读）
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
        pass  # 写失败则退回内存密钥（重启失效，但不阻塞登录）
    return secret


_SECRET = _load_secret()
PBKDF2_ITERS = 200_000
TOKEN_TTL = 60 * 60 * 24 * 30  # 30 天


# --------------------------------------------------------------------------- #
# 密码哈希
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return salt.hex(), dk.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return hmac.compare_digest(dk, expected)


# --------------------------------------------------------------------------- #
# Token（HMAC 签名，无状态）
# --------------------------------------------------------------------------- #
def _b64url(b: bytes) -> str:
    return b64e(b)


def b64e(b: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64d(s: str) -> bytes:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def issue_token(user_id: str) -> str:
    iat = int(time.time())
    exp = iat + TOKEN_TTL
    payload = _b64url(f"{user_id}.{iat}.{exp}".encode("utf-8"))
    # 防篡改：对 user_id + payload 签名
    sig = hmac.new(_SECRET, f"{user_id}.{payload}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{user_id}.{payload}.{sig}"


def verify_token(token: str) -> Optional[str]:
    if not token or token.count(".") != 2:
        return None
    user_id, payload, sig = token.split(".")
    exp = None
    try:
        decoded = b64d(payload).decode("utf-8")
        # decoded = f"{uid}.{iat}.{exp}"
        parts = decoded.split(".")
        if len(parts) == 3:
            exp = int(parts[2])
    except Exception:
        return None
    expected = hmac.new(_SECRET, f"{user_id}.{payload}".encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    if exp is not None and int(time.time()) > exp:
        return None
    return user_id


def token_from_header(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    if authorization.startswith("Bearer "):
        return verify_token(authorization[7:].strip())
    return None


# --------------------------------------------------------------------------- #
# 账号表
# --------------------------------------------------------------------------- #
def _load_users() -> dict:
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


def _save_users(data: dict) -> None:
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


def _normalize(identifier: str) -> str:
    return identifier.strip().lower()


def create_user(identifier: str, password: str) -> Optional[str]:
    """注册。成功返回 user_id，账号已存在返回 None。"""
    ident = _normalize(identifier)
    data = _load_users()
    if ident in data["by_identifier"]:
        return None
    import uuid
    user_id = "u_" + uuid.uuid4().hex[:16]
    salt, pw_hash = hash_password(password)
    data["users"].append({
        "user_id": user_id,
        "identifier": ident,
        "salt": salt,
        "pw_hash": pw_hash,
        "created_at": int(time.time()),
    })
    data["by_identifier"][ident] = user_id
    _save_users(data)
    return user_id


def authenticate(identifier: str, password: str) -> Optional[str]:
    """校验凭证，成功返回 user_id，否则 None。"""
    ident = _normalize(identifier)
    data = _load_users()
    user_id = data["by_identifier"].get(ident)
    if not user_id:
        return None
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return None
    if verify_password(password, user["salt"], user["pw_hash"]):
        return user_id
    return None


def user_identifier(user_id: str) -> Optional[str]:
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    return user["identifier"] if user else None


def reset_password(identifier: str, new_password: str) -> bool:
    """本地重置密码（无邮箱验证，本机自助）。成功返回 True，账号不存在/密码过短返回 False。"""
    if not new_password or len(new_password) < 6:
        return False
    ident = _normalize(identifier)
    data = _load_users()
    user_id = data["by_identifier"].get(ident)
    if not user_id:
        return False
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return False
    salt, pw_hash = hash_password(new_password)
    user["salt"] = salt
    user["pw_hash"] = pw_hash
    user["updated_at"] = int(time.time())
    _save_users(data)
    return True
