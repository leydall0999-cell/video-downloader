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
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _base_dir() -> Path:
    # 覆盖位：smoke / E2E 测本进程可能用 PyInstaller 冻结，Path.home() 不一定
    # 跟随 HOME，因此提供 VDL_DATA_DIR 显式隔离数据目录（不设置则走平台默认）。
    override = os.environ.get("VDL_DATA_DIR", "").strip()
    if override:
        return Path(override)
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
def _rebuild_index(data: dict) -> dict:
    """由 users 列表重建 by_identifier 索引，避免索引与列表不同步导致登录失败。

    历史上曾因手动改文件 / 部分写入，使 by_identifier 少于 users，造成
    '账号或密码错误'（authenticate 反查不到 user_id）。此处做自愈。
    """
    bi: dict[str, str] = {}
    for u in data.get("users", []):
        ident = u.get("identifier")
        uid = u.get("user_id")
        if ident and uid:
            bi[ident] = uid
    data["by_identifier"] = bi
    return data


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
    # 自愈：补齐缺失/陈旧的 identifier 索引
    if len(data["by_identifier"]) != len(data["users"]):
        _rebuild_index(data)
    return data


def _save_users(data: dict) -> None:
    p = _users_path()
    # 写前重建索引，保证 by_identifier 与 users 始终一致
    _rebuild_index(data)
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


def create_user(identifier: str, password: str, is_admin: bool = False) -> Optional[str]:
    """注册。成功返回 user_id，账号已存在返回 None。

    is_admin 仅经后台显式提权（set_user_admin / ensure_superusers），常规自注册恒为 False。
    """
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
        "is_admin": bool(is_admin),
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


# --------------------------------------------------------------------------- #
# 超级用户（is_admin）：后台管理面板访问授权
# --------------------------------------------------------------------------- #
_SUPERUSER_CACHE: dict[str, Any] = {"ts": 0.0}


def user_is_admin(user_id: str) -> bool:
    """该账号是否被标记为超级用户（后台管理权限）。"""
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    return bool(user.get("is_admin", False)) if user else False


def set_user_admin(user_id: str, flag: bool) -> dict[str, Any]:
    """后台面板提权/降权。成功返回 ok=True 与当前 is_admin。"""
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    user["is_admin"] = bool(flag)
    user["updated_at"] = int(time.time())
    _save_users(data)
    return {"ok": True, "is_admin": bool(flag)}


def ensure_superusers() -> None:
    """Bootstrap 超级用户（幂等，带 60s 缓存避免频繁扫盘）。

    - 显式名单：~/.video-downloader/admin.json 的 admin_identifiers（列表），或环境变量
      VDL_ADMIN_IDENTIFIER（逗号分隔）。名单内账号登录后标记为 is_admin=True。
    - 无显式名单且当前无任何 is_admin 账号时，把最早注册的账号提升为超级用户
      （避免首次启动锁定；桌面端单机即机主本人）。

    提权为单向：本函数永不降级，降级须经后台面板 /api/admin/users/{id}/set-admin。
    """
    import json as _json
    now = time.time()
    data = _load_users()
    has_admin = any(u.get("is_admin", False) for u in data["users"])
    # 已有超管：60s 内不重复扫盘（显式名单变更最迟 60s 后生效，可接受）
    if has_admin and (now - _SUPERUSER_CACHE["ts"] < 60):
        return

    idents: list[str] = []
    env_ids = os.environ.get("VDL_ADMIN_IDENTIFIER", "")
    if env_ids:
        for x in env_ids.split(","):
            x = x.strip().lower()
            if x:
                idents.append(x)
    try:
        p = _base_dir() / "admin.json"
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                cfg = _json.load(f)
            if isinstance(cfg, dict):
                for x in (cfg.get("admin_identifiers") or []):
                    x = str(x).strip().lower()
                    if x:
                        idents.append(x)
    except Exception:
        pass

    explicit = bool(idents)
    changed = False
    if explicit:
        for u in data["users"]:
            if u.get("identifier", "").strip().lower() in idents and not u.get("is_admin", False):
                u["is_admin"] = True
                changed = True
    else:
        # 无显式名单：尚无任何超管时，提升最早注册账号
        if data["users"] and not has_admin:
            earliest = min(data["users"], key=lambda u: u.get("created_at", 0))
            earliest["is_admin"] = True
            changed = True
    if changed:
        _save_users(data)
    _SUPERUSER_CACHE["ts"] = now


# --------------------------------------------------------------------------- #
# 找回密码：邮箱/手机验证码（V1 用 dev 模式本地投递，后续可切真实网关）
# --------------------------------------------------------------------------- #
RESET_CODE_TTL = 5 * 60  # 5 分钟有效
_RESEND_COOLDOWN = 60    # 重发冷却（秒）
_RESET_CODES: dict[str, dict] = {}   # ident -> {"code": str, "exp": float, "last": float}
_RESET_LOCK = threading.Lock()


def _send_mode() -> str:
    """验证码投递模式：显式 VDL_SEND_MODE 优先；未设置时存在 smtp.json 即自动切 smtp，否则 dev。

    - dev：本地测试（日志 + 返回 dev_code）
    - smtp：真实邮件投递（已接入，依赖 ~/.video-downloader/smtp.json）
    """
    env = os.environ.get("VDL_SEND_MODE")
    if env:
        return env.lower()
    if _smtp_accounts() is not None:
        return "smtp"
    return "dev"


def generate_reset_code(identifier: str) -> Optional[str]:
    """为存在的账号生成 6 位验证码；账号不存在返回 None。V1 不暴露账号是否真实存在。"""
    ident = _normalize(identifier)
    data = _load_users()
    if ident not in data["by_identifier"]:
        return None
    import secrets as _s
    code = f"{_s.randbelow(1_000_000):06d}"
    now = time.time()
    with _RESET_LOCK:
        _RESET_CODES[ident] = {"code": code, "exp": now + RESET_CODE_TTL, "last": now}
    return code


def reset_code_cooldown_ok(identifier: str) -> bool:
    """是否可发送（冷却期内不可）。"""
    ident = _normalize(identifier)
    with _RESET_LOCK:
        rec = _RESET_CODES.get(ident)
        if not rec:
            return True
        return (time.time() - rec.get("last", 0)) >= _RESEND_COOLDOWN


def verify_reset_code(identifier: str, code: str) -> bool:
    """校验验证码；正确则一次性作废（防重放）。"""
    ident = _normalize(identifier)
    with _RESET_LOCK:
        rec = _RESET_CODES.get(ident)
        if not rec:
            return False
        if time.time() > rec["exp"]:
            _RESET_CODES.pop(ident, None)
            return False
        ok = hmac.compare_digest(rec["code"], (code or "").strip())
        if ok:
            _RESET_CODES.pop(ident, None)  # 一次性
        return ok


def _smtp_accounts() -> Optional[list]:
    """读取 SMTP 账户配置（~/.video-downloader/smtp.json，0600）。缺文件返回 None。

    支持两种格式（自动兼容）：
    1. 单账户（旧）：直接是 {host, user, ...} 对象 → 当作唯一账户（default）。
    2. 多账户（新）：{"accounts": [ {...}, {...} ]}，每个账户可选
       match_domains（收件域路由，如 ["qq.com","foxmail.com"]）与 default（兜底）。
    非 dict 或无可解析账户返回 None。
    """
    p = _base_dir() / "smtp.json"
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if "accounts" in data and isinstance(data["accounts"], list):
        return [a for a in data["accounts"]
                if isinstance(a, dict) and a.get("host") and a.get("user")]
    # 单账户兼容
    if data.get("host") and data.get("user"):
        return [data]
    return None


def _select_smtp_account(to_email: str) -> Optional[dict]:
    """按收件人域名选择 SMTP 账户。优先 match_domains 命中，其次 default，否则第一个。"""
    accounts = _smtp_accounts()
    if not accounts:
        return None
    domain = ""
    if to_email and "@" in to_email:
        domain = to_email.split("@", 1)[1].strip().lower()
    for acc in accounts:
        doms = acc.get("match_domains") or []
        doms = [d.strip().lower() for d in doms]
        if domain and domain in doms:
            return acc
    for acc in accounts:
        if acc.get("default"):
            return acc
    return accounts[0]


def _send_smtp(to_email: str, code: str) -> None:
    """通过 SMTP 发送验证码邮件（仅标准库，无第三方依赖）。

    按收件人域名自动选择对应 SMTP 账户（QQ 收件→QQ 发件、Gmail 收件→Gmail 发件等）。
    多账户配置见 ~/.video-downloader/smtp.json（{"accounts": [...]}，每个账户可带 match_domains）。
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.utils import formataddr, formatdate

    cfg = _select_smtp_account(to_email)
    if not cfg or not cfg.get("host") or not cfg.get("user"):
        raise RuntimeError("SMTP 未配置：缺少 ~/.video-downloader/smtp.json 或对应账户")
    host = cfg["host"]
    use_ssl = bool(cfg.get("use_ssl", int(cfg.get("port", 0)) == 465))
    port = int(cfg.get("port", 465 if use_ssl else 587))
    user = cfg["user"]
    password = cfg.get("pass", "")
    from_addr = cfg.get("from") or user
    from_name = cfg.get("from_name") or "VideoDownloader"
    use_tls = bool(cfg.get("use_tls", not use_ssl))

    subject = "VideoDownloader 密码重置验证码"
    body = (
        "您正在重置 VideoDownloader 账号密码。\n\n"
        f"验证码：{code}\n"
        "该验证码 5 分钟内有效，且仅可使用一次。\n\n"
        "若非本人操作，请忽略此邮件。"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                s.login(user, password)
                s.sendmail(from_addr, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                if use_tls:
                    s.starttls()
                s.login(user, password)
                s.sendmail(from_addr, [to_email], msg.as_string())
    except Exception as e:  # noqa: BLE001
        logging.getLogger("vdl.auth").error("SMTP 发送失败 to=%s: %s", to_email, e)
        raise


def deliver_reset_code(identifier: str, code: str) -> None:
    """投递验证码。dev=本地调试展示（日志 + 写文件）；smtp=真实邮件投递。"""
    mode = _send_mode()
    if mode == "dev":
        logging.getLogger("vdl.auth").info("V1 dev 验证码 for %s: %s", identifier, code)
        try:
            with open(_base_dir() / "reset_code.dev.log", "a", encoding="utf-8") as f:
                f.write(f"{int(time.time())} {identifier} {code}\n")
        except OSError:
            pass
    elif mode == "smtp":
        _send_smtp(identifier, code)
    elif mode == "sms":
        # TODO: 真实短信投递（Twilio / 阿里云短信 / 火山短信）
        raise NotImplementedError("SMS 投递未接入")
    else:
        raise NotImplementedError(f"未知投递模式: {mode}")
