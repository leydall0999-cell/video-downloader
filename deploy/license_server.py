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
  POST /api/license/spend     {token, items:[{pool,cost,op,id}]}   客户端积分扣减上报（幂等）
  POST /api/license/daily     {token, items:[{res,n,id}]}          客户端每日用量上报（幂等，两端共用配额）
  POST /api/license/ban       {email, banned, note?, token}        管理员封禁/解封账号
  POST /api/license/set-admin {email, is_admin, token}             管理员设置/取消超管（2026-10-16 is_admin 上云）
  POST /api/license/import-account {email, salt, pw_hash, created_at?, is_admin?,
                                    authority?, devices?, token}  存量账号全量搬迁（含权益/设备）
                                                                   存量账号搬迁：本机账号表记录原样搬入（密码无感）
  POST /api/license/delete-user {email, token}                    管理员删除账号（幂等，2026-10-16 测试号清理）
  POST /api/license/adjust    {email, pool, delta, note?, token}   管理员调整积分（可负）
  POST /api/license/setstate  {email, member_until_dl?, member_until_ai?, perm_credits?, ai_credits_left?, token}
                                                                   管理员设定权威基线（迁移/纠错）
  POST /api/license/usage     {email, token}                       管理员查权益与流水
  POST /api/license/recon     {days?, token}                       管理员每日入账/充值对账报告
                                                                   days 1~400（前端给 1/3/7/30/180/365）
                                                                   （后台另有 10 分钟自扫线程，差异自动告警）

  权益权威化（v1）：登录/心跳/充值响应的 account.authority 快照是会员到期与积分
  余额的唯一真源；客户端用它覆盖本地文件 —— 篡改 memberships/*.json 一联网即回滚。

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
# 安全审计 2026-10-11（R-07）：管理员鉴权失败锁定。原实现只做 compare_digest、无失败
# 计数 —— 一旦 ADMIN_TOKEN 被在线爆破即完整接管授权中心。此处用**模块级**状态
# （BaseHTTPRequestHandler 每请求一个新实例，实例属性不跨请求）。
_ADMIN_MAX_FAILS = 10          # 连续失败上限
_ADMIN_LOCK_SECONDS = 300      # 触发后锁定时长（秒）
_ADMIN_GUARD_LOCK = threading.Lock()
_ADMIN_FAILS = 0
_ADMIN_LOCK_UNTIL = 0.0
PORT = int(os.environ.get("VDL_LICENSE_PORT") or "8902")
DATA_PATH = Path(os.environ.get("VDL_LICENSE_DATA")
                 or os.path.expanduser("~/.vdl-license/cards.json"))
EVENT_LOG_PATH = DATA_PATH.parent / "events.jsonl"   # 充值/消耗全量事件流水（审计+监控源）

MAX_DEVICES = int(os.environ.get("VDL_LICENSE_MAX_DEVICES") or "2")
if MAX_DEVICES < 1:
    MAX_DEVICES = 1
TOKEN_TTL = float(os.environ.get("VDL_LICENSE_TOKEN_TTL_DAYS") or "30") * 86400.0
PBKDF2_ITERS = 200_000
MIN_PASSWORD = 6

# 套餐短码 → App 侧 membership 套餐 code（唯一映射，两端共同语义）
PLAN_MAP: dict[str, str] = {
    "DL1":  "download_1day",
    "DL3":  "download_3day",
    "DL7":  "download_7day",
    "DLM":  "download_month",
    "DLH":  "download_half_year",
    "DLY":  "download_year",
    "AIM5": "ai_5500",
    "AM15": "ai_15000",
    "CP5K": "credits_5000",
    "CK15": "credits_15000",
}
PLAN_CODE_TO_SHORT = {v: k for k, v in PLAN_MAP.items()}

# 卡密通道下线开关（2026-09-26）：充值统一走支付宝/微信在线支付（pay_server 自动
# grant），补单走管理员 grant —— 卡密不再构成任何入账/发货通路。设 VDL_REDEEM_DISABLED=1
# 关闭核销（含「库外合法签名卡自动建卡」的旁路，防泄露卡密白嫖）；留空/0 = 可用（测试/应急）。
REDEEM_DISABLED = (os.environ.get("VDL_REDEEM_DISABLED") or "").strip().lower() not in ("", "0", "false")

# ── 套餐效果（2026-09-25 权益云端权威化）───────────────────────────────────── #
# 服务端按「事件推进」维护账号权益状态（与 App 端 activate() 同语义：
# 续费顺延 = max(now, 当前到期) + days），登录/心跳响应携带权威快照，
# 客户端用它覆盖本地 memberships/*.json —— 用户篡改本地文件一联网即回滚。
PLAN_EFFECT: dict[str, dict[str, Any]] = {
    "download_1day":      {"kind": "dl",   "days": 1},
    "download_3day":      {"kind": "dl",   "days": 3},
    "download_7day":      {"kind": "dl",   "days": 7},
    "download_month":     {"kind": "dl",   "days": 30},
    "download_half_year": {"kind": "dl",   "days": 180},
    "download_year":      {"kind": "dl",   "days": 365},
    "ai_5500":            {"kind": "ai",   "days": 30, "credits": 5500},
    "ai_15000":           {"kind": "ai",   "days": 30, "credits": 15000},
    "credits_5000":       {"kind": "pack", "credits": 5000},
    "credits_15000":      {"kind": "pack", "credits": 15000},
}
SPEND_LEDGER_CAP = 300     # 每账号积分流水保留条数（审计用，余额是独立累计字段）
SPEND_ID_CAP = 600         # 幂等 id 去重表容量
PURCHASE_LEDGER_CAP = 500

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
        "banned": bool(user.get("banned")),
        # 超管标记（2026-10-16 上云）：登录/心跳快照带出，各端以云端为权威，
        # 本机 auth_store 的 is_admin 退化为离线兜底（断网时门禁仍可用）。
        "is_admin": bool(user.get("is_admin")),
        # 权威权益快照（v1）：客户端 presence 检测后用它覆盖本地 memberships
        "authority": _authority_view(user, time.time()),
        "purchases": [
            {"id": p.get("id", ""), "plan_code": p.get("plan_code", ""),
             "at": float(p.get("at", 0))}
            for p in (user.get("purchases") or [])
        ],
    }


# ── 权益权威状态（2026-09-25 防破解：会员到期/积分余额以本服务为准）─────────── #
def _apply_plan_effect(user: dict[str, Any], plan_code: str, now: float) -> None:
    """把一次套餐/积分包购买推进账号的权威权益状态（与 App 端 activate 同语义）。"""
    eff = PLAN_EFFECT.get(plan_code)
    if not eff:
        return
    if eff["kind"] == "dl":
        cur = float(user.get("member_until_dl") or 0)
        user["member_until_dl"] = max(now, cur) + eff["days"] * 86400.0
    elif eff["kind"] == "ai":
        cur = float(user.get("member_until_ai") or 0)
        ai_until = max(now, cur) + eff["days"] * 86400.0
        user["member_until_ai"] = ai_until
        # App 端语义：AI 会员捆绑下载权益（下载覆盖到 AI 到期）
        if float(user.get("member_until_dl") or 0) < ai_until:
            user["member_until_dl"] = ai_until
        user["ai_grant_total"] = int(user.get("ai_grant_total") or 0) + int(eff["credits"])
    elif eff["kind"] == "pack":
        user["perm_credits"] = int(user.get("perm_credits") or 0) + int(eff["credits"])
    user["authority_init"] = True


def _ensure_authority(user: dict[str, Any], now: float) -> None:
    """惰性迁移：老账号有 purchases 但从未算过权威状态 → 按 at 时序重放一次。

    重放结果与 App 端逐笔 activate 的本地累计一致（同一套 max(now,cur)+days 语义），
    因此已登录老用户首次心跳即完成迁移，本地权益不会被清零。
    """
    if user.get("authority_init"):
        return
    for p in sorted(user.get("purchases") or [], key=lambda x: float(x.get("at", 0))):
        _apply_plan_effect(user, str(p.get("plan_code") or ""), now)
    user["authority_init"] = True


def _authority_view(user: dict[str, Any], now: float) -> dict[str, Any]:
    """登录/心跳响应携带的权威快照（客户端 presence 检测 v>=1 才覆盖本地）。"""
    _ensure_authority(user, now)
    ai_active = float(user.get("member_until_ai") or 0) > now
    ai_left = 0
    if ai_active:
        ai_left = max(0, int(user.get("ai_grant_total") or 0) - int(user.get("ai_spent_total") or 0))
    # 账号级每日用量（北京时间日切）：同一账号在 App / 网页版共用同一份每日配额，
    # 本地计数只是预检缓存 —— 快照随心跳/上报响应下发，客户端 max(本地,云端) 合并。
    daily = user.get("daily") or {}
    today = _bj_day(now)
    usage: dict[str, int] = {}
    if str(daily.get("date") or "") == today:
        for k, v in (daily.get("counts") or {}).items():
            try:
                usage[str(k)[:32]] = int(v)
            except (TypeError, ValueError):
                continue
    return {
        "v": 1,
        "member_until_dl": float(user.get("member_until_dl") or 0),
        "member_until_ai": float(user.get("member_until_ai") or 0),
        "ai_credits_left": ai_left,
        "perm_credits": int(user.get("perm_credits") or 0),
        "banned": bool(user.get("banned")),
        "usage_date": today,
        "usage": usage,
        # 账号级云端免费额度（2026-10-06）：客户端据此回灌本机 quota.json 缓存，
        # 避免「重装系统 → 计数归零 → 又白嫖 3 次」。函数体在本文件更下方定义，
        # 运行期才求值（与 daily 同理），故这里可直接调用。
        "cloud_quota": _cloud_quota_view(user, now),
    }


def spend_impl(state: dict[str, Any], token: str, items: Any, now: float,
               secret: str, ip: str = "") -> dict[str, Any]:
    """客户端积分扣减上报（幂等）：永久积分直接扣余额，AI 积分记累计消耗。

    幂等键 id 由客户端生成（uuid），重复上报（断网重发）只记一次。
    余额允许扣成负数——正常客户端本地已做不足拒绝，负数即篡改/并发信号，供审计。
    """
    uid = parse_token(token, secret, now)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    if user.get("banned"):
        raise ApiError(403, "ACCOUNT_BANNED", "账号已被停用，如有疑问请联系客服")
    if not isinstance(items, list):
        raise ApiError(400, "BAD_BODY", "items 必须是数组")
    ids = user.setdefault("spend_ids", [])
    ledger = user.setdefault("spends", [])
    applied = 0
    for it in items[:50]:
        if not isinstance(it, dict):
            continue
        try:
            cost = int(it.get("cost") or 0)
        except (TypeError, ValueError):
            continue
        if cost <= 0:
            continue
        iid = str(it.get("id") or "").strip()[:64]
        if iid and iid in ids:
            continue                                   # 幂等：重发只记一次
        pool = str(it.get("pool") or "permanent")
        if pool not in ("permanent", "ai"):
            pool = "permanent"
        if pool == "permanent":
            user["perm_credits"] = int(user.get("perm_credits") or 0) - cost
        else:
            user["ai_spent_total"] = int(user.get("ai_spent_total") or 0) + cost
        applied += cost
        if iid:
            ids.append(iid)
        ledger.append({"op": str(it.get("op") or "")[:40], "pool": pool,
                       "cost": cost, "at": now, "id": iid})
    user["spends"] = ledger[-SPEND_LEDGER_CAP:]
    user["spend_ids"] = ids[-SPEND_ID_CAP:]
    if applied > 0:
        _log_event(state, "spend", now, email=uid, applied=applied)
        if int(user.get("perm_credits") or 0) < 0:
            _raise_alert(state, "negative_balance", "critical", uid, "",
                         f"永久积分余额为负（{user['perm_credits']}），疑似篡改/并发扣减", now)
    return {"ok": True, "applied": applied, "authority": _authority_view(user, now)}


def daily_impl(state: dict[str, Any], token: str, items: Any, now: float,
               secret: str, ip: str = "") -> dict[str, Any]:
    """客户端每日用量上报（幂等）：同一账号在 App / 网页版共用同一份每日配额。

    本地 use_daily 只是预检缓存；真实用量以本接口按账号累计为准（北京时间日切）。
    幂等键 id 由客户端生成（uuid），断网重发只计一次。响应带最新 authority 快照，
    客户端上报成功后立即校准本地计数。
    """
    uid = parse_token(token, secret, now)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    if user.get("banned"):
        raise ApiError(403, "ACCOUNT_BANNED", "账号已被停用，如有疑问请联系客服")
    if not isinstance(items, list):
        raise ApiError(400, "BAD_BODY", "items 必须是数组")
    ids = user.setdefault("usage_ids", [])
    today = _bj_day(now)
    daily = user.get("daily") or {}
    if str(daily.get("date") or "") != today:
        daily = {"date": today, "counts": {}}
    counts = daily.setdefault("counts", {})
    applied = 0
    for it in items[:50]:
        if not isinstance(it, dict):
            continue
        res = str(it.get("res") or "").strip()[:32]
        if not res:
            continue
        try:
            n = int(it.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        iid = str(it.get("id") or "").strip()[:64]
        if iid and iid in ids:
            continue                                   # 幂等：重发只计一次
        counts[res] = int(counts.get(res) or 0) + n
        applied += n
        if iid:
            ids.append(iid)
    user["daily"] = daily
    user["usage_ids"] = ids[-500:]
    if applied > 0:
        _log_event(state, "daily", now, email=uid, applied=applied)
    return {"ok": True, "applied": applied, "authority": _authority_view(user, now)}


def _trial_key(op: str, mode: str) -> str:
    """免费体验名额键：once 口径按账号计（"*"），per_op 口径按功能计。

    与两端 membership._trial_key 保持同一套语义，保证「本地键 == 云端键」。
    """
    return "*" if str(mode or "once") == "once" else str(op)


def trial_claim_impl(state: dict[str, Any], token: str, op: str, mode: str,
                     now: float, secret: str, ip: str = "") -> dict[str, Any]:
    """免费体验名额跨端原子领取：同一账号在 App / 网页版共享同一份「首免」名额。

    调用方（两端 `trial_consume`）在「本机积分不够、且本机试用名额仍显示可用」时，
    先来这里做一次**原子领取**：
      · 返回 already=False → 本端成功领取到全局唯一名额（放行、不扣积分）；
      · 返回 already=True  → 另一台设备/网页已领走（本端应拒绝，走 402）；
    断网 / 授权中心不可达由调用方按 fail-open 处理（视为本端领取）。

    额度领取是幂等的：同一 key 重复上报只记一次（无需客户端 id）。
    """
    uid = parse_token(token, secret, now)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    if user.get("banned"):
        raise ApiError(403, "ACCOUNT_BANNED", "账号已被停用，如有疑问请联系客服")
    key = _trial_key(str(op or ""), str(mode or "once"))
    trials = user.setdefault("free_trials", {})
    already = key in trials
    if not already:
        trials[key] = now
        _log_event(state, "trial_claim", now, email=uid, op=op, mode=mode)
    return {"ok": True, "claimed": True, "already": already,
            "authority": _authority_view(user, now)}


# 免费云端额度上限（账号级终身 / 每日 auto）。与两端 quota.py 的
# LIFETIME_CLOUD_EVENTS / DAILY_AUTO_RUNS 必须一致 —— 中心是唯一真源。
# 🔴 2026-10-06 拆池：8 个云端功能各自独立「终身免费次数」，每个功能独立计、独立 cap。
#   默认值仍各 3 次（与旧总池一致），只是按功能各一份。
#   CLOUD_LIFETIME_LIMIT 是旧单值（向后兼容旧字段），新客户端走字典。
# 🔴 2026-10-06 第二轮（用户「剩下没拆的全部拆开」）：上一轮只把「转码/字幕/去水印」
#   各自拆开，但每档内部还塞着多个功能（在线转码里含拼接、字幕里含烧录与翻译、
#   去水印里含 PDF）。本轮把 8 个功能彻底拆开，一个键 = 一个用户可见功能。
CLOUD_LIFETIME_LIMIT = 3
CLOUD_LIFETIME_LIMITS = {
    "cloud_commentary": 3,        # 视频解说
    "cloud_convert": 3,           # 在线转码
    "cloud_concat": 3,            # 在线拼接（视频/音频片段合并）
    "cloud_dewatermark": 3,       # 在线去水印（图片）
    "cloud_dewatermark_pdf": 3,   # 在线去水印（PDF）
    "cloud_subtitle": 3,          # 在线字幕提取/识别
    "cloud_subtitle_burn": 3,     # 字幕烧录
    "cloud_subtitle_translate": 3,  # 字幕翻译
}
_CLOUD_RESOURCES = tuple(CLOUD_LIFETIME_LIMITS.keys())
# 每日 auto 护栏：同样按功能独立（一个键一份），整数为旧写法（全部功能同一值）。
CLOUD_DAILY_AUTO_LIMIT = 1
CLOUD_DAILY_AUTO_LIMITS = {r: CLOUD_DAILY_AUTO_LIMIT for r in _CLOUD_RESOURCES}


def _free_quota_overrides(st: dict[str, Any]) -> dict[str, Any]:
    """管理员下发的免费额度覆盖（state["free_quota_overrides"]）。"""
    fq = (st or {}).get("free_quota_overrides")
    return fq if isinstance(fq, dict) else {}


def effective_cloud_limits(st: dict[str, Any]) -> dict[str, int]:
    """**当前生效**的 per-resource 终身免费次数：覆盖层 > 代码常量。

    🔴 2026-10-06：后台「终身免费次数」此前只写客户端本机 plans.json，中心是
    硬编码常量 ⇒ 管理员改了不生效（承诺了做不到）。现在后台保存会下发到中心，
    中心按本函数判定放行/拒绝，与后台显示一致。
    覆盖写法两种（与客户端 plans.json 对齐）：整数 = 全部功能同一值；字典 = 按功能。
    """
    limits = {r: int(CLOUD_LIFETIME_LIMIT) for r in _CLOUD_RESOURCES}
    v = _free_quota_overrides(st).get("cloud_lifetime")
    if isinstance(v, dict):
        for r in _CLOUD_RESOURCES:
            if r in v:
                try:
                    n = int(v[r])
                    if n >= 0:
                        limits[r] = n
                except (TypeError, ValueError):
                    pass
    elif v is not None:
        try:
            n = int(v)
            if n >= 0:
                limits = {r: n for r in _CLOUD_RESOURCES}
        except (TypeError, ValueError):
            pass
    return limits


def _cloud_quota_view(user: dict[str, Any], now: float,
                      limits: dict[str, int] | None = None) -> dict[str, Any]:
    """账号级云端免费额度快照（客户端用它回灌本地 quota.json 缓存）。

    🔴 2026-10-06 拆池：lifetime 与 daily 都从「扁平总池」升级为「按 resource 字典」。
    旧客户端（未升级）仍读 lifetime_used / daily_auto_used（= 总池和 / 单值），
    新客户端读 lifetime / daily（字典）。
    """
    cq = user.get("cloud_quota") or {}
    daily = cq.get("daily") or {}
    today = _bj_day(now)
    is_today = str(daily.get("date") or "") == today
    _auto = daily.get("auto")
    if isinstance(_auto, dict):                      # 新：按 resource 记
        daily_dict = {r: (max(0, int(_auto.get(r) or 0)) if is_today else 0)
                      for r in _CLOUD_RESOURCES}
    else:                                            # 旧：单值总池 → 归默认键
        legacy_daily = max(0, int(_auto or 0)) if is_today else 0
        daily_dict = {r: (legacy_daily if r == "cloud_commentary" else 0)
                      for r in _CLOUD_RESOURCES}
    daily_used = sum(daily_dict.values())
    _life = cq.get("lifetime")
    if isinstance(_life, dict):
        life_dict = {r: max(0, int(_life.get(r) or 0)) for r in _CLOUD_RESOURCES}
        life_used = sum(life_dict.values())          # 旧字段：总池和
    else:
        life_used = max(0, int(_life or 0))
        life_dict = {r: (life_used if r == "cloud_commentary" else 0)
                     for r in _CLOUD_RESOURCES}
    lim = limits or {r: int(CLOUD_LIFETIME_LIMIT) for r in _CLOUD_RESOURCES}
    life_remaining = {r: max(0, int(lim.get(r, CLOUD_LIFETIME_LIMIT)) - life_dict[r])
                      for r in _CLOUD_RESOURCES}
    daily_remaining = {r: max(0, CLOUD_DAILY_AUTO_LIMITS[r] - daily_dict[r])
                       for r in _CLOUD_RESOURCES}
    return {
        "lifetime": life_dict,                        # 新：字典
        # 🔴 2026-10-06 新增：生效上限字典。管理员可在后台改终身次数（覆盖层），
        #   客户端 402 文案不能再写死「终身 3 次」—— 改完配置就会报错数字。
        #   旧客户端忽略本字段，新客户端用它渲染文案。
        "lifetime_limits": {r: int(lim.get(r, CLOUD_LIFETIME_LIMIT)) for r in _CLOUD_RESOURCES},
        "lifetime_used": life_used,                    # 旧：总池和（兼容旧客户端）
        "lifetime_limit": CLOUD_LIFETIME_LIMIT,        # 旧：单值
        "lifetime_remaining": life_remaining,          # 新：字典
        "daily": daily_dict,                          # 新：按 resource 字典
        "daily_auto_used": daily_used,                 # 旧：总池和
        "daily_auto_limit": CLOUD_DAILY_AUTO_LIMIT,
        "daily_auto_remaining": daily_remaining,       # 新：字典
        "date": today,
    }


def cloud_quota_impl(state: dict[str, Any], token: str, now: float, secret: str,
                     lifetime: int = 0, daily: int = 0, refund: bool = False,
                     resource: str = "", ip: str = "",
                     allow_refund: bool = False, event_id: str = "") -> dict[str, Any]:
    """免费云端额度跨端原子扣减 / 退还（2026-10-06）。

    🔴 为什么必须有这个端点：桌面端原先把「终身 3 次云端额度」记在**本机**
    `~/.video-downloader/quota.json`，授权中心完全不知道。于是免费用户重装系统 /
    换台电脑 → 计数归零 → 又白嫖 3 次真实云端大模型调用（commentary_llm 单次
    成本 ¥0.04~0.19），且可无限重复。网页端当时走的是积分墙，只有桌面端漏，
    但根治必须让两端共用同一份账号级计数。

    语义：
      · lifetime/daily > 0 → 扣减（原子：先判余额再改，超额不落账并返回 allowed=False）
      · refund=True       → 退还（**仅受信管理端**，见下），绝不超退到 0 以下
      · 两者都不传         → 只查询当前余额（客户端冷启动/心跳回灌用）

    🔴 安全审计 2026-10-11（P0-4「云额度可被用户自行回退」）：
      修复前有两个致命口子 ——
        ① 负值守卫写的是 `life_n < 0 **and** day_n < 0`：只传**一个**负数
           （如 lifetime=-3）就绕过守卫，而 `max(0, used + (-3))` 会把已用数
           回退 ⇒ 用户自行把「终身免费次数」刷回满额，无限白嫖；
        ② `refund` 由**客户端**直接传入即生效 ⇒ 消费→退还→再消费，循环套利。
      现在：
        · 任一负值即 400（`or` 而非 `and`）；
        · `refund` 仅当 `allow_refund=True`（管理端 `cloud_quota_refund`）才受理，
          客户端传 refund 一律 403；
        · 退还必须携带 `event_id`（原消费事件号）且**一次性** —— 同一事件
          重复退还直接 409，杜绝「反复退同一笔」把额度退成负数或重复回补。
      日切按北京时间（与 daily_impl / _authority_view 的 usage_date 一致）。
      断网时由调用方按 fail-open 处理（沿用本机计数），故本端点只在能连通时调用。
    """
    uid = parse_token(token, secret, now)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    if user.get("banned"):
        raise ApiError(403, "ACCOUNT_BANNED", "账号已被停用，如有疑问请联系客服")

    try:
        life_n = int(lifetime or 0)
        day_n = int(daily or 0)
    except (TypeError, ValueError):
        raise ApiError(400, "BAD_BODY", "lifetime/daily 必须是整数")

    if refund:
        # 退还只允许受信管理端发起；客户端自退 = 消费→退还→再消费的无限套利。
        if not allow_refund:
            raise ApiError(403, "REFUND_FORBIDDEN",
                           "该接口不支持退还云端额度，如需补退请联系客服")
        if not str(event_id or "").strip():
            raise ApiError(400, "BAD_BODY", "退还必须携带 event_id（原消费事件号）")
        life_n, day_n = -abs(life_n), -abs(day_n)
    elif life_n < 0 or day_n < 0:
        # ⚠️ 必须是 or：写 and 时「只传一个负数」可绕过，正是 P0-4 的根因。
        raise ApiError(400, "BAD_BODY", "扣减数不能为负")


    cq = user.setdefault("cloud_quota", {})
    # 退还幂等（P0-4）：同一消费事件只允许退还一次。
    if refund:
        _done = cq.get("refunded_events") or []
        if str(event_id or "").strip() in _done:
            raise ApiError(409, "REFUND_DUPLICATE", "该消费事件已退还过，不能重复退还")
    today = _bj_day(now)
    daily_state = cq.get("daily") or {}
    if str(daily_state.get("date") or "") != today:
        daily_state = {"date": today, "auto": {}}      # 自然日重置（按 resource 字典）

    # 🔴 2026-10-06 拆池：lifetime 从扁平 int 升级为按 resource 字典。
    # 旧数据（int）迁移进 cloud_commentary（历史唯一云端功能），其余归 0。
    _legacy = cq.get("lifetime")
    if isinstance(_legacy, dict):
        life_dict = {r: max(0, int(_legacy.get(r) or 0)) for r in _CLOUD_RESOURCES}
    else:
        life_dict = {r: 0 for r in _CLOUD_RESOURCES}
        if _legacy:
            life_dict["cloud_commentary"] = max(0, int(_legacy))
    # daily 同样从标量升级为字典；旧标量迁移进 cloud_commentary。
    _auto = daily_state.get("auto")
    if isinstance(_auto, dict):
        day_dict = {r: max(0, int(_auto.get(r) or 0)) for r in _CLOUD_RESOURCES}
    else:
        day_dict = {r: 0 for r in _CLOUD_RESOURCES}
        if _auto:
            day_dict["cloud_commentary"] = max(0, int(_auto))

    # 🔴 2026-10-06 拆池：决定落到哪个 resource（旧调用方不传 → 默认 cloud_commentary）。
    res = str(resource or "").strip() or "cloud_commentary"
    if res not in _CLOUD_RESOURCES:
        res = "cloud_commentary"

    limits = effective_cloud_limits(state)
    if life_n > 0:
        _lim = int(limits.get(res, CLOUD_LIFETIME_LIMIT))
        if life_dict[res] + life_n > _lim:
            return {"ok": True, "allowed": False, "reason": "lifetime_exhausted",
                    "applied": {"lifetime": 0, "daily": 0},
                    "cloud_quota": _cloud_quota_view(user, now, limits),
                    "authority": _authority_view(user, now)}
    if day_n > 0 and day_dict[res] + day_n > CLOUD_DAILY_AUTO_LIMITS.get(res, CLOUD_DAILY_AUTO_LIMIT):
        return {"ok": True, "allowed": False, "reason": "daily_auto_exhausted",
                "applied": {"lifetime": 0, "daily": 0},
                "cloud_quota": _cloud_quota_view(user, now, limits),
                "authority": _authority_view(user, now)}

    life_dict[res] = max(0, life_dict[res] + life_n)
    day_dict[res] = max(0, day_dict[res] + day_n)
    cq["lifetime"] = life_dict
    daily_state["auto"] = day_dict
    cq["daily"] = daily_state

    # 消费事件号：扣减时服务端生成（随响应回给调用方），退还时沿用原事件号。
    # 有了它，「退还」才是可核验的（凭既有消费记录）、且天然一次性。
    ev_id = str(event_id or "").strip()
    if not ev_id and (life_n > 0 or day_n > 0):
        ev_id = secrets.token_hex(8)
    if refund and ev_id:
        _done = cq.setdefault("refunded_events", [])
        if ev_id not in _done:
            _done.append(ev_id)
        cq["refunded_events"] = _done[-500:]      # 只留最近 500 条，防无限膨胀

    user["cloud_quota"] = cq

    if life_n or day_n:
        # 审计流水（P0-4）：每次额度变更都留痕 —— 来源（客户端/受信管理端）、
        # 事件号、资源、变更量、变更后余量、来源 IP，便于对账与追责。
        _log_event(state, "cloud_quota", now, email=uid, resource=res,
                   lifetime=life_n, daily=day_n, refund=bool(refund),
                   event_id=ev_id,
                   source=("trusted" if allow_refund else "client"),
                   ip=ip or "",
                   life_used=life_dict[res], day_used=day_dict[res])
    return {"ok": True, "allowed": True, "refunded": bool(refund),
            "event_id": ev_id,
            "applied": {"lifetime": life_n, "daily": day_n},
            "cloud_quota": _cloud_quota_view(user, now, limits),
            "authority": _authority_view(user, now)}


_UNSET = object()   # 区分「调用方没传」与「显式传 null 清除覆盖」


def free_quota_impl(state: dict[str, Any], now: float,
                    cloud_lifetime: Any = _UNSET,
                    mode: Any = _UNSET) -> dict[str, Any]:
    """管理员下发「免费额度」覆盖（终身免费次数 + 免费额度口径 mode）。

    与 `plans_set` 分开的原因：plans_set 的入参契约是**套餐价格**（每档必须含
    `price_cny` 并逐条 round(2)），额度是 `{resource: 次数}` 形状，走同一条路会被
    价格校验拒掉。
    传 `cloud_lifetime=null` = 清除覆盖（回到代码常量）。

    🔴 2026-10-13：新增 `mode`（"on"/"off"）—— 免费额度口径也上云。
    本机 plans.json 只是缓存，换机/重装后会被重建 ⇒ 口径回默认 on。把 mode 也
    落到中心的 free_quota_overrides，客户端从 `/api/license/plans` 的 free_quota
    字段读回（与终身次数同一条通道）。
    传 `mode=null` = 清除覆盖（回默认 on）；传 "on"/"off" = 设置；不传 = 不动。

    🔴 鉴权说明：与 plans_set 一样，**管理员身份由 dispatch 的 `_require_admin`
    校验**（本函数只处理已鉴权后的写入），因此这里不再解析令牌 ——
    管理员令牌（license_admin）与用户令牌（parse_token）本来就是两套，
    误用 parse_token 会把合法的后台保存判成「登录已失效」。
    """
    changed = False
    fq = dict(_free_quota_overrides(state))

    if cloud_lifetime is not _UNSET:
        if cloud_lifetime is None:
            # 显式清除：回到代码常量（后台「恢复默认」按钮走这条路）
            fq.pop("cloud_lifetime", None)
        elif isinstance(cloud_lifetime, dict):
            clean: dict[str, int] = {}
            for k, v in cloud_lifetime.items():
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    raise ApiError(400, "BAD_LIMITS", f"{k} 的次数必须是整数")
                if n < 0:
                    raise ApiError(400, "BAD_LIMITS", f"{k} 的次数不能为负")
                clean[str(k)] = n
            fq["cloud_lifetime"] = clean
        elif isinstance(cloud_lifetime, int):
            fq["cloud_lifetime"] = int(cloud_lifetime)
        else:
            raise ApiError(400, "BAD_LIMITS",
                           "cloud_lifetime 必须是整数、{resource: 次数} 字典或 null")
        changed = True

    if mode is not _UNSET:
        if mode is None:
            fq.pop("mode", None)
        else:
            m = str(mode).strip().lower()
            if m not in ("on", "off"):
                raise ApiError(400, "BAD_MODE", "mode 只能是 on / off 或 null")
            fq["mode"] = m
        changed = True

    if changed:
        state["free_quota_overrides"] = fq
        _save_state(state)
    return {"ok": True, "free_quota_overrides": _free_quota_overrides(state),
            "effective_cloud_limits": effective_cloud_limits(state),
            "updated_at": now}


def _admin_find_user(state: dict[str, Any], email: str, now: float) -> dict[str, Any]:
    uid = _norm_id(email)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(404, "NOT_FOUND", f"账号不存在: {uid}")
    _ensure_authority(user, now)
    return user


def ban_impl(state: dict[str, Any], email: str, flag: bool, now: float,
             note: str = "") -> dict[str, Any]:
    """管理员封禁/解封账号：封禁后登录被拒、心跳回 ACCOUNT_BANNED、权益锁定。"""
    user = _admin_find_user(state, email, now)
    user["banned"] = bool(flag)
    user["ban_note"] = note[:120]
    user["banned_at"] = now if flag else 0.0
    return {"ok": True, "email": _norm_id(email), "banned": bool(flag),
            "authority": _authority_view(user, now)}


def adjust_impl(state: dict[str, Any], email: str, pool: str, delta: int,
                now: float, note: str = "") -> dict[str, Any]:
    """管理员调整积分（可为负=强扣）：pool ∈ permanent|ai。"""
    if pool not in ("permanent", "ai"):
        raise ApiError(400, "BAD_POOL", "pool 必须是 permanent 或 ai")
    user = _admin_find_user(state, email, now)
    if pool == "permanent":
        user["perm_credits"] = int(user.get("perm_credits") or 0) + int(delta)
    else:
        # 调 AI 订阅积分：直接动剩余额度（grant − spent 的差值口径），等价于调 grant
        user["ai_grant_total"] = int(user.get("ai_grant_total") or 0) + int(delta)
    user.setdefault("adjusts", []).append(
        {"pool": pool, "delta": int(delta), "note": note[:120], "at": now})
    user["adjusts"] = user["adjusts"][-SPEND_LEDGER_CAP:]
    _log_event(state, "adjust", now, email=_norm_id(email), pool=pool,
               delta=int(delta), note=note[:80])
    return {"ok": True, "email": _norm_id(email), "pool": pool, "delta": int(delta),
            "authority": _authority_view(user, now)}


def setstate_impl(state: dict[str, Any], email: str, now: float,
                  member_until_dl: float = -1.0, member_until_ai: float = -1.0,
                  perm_credits: int = -1, ai_credits_left: int = -1,
                  risk_exempt: int = -1) -> dict[str, Any]:
    """管理员直接设定权威基线（迁移存量本机权益 / 纠错用）。传 -1 表示不改该项。

    risk_exempt（2026-10-14 反破解风控）：-1 不改 / 0 取消 / 1 豁免 ——
    豁免账号（内部测试/管理员）永不进自动风控响应。
    """
    user = _admin_find_user(state, email, now)
    if member_until_dl >= 0:
        user["member_until_dl"] = float(member_until_dl)
    if member_until_ai >= 0:
        user["member_until_ai"] = float(member_until_ai)
        if user["member_until_ai"] > float(user.get("member_until_dl") or 0):
            user["member_until_dl"] = user["member_until_ai"]   # AI 捆绑下载权益
    if perm_credits >= 0:
        user["perm_credits"] = int(perm_credits)
    if ai_credits_left >= 0:
        user["ai_grant_total"] = int(ai_credits_left) + int(user.get("ai_spent_total") or 0)
    if risk_exempt >= 0:
        _get_risk(user)["exempt"] = bool(risk_exempt)
    user["authority_init"] = True
    return {"ok": True, "email": _norm_id(email), "authority": _authority_view(user, now)}


def usage_impl(state: dict[str, Any], email: str, now: float) -> dict[str, Any]:
    """管理员查账号权益与流水（审计篡改/客诉依据）。"""
    user = _admin_find_user(state, email, now)
    return {
        "ok": True,
        "email": _norm_id(email),
        "authority": _authority_view(user, now),
        "purchases": user.get("purchases") or [],
        "spends": user.get("spends") or [],
        "adjusts": user.get("adjusts") or [],
        "banned": bool(user.get("banned")),
        "ban_note": user.get("ban_note", ""),
        "devices": [
            {"fp": d.get("fp", ""), "name": d.get("name", ""),
             "last_seen": float(d.get("last_seen", 0))}
            for d in (user.get("devices") or [])
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


def _clean_device_meta(meta: Any) -> dict[str, Any]:
    """清洗客户端上报的设备画像（2026-10-13 安装/插件统计的落库口径）。

    只认白名单字段，逐字段限长；解析失败/缺省 → 空 dict（画像缺失只影响
    统计丰富度，绝不让心跳/登录失败）。ext.browser 只认固定枚举。
    """
    if not isinstance(meta, dict):
        return {}
    out: dict[str, Any] = {}
    p = str(meta.get("platform") or "").strip().lower()
    # 2026-10-16：加 "web" —— 网页版（cn/hk）登录也占设备位，画像里能看出是浏览器端，
    # 安装/设备统计按端分开（macos/windows/linux/web）。
    if p in ("macos", "windows", "linux", "web"):
        out["platform"] = p
    av = str(meta.get("app_version") or "").strip()
    if av:
        out["app_version"] = av[:32]
    ext = meta.get("ext")
    if isinstance(ext, dict):
        b = str(ext.get("browser") or "").strip().lower()
        v = str(ext.get("version") or "").strip()
        # 2026-10-13（#4 浏览器判定精确化）：扩展在哪个浏览器里跑，UA 就是那个浏览器。
        # 客户端现在能区分出 opera/vivaldi/yandex，白名单同步放开，否则会被丢弃、
        # 统计里看不到（等于白判）。
        if b in ("chrome", "edge", "brave", "chromium", "firefox", "safari",
                 "opera", "vivaldi", "yandex"):
            e: dict[str, str] = {"browser": b}
            if v:
                e["version"] = v[:32]
            out["ext"] = e
    return out


def _merge_device_meta(rec: dict, meta: dict[str, Any]) -> None:
    """把画像合并进既有设备记录：字段级刷新，first_seen 只在首次落笔。"""
    if not meta:
        return
    if meta.get("platform"):
        rec["platform"] = meta["platform"]
    if meta.get("app_version"):
        rec["app_version"] = meta["app_version"]
    if meta.get("ext"):
        rec["ext"] = meta["ext"]


def _attach_device(user: dict, device: Optional[dict], now: float) -> list[dict]:
    """占一个设备位。返回被自动淘汰的设备列表（可能为空）。"""
    dev = device or {}
    fp = str(dev.get("fp") or "").strip()[:128]
    name = str(dev.get("name") or "").strip()[:64] or "未命名设备"
    meta = _clean_device_meta(dev.get("meta"))
    devices = user.setdefault("devices", [])
    for d in devices:
        if d.get("fp") == fp:                      # 本机已在位 → 只刷新活跃时间
            d["last_seen"] = now
            d["name"] = name or d.get("name", "")
            _merge_device_meta(d, meta)
            return []
    evicted: list[dict] = []
    while len(devices) >= MAX_DEVICES:             # 超配额：挤掉最久没活动的
        oldest = min(devices, key=lambda x: float(x.get("last_seen", 0)))
        devices.remove(oldest)
        evicted.append(oldest)
    rec: dict[str, Any] = {"fp": fp, "name": name, "last_seen": now,
                           "first_seen": now}      # 安装统计：首次出现 = 安装时刻
    _merge_device_meta(rec, meta)
    devices.append(rec)
    return evicted


def _read_dl_stats(path: str = "/opt/vdl-dl-stats/downloads.json") -> dict[str, Any]:
    """读取下载计数文件（网页版服务写的：安装包 / 浏览器扩展，按平台）。

    与 stat_aggregate 同机不同服务 —— 网页版落盘固定路径，这里只读合并。
    today 由 by_day 现算；文件缺失/损坏 → 空 dict（统计缺位不影响设备数据）。
    """
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(d, dict):
        return {}
    day = time.strftime("%Y-%m-%d")
    out: dict[str, Any] = {}
    for bucket, slots in d.items():
        if bucket == "updated_at" or not isinstance(slots, dict):
            continue
        b = out.setdefault(str(bucket), {})
        for plat, slot in slots.items():
            if not isinstance(slot, dict):
                continue
            by_day = slot.get("by_day") or {}
            b[str(plat)] = {"total": int(slot.get("total") or 0),
                            "today": int(by_day.get(day) or 0)}
    return out


def stat_aggregate(state: dict[str, Any], now: float) -> dict[str, Any]:
    """「安装 / 插件次数统计（按平台）」聚合（纯函数，便于离线守卫）。

    口径（2026-10-13 用户定档「为后续计划做铺垫」）：
      · installs  = 设备的 first_seen 分布（首次出现 = 安装），按 platform 分组，
        给 累计 / 今日 / 近 7 天 三列；
      · ext       = 按浏览器分组的扩展数量（以最近一次上报为准）；
      · active    = 近 7 / 30 天有活跃（last_seen）的设备数；
      · devices_total = 名单内设备总数。
    platform 缺失的旧设备归入 "unknown"（诚实呈现，不臆测）。
    """
    day0 = int(now // 86400) * 86400
    installs: dict[str, dict[str, int]] = {}
    exts: dict[str, int] = {}
    active7 = active30 = total = 0
    for user in _users(state).values():
        for d in (user.get("devices") or []):
            if not isinstance(d, dict):
                continue
            total += 1
            plat = str(d.get("platform") or "").strip().lower() or "unknown"
            fs = float(d.get("first_seen") or d.get("last_seen") or 0)
            slot = installs.setdefault(plat, {"total": 0, "today": 0, "d7": 0})
            slot["total"] += 1
            if fs >= day0:
                slot["today"] += 1
            if fs >= now - 7 * 86400:
                slot["d7"] += 1
            ext = d.get("ext")
            if isinstance(ext, dict) and ext.get("browser"):
                exts[str(ext["browser"])] = exts.get(str(ext["browser"]), 0) + 1
            ls = float(d.get("last_seen") or 0)
            if ls >= now - 7 * 86400:
                active7 += 1
            if ls >= now - 30 * 86400:
                active30 += 1
    out = {"devices_total": total, "installs": installs, "ext": exts,
           "active": {"d7": active7, "d30": active30},
           "downloads": _read_dl_stats()}
    return out


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
    if user.get("banned"):
        raise ApiError(403, "ACCOUNT_BANNED",
                       "账号已被停用，如有疑问请联系客服")
    fp = str((device or {}).get("fp") or "").strip()
    had_device = any(str(d.get("fp") or "") == fp
                     for d in (user.get("devices") or []))
    evicted = _attach_device(user, device, now)
    if fp and not had_device:               # 新设备落位 → 记事件 + 指纹反查（S1/S2 数据源）
        _log_event(state, "device_add", now, email=uid, fp=fp)
        _touch_fp_index(state, uid, fp, now)
    user["last_login"] = now
    out = {"ok": True, "token": make_token(uid, secret, now),
           "account": _public_user(user, fp)}
    if evicted:
        out["evicted"] = [{"fp": d.get("fp", ""), "name": d.get("name", "")}
                          for d in evicted]
    return out


def heartbeat_impl(state: dict[str, Any], token: str, fp: str, now: float,
                   secret: str, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """续期 + 查自己是否还在设备名单里（被别的机器挤掉则返回 DEVICE_EVICTED）。"""
    uid = parse_token(token, secret, now)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    if user.get("banned"):
        return {"ok": False, "code": "ACCOUNT_BANNED",
                "error": "账号已被停用，如有疑问请联系客服",
                "account": _public_user(user, "")}
    fp = (fp or "").strip()
    clean_meta = _clean_device_meta(meta)
    devices = user.setdefault("devices", [])
    for d in devices:
        if d.get("fp") == fp:
            d["last_seen"] = now
            _merge_device_meta(d, clean_meta)   # 2026-10-13：画像随心跳刷新
            _touch_hb_ring(user, now)           # 2026-10-14：心跳时间戳环（S3 数据源）
            return {"ok": True, "account": _public_user(user, fp)}
    return {"ok": False, "code": "DEVICE_EVICTED",
            "error": "该账号已在其他两台设备登录，如需使用请重新登录",
            "account": _public_user(user, "")}


def password_impl(state: dict[str, Any], email: str, new_password: str, now: float,
                  secret: str, old_password: str = "", token: str = "",
                  admin_token: str = "") -> dict[str, Any]:
    """把 App 本机账号的新密码同步到云端（「一套账号」的关键一环）。

    🔴 为什么必须有：账号其实是**两套库**——本机 `auth_store`（账号是否存在/密码对不对
      的权威 + 功能门禁）与云端本文件（会员权益 + 设备位）。两边各存一份密码哈希，
      任何一侧改密而另一侧不同步 → 用户「在这台能登、换台说密码错」。
      本机侧改动（个人中心改密 / 忘记密码重置）通过本接口推过来。

    鉴权三选一，都必须能证明对该账号的持有（或管理权）：
      · old_password：知道原密码（个人中心「修改密码」）
      · token       ：云端签发的登录 token（忘记密码只走验证码，本机已无原密码）
      · admin_token ：授权中心 ADMIN_TOKEN（2026-10-16 超管后台重置密码——
        管理员不知道用户原密码，但持管理令牌；重置后两端同密码，不分叉）

    云端没有该账号 → `{ok: True, synced: False}`（这是「只在本机注册过」的老账号，
    下次登录时 App 会把它自愈补建到云端），**不算失败**，别让用户看到报错。
    """
    uid = _norm_id(email)
    if not _valid_account_id(uid):
        raise ApiError(400, "BAD_ACCOUNT", "请输入有效的邮箱或手机号")
    if len(new_password or "") < MIN_PASSWORD:
        raise ApiError(400, "WEAK_PASSWORD", f"密码至少 {MIN_PASSWORD} 位")
    user = _users(state).get(uid)
    if not user:
        return {"ok": True, "synced": False, "reason": "cloud_no_account", "email": uid}
    ok = bool(old_password) and verify_password(
        old_password, user.get("salt", ""), user.get("pw_hash", ""))
    if not ok and token:
        try:
            ok = parse_token(token, secret, now) == uid
        except ApiError:
            ok = False
    if not ok and admin_token and ADMIN_TOKEN:
        ok = hmac.compare_digest(admin_token, ADMIN_TOKEN)
    if not ok:
        raise ApiError(401, "BAD_CREDENTIALS", "云端校验未通过，密码未同步")
    salt, pw_hash = hash_password(new_password)
    user["salt"] = salt
    user["pw_hash"] = pw_hash
    user["pw_changed_at"] = now
    return {"ok": True, "synced": True, "email": uid}


def redeem_impl(state: dict[str, Any], token: str, code: str, now: float,
                secret: str, ip: str = "") -> dict[str, Any]:
    """卡密充值到账号（不再绑机器）。同一张卡第二次用会拒绝。

    VDL_REDEEM_DISABLED=1 时整条通道关闭（410），失败也入事件流留审计痕迹。
    """
    if not secret:
        raise ApiError(500, "NO_SECRET", "服务端未配置 VDL_LICENSE_SECRET")
    if REDEEM_DISABLED:
        _log_event(state, "redeem_fail", now, ip=ip, code=code[:48], err="REDEEM_DISABLED")
        _scan_redeem_fail_anomalies(state, now, ip)
        raise ApiError(410, "REDEEM_DISABLED", "卡密充值已下线，请在会员中心使用支付宝/微信扫码充值")
    uid = parse_token(token, secret, now)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    try:
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
    except ApiError as e:
        # 核销失败入事件流（卡密爆破检测数据源）+ 告警扫描；任何调用路径都覆盖
        # 2026-10-14：补 email —— 风险评分 S5「同账号核销失败」需按账号归因（原只有 IP）。
        _log_event(state, "redeem_fail", now, ip=ip, email=uid,
                   code=code[:48], err=e.code)
        _scan_redeem_fail_anomalies(state, now, ip)
        raise
    rec.update({"status": "used", "bound_user": uid, "bound_at": now, "plan": short,
                "plan_code": plan_code})
    cards[code] = rec
    pid = secrets.token_hex(6)
    user.setdefault("purchases", []).append(
        {"id": pid, "plan_code": plan_code, "at": now})
    _apply_plan_effect(user, plan_code, now)   # 权威状态同步推进（余额/到期）
    _log_event(state, "recharge", now, email=uid, ip=ip,
               plan_code=plan_code, code=code[:48])
    _scan_recharge_anomalies(state, now, uid, ip, plan_code)
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
    # 🔴 安全审计 2026-10-11（P1-1）订单级幂等：同一笔支付（同一 note 里的
    #    渠道+订单号）重复回调只发一次权益，直接返回既有结果。
    #    放在限购闸之前 —— 重复回调不是「超限购买」，不该被记成 LIMIT_REACHED 告警。
    _okey = _order_key_of_note(note)
    if _okey:
        for _p in (user.get("purchases") or []):
            if _order_key_of_note(_p.get("note")) == _okey:
                _log_event(state, "grant_duplicate", now, email=uid,
                           order_key=_okey,
                           plan_code=str(_p.get("plan_code") or plan_code))
                return {"ok": True, "user_id": uid,
                        "plan_code": str(_p.get("plan_code") or plan_code),
                        "purchase_id": str(_p.get("id") or ""),
                        "duplicate": True,
                        "account": _public_user(user)}
    # 🔴 每人限购硬闸（2026-10-09）：活动价档可设 limit_per_user。
    #    收款前的拦截在 pay_server（正常路径根本走不到这里）；本闸只用于兜住
    #    「并发多单 / 直接调 grant」的绕过。仅对**支付驱动**的发货生效
    #    （note 形如 xunhupay-auto:XXX）—— 管理员手动补发不受限，避免误拦补单。
    if _is_auto_grant_note(note):
        _lim = _plan_limit(state, plan_code, now)
        if _lim > 0:
            _already = sum(1 for p in (user.get("purchases") or [])
                           if p.get("plan_code") == plan_code)
            if _already >= _lim:
                _raise_alert(state, "limit_exceeded", "critical", uid, "",
                             f"套餐 {plan_code} 每人限购 {_lim} 次，该账号已购 "
                             f"{_already} 次；本笔支付驱动发货被拦截（note={str(note)[:60]}）。"
                             f"若已收款属超限重复购买，请按订单退款。", now)
                raise ApiError(409, "LIMIT_REACHED",
                               f"该活动每个账号限购 {_lim} 次（已购 {_already} 次）")
    pid = secrets.token_hex(6)
    user.setdefault("purchases", []).append(
        {"id": pid, "plan_code": plan_code, "at": now, "note": note[:120]})
    _apply_plan_effect(user, plan_code, now)   # 权威状态同步推进（余额/到期）
    # 全量事件（2026-09-26 每日对账的数据源：note 以 alipay-auto 开头 = 支付回调自动发货，
    # 对账时必须能对应上一笔已收款订单；管理员手动 grant/补发不计入资金口径）
    _log_event(state, "grant", now, email=uid, plan_code=plan_code, note=note[:120])
    return {"ok": True, "user_id": uid, "plan_code": plan_code, "purchase_id": pid,
            "account": _public_user(user)}


def _spec_in_activity(spec: Any, now: float) -> bool:
    """该档此刻是否算**活动档**（供「活动价每人限购一次」）。

    与 pay_server::_sales_state::is_activity 严格同口径：
      1) `flash_price > 0` 且当前时刻落在 `[flash_start, flash_end]` 内 → 是
         （忽略 mode，这样 mode=limited 但配了秒杀窗口的档也能被识别）；
      2) **窗口外**，若秒杀价**未回升到常态价之上**（`flash_price >= price_cny`）
         → 也算。例：`price_cny = flash_price = 0.10`（管理员没有恢复价格），
         窗口过后用户看到的仍是 ¥0.10，此时若判「非活动」就会放行重复购买。
    🔴 2026-10-09 实测漏洞：download_1day 窗口 00:27–02:35，窗口后价格仍 0.10，
       旧口径判「非活动」⇒ 限购失效 ⇒ 已购账号可无限次重复下单。

    未显式配置 limit_per_user 的活动档默认限购 1。
    口径必须与收款侧一致，否则会出现「下单被拦 / 发货放行」的分叉。
    """
    if not isinstance(spec, dict):
        return False
    try:
        fp = float(spec.get("flash_price") or 0)
        fs = float(spec.get("flash_start") or 0)
        fe = float(spec.get("flash_end") or 0)
        bp = float(spec.get("price_cny") or 0)
    except (TypeError, ValueError):
        return False
    if fp <= 0:
        return False
    if fs and fe and fs <= now <= fe:
        return True
    return fp >= bp


def _plan_limit(state: dict[str, Any], plan_code: str,
                now: Optional[float] = None) -> int:
    """该档「每人限购次数」（0 = 不限）。读云端套餐覆盖表，容错非数字。

    与 plans_set 写入侧同源：limit_per_user 随套餐 spec 原样透传（无白名单）。
    覆盖表是**两级**结构 {类目: {code: spec}}，故需遍历类目。

    优先级：显式 `limit_per_user` > **活动档默认 1** > 0（不限）。
    🔴 第二档就是「活动价格每个用户仅限购一次」的落地处；调用方**必须**传 now，
       否则活动档会被当成不限购（漏拦）。
    """
    now = float(now if now is not None else time.time())
    for cat in (_plan_overrides(state) or {}).values():
        if isinstance(cat, dict) and plan_code in cat:
            spec = cat.get(plan_code)
            if isinstance(spec, dict):
                raw = spec.get("limit_per_user")
                if raw not in (None, ""):
                    try:
                        return max(0, int(float(raw)))
                    except (TypeError, ValueError):
                        return 0
                return 1 if _spec_in_activity(spec, now) else 0
    return 0


def plan_usage_impl(state: dict[str, Any], token: str, secret: str, now: float,
                    plan_code: str = "") -> dict[str, Any]:
    """当前账号各套餐的**已购次数** —— 活动价限购的取数源。

    计数以 purchases 为准（每笔成功购买/发货追加一条，含管理员补发）。
    plan_code 非空时额外回该档的限购上限与是否仍可购买（供收款前拦截）。
    """
    uid = parse_token(token, secret, now)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(401, "NO_ACCOUNT", "账号不存在，请重新登录")
    counts: dict[str, int] = {}
    for p in (user.get("purchases") or []):
        c = str(p.get("plan_code") or "")
        if c:
            counts[c] = counts.get(c, 0) + 1
    out: dict[str, Any] = {"ok": True, "counts": counts}
    if plan_code:
        lim = _plan_limit(state, plan_code, now)
        n = counts.get(plan_code, 0)
        out.update({"plan_code": plan_code, "count": n, "limit": lim,
                    "allowed": (lim <= 0 or n < lim)})
    return out


def users_impl(state: dict[str, Any]) -> dict[str, Any]:
    users = _users(state)
    out = []
    for uid, u in users.items():
        item = _public_user(u)
        item["last_login"] = float(u.get("last_login", 0))
        item["created_at"] = float(u.get("created_at", 0))
        item["purchases"] = [
            {"id": p.get("id"), "plan_code": p.get("plan_code"), "at": p.get("at")}
            for p in (u.get("purchases") or [])
        ]
        out.append(item)
    out.sort(key=lambda x: x["last_login"], reverse=True)
    return {"ok": True, "users": out, "count": len(out)}


def set_admin_impl(state: dict[str, Any], email: str, flag: bool,
                   now: float) -> dict[str, Any]:
    """管理员设置/取消超管（2026-10-16：is_admin 上云，唯一真源）。

    桌面 App 与网页版各节点的本机 is_admin 退为离线兜底；联网登录时以本服务为准。
    """
    user = _admin_find_user(state, email, now)
    user["is_admin"] = bool(flag)
    return {"ok": True, "email": _norm_id(email), "is_admin": bool(flag),
            "authority": _authority_view(user, now)}


def delete_user_impl(state: dict[str, Any], email: str,
                     now: float) -> dict[str, Any]:
    """删除账号（2026-10-16：历史测试号清理）。

    幂等：账号不存在时同样返回 ok（便于批量清理脚本可重复执行）。
    仅管理端持令牌调用；真实用户数据只在其明确要求下删除。
    """
    uid = _norm_id(email)
    if not _valid_account_id(uid):
        raise ApiError(400, "BAD_ACCOUNT", "请输入有效的邮箱或手机号")
    users = _users(state)
    if uid not in users:
        return {"ok": True, "email": uid, "deleted": False}
    del users[uid]
    _log_event(state, "account_delete", now, email=uid)
    return {"ok": True, "email": uid, "deleted": True}


# 权益字段零值表（import_account_impl 搬迁时按字段类型清洗用）
_AUTH_ZERO = {"member_until_dl": 0.0, "member_until_ai": 0.0,
              "ai_grant_total": 0, "ai_spent_total": 0, "perm_credits": 0}


def import_account_impl(state: dict[str, Any], email: str, salt: str, pw_hash: str,
                        now: float, created_at: float = 0.0,
                        is_admin: bool = False,
                        authority: Optional[dict[str, Any]] = None,
                        devices: Optional[list] = None) -> dict[str, Any]:
    """存量账号搬迁（2026-10-16「所有端用户上云端」）：把本机账号记录原样搬入。

    本机 auth_store 与本服务的密码哈希格式一致（PBKDF2-HMAC-SHA256 20 万轮，
    hex 盐 / hex 摘要）⇒ 直接搬盐与哈希即可，**用户密码无感、无需重置**。

    authority（2026-10-16 用户「真实账号的所有数据也得导入」）：本机 memberships
    的权益态一并搬来——member_until_dl / member_until_ai / ai_grant_total /
    ai_spent_total / perm_credits / daily{date,counts}。devices：本机账号镜像里的
    设备画像（fp/name/last_seen/platform）。**只在新账号创建时整体写入**；已存在
    账号一律不覆盖（云端是权威——云端 purchases 重放出来的权益可能强于本机，
    覆盖等于给用户降级），仅按需补 is_admin。
    """
    uid = _norm_id(email)
    if not _valid_account_id(uid):
        raise ApiError(400, "BAD_ACCOUNT", "请输入有效的邮箱或手机号")
    if not salt or not pw_hash:
        raise ApiError(400, "BAD_CREDENTIAL", "salt/pw_hash 不能为空")
    users = _users(state)
    if uid in users:
        user = users[uid]
        if is_admin and not user.get("is_admin"):
            user["is_admin"] = True
        return {"ok": True, "email": uid, "created": False,
                "is_admin": bool(user.get("is_admin"))}
    rec: dict[str, Any] = {
        "user_id": uid, "email": uid, "salt": salt, "pw_hash": pw_hash,
        "created_at": created_at or now, "devices": [], "purchases": [],
        "is_admin": bool(is_admin), "imported": True,
    }
    auth = authority if isinstance(authority, dict) else {}
    for key in ("member_until_dl", "member_until_ai", "ai_grant_total",
                "ai_spent_total", "perm_credits"):
        try:
            rec[key] = type(_AUTH_ZERO[key])(auth.get(key) or 0)
        except (TypeError, ValueError):
            rec[key] = _AUTH_ZERO[key]
    daily = auth.get("daily")
    if isinstance(daily, dict) and str(daily.get("date") or ""):
        rec["daily"] = {"date": str(daily.get("date"))[:16],
                        "counts": {str(k)[:32]: int(v)
                                   for k, v in (daily.get("counts") or {}).items()
                                   if isinstance(v, (int, float))}}
    if isinstance(devices, list):
        clean = []
        for d in devices[:MAX_DEVICES]:
            if not isinstance(d, dict):
                continue
            fp = str(d.get("fp") or "").strip()[:64]
            if not fp:
                continue
            clean.append({
                "fp": fp,
                "name": str(d.get("name") or "")[:64],
                "first_seen": float(d.get("first_seen") or d.get("last_seen") or now),
                "last_seen": float(d.get("last_seen") or now),
            })
        rec["devices"] = clean
    users[uid] = rec
    _log_event(state, "account_import", now, email=uid)
    return {"ok": True, "email": uid, "created": True, "is_admin": bool(is_admin)}


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


# ── 充值监控与异常告警（2026-09-25 超管实时监控）───────────────────────────── #
# 事件双写：state.ev_ring（内存环，供异常规则回看）+ events.jsonl（全量审计流水）。
# 告警存 state.alerts（超管 alerts 接口拉取），同源同类 5 分钟内合并防轰炸。
EVENT_RING_CAP = 800
ALERTS_CAP = 200
ALERT_DEDUPE_WINDOW = 300.0
RECHARGE_BURST_N = 3            # 同账号 10 分钟内 ≥3 笔充值 → 连刷告警
RECHARGE_BURST_WINDOW = 600.0
REDEEM_BRUTE_N = 5              # 同 IP 10 分钟内 ≥5 次核销失败 → 爆破告警
REDEEM_BRUTE_WINDOW = 600.0
HIGH_VALUE_PLANS = {"download_year", "ai_15000", "credits_15000"}


def _log_event(state: dict[str, Any], kind: str, now: float, **fields: Any) -> dict[str, Any]:
    ev = {"kind": kind, "at": float(now)}
    ev.update(fields)
    ring = state.setdefault("ev_ring", [])
    ring.append(ev)
    state["ev_ring"] = ring[-EVENT_RING_CAP:]
    try:
        EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(EVENT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except OSError:
        pass                                    # 审计流水写失败不阻断业务
    return ev


def _raise_alert(state: dict[str, Any], kind: str, level: str, email: str,
                 ip: str, detail: str, now: float) -> dict[str, Any]:
    alerts = state.setdefault("alerts", [])
    for a in alerts[-30:]:                      # 近窗口内去重合并（count 递增）
        if (a.get("kind") == kind and a.get("email") == email and a.get("ip") == ip
                and now - float(a.get("at", 0)) <= ALERT_DEDUPE_WINDOW):
            a["count"] = int(a.get("count", 1)) + 1
            a["at"] = float(now)
            a["detail"] = detail[:300]
            a["seen"] = False
            return a
    a = {"id": secrets.token_hex(6), "kind": kind, "level": level,
         "email": email or "", "ip": ip or "", "detail": detail[:300],
         "at": float(now), "count": 1, "seen": False}
    alerts.append(a)
    state["alerts"] = alerts[-ALERTS_CAP:]
    return a


def _scan_recharge_anomalies(state: dict[str, Any], now: float, email: str,
                             ip: str, plan_code: str) -> None:
    """充值后扫描：大额单笔 / 同账号短时连刷。"""
    if plan_code in HIGH_VALUE_PLANS:
        _raise_alert(state, "high_value_recharge", "warn", email, ip,
                     f"大额充值：{plan_code}", now)
    ring = state.get("ev_ring") or []
    recent = [e for e in ring
              if e.get("kind") == "recharge" and e.get("email") == email
              and now - float(e.get("at", 0)) <= RECHARGE_BURST_WINDOW]
    if len(recent) >= RECHARGE_BURST_N:
        _raise_alert(state, "recharge_burst", "critical", email, ip,
                     f"10 分钟内连续充值 {len(recent)} 笔（最近 {plan_code}），疑似异常", now)


def _scan_redeem_fail_anomalies(state: dict[str, Any], now: float, ip: str) -> None:
    """核销失败扫描：同 IP 短时多次失败 → 疑似卡密爆破。"""
    if not ip:
        return
    ring = state.get("ev_ring") or []
    recent = [e for e in ring
              if e.get("kind") == "redeem_fail" and e.get("ip") == ip
              and now - float(e.get("at", 0)) <= REDEEM_BRUTE_WINDOW]
    if len(recent) >= REDEEM_BRUTE_N:
        _raise_alert(state, "redeem_bruteforce", "critical", "", ip,
                     f"IP {ip} 10 分钟内核销失败 {len(recent)} 次，疑似卡密爆破", now)


def alerts_impl(state: dict[str, Any], since: float = 0.0,
                unseen_only: bool = False, limit: int = 100) -> dict[str, Any]:
    """管理员拉取告警列表（at 倒序）。unseen_only=True 只回未确认；unseen=当前未确认总数。"""
    all_alerts = state.get("alerts") or []
    out = [a for a in all_alerts if float(a.get("at", 0)) > float(since or 0)]
    if unseen_only:
        out = [a for a in out if not a.get("seen")]
    out = sorted(out, key=lambda a: float(a.get("at", 0)), reverse=True)
    return {"ok": True, "alerts": out[:max(1, min(int(limit), 200))],
            "unseen": len([a for a in all_alerts if not a.get("seen")])}


def alerts_ack_impl(state: dict[str, Any], ids: Any) -> dict[str, Any]:
    """管理员确认告警。ids 为空 = 全部确认。"""
    idset = {str(i) for i in (ids or []) if str(i)}
    n = 0
    for a in state.get("alerts") or []:
        if (not idset or str(a.get("id")) in idset) and not a.get("seen"):
            a["seen"] = True
            n += 1
    return {"ok": True, "acked": n}


# ── 风险评分（反破解风控 · 第 1 批「纯观测」，2026-10-14）───────────────────── #
# 🔴 设计红线（完整方案见 WorkBuddy/反破解风控方案_异常检测_自动降级_人工复核_2026-10-14.md）：
#   · 判定维度只用「账号 / 设备」，IP 只用于限流，**永不进封禁路径**（大陆 CGNAT 一锁锁一片）；
#   · 本批【只算分、记事件、交给看板复核】，不挂任何自动响应；ban 永远人工；
#   · 客户端上报的信号是弱信号（可被伪造），必须与其他信号叠加才计分，不得单独触发；
#   · 分数由「当前命中项」现算 ⇒ 命中项离开各自信号窗口后自动失效（等效衰减），
#     不累加、不沉淀，故不存在「历史误判永久累计」。_get_risk 里的 peak 仅供看板展示。
RISK_T1 = 30                    # 观测 → 限流（第 2 批启用）
RISK_T2 = 55                    # 限流 → 挑战（第 2 批启用）
RISK_T3 = 80                    # 挑战 → 人工复核（第 2 批启用）
RISK_REASON_CAP = 20            # 单账号保留的命中条目上限
RISK_DEV_CHURN_N = 4            # 7 天内新增设备 ≥N 台
RISK_DEV_CHURN_WINDOW = 604800.0
RISK_FP_REUSE_N = 3             # 同一设备指纹出现在 ≥N 个账号
RISK_HB_MIN_GAP = 30.0          # 心跳间隔 < 30s 视为机器行为
RISK_HB_BURST_N = 10            # 连续 ≥N 次过密心跳
RISK_HB_RING_CAP = 30           # 每账号保留的心跳时间戳条数
RISK_SPEND_BURST_N = 30         # 10 分钟内 ≥N 次扣费/用量上报
RISK_SPEND_BURST_WINDOW = 600.0
RISK_REDEEM_BRUTE_N = 5         # 与既有 REDEEM_BRUTE_N 同口径（同账号 10 分钟内 ≥5 次失败）
RISK_REDEEM_BRUTE_WINDOW = 600.0
FP_INDEX_CAP = 5000             # 指纹反查索引容量上限（超限按 last 淘汰最久未出现）
RISK_WEIGHTS: dict[str, int] = {
    "device_churn": 20, "fp_reuse": 25, "hb_anomaly": 15,
    "spend_burst": 20, "redeem_brute": 15, "integrity": 8,
}


def _risk_exempt_fps() -> set[str]:
    """内部测试设备白名单（env 逗号分隔）——命中即永不参与自动响应（第 2 批生效）。"""
    raw = os.environ.get("VDL_LICENSE_RISK_EXEMPT_FPS") or ""
    return {s.strip() for s in raw.split(",") if s.strip()}


def _risk_defaults() -> dict[str, Any]:
    return {"score": 0, "level": "observe", "reasons": [],
            "updated": 0.0, "peak": 0, "exempt": False, "cleared_at": 0.0}


def _get_risk(user: dict[str, Any]) -> dict[str, Any]:
    """取（并按需补齐）账号的风险摘要。旧账号缺字段自动补默认值，向后兼容。"""
    r = user.get("risk")
    if not isinstance(r, dict):
        r = _risk_defaults()
        user["risk"] = r
    else:
        for k, v in _risk_defaults().items():
            r.setdefault(k, v)
    return r


def _risk_is_exempt(user: dict[str, Any]) -> bool:
    if isinstance(user.get("risk"), dict) and user["risk"].get("exempt"):
        return True
    allow = _risk_exempt_fps()
    if not allow:
        return False
    return any(str(d.get("fp") or "") in allow for d in (user.get("devices") or []))


def _reason(k: str, now: float, note: str = "") -> dict[str, Any]:
    return {"k": k, "w": int(RISK_WEIGHTS.get(k, 0)), "at": float(now),
            "note": note[:80]}


def _ring_match(state: dict[str, Any], kind: str, within: float, now: float,
                **match: Any) -> list[dict[str, Any]]:
    """按 kind + 字段匹配扫事件环（只取近 within 秒）。纯读，不改 state。"""
    out: list[dict[str, Any]] = []
    for e in (state.get("ev_ring") or []):
        if not isinstance(e, dict) or e.get("kind") != kind:
            continue
        if now - float(e.get("at", 0)) > within:
            continue
        if any(e.get(f) != v for f, v in match.items()):
            continue
        out.append(e)
    return out


def _touch_fp_index(state: dict[str, Any], uid: str, fp: str, now: float) -> None:
    """维护「设备指纹 → 用过它的账号」反查索引（S2 指纹复用的数据源）。

    只在登录/注册（新增设备）时调用，避免心跳高频写放大。
    """
    fp = (fp or "").strip()
    if not fp:
        return
    idx = state.setdefault("fp_index", {})
    rec = idx.get(fp)
    if not isinstance(rec, dict):
        rec = {"uids": [], "first": float(now), "last": float(now)}
        idx[fp] = rec
    uids = rec.setdefault("uids", [])
    if uid and uid not in uids:
        uids.append(uid)
    rec["last"] = float(now)
    if len(idx) > FP_INDEX_CAP:               # 超容量 → 淘汰最久未出现
        drop = sorted(idx, key=lambda x: float((idx[x] or {}).get("last", 0))
                      )[: len(idx) - FP_INDEX_CAP]
        for k in drop:
            idx.pop(k, None)


def _touch_hb_ring(user: dict[str, Any], now: float) -> None:
    """记一次心跳时间戳（有界环，S3 自动化脚本特征的数据源）。

    🔴 刻意**不写 events.jsonl**：心跳量级远大于业务事件，落全量流水会把文件撑爆；
       有界环随账号持久化，容量 RISK_HB_RING_CAP 条，足够判「连续过密」。
    """
    ring = user.get("hb_ring")
    if not isinstance(ring, list):
        ring = []
    ring.append(float(now))
    user["hb_ring"] = ring[-RISK_HB_RING_CAP:]


def _sig_device_churn(state: dict[str, Any], uid: str, user: dict,
                      now: float) -> list[dict[str, Any]]:
    n = len(_ring_match(state, "device_add", RISK_DEV_CHURN_WINDOW, now, email=uid))
    if n >= RISK_DEV_CHURN_N:
        return [_reason("device_churn", now, f"7 天内新增设备 {n} 台")]
    return []


def _sig_fp_reuse(state: dict[str, Any], uid: str, user: dict,
                  now: float) -> list[dict[str, Any]]:
    idx = state.get("fp_index") or {}
    worst = 0
    for d in (user.get("devices") or []):
        rec = idx.get(str(d.get("fp") or ""))
        if isinstance(rec, dict):
            worst = max(worst, len(rec.get("uids") or []))
    if worst >= RISK_FP_REUSE_N:
        return [_reason("fp_reuse", now, f"同一设备指纹被 {worst} 个账号使用")]
    return []


def _hb_overdense(hb_ring: Any) -> int:
    """最长一段「相邻心跳间隔 < RISK_HB_MIN_GAP 秒」的连续次数（自动化脚本特征）。"""
    ts = sorted(float(t) for t in (hb_ring or []) if isinstance(t, (int, float)))
    best = run = 0
    for i in range(1, len(ts)):
        if ts[i] - ts[i - 1] < RISK_HB_MIN_GAP:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def _sig_hb_anomaly(state: dict[str, Any], uid: str, user: dict,
                    now: float) -> list[dict[str, Any]]:
    n = _hb_overdense(user.get("hb_ring"))
    if n >= RISK_HB_BURST_N:
        return [_reason("hb_anomaly", now,
                        f"连续 {n} 次心跳过密（<{int(RISK_HB_MIN_GAP)}s）")]
    return []


def _sig_spend_burst(state: dict[str, Any], uid: str, user: dict,
                     now: float) -> list[dict[str, Any]]:
    n = len(_ring_match(state, "spend", RISK_SPEND_BURST_WINDOW, now, email=uid))
    n += len(_ring_match(state, "daily", RISK_SPEND_BURST_WINDOW, now, email=uid))
    if n >= RISK_SPEND_BURST_N:
        return [_reason("spend_burst", now, f"10 分钟内 {n} 次扣费/用量上报")]
    return []


def _sig_redeem_brute(state: dict[str, Any], uid: str, user: dict,
                      now: float) -> list[dict[str, Any]]:
    n = len(_ring_match(state, "redeem_fail", RISK_REDEEM_BRUTE_WINDOW, now, email=uid))
    if n >= RISK_REDEEM_BRUTE_N:
        return [_reason("redeem_brute", now, f"10 分钟内核销失败 {n} 次")]
    return []


def _sig_integrity(state: dict[str, Any], uid: str, user: dict,
                   now: float) -> list[dict[str, Any]]:
    """客户端完整性自检（弱信号）。字段由**第 3 批**接入，在此之前恒不命中。"""
    for d in (user.get("devices") or []):
        if str(d.get("integrity") or "") == "fail":
            return [_reason("integrity", now, "客户端完整性自检失败")]
    return []


RISK_SIGNALS = (_sig_device_churn, _sig_fp_reuse, _sig_hb_anomaly,
                _sig_spend_burst, _sig_redeem_brute, _sig_integrity)


def _level_of(score: int) -> str:
    if score >= RISK_T3:
        return "review"
    if score >= RISK_T2:
        return "challenge"
    if score >= RISK_T1:
        return "throttle"
    return "observe"


def risk_eval(state: dict[str, Any], uid: str, now: float) -> dict[str, Any]:
    """计算账号风险分与档位；就地刷新该账号的 risk 摘要（结果只由当前 state 决定 ⇒ 幂等）。

    ⚠️ 本批为**纯观测**：返回的 level 仅供看板展示与后续批次使用，调用方**不得**
       据此拒绝任何请求。第 2 批挂响应门时，🔴 必须挂在扣费/记账**之前**（顺序约束）。
    """
    user = _users(state).get(uid)
    if not isinstance(user, dict):
        return {"score": 0, "level": "observe", "reasons": [], "exempt": False}
    r = _get_risk(user)
    if _risk_is_exempt(user):
        r.update({"score": 0, "level": "observe", "reasons": [], "updated": float(now)})
        return {"score": 0, "level": "observe", "reasons": [], "exempt": True}
    hits: list[dict[str, Any]] = []
    for fn in RISK_SIGNALS:
        try:
            hits.extend(fn(state, uid, user, now))
        except Exception:                       # 单信号异常绝不阻断主流程
            continue
    dedup: dict[str, dict[str, Any]] = {}
    for h in hits:                              # 同一信号只留最高权重那条
        cur = dedup.get(h["k"])
        if cur is None or h["w"] > cur["w"]:
            dedup[h["k"]] = h
    merged = sorted(dedup.values(), key=lambda x: -x["w"])[:RISK_REASON_CAP]
    score = min(100, sum(h["w"] for h in merged))
    r.update({"score": score, "level": _level_of(score), "reasons": merged,
              "updated": float(now)})
    r["peak"] = max(int(r.get("peak") or 0), score)
    return {"score": score, "level": r["level"], "reasons": merged, "exempt": False}


def risk_query_impl(state: dict[str, Any], email: str, now: float) -> dict[str, Any]:
    """管理员查看账号风险画像（现场重算，保证与看板一致）。"""
    uid = _norm_id(email)
    if not _users(state).get(uid):
        raise ApiError(404, "NO_ACCOUNT", "账号不存在")
    user = _users(state)[uid]
    ev = risk_eval(state, uid, now)
    r = _get_risk(user)
    return {"ok": True, "email": uid, "score": ev["score"], "level": ev["level"],
            "reasons": ev["reasons"], "exempt": bool(ev["exempt"]),
            "peak": int(r.get("peak") or 0), "updated": float(r.get("updated") or 0)}


def risk_clear_impl(state: dict[str, Any], email: str, now: float,
                    note: str = "") -> dict[str, Any]:
    """人工抹平（确认误报）：清空风险摘要，保留 exempt 标记。

    🔴 注意：清空**不会**让仍在窗口内的信号消失 —— risk_eval 是现算的，下一个调用
       还会重新命中。cleared_at 就是给第 2 批用的：响应门只对 `at > cleared_at`
       的命中项生效，从而让「确认误报」真正压制自动动作。本批（纯观测）只记不判。
    """
    uid = _norm_id(email)
    user = _users(state).get(uid)
    if not user:
        raise ApiError(404, "NO_ACCOUNT", "账号不存在")
    keep_exempt = bool((user.get("risk") or {}).get("exempt")) \
        if isinstance(user.get("risk"), dict) else False
    fresh = _risk_defaults()
    fresh["exempt"] = keep_exempt
    fresh["updated"] = float(now)
    fresh["cleared_at"] = float(now)     # 🔴 第 2 批据此抑制「已确认误报」的自动响应
    user["risk"] = fresh
    _log_event(state, "risk_clear", now, email=uid, note=note[:80])
    return {"ok": True, "email": uid, "score": 0, "level": "observe",
            "reasons": [], "exempt": keep_exempt, "peak": 0, "updated": now}


def risk_top_impl(state: dict[str, Any], now: float, limit: int = 50,
                  min_score: int = 1) -> dict[str, Any]:
    """风险最高的账号列表（看板「风险复核队列」的数据源）。

    🔴 按需拉取、不设后台常扫线程 —— 反破解是低频运维动作，没必要让它常驻占 CPU；
       代价是每次调用要遍历全部账号做一遍 risk_eval（无 IO，纯内存扫描，实测足够快）。
    """
    floor = max(1, int(min_score))
    rows: list[dict[str, Any]] = []
    for uid in list(_users(state).keys()):
        ev = risk_eval(state, uid, now)
        if ev["score"] < floor:
            continue
        rows.append({"email": uid, "score": ev["score"], "level": ev["level"],
                     "reasons": ev["reasons"], "exempt": bool(ev["exempt"])})
    rows.sort(key=lambda r: (-r["score"], r["email"]))
    cap = max(1, min(int(limit), 200))
    return {"ok": True, "now": now, "count": len(rows), "rows": rows[:cap]}


# ── 每日入账与充值对账（2026-09-26 资金核对）───────────────────────────────── #
# 口径（钱 vs 权益，两个独立数据源交叉核对）：
#   入账     = pay_server 的 pay_orders.json 里 status ∈ {PAID, GRANT_FAILED} 的订单
#              （GRANT_FAILED = 用户已付款但自动发货失败；卡在 GRANTING 超时同理）。
#   自动发货 = 本服务事件流里 note 以 "alipay-auto" 开头的 grant 事件
#              （pay_server 回调成功后经 /grant 落账；新版 note 带 order_id 可精确对号）。
#   卡密核销 = redeem 事件 —— 线下收款发卡，系统外入账，单列展示、不算资金差异。
# 差异三类（发现即告警，走 _raise_alert → 超管横幅/系统通知/看板）：
#   paid_no_grant        已收款但没发货（critical：用户花了钱没拿到权益，需补发）
#   grant_no_pay         发了货但找不到已收款订单（critical：疑似绕过支付/伪造发货）
#   plan_amount_mismatch 发货套餐与订单实付不符（warn：金额与权益对不上）
PAY_ORDERS_PATH = Path(os.environ.get("VDL_PAY_ORDERS")
                       or str(DATA_PATH.parent / "pay_orders.json"))
RECON_DAYS = 7                  # 每轮回看近 N 天（新差异只告警一次，老差异不重复吵）
# 查账区间的上限（2026-10-09 用户要求「入账可查 1 天/3 天/7 天/1 个月/半年/1 年」）：
# 前端最大是 365（近 1 年），这里留余量到 400。🔴 同一上限在**三层**里都有：
#   桌面 App 本机 server/routers/core.py → ECS worker server/routers/core.py → 本文件。
#   三处必须同步改，否则大区间会被上游先截断成 60 天，还**不报错**（静默给少）。
RECON_DAYS_MAX = 400
RECON_INTERVAL = 600.0          # 后台 10 分钟自扫一轮（对账很轻：两个小文件）
RECON_SEEN_CAP = 1000
GRANTING_STUCK = 1800.0         # 订单卡在 GRANTING 超 30 分钟 = 发货挂了
MATCH_FALLBACK_WINDOW = 172800.0  # 老数据无 order_id 时按 (账号,套餐) 就近配对的窗口 48h
# 🔴 2026-10-09 修：原写死 "alipay-auto"，而 2026-10-09 起支付通道切为虎皮椒，
#   自动发货 note 是 "xunhupay-auto:<order_id>" ⇒ 发货侧对账完全看不到发货，
#   每笔真实付款都被误报「已收款未发货」critical（实测 2 条，其实均已发货）。
#   改为识别任意 "<渠道>-auto:" 前缀：以后新增支付渠道零改动适配。
# 🔴 2026-10-14 修：更早（2026-10-09 前）的自动发货 note 是**裸的** "alipay-auto"
#   （无冒号、无 order_id）⇒ 原正则强制要求冒号，把这批历史发货全判成"非自动发货"，
#   对账 pass 2 兜底配对也就无从触发 ⇒ 老订单误报 paid_no_grant（已收款未发货）critical。
#   冒号改 `(:|$)`：既认现役 `<渠道>-auto:<order_id>`，也认历史整串 `<渠道>-auto`。
AUTO_NOTE_PREFIX = "alipay-auto"          # 兼容旧引用（判定请用 _is_auto_grant_note）
_AUTO_NOTE_RE = re.compile(r"^[A-Za-z0-9_]+-auto(:|$)")


def _is_auto_grant_note(note: Any) -> bool:
    """该 grant 是否由支付渠道自动发货写下。
    现役 note = "<渠道>-auto:<order_id>"；历史（2026-10-09 前）为裸 "<渠道>-auto"。"""
    return bool(_AUTO_NOTE_RE.match(str(note or "")))


# 安全审计 2026-10-11（P1-1「回调重复发权益」）：从自动发货 note 里取
# `(渠道, 订单号)` 唯一键，用于**发货侧**幂等去重。
_ORDER_KEY_RE = re.compile(r"^([A-Za-z0-9_]+)-auto:(\S+)$")


def _order_key_of_note(note: Any) -> str:
    """自动发货 note → "<渠道>:<订单号>" 唯一键；非支付驱动发货返回空串。

    支付渠道会重试回调；并发两笔通知、或「发货成功但订单落盘前进程重启」后重放，
    都会走到 grant 一次以上。旧实现只有调用方（pay_server）的本地订单状态做去重，
    且 `_grant` 调用在锁外 —— 并发时双双通过检查即**重复发货**（会员天数/积分翻倍，
    真实账目损失）。去重必须落在**权威侧**（授权中心），这里就是那道闸。
    """
    m = _ORDER_KEY_RE.match(str(note or "").strip())
    if not m:
        return ""
    return f"{m.group(1).lower()}:{m.group(2)}"


def _grant_prefix_for(order: dict[str, Any]) -> str:
    """该订单对应的自动发货 note 前缀（按支付渠道），用于告警里的补发指引。"""
    mode = str((order or {}).get("mode") or "").lower()
    if "xunhu" in mode:
        return "xunhupay-auto"
    return AUTO_NOTE_PREFIX

# 金额真源（与 deploy/pay_server.py PRICE_MAP 保持一致，单位元；对账报告估算用）
PLAN_PRICE: dict[str, str] = {
    "download_month": "29.80", "download_half_year": "99.90",
    "download_year": "179.00", "ai_5500": "49.90", "ai_15000": "99.90",
    "credits_5000": "50.00", "credits_15000": "99.00",
}


# ── 套餐价格单一真源（2026-10-03）────────────────────────────────────────────
# 背景：桌面后台「套餐与积分成本」原来只写本机 plans.json 覆盖层，云端与网页各用
# 自己的常量，于是出现「App 159/298、网页 99.90/179、实际扣款按云端」的三方分叉。
# 现在授权中心是真源：
#   - 读：POST /api/license/plans（公开，价格本就是公开信息）→ 各端合并到本地默认
#   - 写：POST /api/license/plans_set（管理员 token）→ 落 state["plan_overrides"]
#   - 收款/对账估价统一走 plan_price()，改价后立刻生效，不再有第二份真源。
PLAN_PRICE_UPDATED_KEY = "plan_price_updated_at"


def _plan_overrides(st: dict) -> dict:
    ov = (st or {}).get("plan_overrides")
    return ov if isinstance(ov, dict) else {}


def plan_price(st: dict, plan_code: str) -> str:
    """取某档位金额：优先管理员下发的覆盖，其次内置默认。单位元、两位小数字符串。

    🔴 覆盖表是**两级**结构 {类目: {code: spec}}（plans_set 写入侧即如此），
       早期实现直接 .get(plan_code) 恒取不到覆盖 ⇒ 对账金额一直用内置默认
       （1/3/7 天档更因不在 PLAN_PRICE 里而恒为 0.00）。2026-10-09 修为**遍历类目取**。
    """
    key = str(plan_code or "")
    for cat in (_plan_overrides(st) or {}).values():
        if isinstance(cat, dict) and key in cat:
            ov = cat.get(key)
            if isinstance(ov, dict):
                price = ov.get("price_cny")
                try:
                    if price not in (None, ""):
                        return f"{float(price):.2f}"
                except (TypeError, ValueError):
                    pass
            break
    return PLAN_PRICE.get(key, "0.00")


def plans_payload(st: dict) -> dict:
    """给各端拉取的价格表：{plan_key: {price_cny, days, credits, label, ...}}。

    🔴 2026-10-13：附带 `free_quota`（管理员下发的免费额度覆盖，含 `mode` 口径）。
    客户端在 `/api/license/plans` 的同一次拉取里就能读回「免费额度口径」，
    换机/重装后不再回默认 on（与终身次数共用这条通道，避免再加一个往返）。
    """
    return {
        "plans": _plan_overrides(st),
        "free_quota": _free_quota_overrides(st),
        "updated_at": float((st or {}).get(PLAN_PRICE_UPDATED_KEY) or 0.0),
    }


def _bj_day(ts: float) -> str:
    """账期日切按北京时间（UTC+8）—— 与人工对账习惯一致。"""
    return time.strftime("%Y-%m-%d", time.gmtime(float(ts) + 8 * 3600))


def _bj_dt(ts: float) -> str:
    """精确到分（北京时间 YYYY-MM-DD HH:MM）：2026-10-09 用户要求「日期精确到分」。

    对账明细是钱账，只到「天」没法跟发货日志/授权中心流水对上——一条差异要查明
    得知道是那一分钟发的货。⚠️ 只用于**展示文案**：账期日切（day_rows 汇总键、
    告警去重键 `recon_seen`）必须继续用 `_bj_day`，两者不可互替（改了会把同一天
    的差异拆成多条、汇总表也会裂成多行）。
    """
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(float(ts) + 8 * 3600))


def _read_recon_events(path: Path, kinds: set) -> list:
    """读全量审计流水（events.jsonl），只挑指定 kind。文件缺失/损坏行静默跳过。"""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if ev.get("kind") in kinds:
                    out.append(ev)
    except OSError:
        pass
    return out


_GNO_MONEY_STATES = {"PAID", "GRANT_FAILED"}   # 与 recon_impl 的 in_money 口径同源


def _grant_no_pay_hint(oids: list, all_by_oid: dict) -> str:
    """给 grant_no_pay 差异「自证性质」：订单号根本不存在 vs 订单存在但未收款。

    2026-10-09：原文案一律写「疑似绕过支付/伪造发货调用」，于是**探针留下的一笔**和
    **真正的绕过支付**在面板上长得一模一样，用户只能来问「这两条红字是什么、有没有风险」。
    归并后一行可能引用多个订单号，逐类判定：

    - **不存在**：note 里的订单号在**全量订单表**（含未收款单）里也查不到 ⇒ 该订单号是编的，
      可能是限购探针、也可能是有人直接调发货接口 —— 需核查调用来源，不可默认放过；
    - **未进口径**：订单真实存在，但状态不是 PAID / GRANT_FAILED（PENDING 未付、EXPIRED
      已过期、GRANTING 处理中等）⇒ 按状态给处置建议；
    - **窗口外**：订单已是收款状态却未进 in_money ⇒ 只可能落在账期窗口外（非漏发）。

    返回追加在 detail 后的说明文案；无单可查时返回「note 未携带订单号」的说明。
    """
    missing, paid_out, others = [], [], []
    other_states: set = set()
    for oid in dict.fromkeys(str(x) for x in oids):    # 同一订单号多笔只报一次
        if not oid:
            continue
        o = all_by_oid.get(oid)
        if o is None:
            missing.append(oid)
        elif str(o.get("status")) in _GNO_MONEY_STATES:
            # 已是收款状态却没被配对 ⇒ 只可能落在账期窗口外（in_money 只取近 N 天）
            paid_out.append(f"{oid}（{o.get('status')}）")
        else:
            other_states.add(str(o.get("status") or "").upper())
            others.append(f"{oid}（{o.get('status')}：{_grant_no_pay_status_note(o.get('status'))}）")
    if not missing and not paid_out and not others:
        return "note 未携带订单号 —— 无法核对收款，请核查调用来源"
    parts = []
    if missing:
        head = "、".join(missing[:3]) + ("…" if len(missing) > 3 else "")
        parts.append(f"note 引用的订单号 {head} 在本机订单表中**不存在**"
                     f" —— 该订单号是编的（探针或伪造发货调用），请核查调用来源")
    if others:
        head = "、".join(others[:3]) + ("…" if len(others) > 3 else "")
        # GRANTING = 发货还在跑（下次重扫自动销号），再喊「核查付款是否未完成」会自相矛盾
        tail = "" if other_states <= {"GRANTING"} else " —— 请核查付款是否未完成即发货"
        parts.append(f"订单 {head} **真实存在但未进入已收款口径**{tail}")
    if paid_out:
        head = "、".join(paid_out[:3]) + ("…" if len(paid_out) > 3 else "")
        parts.append(f"订单 {head} **已收款但落在账期窗口外** —— 属跨窗对账，非漏发；"
                     f"窗口内若有发货请另核")
    return "；".join(parts)


def _grant_no_pay_status_note(status: str) -> str:
    """未收款订单的状态白话对照表（用于 grant_no_pay 文案的处置建议）。"""
    s = str(status or "").upper()
    return {
        "PENDING": "付款未完成 / 回调未达",
        "GRANTING": "发货处理中，下次重扫（≤10 分钟）会自动销号",
        "EXPIRED": "订单已过期，从未收款",
        "REFUNDED": "订单已退款",
        "LIMIT_REJECTED": "超限单，应退款而非发货",
    }.get(s, "非已收款状态")


def recon_impl(state: dict[str, Any], days: int = RECON_DAYS,
               now: Optional[float] = None, notify: bool = True) -> dict[str, Any]:
    """每日入账与充值对账：实付订单 vs 自动发货，差异即刻告警。

    幂等：同一差异只在第一次发现时告警（recon_seen 去重表），修复后自动消失。

    `days` = 查账区间（1 ~ RECON_DAYS_MAX），后台自扫固定 `RECON_DAYS`；
    `notify=False` = **只看报告、不动告警状态**（用户手工拉大区间时用：否则把近一年
    的账一拉，窗口外从未扫过的历史差异会一次性炸成 N 条新 critical 告警并被钉住）。
    """
    now = time.time() if now is None else float(now)
    days = max(1, min(int(days), RECON_DAYS_MAX))
    horizon = 86400.0 * days

    # 入账侧：已收款订单（PAID / GRANT_FAILED / 卡死的 GRANTING）
    orders = []
    try:
        raw = json.loads(PAY_ORDERS_PATH.read_text(encoding="utf-8") or "{}")
        for oid, o in (raw or {}).items():
            if isinstance(o, dict):
                o = dict(o)
                o["order_id"] = str(oid)
                orders.append(o)
    except (OSError, json.JSONDecodeError, ValueError):
        pass                                        # 支付文件缺失 = 尚无线上订单

    def _paid_at(o: dict) -> float:
        return float(o.get("paid_at") or o.get("created_at") or 0)

    in_money = [o for o in orders
                if o.get("status") in ("PAID", "GRANT_FAILED")
                and now - _paid_at(o) <= horizon]
    in_money += [o for o in orders
                 if o.get("status") == "GRANTING"
                 and now - float(o.get("created_at") or 0) > GRANTING_STUCK
                 and now - float(o.get("created_at") or 0) <= horizon]

    # 发货侧：自动发货 grant 事件（管理员手动 grant 的 note 无 "-auto:"，不计资金口径）
    grants = [e for e in _read_recon_events(EVENT_LOG_PATH, {"grant"})
              if _is_auto_grant_note(e.get("note"))
              and now - float(e.get("at", 0)) <= horizon]

    matched_orders: set = set()
    matched_grants: set = set()
    mismatches: list = []

    def _amount(o: dict) -> str:
        return str(o.get("amount") or plan_price(state, str(o.get("plan_code"))) or "?")

    # pass 1：order_id 精确对号（note = "<渠道>-auto:<order_id>"）
    by_oid = {o["order_id"]: o for o in in_money}
    # 全量订单表（含未收款单）：只用于 grant_no_pay 的「订单号到底存不存在」判定，
    # 不参与配对 —— 配对必须只用 in_money，否则未收款单会把差异消化掉（漏报）。
    all_by_oid = {o["order_id"]: o for o in orders}
    for g in grants:
        note = str(g.get("note") or "")
        oid = note.split(":", 1)[1].strip() if ":" in note else ""
        o = by_oid.get(oid)
        if o:
            matched_orders.add(oid)
            matched_grants.add(id(g))
            if str(o.get("plan_code")) != str(g.get("plan_code")):
                mismatches.append({
                    "kind": "plan_amount_mismatch", "order_id": oid, "at": _paid_at(o),
                    "email": str(g.get("email") or ""),
                    "detail": f"订单 {oid}（{_bj_dt(_paid_at(o))}）实付「{o.get('plan_code')}」{_amount(o)} 元，"
                              f"但发货「{g.get('plan_code')}」—— 金额与权益不符，请人工核对"})

    # pass 2：兜底（升级前的老订单/老 note 没有 order_id）—— 按 (账号,套餐) 时间就近配对
    for g in grants:
        if id(g) in matched_grants:
            continue
        cand = [o for o in in_money
                if o["order_id"] not in matched_orders
                and str(o.get("email")) == str(g.get("email"))
                and str(o.get("plan_code")) == str(g.get("plan_code"))
                and abs(_paid_at(o) - float(g.get("at", 0))) <= MATCH_FALLBACK_WINDOW]
        if cand:
            o = min(cand, key=lambda x: abs(_paid_at(x) - float(g.get("at", 0))))
            matched_orders.add(o["order_id"])
            matched_grants.add(id(g))

    for o in in_money:
        if o["order_id"] in matched_orders:
            continue
        mismatches.append({
            "kind": "paid_no_grant", "order_id": o["order_id"], "at": _paid_at(o),
            "email": str(o.get("email") or ""),
            "detail": f"订单 {o['order_id']} 已收款 {_amount(o)} 元"
                      f"（{o.get('plan_code')}，{_bj_dt(_paid_at(o))}，状态 {o.get('status')}）"
                      f"但没有对应发货 —— 用户花了钱没拿到权益，请立即补发"
                      f"（grant note 带 {_grant_prefix_for(o)}:{o['order_id']} 即可自动对账销号）"})
    # 🔴 2026-10-09 二次修：原先**一笔事件一行**。同一次异常若连发多笔（探针连发、
    #   渠道重试、批量伪造），面板上就是 N 行**内容完全相同**的红字 —— 明细只到分，
    #   13 秒内的两笔都渲染成同一个 HH:MM，既看不出是「N 笔」也看不出时间跨度，
    #   用户只能看到「两条一模一样的告警」。改为按 (账号, 档位, 账期日) 归并成一行：
    #   明细带**总笔数**，多笔时改用**到秒的时刻区间**（分钟级无从区分同分钟的多笔），
    #   单笔时保持原来的分钟级文案。day_rows 的差异计数随之由「事件数」变为「异常次数」，
    #   这才与 paid_no_grant 的口径一致（后者本就按 (订单,账号,天) 一行一条）。
    unmatched: dict = {}
    for g in grants:
        if id(g) in matched_grants:
            continue
        at = float(g.get("at", 0))
        _note = str(g.get("note") or "")
        # note 形如 `xunhupay-auto:<order_id>`；探针能**伪造渠道前缀**，但编不出真实订单号，
        # 所以把订单号收集起来，交给 _grant_no_pay_hint 自证差异性质。
        _oid = _note.split(":", 1)[1].strip() if ":" in _note else ""
        key = (str(g.get("email") or ""), str(g.get("plan_code") or ""), _bj_day(at))
        it = unmatched.get(key)
        if it is None:
            unmatched[key] = {"n": 1, "first": at, "last": at, "oids": [_oid]}
        else:
            it["n"] += 1
            it["first"] = min(it["first"], at)
            it["last"] = max(it["last"], at)
            it["oids"].append(_oid)
    for (email, plan, day), it in unmatched.items():
        if it["n"] > 1:
            t1 = time.strftime("%H:%M:%S", time.gmtime(it["first"] + 8 * 3600))
            t2 = time.strftime("%H:%M:%S", time.gmtime(it["last"] + 8 * 3600))
            when = f"{day} {t1} ~ {t2}（共 {it['n']} 笔）"
        else:
            when = _bj_dt(it["first"])
        mismatches.append({
            "kind": "grant_no_pay", "order_id": "", "at": it["last"], "email": email,
            "detail": f"{when} 给 {email} 自动发货「{plan}」但找不到已收款订单"
                      f" —— {_grant_no_pay_hint(it.get('oids') or [], all_by_oid)}"})

    # 按天汇总（展示用；差异在行内带明细）
    day_rows: dict = {}

    def _row(d: str) -> dict:
        return day_rows.setdefault(d, {"date": d, "income_yuan": 0.0, "paid_orders": 0,
                                       "grant_failed": 0, "limit_refund": 0,
                                       "auto_grants": 0, "redeems": 0,
                                       "redeem_income_est": 0.0, "mismatch": 0})

    for o in in_money:
        r = _row(_bj_day(_paid_at(o)))
        r["paid_orders"] += 1
        if o.get("status") == "GRANT_FAILED":
            r["grant_failed"] += 1
        try:
            r["income_yuan"] += float(o.get("amount")
                                      or plan_price(state, str(o.get("plan_code"))) or 0)
        except (TypeError, ValueError):
            pass
    for g in grants:
        _row(_bj_day(float(g.get("at", 0))))["auto_grants"] += 1

    # 🔴 限购拒绝单（2026-10-09）：钱已收，但按「活动价每人限购 1 次」**不该发货** ——
    #    正确处置是**退款**，绝不能进 in_money（那会触发 paid_no_grant「请立即补发」，
    #    等于给已经超限的账号又发一次权益）。这里单独列出并告警退款。
    #    产生场景：修好限购前用户重复下单留下的 PENDING 单，若之后被支付就会走到这里。
    limit_rejected = [o for o in orders
                      if o.get("status") == "LIMIT_REJECTED"
                      and now - _paid_at(o) <= horizon]
    for o in limit_rejected:
        r = _row(_bj_day(_paid_at(o)))
        r["limit_refund"] += 1
        # 超限单的款项会退回，故**不计入收入**（避免虚增营业额），只计数提示
        mismatches.append({
            "kind": "limit_rejected_refund", "order_id": o.get("order_id", ""),
            "at": _paid_at(o), "email": str(o.get("email") or ""),
            "detail": f"订单 {o.get('order_id')} 用户已付 "
                      f"{str(o.get('amount') or plan_price(state, str(o.get('plan_code'))) or '?')} 元"
                      f"（{o.get('plan_code')}，{_bj_dt(_paid_at(o))}），但该档每人限购已满，"
                      f"属超限重复购买 —— **请按订单退款**，不要补发"})
    for e in _read_recon_events(EVENT_LOG_PATH, {"recharge"}):
        if now - float(e.get("at", 0)) <= horizon:
            r = _row(_bj_day(float(e.get("at", 0))))
            r["redeems"] += 1
            try:
                r["redeem_income_est"] += float(plan_price(state, str(e.get("plan_code"))) or 0)
            except (TypeError, ValueError):
                pass
    for m in mismatches:
        _row(_bj_day(m["at"]))["mismatch"] += 1

    # 告警：只对「第一次发现」的差异响铃（recon_seen 持久去重，修复后销号）
    # notify=False（用户手工查账）时整块跳过：不写 recon_seen、不响铃，纯报告。
    new_alerts = 0
    if notify:
        seen = {str(x) for x in (state.get("recon_seen") or [])}
        for m in mismatches:
            key = f"{m['kind']}:{m.get('order_id') or ''}:{m.get('email') or ''}:{_bj_day(m['at'])}"
            if key in seen:
                continue
            seen.add(key)
            _raise_alert(state, "recon_mismatch",
                         "warn" if m["kind"] == "plan_amount_mismatch" else "critical",
                         m.get("email") or "", "", m["detail"], now)
            new_alerts += 1
        state["recon_seen"] = sorted(seen)[-RECON_SEEN_CAP:]

    return {"ok": True, "days": days, "checked_at": now,
            "income_yuan_total": round(sum(r["income_yuan"] for r in day_rows.values()), 2),
            "day_rows": sorted(day_rows.values(), key=lambda r: r["date"], reverse=True),
            "mismatches": mismatches, "new_alerts": new_alerts}


def _recon_loop() -> None:
    """后台对账线程：每 10 分钟扫一遍近 7 天，差异即时告警（只响一次）。"""
    while True:
        try:
            with _LOCK:
                st = _load_state()
                recon_impl(st, days=RECON_DAYS, now=time.time())
                _save_state(st)
        except Exception as e:                      # 对账失败绝不影响业务
            sys.stderr.write(f"[license] recon loop error: {e}\n")
        time.sleep(RECON_INTERVAL)


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
            action = path.rsplit("/", 1)[-1]

            # --- 公开只读：套餐价格表（价格本就是公开信息，不含任何凭据）---
            # 放在用户接口限流之前：各端启动/刷新都要拉，量大且无副作用。
            # 空 body 的裸 POST/GET 也放行（只读、无副作用，不需要令牌）。
            if action == "plans":
                try:
                    self._body()  # 有 body 就读掉，避免 keep-alive 串包
                except ApiError:
                    pass
                with _LOCK:
                    out = {"ok": True, **plans_payload(_load_state())}
                return self._json(200, out)

            data = self._body()

            # --- 管理员接口（不做 IP 限流，走 token）---
            if action in ("gen", "revoke", "grant", "users",
                          "ban", "adjust", "setstate", "usage",
                          "set-admin", "import-account", "delete-user",
                          "alerts", "alerts_ack", "recon", "plans_set",
                            # 🔴 2026-10-06 免费额度覆盖下发（终身免费次数）
                            "free_quota_set",
                            # 🔴 安全审计 2026-10-11（P0-4）：受信额度退还。
                            #    客户端不得自行退还（否则消费→退还→再消费可无限白嫖），

                            # 退还只允许管理端凭消费事件号一次性冲正。
                            "cloud_quota_refund",
                            # 🔴 2026-10-13 安装/插件统计（按平台）聚合查询（只读）
                            "stat_query",
                            # 🔴 2026-10-14 反破解风控（第 1 批纯观测）：风险画像查询 / 人工抹平 / 队列
                            "risk_query", "risk_clear", "risk_top"):
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
                    elif action == "ban":
                        out = ban_impl(st, str(data.get("email") or ""),
                                       bool(data.get("banned", data.get("flag", True))),
                                       now, str(data.get("note") or ""))
                        _save_state(st)
                    elif action == "set-admin":
                        # 2026-10-16：超管标记上云（各端登录时以云端快照为准）
                        out = set_admin_impl(st, str(data.get("email") or ""),
                                             bool(data.get("is_admin", True)), now)
                        _save_state(st)
                    elif action == "import-account":
                        # 2026-10-16：存量账号搬迁（本机 users.json + memberships
                        # → 云端，盐+哈希+权益原样；用户「真实账号的所有数据也得导入」）
                        out = import_account_impl(
                            st, str(data.get("email") or ""),
                            str(data.get("salt") or ""),
                            str(data.get("pw_hash") or ""), now,
                            float(data.get("created_at") or 0),
                            bool(data.get("is_admin", False)),
                            data.get("authority"),
                            data.get("devices"))
                        _save_state(st)
                    elif action == "delete-user":
                        # 2026-10-16：历史测试号清理（幂等；管理端持令牌调用）
                        out = delete_user_impl(st, str(data.get("email") or ""), now)
                        _save_state(st)
                    elif action == "adjust":
                        try:
                            delta = int(data.get("delta"))
                        except (TypeError, ValueError):
                            raise ApiError(400, "BAD_DELTA", "delta 必须为整数")
                        out = adjust_impl(st, str(data.get("email") or ""),
                                          str(data.get("pool") or "permanent"),
                                          delta, now, str(data.get("note") or ""))
                        _save_state(st)
                    elif action == "setstate":
                        def _num(key, default=-1.0, cast=float):
                            try:
                                v = data.get(key)
                                return cast(v) if v is not None else default
                            except (TypeError, ValueError):
                                return default
                        out = setstate_impl(st, str(data.get("email") or ""), now,
                                            member_until_dl=_num("member_until_dl"),
                                            member_until_ai=_num("member_until_ai"),
                                            perm_credits=int(_num("perm_credits", -1, int)),
                                            ai_credits_left=int(_num("ai_credits_left", -1, int)),
                                            risk_exempt=int(_num("risk_exempt", -1, int)))
                        _save_state(st)
                    elif action == "usage":
                        out = usage_impl(st, str(data.get("email") or ""), now)
                    elif action == "alerts":
                        try:
                            since = float(data.get("since") or 0)
                        except (TypeError, ValueError):
                            since = 0.0
                        out = alerts_impl(st, since=since,
                                          unseen_only=bool(data.get("unseen_only")),
                                          limit=int(data.get("limit") or 100))
                    elif action == "alerts_ack":
                        out = alerts_ack_impl(st, data.get("ids"))
                        _save_state(st)
                    elif action == "recon":
                        try:
                            d = int(data.get("days") or RECON_DAYS)
                        except (TypeError, ValueError):
                            d = RECON_DAYS
                        # 查账区间 ≤ 自扫窗口时维持原行为（会告警、写 recon_seen）；更大区间
                        # 是「查历史账」只出报告 —— 否则一次拉近一年，窗口外从未扫过的历史
                        # 差异会炸成一堆新 critical 告警并被 recon_seen 钉住。
                        out = recon_impl(st, days=d, now=now, notify=d <= RECON_DAYS)
                        _save_state(st)
                    elif action == "plans_set":
                        # 桌面后台「套餐与积分成本」保存时下发：整表覆盖式写入
                        # （按档位 key 合并由调用方保证），随后收款/对账估价立即生效。
                        plans = data.get("plans")
                        if not isinstance(plans, dict) or not plans:
                            raise ApiError(400, "BAD_PLANS", "plans 必须是非空对象")
                        clean: dict[str, dict[str, Any]] = {}
                        for cat, obj in plans.items():
                            if not isinstance(obj, dict):
                                continue
                            for key, spec in obj.items():
                                if not isinstance(spec, dict):
                                    continue
                                try:
                                    price = float(spec.get("price_cny"))
                                except (TypeError, ValueError):
                                    raise ApiError(400, "BAD_PRICE", f"{key} 价格必须是数字")
                                item = dict(spec)
                                item["price_cny"] = round(price, 2)
                                clean.setdefault(str(cat), {})[str(key)] = item
                        if not clean:
                            raise ApiError(400, "BAD_PLANS", "plans 内容为空")
                        st["plan_overrides"] = clean
                        st[PLAN_PRICE_UPDATED_KEY] = now
                        _save_state(st)
                        out = {"ok": True, "plans": clean,
                               "updated_at": now}
                    elif action == "free_quota_set":
                        # 🔴 2026-10-06 免费额度覆盖：后台改终身次数 → 下发中心，
                        #   中心按覆盖值判定放行（此前只写客户端本机，改了不生效）。
                        #   ⚠️ 用分支外已解析的 data，**不要**再 self._body()：
                        #   body 流已被 _require_admin 消费过，二次读会阻塞等流（实测挂起）。
                        out = free_quota_impl(
                            st, now,
                            cloud_lifetime=(data["cloud_lifetime"]
                                            if "cloud_lifetime" in data else _UNSET),
                            # 2026-10-13：免费额度口径 mode 也随之下发/清除。
                            mode=(data["mode"] if "mode" in data else _UNSET))
                    elif action == "cloud_quota_refund":
                        # 🔴 安全审计 2026-10-11（P0-4）受信额度退还：
                        #   客户端自退 = 消费→退还→再消费的无限套利（真实云端大模型
                        #   调用成本 ¥0.04~0.19/次），故退还只允许管理端发起，
                        #   且必须凭**既有消费事件号**一次性冲正（重复退还 → 409）。
                        out = cloud_quota_impl(st, str(data.get("token") or ""),
                                               now, SECRET,
                                               lifetime=data.get("lifetime") or 0,
                                               daily=data.get("daily") or 0,
                                               refund=True,
                                               resource=str(data.get("resource") or ""),
                                               ip=ip,
                                               allow_refund=True,
                                               event_id=str(data.get("event_id") or ""))
                        _save_state(st)
                    elif action == "stat_query":
                        # 🔴 2026-10-13 安装 / 插件次数统计（按平台）：纯只读聚合，
                        #   不写 state。设备画像（platform/ext/first_seen）随心跳与
                        #   登录落入设备记录，这里聚合给管理面板。
                        out = {"ok": True, "stats": stat_aggregate(st, now)}
                    elif action == "risk_query":
                        # 现场重算会刷新 risk 摘要（peak/updated），须落盘
                        out = risk_query_impl(st, str(data.get("email") or ""), now)
                        _save_state(st)
                    elif action == "risk_clear":
                        out = risk_clear_impl(st, str(data.get("email") or ""), now,
                                              str(data.get("note") or ""))
                        _save_state(st)
                    elif action == "risk_top":
                        try:
                            _lim = int(data.get("limit") or 50)
                            _floor = int(data.get("min_score") or 1)
                        except (TypeError, ValueError):
                            _lim, _floor = 50, 1
                        out = risk_top_impl(st, now, limit=_lim, min_score=_floor)
                        _save_state(st)     # 遍历时会刷新各账号 risk 摘要，须落盘
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
                                         now, SECRET,
                                         meta=(data.get("meta")
                                               if isinstance(data.get("meta"), dict) else None))
                    if out.get("ok"):
                        _save_state(st)
                elif action == "password":
                    # App 本机改密 / 忘记密码重置后同步过来（两端密码不分叉）；
                    # 2026-10-16 起也承接超管后台重置（admin_token 通道）。
                    out = password_impl(st, str(data.get("email") or ""),
                                        str(data.get("new_password") or ""), now, SECRET,
                                        old_password=str(data.get("old_password") or ""),
                                        token=str(data.get("token") or ""),
                                        admin_token=str(data.get("admin_token") or ""))
                    _save_state(st)
                elif action == "redeem":
                    try:
                        out = redeem_impl(st, str(data.get("token") or ""),
                                          str(data.get("code") or "").strip(), now,
                                          SECRET, ip=ip)
                    finally:
                        # 失败路径 impl 内已记事件/告警；成败都要把 ring/alerts 落盘
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
                elif action == "spend":
                    out = spend_impl(st, str(data.get("token") or ""),
                                     data.get("items"), now, SECRET, ip=ip)
                    _save_state(st)
                elif action == "daily":
                    out = daily_impl(st, str(data.get("token") or ""),
                                     data.get("items"), now, SECRET, ip=ip)
                    _save_state(st)
                elif action == "trial_claim":
                    out = trial_claim_impl(st, str(data.get("token") or ""),
                                          str(data.get("op") or ""),
                                          str(data.get("mode") or "once"),
                                          now, SECRET, ip=ip)
                    _save_state(st)
                elif action == "plan_usage":
                    # 活动价限购取数：客户端 / 收款服务查该账号各档已购次数
                    out = plan_usage_impl(st, str(data.get("token") or ""),
                                          SECRET, now,
                                          str(data.get("plan_code") or ""))
                elif action == "cloud_quota":
                    out = cloud_quota_impl(st, str(data.get("token") or ""),
                                           now, SECRET,
                                           lifetime=data.get("lifetime") or 0,
                                           daily=data.get("daily") or 0,
                                           refund=bool(data.get("refund")),
                                           resource=str(data.get("resource") or ""),
                                           ip=ip)
                    _save_state(st)
                else:
                    return self._json(404, {"ok": False, "error": "unknown action"})
            return self._json(200, out)
        except ApiError as e:
            return self._json(e.status, {"ok": False, "error": e.msg, "code": e.code})

    @staticmethod
    def _device(data: dict[str, Any]) -> dict[str, Any]:
        d = data.get("device")
        if not isinstance(d, dict):
            d = {}
            if isinstance(data.get("fingerprint"), str):
                d = {"fp": data["fingerprint"]}
        fp = str(d.get("fp") or "").strip()[:128]
        name = str(d.get("name") or "").strip()[:64]
        if not fp:
            raise ApiError(400, "NO_DEVICE", "缺少设备标识 fingerprint")
        out: dict[str, Any] = {"fp": fp, "name": name or "未命名设备"}
        meta = data.get("meta")
        if isinstance(meta, dict) and meta:
            out["meta"] = meta          # 设备画像（安装/插件统计），_attach_device 清洗
        return out

    def _require_admin(self, data: dict[str, Any]) -> None:
        global _ADMIN_FAILS, _ADMIN_LOCK_UNTIL
        if not ADMIN_TOKEN:
            raise ApiError(500, "NO_ADMIN_TOKEN", "服务端未配置 VDL_LICENSE_ADMIN_TOKEN")
        now = time.time()
        with _ADMIN_GUARD_LOCK:
            if now < _ADMIN_LOCK_UNTIL:
                raise ApiError(429, "RATE_LIMITED",
                               f"管理员鉴权失败次数过多，请 {int(_ADMIN_LOCK_UNTIL - now) + 1} 秒后再试")
        tok = str(data.get("token") or "")
        if not hmac.compare_digest(tok, ADMIN_TOKEN):
            # 安全审计 2026-10-11（R-07）：登记失败；连续超限则锁定一段时间。
            with _ADMIN_GUARD_LOCK:
                _ADMIN_FAILS += 1
                if _ADMIN_FAILS >= _ADMIN_MAX_FAILS:
                    _ADMIN_LOCK_UNTIL = time.time() + _ADMIN_LOCK_SECONDS
                    _ADMIN_FAILS = 0
            raise ApiError(403, "FORBIDDEN", "管理员 token 不正确")
        with _ADMIN_GUARD_LOCK:
            _ADMIN_FAILS = 0

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
    # 🔴 2026-10-09：对账的「入账侧」依赖 pay_server 的 pay_orders.json。该路径不是本服务
    #    的自有数据，只能靠 VDL_PAY_ORDERS 显式指过来（默认落在本服务 data/ 下，那里通常
    #    **没有**这个文件）。一旦环境变量丢失/改名，orders 会静默读成空表 ⇒ 收入恒为 ¥0、
    #    paid_no_grant（已收款未发货）**永不告警** —— 收款链路整段失明却不报错。
    #    这里在启动时显式吼一声，避免「对账看着一切正常、其实什么都没对」。
    if not PAY_ORDERS_PATH.exists():
        sys.stderr.write(
            f"[license] ⚠️ 对账入账侧为空：订单文件不存在 {PAY_ORDERS_PATH}\n"
            f"[license]   → 请确认 VDL_PAY_ORDERS 指向 pay_server 的 pay_orders.json；"
            f"否则收入恒为 0 且「已收款未发货」不会告警。\n")
    # 每日入账/充值对账后台线程（10 分钟一轮，差异自动告警；失败不影响业务）
    threading.Thread(target=_recon_loop, daemon=True, name="recon").start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[license] listening on 127.0.0.1:{PORT}  data={DATA_PATH}"
          f"  pay_orders={PAY_ORDERS_PATH}"
          f"  max_devices={MAX_DEVICES}"
          f"  secret={'set' if SECRET else 'MISSING'}"
          f"  admin={'set' if ADMIN_TOKEN else 'MISSING'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
