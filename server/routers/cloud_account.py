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


def _after_login(store, email: str, resp: dict[str, Any], fp: str, name: str) -> None:
    acct = resp.get("account") or {}
    store.save_account(email, resp.get("token", ""), acct, fp=fp, name=name)
    store.apply_cloud_purchases(acct.get("purchases") or [])


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
    applied = store.apply_cloud_purchases(acct.get("purchases") or [])
    return _pub(store, {"applied": applied.get("applied") or []})


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
    applied = store.apply_cloud_purchases(acct.get("purchases") or [])
    return _pub(store, {"plan_code": r.get("plan_code"),
                        "applied": applied.get("applied") or []})


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
            store.apply_cloud_purchases(acct.get("purchases") or [])
        elif r.get("code") == "DEVICE_EVICTED":
            store.set_evicted(True)
    except Exception:
        pass  # 网络/指纹异常：保持现状（宽限语义）
