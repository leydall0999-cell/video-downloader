"""server/baidu_cred_store.py — 网页版百度网盘登录态存储（存服务器，按用户隔离）。

用户 2026-10-25 拍板：网页版的百度登录态也存到服务器上，填一次以后不用再填。

存的是 **BDUSS 票据**（不是账号密码）：一次登录长期有效，用起来和存密码一样，
但密码不落在任何地方 —— 泄露面小得多，也没必要把密码交给服务器。

落盘形态 = 加密。密钥取环境变量 VDL_BAIDU_SECRET（缺省回落 VDL_LICENSE_SECRET），
经 PBKDF2-HMAC-SHA256 派生出 32 字节主密钥，用 AES-256-GCM 加密；每个用户的密文
绑定自己的 uid 作 AAD ⇒ 密文无法在用户之间搬运复用。**没有密钥就拒绝落盘**
（绝不写明文，宁可功能不可用）。

文件：{VDL_DATA_DIR}/baidu_credentials.json，权限 600。

另提供 seal_token/open_token：给「直链」签发不透明令牌用（直链本身不外泄给浏览器，
只有本人拿着令牌才能让服务器去取文件）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AAD 前缀：不同用途用不同前缀，避免密文跨用途挪用
_AAD_CRED = b"vdl:baidu-cred:"
_AAD_TOKEN = b"vdl:baidu-token:"

_PBKDF2_ITERS = 120_000


class CredentialError(RuntimeError):
    """凭据无法安全落盘（缺密钥等）。上层据此给出可读提示。"""


def _base_dir() -> Path:
    """数据目录：VDL_DATA_DIR 优先（测试隔离），否则沿用 auth_store 的约定。"""
    override = os.environ.get("VDL_DATA_DIR", "").strip()
    if override:
        return Path(override)
    try:
        from auth_store import _base_dir as _bd
        return _bd()
    except Exception:  # noqa: BLE001
        return Path.home() / ".video-downloader"


def _path() -> Path:
    return _base_dir() / "baidu_credentials.json"


def _secret() -> bytes:
    """取加密密钥。找不到就抛错 —— 绝不退化成明文存储。"""
    for name in ("VDL_BAIDU_SECRET", "VDL_LICENSE_SECRET"):
        val = (os.environ.get(name) or "").strip()
        if val:
            return val.encode("utf-8")
    raise CredentialError(
        "服务端未配置 VDL_BAIDU_SECRET，无法安全保存百度登录态（已拒绝以明文落盘）"
    )


_MK_CACHE: dict = {}


def _master_key() -> bytes:
    """由密钥派生主密钥（固定 salt：数据文件本身已 600 + 加密，不需要每文件 salt）。

    ⚠️ 必须缓存：PBKDF2 12 万次迭代单次约 80ms，而文件夹分享一次解析要给
    **每个文件**签一个直链令牌（2026-10-27 实测 201 个文件 ⇒ 不缓存时
    光这一步就 16 秒，整个解析 21~30 秒；缓存后回到 5 秒内）。
    """
    secret = _secret()
    key = _MK_CACHE.get(secret)
    if key is None:
        key = hashlib.pbkdf2_hmac("sha256", secret, b"vdl-baidu-cred-v1", _PBKDF2_ITERS, dklen=32)
        _MK_CACHE.clear()          # 只驻留当前密钥，密钥轮换后旧的自动丢弃
        _MK_CACHE[secret] = key
    return key


def _aad(uid: str, prefix: bytes) -> bytes:
    return prefix + (uid or "").encode("utf-8")


def _encrypt(uid: str, plaintext: str, prefix: bytes) -> str:
    key = _master_key()
    nonce = secrets.token_bytes(12)
    blob = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), _aad(uid, prefix))
    return base64.urlsafe_b64encode(nonce + blob).decode("ascii")


def _decrypt(uid: str, token: str, prefix: bytes) -> str:
    raw = base64.urlsafe_b64decode((token or "").encode("ascii"))
    nonce, blob = raw[:12], raw[12:]
    key = _master_key()
    return AESGCM(key).decrypt(nonce, blob, _aad(uid, prefix)).decode("utf-8")


# ——————————————————————————————————————————————————————————————
# 落盘读写
# ——————————————————————————————————————————————————————————————

def _read_all() -> dict[str, Any]:
    p = _path()
    try:
        obj = json.loads(p.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — 文件不存在/损坏都当作没有凭据
        return {}
    return obj if isinstance(obj, dict) else {}


def _write_all(obj: dict[str, Any]) -> None:
    from atomic_io import atomic_write_json, mutation
    p = _path()
    with mutation(p):
        atomic_write_json(p, obj, mode=0o600)


def _normalize(bduss: str) -> str:
    """接受完整 Cookie 头或裸 BDUSS 值，统一成合法 Cookie 头。"""
    val = (bduss or "").strip()
    if not val:
        return ""
    if "=" not in val:
        val = f"BDUSS={val}"
    return val


# ——————————————————————————————————————————————————————————————
# 对外接口
# ——————————————————————————————————————————————————————————————

def save_bduss(uid: str, bduss: str) -> str:
    """保存某个用户的百度登录态。返回归一化后的 Cookie 头。

    - 接受完整 Cookie 头（`BDUSS=xxx; BAIDUID=yyy`）或裸 BDUSS 值
    - 空值 ⇒ ValueError；缺密钥 ⇒ CredentialError（不落明文）
    """
    if not uid:
        raise ValueError("缺少用户标识")
    val = _normalize(bduss)
    if not val:
        raise ValueError("登录态不能为空")
    ct = _encrypt(uid, val, _AAD_CRED)   # 缺密钥在这一步抛 CredentialError
    obj = _read_all()
    users = obj.get("users")
    if not isinstance(users, dict):
        users = {}
    users[uid] = {"ct": ct, "ts": int(time.time())}
    obj["v"] = 1
    obj["users"] = users
    _write_all(obj)
    return val


def load_bduss(uid: str) -> str:
    """取某个用户的百度登录态（空串表示没有 / 解不开）。"""
    if not uid:
        return ""
    obj = _read_all()
    users = obj.get("users")
    if not isinstance(users, dict):
        return ""
    rec = users.get(uid)
    if not isinstance(rec, dict):
        return ""
    try:
        return _decrypt(uid, str(rec.get("ct") or ""), _AAD_CRED)
    except Exception:  # noqa: BLE001 — 密钥换过/密文损坏：当作没有，别抛给用户
        return ""


def has_bduss(uid: str) -> bool:
    return bool(load_bduss(uid))


def clear_bduss(uid: str) -> bool:
    """清除某个用户的登录态。返回是否真的清掉了。"""
    if not uid:
        return False
    obj = _read_all()
    users = obj.get("users")
    if not isinstance(users, dict) or uid not in users:
        return False
    users.pop(uid, None)
    obj["users"] = users
    _write_all(obj)
    return True


# ——————————————————————————————————————————————————————————————
# 直链令牌（不把 dlink 交给浏览器）
# ——————————————————————————————————————————————————————————————

def seal_token(uid: str, plaintext: str) -> str:
    """把直链封成不透明令牌（绑定 uid，别人拿自己的令牌解不开）。"""
    if not plaintext:
        return ""
    return _encrypt(uid, plaintext, _AAD_TOKEN)


def open_token(uid: str, token: str) -> str:
    """解令牌。失败返回空串（不抛，避免把内部细节暴露给前端）。"""
    if not uid or not token:
        return ""
    try:
        return _decrypt(uid, token, _AAD_TOKEN)
    except Exception:  # noqa: BLE001
        return ""


def configured() -> bool:
    """服务端是否具备安全保存凭据的能力（用于前端提示「暂不可用」）。"""
    try:
        _secret()
        return True
    except CredentialError:
        return False


def peek_secret_len() -> int:
    """仅供自检：密钥长度（不返回内容）。"""
    try:
        return len(_secret())
    except CredentialError:
        return 0
