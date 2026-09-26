"""server/cloud_link.py — web 版 ↔ 授权中心（ECS 8902）的账号 / 权益桥。

## 为什么有它（2026-09-26「打通 web 与 App 用户数据」）

在此之前网页版是**另一套账号库**：`/opt/vdl-worker/server/routers/auth.py` 只写本机
`users.json`，全库就一个占位账号；App 的账号/会员在授权中心（`deploy/license_server.py`）。
两边互不相认 —— App 注册的号在网页版登不上，网页版注册的号 App 也不认，会员更谈不上。

打通后的分工（与 App 同一套语义）：
  · **授权中心 = 账号 + 会员 + 积分的唯一权威**；
  · 本机 `auth_store` 退化为「镜像 + 离线兜底 + 功能门禁凭据（bearer）的签发方」；
  · 会员/积分取云端 `authority` 快照，**覆盖**本机 `memberships/{uid}.json`（防篡改）。

## 浏览器设备号

授权中心 register/login 强制带 `device.fp`（`MAX_DEVICES=2` 防共享）。网页版用
服务端签发、存在 cookie `vdl_dev` 里的 uuid 作为 fp（首次响应 Set-Cookie）。
**网页版因此占用一个设备位**，不做豁免——否则「账号共享」就能绕开设备数限制。

## 开关

`VDL_CLOUD_LINK=0` 关闭全部云端联动（离线测试 / 排障用），此时退化为纯本机账号。
任何云端异常都**不让用户白屏**：登录回落到本机账号表，权益保留上一次同步值。
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Optional

import license_client

log = logging.getLogger("vdl.cloud_link")

COOKIE_NAME = "vdl_dev"
COOKIE_MAX_AGE = 3600 * 24 * 365 * 3      # 3 年，够用
AUTHORITY_MIN_INTERVAL = 60.0             # 状态查询时最多每 60s 打一次云端

# store 路径 -> 上次刷新时间（进程内节流，避免个人中心轮询把云端打爆）
_LAST_REFRESH: dict[str, float] = {}


# ── 开关 ───────────────────────────────────────────────────────────────────
def link_enabled() -> bool:
    return (os.environ.get("VDL_CLOUD_LINK") or "1").strip().lower() not in (
        "0", "off", "false", "no")


# ── 浏览器设备号 ────────────────────────────────────────────────────────────
def read_fp(request: Any) -> str:
    """读浏览器设备号 cookie（没有则空串）。"""
    try:
        return str(request.cookies.get(COOKIE_NAME) or "").strip()[:128]
    except Exception:  # noqa: BLE001 — 非 FastAPI request / 无 cookies
        return ""


def issue_fp() -> str:
    return "web-" + uuid.uuid4().hex[:24]


def ensure_fp(request: Any) -> str:
    """取设备号；缺失时读请求头里前端可选携带的 `X-VDL-Device`，否则空串。

    调用方拿到空串时应自行 `issue_fp()` 并在响应里 Set-Cookie（见 routers/auth.py）。
    """
    fp = read_fp(request)
    if fp:
        return fp
    try:
        fp = str(request.headers.get("x-vdl-device") or "").strip()[:128]
    except Exception:  # noqa: BLE001
        fp = ""
    return fp


def device_name(request: Any) -> str:
    """给授权中心的设备名（设备管理页展示用，粗略识别浏览器/系统即可）。"""
    ua = ""
    try:
        ua = str(request.headers.get("user-agent") or "")
    except Exception:  # noqa: BLE001
        ua = ""
    low = ua.lower()
    browser = "浏览器"
    for key, label in (("micromessenger", "微信"), ("edg/", "Edge"), ("chrome/", "Chrome"),
                       ("firefox/", "Firefox"), ("safari/", "Safari")):
        if key in low:
            browser = label
            break
    system = "未知系统"
    for key, label in (("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"),
                       ("mac os x", "macOS"), ("windows", "Windows"), ("linux", "Linux")):
        if key in low:
            system = label
            break
    return f"网页版 · {system}/{browser}"[:64]


# ── 云端调用（统一把网络异常包成 dict，不抛给路由）─────────────────────────
def _unreachable(e: Exception) -> dict[str, Any]:
    log.warning("授权中心不可达: %s", e)
    return {"ok": False, "code": "CLOUD_UNREACHABLE",
            "error": f"授权中心暂时不可达（{e}）"}


def try_register(email: str, password: str, fp: str, name: str) -> dict[str, Any]:
    try:
        return license_client.register_remote(email, password, fp, name)
    except license_client.LicenseCloudError as e:
        return _unreachable(e)
    except Exception as e:  # noqa: BLE001 — 兜底：任何异常都不能让登录 500
        return _unreachable(e)


def try_login(email: str, password: str, fp: str, name: str) -> dict[str, Any]:
    try:
        return license_client.login_remote(email, password, fp, name)
    except license_client.LicenseCloudError as e:
        return _unreachable(e)
    except Exception as e:  # noqa: BLE001
        return _unreachable(e)


# ── 本机账号镜像 ────────────────────────────────────────────────────────────
def mirror_local_account(identifier: str, password: str) -> Optional[str]:
    """把云端已确认的账号落成本机镜像，返回 user_id。

    本机 `auth_store` 依然负责签发 bearer、做功能门禁与离线登录，所以云端认了之后
    本机必须有一份同号同密码的记录：
      · 本机已有且密码一致 → 直接用；
      · 本机没有 → 建号；
      · 本机有但密码不同（两端分叉）→ **以云端为准**覆盖本机密码（云端是权威）。
    """
    ident = (identifier or "").strip().lower()
    if not ident:
        return None
    from auth_store import authenticate, create_user, reset_password
    uid = authenticate(ident, password)
    if uid:
        return uid
    uid = create_user(ident, password)
    if uid:
        return uid
    try:
        reset_password(ident, password)
    except Exception as e:  # noqa: BLE001
        log.warning("本机镜像改密失败 ident=%s: %s", ident, e)
        return None
    return authenticate(ident, password)


# ── 权益落地 ────────────────────────────────────────────────────────────────
def apply_authority(store: Any, account: Optional[dict]) -> dict[str, Any]:
    """把云端权益快照写进本机 store。

    带 `authority.v>=1` → 覆盖式（云端真值，本地篡改一联网即回滚）；
    老服务端无快照 → 退回按 purchase id 幂等追加。
    """
    acct = account or {}
    auth = acct.get("authority") or {}
    try:
        if isinstance(auth, dict) and int(auth.get("v") or 0) >= 1:
            return store.apply_cloud_authoritative(acct)
        return store.apply_cloud_purchases(acct.get("purchases") or [])
    except Exception as e:  # noqa: BLE001 — 权益落地失败不影响登录本身
        log.warning("权益落地失败: %s", e)
        return {"ok": False, "reason": "apply_failed", "error": str(e)}


def apply_login(store: Any, email: str, token: str, account: Optional[dict],
                fp: str, name: str) -> dict[str, Any]:
    """登录/注册成功后的完整落地：登录态 + 权益快照 + 清 evicted 标记。"""
    out: dict[str, Any] = {}
    try:
        store.save_account(email, token, account, fp=fp, name=name)
    except Exception as e:  # noqa: BLE001
        log.warning("save_account 失败: %s", e)
        out["save_error"] = str(e)
    out["authority"] = apply_authority(store, account)
    _LAST_REFRESH[_key(store)] = time.time()
    return out


def _key(store: Any) -> str:
    return str(getattr(store, "path", "") or id(store))


def refresh_authority(store: Any, force: bool = False,
                      min_interval: float = AUTHORITY_MIN_INTERVAL) -> dict[str, Any]:
    """用云端权威快照校准本机权益（`/api/member/status` 前调用）。

    节流：同一 store 默认 60s 内只打一次云端。心跳同时起到「我这台还在不在
    两台名额里」的作用 —— 被挤掉返回 DEVICE_EVICTED，本机降级免费档。
    """
    if not link_enabled():
        return {"ok": False, "reason": "disabled"}
    try:
        sess = store.cloud_session()
    except Exception:  # noqa: BLE001 — 老 store / 未登录
        return {"ok": False, "reason": "no_session"}
    token = str(sess.get("token") or "")
    fp = str(sess.get("fp") or "")
    if not token or not fp:
        return {"ok": False, "reason": "not_logged_in"}
    k = _key(store)
    if not force and (time.time() - _LAST_REFRESH.get(k, 0.0)) < min_interval:
        return {"ok": True, "skipped": "throttled"}
    _LAST_REFRESH[k] = time.time()
    try:
        r = license_client.heartbeat_remote(token, fp)
    except Exception as e:  # noqa: BLE001 — 云端不可达时保留上次同步值（fail-open）
        return _unreachable(e)
    acct = r.get("account") or {}
    if r.get("ok"):
        store.set_evicted(False)
        if acct:
            apply_authority(store, acct)
        flush_pending_spends(store)      # 顺带把离线期积压的扣减补报掉
        return {"ok": True}
    code = str(r.get("code") or "")
    if code == "DEVICE_EVICTED":
        store.set_evicted(True)
    if acct:                      # 封禁等场景云端也会带 account
        apply_authority(store, acct)
    return {"ok": False, "code": code, "error": r.get("error") or "云端校验未通过"}


# ── 积分扣减上云（本地余额是缓存，消耗流水以云端为准）──────────────────────
def _pending(store: Any) -> list[dict]:
    try:
        store._ensure_loaded()
        meta = store._state.setdefault("meta", {})
        q = meta.setdefault("pending_spends", [])
        if not isinstance(q, list):
            q = meta["pending_spends"] = []
        return q
    except Exception:  # noqa: BLE001
        return []


def _queue(store: Any, items: list[dict]) -> None:
    try:
        q = _pending(store)
        q.extend(items)
        store._state["meta"]["pending_spends"] = q[-200:]
        store._persist()
    except Exception:  # noqa: BLE001
        pass


def _report(store: Any, items: list[dict]) -> None:
    """同步上报（后台线程里跑）：成功后用云端权威快照校准本机余额。"""
    try:
        token = str(store.cloud_session().get("token") or "")
        if not token or not items:
            return
        r = license_client.spend_remote(token, items)
        if r and r.get("ok"):
            auth = r.get("authority") or {}
            if auth:
                store.apply_cloud_authoritative({"authority": auth})
        else:
            _queue(store, items)
    except Exception:  # noqa: BLE001 — 断网/超时：排队下次补报
        _queue(store, items)


def report_spend_async(store: Any, amount: int, ai_taken: int = 0,
                       perm_taken: int = 0, reason: str = "") -> None:
    """`MembershipStore.spend_credits` 的上云钩子（fire-and-forget，不阻塞主流程）。

    items 按实际扣减拆分（先 AI 后永久），幂等 id 唯一 → 重试/补报不会重复扣。
    """
    if not link_enabled():
        return
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("VDL_OFFLINE_TESTS"):
        return
    if int(ai_taken) <= 0 and int(perm_taken) <= 0:
        return
    ev = uuid.uuid4().hex[:16]
    items: list[dict] = []
    if int(ai_taken) > 0:
        items.append({"pool": "ai", "cost": int(ai_taken), "op": reason[:40],
                      "id": f"{ev}-ai"})
    if int(perm_taken) > 0:
        items.append({"pool": "permanent", "cost": int(perm_taken),
                      "op": reason[:40], "id": f"{ev}-perm"})
    import threading
    try:
        threading.Thread(target=_report, args=(store, items), daemon=True,
                         name="vdl-spend-report").start()
    except Exception:  # noqa: BLE001 — 起不了线程就排队
        _queue(store, items)


def flush_pending_spends(store: Any) -> None:
    """联网时补报离线期间积累的扣减（心跳/状态刷新成功后调用）。"""
    if not link_enabled():
        return
    try:
        items = list(_pending(store))
        if not items:
            return
        token = str(store.cloud_session().get("token") or "")
        if not token:
            return
        r = license_client.spend_remote(token, items)
        if r and r.get("ok"):
            store._state["meta"]["pending_spends"] = []
            store._persist()
            auth = r.get("authority") or {}
            if auth:
                store.apply_cloud_authoritative({"authority": auth})
    except Exception:  # noqa: BLE001 — 仍离线：队列留着下次再试
        pass


# ── 改密同步（两端一套密码）────────────────────────────────────────────────
def push_password(email: str, new_password: str, old_password: str = "",
                  token: str = "") -> dict[str, Any]:
    """把本机（网页版）改好的新密码推到云端。

    fail-open：本机改密已生效，云端连不上只记日志、不回滚用户操作；下次登录时
    云端会用旧密码校验失败（BAD_PASSWORD）→ 用户改一次即可，避免死结。
    """
    if not link_enabled():
        return {"ok": False, "reason": "disabled"}
    try:
        return license_client.set_password_remote(
            email, new_password, old_password=old_password, token=token)
    except Exception as e:  # noqa: BLE001
        log.warning("密码同步云端失败 email=%s: %s", email, e)
        return {"ok": False, "code": "CLOUD_UNREACHABLE", "error": str(e)}
