#!/usr/bin/env python3
"""VDL 授权中心（账号制 · 零依赖，仅标准库）。

定位：授权决策的唯一真源。App 端不做卡密验签、不做设备判定，只保留云端签发的
登录 token 和本机设备标识；密码哈希、卡密签名密钥都只留在本服务。

== 为什么从「一机一码」改成「账号 + 设备配额」 ==
旧版把会员绑死在某台机器的指纹上：换硬盘/系统重装/换 Mac 就要人工解绑，用户抱怨
「卡得太死」。新语义：
  · 卡密从「绑机器」改成「绑账号」：一次充值，账号终身有效，换机器只要重新登录。
  · 登录是唯一凭证：本机不再拦截设备指纹变化（换个 Mac 直接登录即可）。
  · 限制改成「同一账号最多 N 台设备同时在线」（默认 2，可用环境变量调）。
  · 超过配额时**不拒绝新设备**，而是自动淘汰「最久没活动」的那台，被淘汰的机器
    降级为免费档并提示重新登录（随时能挤回来）—— 宁可漏管一台，也不让用户被锁死。

== 卡密格式（沿用 P2，不变）==
VDL-<PLAN>-<RAND10HEX>-<SIG8HEX>；SIG = HMAC-SHA256(secret, "<PLAN>|<RAND10>")[:8]
secret 只存在环境变量 VDL_LICENSE_SECRET，**永不下发** ⇒ 卡密不可离线伪造。
区别：核销后记的是 bound_user（账号），不再是 bound_fp（机器）。

== 账号 ==
邮箱/手机号（lower+strip 做主键），PBKDF2-HMAC-SHA256 加盐哈希（20 万轮）。
登录 token = base64url("user_id|iat|exp|HMAC前32位")，30 天有效，可续登。

== 端点（全部 JSON，POST 除 healthz）==
  GET  /healthz
  POST /api/license/register  {email, password, device:{fp,name}}  注册并登录
  POST /api/license/login     {email, password, device:{fp,name}}  登录（占设备位）
  POST /api/license/heartbeat {token, fp}                          续期+查自己是否被挤出
  POST /api/license/redeem    {token, code}                        卡密充值到账号
  POST /api/license/devices   {token}                              我的设备列表
  POST /api/license/unbind    {token, fp}                          自己登出某台设备
  POST /api/license/check     {code, fingerprint}                  兼容旧客户端查卡密状态
  POST /api/license/gen       {plan, count, note?, token}          管理员生成卡密
  POST /api/license/revoke    {code, token}                        管理员作废卡密
  POST /api/license/grant     {email, plan, note?, token}          管理员直接给账号开套餐
  POST /api/license/users     {token}                              管理员账号列表

部署：香港机 /opt/vdl-license/，systemd vdl-license 监听 127.0.0.1:8902，
nginx `location ^~ /api/license/` 反代。数据 /opt/vdl-license/data/license_data.json
（旧版 cards.json 结构向后兼容，会自动并进来）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

# ── 配置 ──────────────────────────────────────────────────────────────────── #
SECRET = (os.environ.get("VDL_LICENSE_SECRET") or "").strip()
ADMIN_TOKEN = (os.environ.get("VDL_LICENSE_ADMIN_TOKEN") or "").strip()
PORT = int(os.environ.get("VDL_LICENSE_PORT") or "8902")
DATA_PATH = Path(os.environ.get("VDL_LICENSE_DATA")
                 or os.path.expanduser("~/.vdl-license/cards.json"))

MAX_DEVICES = int(os.environ.get("VDL_LICENSE_MAX_DEVICES") or "2")
if MAX_DEVICES < 1:
    MAX_DEVICES = 1
TOKEN_TTL = float(os.environ.get("VDL_LICENSE_TOKEN_TTL_DAYS") or "30") * 86400.0
PBKDF2_ITERS = 200_000
MIN_PASSWORD = 6

# 套餐短码 → App 侧 membership 套餐 code（唯一映射，两端共同语义）
PLAN_MAP: dict[str, str] = {
    "DLM":  "download_month",
    "DLH":  "download_half_year",
    "DLY":  "download_year",
    "AIM5": "ai_5500",
    "AM15": "ai_15000",
    "CP5K": "credits_5000",
    "CK15": "credits_15000",
}
PLAN_CODE_TO_SHORT = {v: k for k, v in PLAN_MAP.items()}

CODE_RE = re.compile(r"^VDL-([A-Z0-9]{3,4})-([0-9A-Fa-f]{10})-([0-9A-Fa-f]{8})$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# 🔴 2026-09-22：账号主键过去**只认邮箱**（register 里硬判 "@" in uid），而 App 本地
#    账号表与前端校验一直是「邮箱或手机号」都能注册。结果用手机号注册的用户：
#    云端 400 BAD_EMAIL → 前端注册流程静默忽略该失败、继续本地注册成功 →
#    用户看到「注册成功」，但云端从未建号（会员权益只落本机，换机即丢），
#    且此后每次登录云端都回 NO_ACCOUNT「账号不存在」误导用户。
#    此处与 server/routers/auth.py 的正则保持一致，两端同口径。
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")          # 中国大陆手机号
E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")       # 国际格式（+ 开头）


def _valid_account_id(uid: str) -> bool:
    """账号主键合法性：邮箱 或 手机号（与 App 本地账号表同口径）。"""
    if not uid:
        return False
    if "@" in uid:
        return bool(EMAIL_RE.match(uid))
    return bool(PHONE_RE.match(uid) or E164_RE.match(uid))

_THROTTLE: dict[str, deque] = {}
_THROTTLE_GUARD = threading.Lock()
_THROTTLE_LIMIT = 60
_THROTTLE_WINDOW = 60.0


class ApiError(Exception):
    def __init__(self, status: int, code: str, msg: str):
        super().__init__(msg)
        self.status, self.code, self.msg = status, code, msg


# ── 存储原子写 ─────────────────────────────────────────────────────────────── #
_LOCK = threading.Lock()


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_name(DATA_PATH.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, DATA_PATH)


# ── 基础工具 ──────────────────────────────────────────────────────────────── #
def _b64u(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode("ascii")


def _unb64u(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii")).decode("utf-8")


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), PBKDF2_ITERS)
    return salt, dk.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), PBKDF2_ITERS)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), (hash_hex or "").lower())


def make_token(user_id: str, secret: str, now: float, ttl: float = TOKEN_TTL) -> str:
    iat, exp = int(now), int(now + ttl)
    raw = f"{user_id}|{iat}|{exp}"
    sig = hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), "sha256").hexdigest()[:32]
    return _b64u(f"{raw}|{sig}")


def parse_token(token: str, secret: str, now: Optional[float] = None) -> str:
    """校验 token 并返回 user_id；失败抛 ApiError(BAD_TOKEN)。"""
    now = time.time() if now is None else now
    if not token or not secret:
        raise ApiError(401, "BAD_TOKEN", "登录已失效，请重新登录")
    try:
        raw = _unb64u(token)
        uid, iat, exp, sig = raw.split("|", 3)
    except Exception:
        raise ApiError(401, "BAD_TOKEN", "登录已失效，请重新登录")
    expect = hmac.new(secret.encode("utf-8"),
                      f"{uid}|{iat}|{exp}".encode("utf-8"), "sha256").hexdigest()[:32]
    if not hmac.compare_digest(expect, sig):
        raise ApiError(401, "BAD_TOKEN", "登录已失效，请重新登录")
    try:
        if float(exp) < now:
            raise ApiError(401, "TOKEN_EXPIRED", "登录已过期，请重新登录")
    except ValueError:
        raise ApiError(401, "BAD_TOKEN", "登录已失效，请重新登录")
    return uid


def _norm_id(email: str) -> str:
    """账号主键：去空白 + 小写（邮箱大小写不敏感）。"""
    return (email or "").strip().lower()


def _users(state: dict[str, Any]) -> dict[str, Any]:
    return state.setdefault("users", {})


def _public_user(user: dict[str, Any], fp: str = "") -> dict[str, Any]:
    devs = []
    for d in (user.get("devices") or []):
        devs.append({
            "fp": d.get("fp", ""),
            "name": d.get("name", ""),
            "last_seen": float(d.get("last_seen", 0)),
            "current": bool(fp) and d.get("fp") == fp,
        })
    devs.sort(key=lambda x: x["last_seen"], reverse=True)
    return {
        "user_id": user.get("user_id", ""),
        "email": user.get("email", ""),
        "devices": devs,
        "max_devices": MAX_DEVICES,
        "purchases": [
            {"id": p.get("id", ""), "plan_code": p.get("plan_code", ""),
             "at": float(p.get("at", 0))}
            for p in (user.get("purchases") or [])
        ],
    }


# ── 账号 & 设备配额（纯函数，便于单测）───────────────────────────────────────── #
def register_impl(state: dict[str, Any], email: str, password: str, now: float,
                  secret: str, device: Optional[dict] = None) -> dict[str, Any]:
    if not secret:
        raise ApiError(500, "NO_SECRET", "服务端未配置 VDL_LICENSE_SECRET")
    uid = _norm_id(email)
    if not _valid_account_id(uid):
        raise ApiError(400, "BAD_ACCOUNT", "请输入有效的邮箱或手机号")
    if len(password or "") < MIN_PASSWORD:
        raise ApiError(400, "WEAK_PASSWORD", f"密码至少 {MIN_PASSWORD} 位")
    users = _users(state)
    if uid in users:
        raise ApiError(409, "EXISTS", "该账号已存在，请直接登录")
    salt, pw_hash = hash_password(password)
    users[uid] = {
        "user_id": uid, "email": uid, "salt": salt, "pw_hash": pw_hash,
        "created_at": now, "devices": [], "purchases": [],
    }
    out = login_impl(state, uid, password, now, secret, device)
    out["registered"] = True
    return out


def _attach_device(user: dict, device: Optional[dict], now: float) -> list[dict]:
    """占一个设备位。返回被自动淘汰的设备列表（可能为空）。"""
    dev = device or {}
    fp = str(dev.get("fp") or "").strip()[:128]
    name = str(dev.get("name") or "").strip()[:64] or "未命名设备"
    devices = user.setdefault("devices", [])
    for d in devices:
        if d.get("fp") == fp:                      # 本机已在位 → 只刷新活跃时间
            d["last_seen"] = now
            d["name"] = name or d.get("name", "")
            return []
    evicted: list[dict] = []
    while len(devices) >= MAX_DEVICES:             # 超配额：挤掉最久没活动的
        oldest = min(devices, key=lambda x: float(x.get("last_seen", 0)))
        devices.remove(oldest)
        evicted.append(oldest)
    devices.append({"fp": fp, "name": name, "last_seen": now})
    return evicted


def login_impl(state: dict[str, Any], email: str, password: str, now: float,
               secret: str, device: Optional[dict] = None) -> dict[str, Any]:
    if not secret:
        raise ApiError(500, "NO_SECRET", "服务端未配置 VDL_LICENSE_SECRET")
    uid = _norm_id(email)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请先注册")
    if not verify_password(password or "", user.get("salt", ""), user.get("pw_hash", "")):
        raise ApiError(401, "BAD_PASSWORD", "密码不正确")
    fp = str((device or {}).get("fp") or "").strip()
    evicted = _attach_device(user, device, now)
    user["last_login"] = now
    out = {"ok": True, "token": make_token(uid, secret, now),
           "account": _public_user(user, fp)}
    if evicted:
        out["evicted"] = [{"fp": d.get("fp", ""), "name": d.get("name", "")}
                          for d in evicted]
    return out


def heartbeat_impl(state: dict[str, Any], token: str, fp: str, now: float,
                   secret: str) -> dict[str, Any]:
    """续期 + 查自己是否还在设备名单里（被别的机器挤掉则返回 DEVICE_EVICTED）。"""
    uid = parse_token(token, secret, now)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    fp = (fp or "").strip()
    devices = user.setdefault("devices", [])
    for d in devices:
        if d.get("fp") == fp:
            d["last_seen"] = now
            return {"ok": True, "account": _public_user(user, fp)}
    return {"ok": False, "code": "DEVICE_EVICTED",
            "error": "该账号已在其他两台设备登录，如需使用请重新登录",
            "account": _public_user(user, "")}


def redeem_impl(state: dict[str, Any], token: str, code: str, now: float,
                secret: str) -> dict[str, Any]:
    """卡密充值到账号（不再绑机器）。同一张卡第二次用会拒绝。"""
    if not secret:
        raise ApiError(500, "NO_SECRET", "服务端未配置 VDL_LICENSE_SECRET")
    uid = parse_token(token, secret, now)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    short = verify_code(code, secret)
    plan_code = PLAN_MAP[short]
    cards = state.setdefault("cards", {})
    rec = cards.get(code) or {
        "plan": short, "plan_code": plan_code, "status": "unused",
        "bound_fp": "", "bound_at": 0.0, "created_at": now,
    }
    if rec.get("status") == "revoked":
        raise ApiError(403, "REVOKED", "卡密已被作废")
    if rec.get("status") == "used":
        raise ApiError(403, "USED", "卡密已被使用过")
    rec.update({"status": "used", "bound_user": uid, "bound_at": now, "plan": short,
                "plan_code": plan_code})
    cards[code] = rec
    pid = secrets.token_hex(6)
    user.setdefault("purchases", []).append(
        {"id": pid, "plan_code": plan_code, "at": now})
    return {"ok": True, "plan_code": plan_code, "purchase_id": pid,
            "account": _public_user(user)}


def devices_impl(state: dict[str, Any], token: str, secret: str,
                 fp: str = "") -> dict[str, Any]:
    """设备列表。带 fp 时标出哪一台是当前设备（前端显示「本机」用）。"""
    uid = parse_token(token, secret)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    return {"ok": True, "account": _public_user(user, (fp or "").strip())}


def unbind_impl(state: dict[str, Any], token: str, fp: str,
                secret: str) -> dict[str, Any]:
    uid = parse_token(token, secret)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    devices = user.setdefault("devices", [])
    before = len(devices)
    user["devices"] = [d for d in devices if d.get("fp") != (fp or "").strip()]
    if len(user["devices"]) == before:
        raise ApiError(404, "NOT_FOUND", "该设备不在列表中")
    return {"ok": True, "account": _public_user(user)}


def grant_impl(state: dict[str, Any], email: str, plan: str, now: float,
               note: str = "") -> dict[str, Any]:
    """管理员直接给账号开套餐（补发货 / 不用卡密的场景）。"""
    plan_code = PLAN_MAP.get(plan) or (plan if plan in PLAN_MAP.values() else "")
    if not plan_code:
        raise ApiError(400, "BAD_PLAN", f"未知套餐: {plan}")
    uid = _norm_id(email)
    users = _users(state)
    user = users.get(uid)
    if not user:
        user = users[uid] = {
            "user_id": uid, "email": uid, "salt": "", "pw_hash": "",
            "created_at": now, "devices": [], "purchases": [], "no_password": True,
        }
    pid = secrets.token_hex(6)
    user.setdefault("purchases", []).append(
        {"id": pid, "plan_code": plan_code, "at": now, "note": note[:120]})
    return {"ok": True, "user_id": uid, "plan_code": plan_code, "purchase_id": pid,
            "account": _public_user(user)}


def users_impl(state: dict[str, Any]) -> dict[str, Any]:
    users = _users(state)
    out = []
    for uid, u in users.items():
        item = _public_user(u)
        item["last_login"] = float(u.get("last_login", 0))
        item["purchases"] = [
            {"id": p.get("id"), "plan_code": p.get("plan_code"), "at": p.get("at")}
            for p in (u.get("purchases") or [])
        ]
        out.append(item)
    out.sort(key=lambda x: x["last_login"], reverse=True)
    return {"ok": True, "users": out, "count": len(out)}


# ── 卡密（沿用 P2 的签名机制，只是不再写 bound_fp）──────────────────────────── #
def _sign(plan_short: str, rand: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), f"{plan_short}|{rand}".encode("utf-8"),
                    "sha256").hexdigest()[:8]


def gen_code(plan_code: str, secret: str, rng: Any = None) -> str:
    short = PLAN_CODE_TO_SHORT.get(plan_code)
    if not short:
        raise ValueError(f"未知套餐: {plan_code}")
    rng = rng or secrets
    rand = rng.token_hex(5)
    return f"VDL-{short}-{rand}-{_sign(short, rand, secret)}"


def verify_code(code: str, secret: str) -> str:
    """验签名并返回套餐短码。格式/签名不符抛 ApiError(BAD_CODE)。

    容忍用户手输大小写混杂：短码统一大写、随机段/签名统一小写后再比对。
    """
    m = CODE_RE.match((code or "").strip())
    if not m:
        raise ApiError(400, "BAD_CODE", "卡密格式不正确")
    short, rand, sig = m.groups()
    short, rand, sig = short.upper(), rand.lower(), sig.lower()
    if not hmac.compare_digest(_sign(short, rand, secret), sig):
        raise ApiError(400, "BAD_CODE", "卡密签名校验失败")
    if short not in PLAN_MAP:
        raise ApiError(400, "BAD_CODE", f"未知套餐短码: {short}")
    return short


def check_impl(state: dict[str, Any], code: str, fp: str = "") -> dict[str, Any]:
    """兼容旧客户端：查卡密是否被作废。不再做「机器是否匹配」判定。"""
    rec = (state.get("cards") or {}).get((code or "").strip())
    if not rec:
        return {"ok": True, "known": False, "status": "unknown"}
    return {"ok": True, "known": True, "status": rec.get("status"),
            "plan_code": rec.get("plan_code"), "matches": True}


def revoke_impl(state: dict[str, Any], code: str, now: float) -> dict[str, Any]:
    rec = (state.get("cards") or {}).get((code or "").strip())
    if not rec:
        raise ApiError(404, "NOT_FOUND", "卡密不存在")
    rec["status"] = "revoked"
    rec["revoked_at"] = now
    return {"ok": True, "code": code, "status": "revoked"}


# ── 限流 ───────────────────────────────────────────────────────────────────── #
def _throttled(ip: str, now: float) -> bool:
    with _THROTTLE_GUARD:
        dq = _THROTTLE.setdefault(ip, deque())
        while dq and now - dq[0] > _THROTTLE_WINDOW:
            dq.popleft()
        if len(dq) >= _THROTTLE_LIMIT:
            return True
        dq.append(now)
        return False


# ── HTTP 层 ────────────────────────────────────────────────────────────────── #
class Handler(BaseHTTPRequestHandler):
    server_version = "VDLLicense/2.0"

    def log_message(self, fmt, *args):  # noqa: N802
        sys.stderr.write("[license] %s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 64 * 1024:
            raise ApiError(400, "BAD_BODY", "请求体为空或过大")
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "BAD_BODY", "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "BAD_BODY", "请求体必须是 JSON 对象")
        return data

    def _route(self, method: str) -> None:
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if method == "GET":
                if path.endswith("healthz"):
                    return self._json(200, {"status": "ok", "service": "vdl-license",
                                            "max_devices": MAX_DEVICES})
                return self._json(404, {"ok": False, "error": "not found"})
            if not path.startswith("/api/license/"):
                return self._json(404, {"ok": False, "error": "not found"})
            ip = self.client_address[0]
            now = time.time()
            data = self._body()
            action = path.rsplit("/", 1)[-1]

            # --- 管理员接口（不做 IP 限流，走 token）---
            if action in ("gen", "revoke", "grant", "users"):
                self._require_admin(data)
                with _LOCK:
                    st = _load_state()
                    if action == "gen":
                        self._require_secret()
                        out = self._handle_gen(st, data, now)
                    elif action == "revoke":
                        out = revoke_impl(st, str(data.get("code") or ""), now)
                        _save_state(st)
                    elif action == "grant":
                        out = grant_impl(st, str(data.get("email") or ""),
                                         str(data.get("plan") or ""), now,
                                         str(data.get("note") or ""))
                        _save_state(st)
                    else:
                        out = users_impl(st)
                return self._json(200, out)

            # --- 用户接口 ---
            if _throttled(ip, now):
                return self._json(429, {"ok": False, "error": "请求过于频繁，稍后再试"})
            with _LOCK:
                st = _load_state()
                if action == "register":
                    out = register_impl(st, str(data.get("email") or ""),
                                        str(data.get("password") or ""), now,
                                        SECRET, self._device(data))
                    _save_state(st)
                elif action == "login":
                    out = login_impl(st, str(data.get("email") or ""),
                                     str(data.get("password") or ""), now,
                                     SECRET, self._device(data))
                    _save_state(st)
                elif action == "heartbeat":
                    out = heartbeat_impl(st, str(data.get("token") or ""),
                                         str(data.get("fp") or data.get("fingerprint") or ""),
                                         now, SECRET)
                    if out.get("ok"):
                        _save_state(st)
                elif action == "redeem":
                    out = redeem_impl(st, str(data.get("token") or ""),
                                      str(data.get("code") or "").strip(), now, SECRET)
                    _save_state(st)
                elif action == "devices":
                    if _throttled(ip, now):
                        return self._json(429, {"ok": False, "error": "请求过于频繁，稍后再试"})
                    out = devices_impl(st, str(data.get("token") or ""), SECRET,
                                       str(data.get("fp") or data.get("fingerprint") or ""))
                elif action == "unbind":
                    out = unbind_impl(st, str(data.get("token") or ""),
                                      str(data.get("fp") or "").strip(), SECRET)
                    _save_state(st)
                elif action == "check":
                    out = check_impl(st, str(data.get("code") or ""),
                                     str(data.get("fingerprint") or ""))
                else:
                    return self._json(404, {"ok": False, "error": "unknown action"})
            return self._json(200, out)
        except ApiError as e:
            return self._json(e.status, {"ok": False, "error": e.msg, "code": e.code})

    @staticmethod
    def _device(data: dict[str, Any]) -> dict[str, str]:
        d = data.get("device")
        if not isinstance(d, dict):
            d = {}
            if isinstance(data.get("fingerprint"), str):
                d = {"fp": data["fingerprint"]}
        fp = str(d.get("fp") or "").strip()[:128]
        name = str(d.get("name") or "").strip()[:64]
        if not fp:
            raise ApiError(400, "NO_DEVICE", "缺少设备标识 fingerprint")
        return {"fp": fp, "name": name or "未命名设备"}

    def _require_admin(self, data: dict[str, Any]) -> None:
        if not ADMIN_TOKEN:
            raise ApiError(500, "NO_ADMIN_TOKEN", "服务端未配置 VDL_LICENSE_ADMIN_TOKEN")
        tok = str(data.get("token") or "")
        if not hmac.compare_digest(tok, ADMIN_TOKEN):
            raise ApiError(403, "FORBIDDEN", "管理员 token 不正确")

    def _require_secret(self) -> None:
        if not SECRET:
            raise ApiError(500, "NO_SECRET", "服务端未配置 VDL_LICENSE_SECRET")

    def _handle_gen(self, st: dict[str, Any], data: dict[str, Any], now: float) -> dict:
        plan = str(data.get("plan") or "").strip()
        try:
            count = int(data.get("count") or 1)
        except (TypeError, ValueError):
            count = 0
        if not 1 <= count <= 500:
            raise ApiError(400, "BAD_COUNT", "count 取值 1~500")
        note = str(data.get("note") or "")[:120]
        cards = st.setdefault("cards", {})
        codes: list[str] = []
        for _ in range(count):
            c = gen_code(plan, SECRET)
            cards[c] = {"plan": c.split("-")[1], "plan_code": PLAN_MAP[c.split("-")[1]],
                        "status": "unused", "bound_fp": "", "bound_user": "",
                        "bound_at": 0.0, "created_at": now, "note": note}
            codes.append(c)
        _save_state(st)
        return {"ok": True, "codes": codes, "plan": plan, "count": len(codes)}

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")


def main() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[license] listening on 127.0.0.1:{PORT}  data={DATA_PATH}"
          f"  max_devices={MAX_DEVICES}"
          f"  secret={'set' if SECRET else 'MISSING'}"
          f"  admin={'set' if ADMIN_TOKEN else 'MISSING'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
