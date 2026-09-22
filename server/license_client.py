"""VDL 云端授权客户端（App 侧 → 授权中心）。

授权中心部署在香港机（https://hanyuxz.top/api/license/*，见 deploy/license_server.py）。

**账号制（2026-09-22 定版）**：App 端不做卡密验签、不做「设备是否被绑过」判定，
只做三件事：
  register / login   拿云端签发的 token（同时占一个设备位）
  redeem             卡密充值到账号（云端负责验签 + 防重复核销）
  heartbeat          心跳续期，顺带问一句「我这台还在不在两台名额里」

被别的机器挤掉时 heartbeat 返回 code=DEVICE_EVICTED；此时本机会员降级为免费档，
提示用户重新登录（登录 = 抢回设备位）。

**拿 outbound 失败当网络问题，别当授权失败**：网络异常一律抛 LicenseCloudError，
调用方按 fail-open 处理（已购买的权益不会因为断网消失）。

可注入性：base_url / opener 均可替换，单测无需真实网络。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_BASE = "https://hanyuxz.top"


class LicenseCloudError(Exception):
    """云端不可达 / 非 JSON 响应。调用方按「网络异常」向用户展示。"""

    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def _post(path: str, payload: dict[str, Any], base_url: str,
          timeout: float, opener: Optional[Callable] = None) -> dict[str, Any]:
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "VDL-LicenseClient/2.0"},
        method="POST")
    urlopen = opener or urllib.request.urlopen
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310  https 固定基址
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 云端业务错误（4xx 带 JSON body）也要把 error/code 传回去
        try:
            return json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            raise LicenseCloudError(f"HTTP {e.code}", e.code)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise LicenseCloudError(f"无法连接授权中心: {e}")
    try:
        return json.loads(body or "{}")
    except json.JSONDecodeError:
        raise LicenseCloudError("授权中心响应不是 JSON")


# ---- 账号 ----
def register_remote(email: str, password: str, fp: str, name: str = "",
                    base_url: str = DEFAULT_BASE, timeout: float = 12.0,
                    opener: Optional[Callable] = None) -> dict[str, Any]:
    """注册并自动登录。返回 {ok, token, account, evicted?}。"""
    return _post("/api/license/register",
                 {"email": email, "password": password,
                  "device": {"fp": fp, "name": name or "未命名设备"}},
                 base_url, timeout, opener)


def login_remote(email: str, password: str, fp: str, name: str = "",
                 base_url: str = DEFAULT_BASE, timeout: float = 12.0,
                 opener: Optional[Callable] = None) -> dict[str, Any]:
    """登录。返回 {ok, token, account:{email, devices, purchases, max_devices}, evicted?}。"""
    return _post("/api/license/login",
                 {"email": email, "password": password,
                  "device": {"fp": fp, "name": name or "未命名设备"}},
                 base_url, timeout, opener)


def heartbeat_remote(token: str, fp: str, base_url: str = DEFAULT_BASE,
                     timeout: float = 8.0,
                     opener: Optional[Callable] = None) -> dict[str, Any]:
    """心跳续期 / 配额校验。被挤掉时返回 {"ok": False, "code": "DEVICE_EVICTED"}。"""
    return _post("/api/license/heartbeat", {"token": token, "fp": fp},
                 base_url, timeout, opener)


def redeem_remote(token: str, code: str, base_url: str = DEFAULT_BASE,
                  timeout: float = 12.0,
                  opener: Optional[Callable] = None) -> dict[str, Any]:
    """卡密充值到账号。返回 {ok, plan_code, purchase_id, account}。"""
    return _post("/api/license/redeem", {"token": token, "code": code.strip()},
                 base_url, timeout, opener)


def set_password_remote(email: str, new_password: str, old_password: str = "",
                        token: str = "", base_url: str = DEFAULT_BASE,
                        timeout: float = 12.0,
                        opener: Optional[Callable] = None) -> dict[str, Any]:
    """把本机账号的新密码同步到云端（两端一套密码，见 license_server.password_impl）。

    鉴权二选一：知道原密码（改密）或持有云端 token（忘记密码重置）。
    云端没有该账号时返回 {"ok": True, "synced": False}（老的本机专属账号），不是失败。
    网络故障抛 LicenseCloudError —— 调用方 **fail-open**：本机改密已生效，不能因为
    云端连不上就把本机也回滚。
    """
    return _post("/api/license/password",
                 {"email": email, "new_password": new_password,
                  "old_password": old_password, "token": token},
                 base_url, timeout, opener)


def devices_remote(token: str, fp: str = "", base_url: str = DEFAULT_BASE,
                   timeout: float = 8.0,
                   opener: Optional[Callable] = None) -> dict[str, Any]:
    """我的设备列表（含 max_devices）。传 fp 时云端会标出哪台是本机。"""
    return _post("/api/license/devices", {"token": token, "fp": fp},
                 base_url, timeout, opener)


def unbind_remote(token: str, fp: str, base_url: str = DEFAULT_BASE,
                  timeout: float = 8.0,
                  opener: Optional[Callable] = None) -> dict[str, Any]:
    """登出/移除某台设备，腾位置给别人。"""
    return _post("/api/license/unbind", {"token": token, "fp": fp},
                 base_url, timeout, opener)


def check_remote(code: str, fingerprint: str = "", base_url: str = DEFAULT_BASE,
                 timeout: float = 8.0,
                 opener: Optional[Callable] = None) -> dict[str, Any]:
    """卡密状态查询（兼容保留）。网络故障抛 LicenseCloudError（调用方 fail-open）。"""
    return _post("/api/license/check", {"code": code, "fingerprint": fingerprint},
                 base_url, timeout, opener)


# ---- 支付（支付宝，内地 ECS pay.hanyuxz.top /api/pay/*）----
PAY_BASE = "https://pay.hanyuxz.top"


def pay_create_remote(token: str, plan_code: str, base_url: str = PAY_BASE,
                      timeout: float = 15.0,
                      opener: Optional[Callable] = None) -> dict[str, Any]:
    """下单：拿支付宝当面付二维码。返回 {ok, order_id, qr_png, amount, plan_code}。"""
    return _post("/api/pay/create", {"token": token, "plan_code": plan_code},
                 base_url, timeout, opener)


def pay_query_remote(order_id: str, base_url: str = PAY_BASE,
                     timeout: float = 8.0,
                     opener: Optional[Callable] = None) -> dict[str, Any]:
    """订单状态轮询。返回 {ok, order_id, status: PENDING|PAID|GRANT_FAILED}。"""
    return _post("/api/pay/query", {"order_id": order_id},
                 base_url, timeout, opener)
