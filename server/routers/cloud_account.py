"""server/routers/cloud_account.py — 云端账号（登录资质，替代一机一码）。

为什么有这个文件：
  旧版把会员绑死在一台机器的指纹上（P2 一机一码），换硬盘/重装/换 Mac 就要人工
  解绑，用户反馈「卡得太死」。新语义三步走：
    · 账号密码登录（云端仲裁），会员权益挂账号不挂机器
    · 同一账号最多 N 台设备（默认 2），超出时淘汰**最久没活动**的那台
    · 本机权益写进全局 member_store，未登录也能享用；被挤掉才降级为免费档

对外接口（全部 /api/cloud/*，桌面端本机回环调用）：
  POST /register {email, password}   注册并直接登录
  POST /login    {email, password}   登录（占用一个设备位）
  POST /logout                        本地登出（权益保留）
  GET  /status                        账号快照 + 设备列表（离线可读）
  POST /sync                         心跳：拉权益续期 + 查自己是否被挤出
  POST /redeem  {code}               卡密充值到当前账号
  POST /unbind  {fp}                 登出某台设备，腾位置

设计约束：
  - **fail-open**：网络异常一律不动本地权益（断网不惩罚已付费用户）。
  - token 不外泄给前端：/status 只吐邮箱、设备列表、是否登录。
"""
from __future__ import annotations

import platform
import socket
from typing import Any, Optional

from fastapi import APIRouter, Body

router = APIRouter()

_SYNC_MIN_INTERVAL = 1800.0   # 自动同步最小间隔（秒），避免每次请求都打云端


def _store():
    import app
    return app.member_store


def _local_token_for(email: str, password: str) -> str:
    """云端账号即本地账号：保证两者密码一致。

    云端注册/登录成功后，确保本地 auth_store 里也有同名账号，并签发本地 token
    （功能门禁、个人中心、客服等都依赖它）。云端账号不存在时按同密码自动建本地账号，
    因此用户只需记一套邮箱+密码。
    """
    from auth_store import create_user, authenticate, issue_token
    ident = email.strip().lower()
    uid = authenticate(ident, password)
    if not uid:
        uid = create_user(ident, password)
        if uid:
            uid = authenticate(ident, password)
    if not uid:
        raise RuntimeError("本地账号签发失败")
    return issue_token(uid)


def _adopt_local_account(email: str, password: str, fp: str, name: str) -> bool:
    """把「只存在本机」的老账号补建到云端，返回是否已建号（调用方应重试登录）。

    🔴 为什么需要（2026-09-22 实测）：云端注册过去只收邮箱，手机号注册会被拒，
    而前端注册流程吞掉了这个失败、本地照常注册成功 → 这批用户「本机有账号、
    云端没有」，会员权益只在本机，换机/重装即丢，登录时还会被云端回一句
    「账号不存在」误导。

    **必须本机密码校验通过才迁移**：既杜绝替他人建号，也避免密码打错时
    在云端静默建出一个永远用不上的空账号。
    """
    try:
        from auth_store import authenticate
        if not authenticate(email, password):
            return False
    except Exception:
        return False
    try:
        import license_client
        rr = license_client.register_remote(email.strip(), password, fp, name)
    except Exception:
        return False
    return bool(rr and rr.get("ok"))


def _heal_cloud_password(email: str, password: str) -> bool:
    """云端密码与本机不一致时，把本机密码推上云端（返回是否推成功）。

    🔴 为什么需要（2026-09-22）：账号是**两套库**（本机 auth_store / 云端授权中心），
    各存一份密码哈希。只要有一侧单独改过密码（例如断网时在本机改了、或用户在网络不通
    时重置过），两边就分叉 —— 用户「这台能登、换台说密码错」，云端登录失败还会连累
    会员权益同步。这里让分叉在**下一次登录时自动收敛**。

    **三重前提，缺一不推**（防越权改别人账号的密码）：
      ① 本机账号表里该账号存在，且**当前输入的密码在本机校验通过**（用户确实持有密码）
      ② 本机保存着该账号的云端登录 token（说明这台机器上有过合法的云端会话）
      ③ 云端确认该账号存在且确实不认这个密码（调用方只在 BAD_PASSWORD 时进来）
    """
    try:
        from auth_store import authenticate
        if not authenticate(email, password):
            return False
    except Exception:
        return False
    store = _store()
    try:
        store._ensure_loaded()
        acc = (store._state.get("meta") or {}).get("account") or {}
        token = str(acc.get("token") or "")
        if not token or (acc.get("email") or "").strip().lower() != email.strip().lower():
            return False
        import license_client
        r = license_client.set_password_remote(email.strip(), password, token=token)
    except Exception:
        return False
    return bool(r and r.get("ok") and r.get("synced", True))


def _fp_name() -> tuple[str, str]:
    try:
        import device_id
        fp, _strong = device_id.fingerprint()
    except Exception:
        fp = ""
    if not fp:
        return "", ""
    try:
        name = f"{platform.node() or 'Mac'} · {socket.gethostname() or ''}".strip(" ·")
    except Exception:
        name = "未命名设备"
    return fp, name[:64]


def _apply_account_state(store, acct: dict[str, Any]) -> None:
    """云端权益落地（2026-09-25 防破解核心切换点）。

    授权中心带 authority 快照（v>=1）→ 用云端真值**覆盖**本地 memberships
    （篡改的 expire_at/积分一联网即回滚）；老服务端无快照 → 退回旧的幂等追加。
    """
    auth = (acct or {}).get("authority") or {}
    if isinstance(auth, dict) and int(auth.get("v") or 0) >= 1:
        store.apply_cloud_authoritative(acct)
    else:
        store.apply_cloud_purchases(acct.get("purchases") or [])


def _after_login(store, email: str, resp: dict[str, Any], fp: str, name: str) -> None:
    acct = resp.get("account") or {}
    store.save_account(email, resp.get("token", ""), acct, fp=fp, name=name)
    _apply_account_state(store, acct)


# ---- 积分扣减上云（防破解：本地余额只是缓存，消耗流水以云端为准）------------
def _pending_spends_path_guard(store) -> list[dict]:
    try:
        store._ensure_loaded()
        meta = store._state.setdefault("meta", {})
        q = meta.setdefault("pending_spends", [])
        if not isinstance(q, list):
            q = meta["pending_spends"] = []
        return q
    except Exception:
        return []


def _queue_pending_spends(store, items: list[dict]) -> None:
    try:
        q = _pending_spends_path_guard(store)
        q.extend(items)
        store._state["meta"]["pending_spends"] = q[-200:]  # 上限 200 条
        store._persist()
    except Exception:
        pass


def _report_spend(store, items: list[dict]) -> None:
    """同步上报（在后台线程里跑）：成功则用云端权威快照校准本地余额。"""
    try:
        store._ensure_loaded()
        token = str(((store._state.get("meta") or {}).get("account") or {}).get("token") or "")
        if not token or not items:
            return
        import license_client
        r = license_client.spend_remote(token, items)
        if r and r.get("ok"):
            auth = r.get("authority") or {}
            if auth:
                store.apply_cloud_authoritative({"authority": auth})
        else:
            _queue_pending_spends(store, items)
    except Exception:
        _queue_pending_spends(store, items)  # 断网/超时：排队下次补报


def report_spend_async(store, amount: int, ai_taken: int = 0, perm_taken: int = 0,
                       reason: str = "") -> None:
    """membership.spend_credits 的上云钩子（fire-and-forget，不阻塞功能主流程）。

    items 按 spend_credits 的实际扣减拆分（先 AI 后永久），幂等 id 唯一 ——
    重试/补报不会在云端重复扣。
    离线测试环境（pytest / VDL_OFFLINE_TESTS）直接跳过：后台线程会污染全局
    store 单例并发起真实网络请求。
    """
    import os
    if "PYTEST_CURRENT_TEST" in os.environ or os.environ.get("VDL_OFFLINE_TESTS"):
        return
    import threading
    import uuid
    ev = uuid.uuid4().hex[:16]
    items: list[dict] = []
    if ai_taken > 0:
        items.append({"pool": "ai", "cost": int(ai_taken), "op": reason[:40],
                      "id": f"{ev}-ai"})
    if perm_taken > 0:
        items.append({"pool": "permanent", "cost": int(perm_taken), "op": reason[:40],
                      "id": f"{ev}-perm"})
    if not items:
        return
    try:
        threading.Thread(target=_report_spend, args=(store, items),
                         daemon=True, name="vdl-spend-report").start()
    except Exception:
        _queue_pending_spends(store, items)


def flush_pending_spends(store) -> None:
    """联网时补报离线期间积累的扣减（登录/心跳成功后调用）。"""
    try:
        store._ensure_loaded()
        meta = store._state.get("meta") or {}
        pending = meta.get("pending_spends") or []
        if not pending:
            return
        token = str((meta.get("account") or {}).get("token") or "")
        if not token:
            return
        import license_client
        r = license_client.spend_remote(token, pending)
        if r and r.get("ok"):
            meta["pending_spends"] = []
            store._persist()
            auth = r.get("authority") or {}
            if auth:
                store.apply_cloud_authoritative({"authority": auth})
    except Exception:
        pass  # 仍离线：保留队列下次再试


def _pub(store, extra: Optional[dict] = None) -> dict[str, Any]:
    out = {"ok": True, "account": store.account_view()}
    if extra:
        out.update(extra)
    return out


@router.post("/api/cloud/register")
def cloud_register(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    import license_client
    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "")
    if not email or not password:
        return {"ok": False, "error": "请输入邮箱和密码"}
    fp, name = _fp_name()
    if not fp:
        return {"ok": False, "error": "无法取得本机设备标识，请重启 App 后重试"}
    try:
        r = license_client.register_remote(email, password, fp, name)
    except license_client.LicenseCloudError as e:
        return {"ok": False, "error": f"{e}（需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "注册失败",
                "code": r.get("code") or "REJECTED"}
    store = _store()
    _after_login(store, email, r, fp, name)
    try:
        local_token = _local_token_for(email, password)
    except Exception:
        local_token = ""
    return _pub(store, {"registered": True, "local_token": local_token})


@router.post("/api/cloud/login")
def cloud_login(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    import license_client
    email = str(payload.get("email") or "").strip()
    password = str(payload.get("password") or "")
    if not email or not password:
        return {"ok": False, "error": "请输入邮箱和密码"}
    fp, name = _fp_name()
    if not fp:
        return {"ok": False, "error": "无法取得本机设备标识，请重启 App 后重试"}
    try:
        r = license_client.login_remote(email, password, fp, name)
    except license_client.LicenseCloudError as e:
        return {"ok": False, "error": f"{e}（登录需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        # 老账号自愈：云端不认识这个账号，但本机账号表里有且密码正确 → 说明这是
        # 当年用手机号注册、云端从没建号的用户，就地补建后重试一次登录。
        healed = False
        if r.get("code") == "NO_ACCOUNT":
            healed = _adopt_local_account(email, password, fp, name)
        elif r.get("code") == "BAD_PASSWORD":
            # 两端密码分叉（本机改过、云端没跟上）→ 把本机密码推上云端再试一次。
            # 这样用户永远不会遇到「本机密码对、云端说密码错」的死结。共用一个重试。
            healed = _heal_cloud_password(email, password)
        if healed:
            try:
                r = license_client.login_remote(email, password, fp, name)
            except license_client.LicenseCloudError as e:
                return {"ok": False, "error": f"{e}（登录需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "登录失败",
                "code": r.get("code") or "REJECTED"}
    store = _store()
    _after_login(store, email, r, fp, name)
    extra = {}
    try:
        extra["local_token"] = _local_token_for(email, password)
    except Exception:
        extra["local_token"] = ""
    if r.get("evicted"):
        extra["notice"] = "已挤下线一台闲置设备（该账号最多同时登录 " \
                          f"{store.account_view().get('max_devices', 2)} 台）"
    return _pub(store, extra)


@router.post("/api/cloud/logout")
def cloud_logout() -> dict[str, Any]:
    """本地登出。权益不做清除（换账号/换机都不丢已买的会员）。"""
    store = _store()
    store.clear_account()
    return _pub(store)


@router.get("/api/cloud/status")
def cloud_status() -> dict[str, Any]:
    """离线可读：账号快照 + 设备列表 + 是否被挤出。"""
    return _pub(_store())


@router.post("/api/cloud/sync")
def cloud_sync() -> dict[str, Any]:
    """主动同步：续期 + 拉新权益 + 检查设备名额。断网时不动本地（fail-open）。"""
    import license_client
    store = _store()
    acc = (store._state.get("meta") or {}).get("account") or {}
    token = str(acc.get("token") or "")
    if not token:
        return {"ok": False, "error": "尚未登录", "code": "NOT_LOGGED_IN"}
    fp, name = _fp_name()
    if not fp:
        return {"ok": False, "error": "无法取得本机设备标识"}
    try:
        r = license_client.heartbeat_remote(token, fp)
    except license_client.LicenseCloudError as e:
        return {"ok": True, "offline": True, "warning": str(e)}
    if not r.get("ok"):
        if r.get("code") == "DEVICE_EVICTED":
            store.set_evicted(True)
            return _pub(store, {"evicted": True,
                                "error": "该账号已在其他两台设备登录，请重新登录以使用本机"})
        return {"ok": False, "error": r.get("error") or "同步失败",
                "code": r.get("code") or "REJECTED"}
    acct = r.get("account") or {}
    store.set_evicted(False)
    store.save_account(acc.get("email", ""), token, acct, fp=fp, name=name)
    _apply_account_state(store, acct)
    flush_pending_spends(store)
    return _pub(store)


@router.post("/api/cloud/redeem")
def cloud_redeem(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """卡密充值到当前登录账号（一次充值终身有效，换机无需解绑）。"""
    import license_client
    code = str(payload.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "请输入卡密"}
    store = _store()
    acc = (store._state.get("meta") or {}).get("account") or {}
    token = str(acc.get("token") or "")
    if not token:
        return {"ok": False, "error": "请先登录账号", "code": "NOT_LOGGED_IN"}
    try:
        r = license_client.redeem_remote(token, code)
    except license_client.LicenseCloudError as e:
        return {"ok": False, "error": f"{e}（充值需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "卡密无效",
                "code": r.get("code") or "REJECTED"}
    acct = r.get("account") or {}
    store.save_account(acc.get("email", ""), token, acct,
                       fp=acc.get("fp", ""), name=acc.get("name", ""))
    _apply_account_state(store, acct)
    flush_pending_spends(store)
    return _pub(store, {"plan_code": r.get("plan_code")})


@router.post("/api/cloud/unbind")
def cloud_unbind(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """把某台设备踢下线（腾出名额给新机器）。"""
    import license_client
    fp = str(payload.get("fp") or "").strip()
    if not fp:
        return {"ok": False, "error": "缺少设备标识"}
    store = _store()
    acc = (store._state.get("meta") or {}).get("account") or {}
    token = str(acc.get("token") or "")
    if not token:
        return {"ok": False, "error": "请先登录账号", "code": "NOT_LOGGED_IN"}
    try:
        r = license_client.unbind_remote(token, fp)
    except license_client.LicenseCloudError as e:
        return {"ok": False, "error": f"{e}（需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "操作失败",
                "code": r.get("code") or "REJECTED"}
    acct = r.get("account") or {}
    store.save_account(acc.get("email", ""), token, acct,
                       fp=acc.get("fp", ""), name=acc.get("name", ""))
    return _pub(store)


@router.post("/api/cloud/pay/create")
def cloud_pay_create(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """购买下单：调内地 ECS 支付服务（pay.hanyuxz.top）生成支付宝当面付二维码。

    金额由服务端（pay_server.PRICE_MAP）决定，前端只传 plan_code，防改价。
    """
    import license_client
    plan_code = str(payload.get("plan_code") or "")
    if not plan_code:
        return {"ok": False, "error": "缺少套餐"}
    store = _store()
    acc = (store._state.get("meta") or {}).get("account") or {}
    token = str(acc.get("token") or "")
    if not token:
        return {"ok": False, "error": "请先登录账号", "code": "NOT_LOGGED_IN"}
    try:
        r = license_client.pay_create_remote(token, plan_code)
    except license_client.LicenseCloudError as e:
        return {"ok": False, "error": f"{e}（下单需要联网）", "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "下单失败",
                "code": r.get("code") or "REJECTED"}
    return {"ok": True, "order_id": r.get("order_id"), "qr_png": r.get("qr_png"),
            "amount": r.get("amount"), "plan_code": r.get("plan_code")}


@router.post("/api/cloud/pay/query")
def cloud_pay_query(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """订单状态轮询（App 前端轮询，支付成功自动开通）。"""
    import license_client
    order_id = str(payload.get("order_id") or "")
    if not order_id:
        return {"ok": False, "error": "缺少订单号"}
    try:
        r = license_client.pay_query_remote(order_id)
    except license_client.LicenseCloudError as e:
        return {"ok": False, "error": str(e), "code": "CLOUD_UNREACHABLE"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "查询失败",
                "code": r.get("code") or "REJECTED"}
    return {"ok": True, "order_id": order_id, "status": r.get("status"),
            "plan_code": r.get("plan_code"), "amount": r.get("amount")}


def maybe_sync_account(store) -> None:
    """member_status 的惰性钩子：登录过的前提下，每隔 _SYNC_MIN_INTERVAL 同步一次。

    - 未登录：直接返回（老用户不受影响）
    - 网络异常：不改动本地权益（fail-open）
    """
    import time

    try:
        store._ensure_loaded()
        acc = (store._state.get("meta") or {}).get("account") or {}
        token = str(acc.get("token") or "")
        if not token:
            return
        if time.time() - float(acc.get("last_sync") or 0) < _SYNC_MIN_INTERVAL:
            return
        import license_client
        fp, name = _fp_name()
        if not fp:
            return
        r = license_client.heartbeat_remote(token, fp)
        if r.get("ok"):
            acct = r.get("account") or {}
            store.set_evicted(False)
            store.save_account(acc.get("email", ""), token, acct, fp=fp, name=name)
            _apply_account_state(store, acct)
            flush_pending_spends(store)
        elif r.get("code") == "DEVICE_EVICTED":
            store.set_evicted(True)
        elif r.get("code") == "ACCOUNT_BANNED":
            # 云端封禁：落 meta.account.banned → device_lock_reason 全局锁权益
            try:
                store._ensure_loaded()
                store._state.setdefault("meta", {}).setdefault("account", {})["banned"] = True
                store._persist()
            except Exception:
                pass
    except Exception:
        pass  # 网络/指纹异常：保持现状（宽限语义）
