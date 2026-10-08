"""VDL 云端授权客户端（App 侧 → 授权中心）。

授权中心部署在国内 ECS（`8.138.223.3:8902`，nginx `location /api/license/` 分流；
源码见 deploy/license_server.py）。**web 版（ECS /opt/vdl-worker）2026-09-26 起也复用
本文件**，用来把网页版账号接到同一份云端账号库 —— 见 server/cloud_link.py。
web 侧部署时务必注入 `VDL_LICENSE_BASE=http://127.0.0.1:8902`（同机直连，绕开公网）。

**账号制（2026-09-22 定版）**：客户端不做卡密验签、不做「设备是否被绑过」判定，
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
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_BASE = "https://hanyuxz.top"

_BASE_CACHE: dict[str, str] = {}


def license_base() -> str:
    """授权中心基址（2026-09-22 授权中心自香港迁国内 ECS 后引入）。

    解析顺序：
      1. env `VDL_LICENSE_BASE`（部署侧显式覆盖）；
      2. `~/.videodownloader/cloud_sync.json` 的 `url`（worker 与授权中心同在
         ECS 8888，nginx 按 location 分流 —— 一个 origin 服务两个后端）；
      3. 回落 DEFAULT_BASE（hanyuxz.top，历史兼容）。

    背景：原 DEFAULT_BASE 走 hanyuxz.top → Cloudflare → 香港，实测单次
    login 7.5s，贴着 12s 超时线抖动，用户登录时随机「云端未同步」。
    迁 ECS 后同链路实测 ~86ms。结果进程内缓存，避免每次请求都读盘。
    """
    cached = _BASE_CACHE.get("base")
    if cached:
        return cached
    base = (os.environ.get("VDL_LICENSE_BASE") or "").strip()
    if not base:
        try:
            cfg_path = os.path.join(os.path.expanduser("~"), ".videodownloader", "cloud_sync.json")
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh) or {}
            base = str(cfg.get("url") or "").strip()
        except Exception:
            base = ""
    if not base:
        base = DEFAULT_BASE
    _BASE_CACHE["base"] = base.rstrip("/")
    return _BASE_CACHE["base"]


class LicenseCloudError(Exception):
    """云端不可达 / 非 JSON 响应。调用方按「网络异常」向用户展示。"""

    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def _post(path: str, payload: dict[str, Any], base_url: Optional[str],
          timeout: float, opener: Optional[Callable] = None) -> dict[str, Any]:
    if not base_url:
        base_url = license_base()
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
                    base_url: Optional[str] = None, timeout: float = 12.0,
                    opener: Optional[Callable] = None) -> dict[str, Any]:
    """注册并自动登录。返回 {ok, token, account, evicted?}。"""
    return _post("/api/license/register",
                 {"email": email, "password": password,
                  "device": {"fp": fp, "name": name or "未命名设备"}},
                 base_url, timeout, opener)


def login_remote(email: str, password: str, fp: str, name: str = "",
                 base_url: Optional[str] = None, timeout: float = 12.0,
                 opener: Optional[Callable] = None) -> dict[str, Any]:
    """登录。返回 {ok, token, account:{email, devices, purchases, max_devices}, evicted?}。"""
    return _post("/api/license/login",
                 {"email": email, "password": password,
                  "device": {"fp": fp, "name": name or "未命名设备"}},
                 base_url, timeout, opener)


def heartbeat_remote(token: str, fp: str, base_url: Optional[str] = None,
                     timeout: float = 8.0,
                     opener: Optional[Callable] = None) -> dict[str, Any]:
    """心跳续期 / 配额校验。被挤掉时返回 {"ok": False, "code": "DEVICE_EVICTED"}。"""
    return _post("/api/license/heartbeat", {"token": token, "fp": fp},
                 base_url, timeout, opener)


def redeem_remote(token: str, code: str, base_url: Optional[str] = None,
                  timeout: float = 12.0,
                  opener: Optional[Callable] = None) -> dict[str, Any]:
    """卡密充值到账号。返回 {ok, plan_code, purchase_id, account}。"""
    return _post("/api/license/redeem", {"token": token, "code": code.strip()},
                 base_url, timeout, opener)


def spend_remote(token: str, items: list, base_url: Optional[str] = None,
                 timeout: float = 8.0,
                 opener: Optional[Callable] = None) -> dict[str, Any]:
    """积分扣减上报（幂等）。items: [{pool: permanent|ai, cost, op, id}]。

    返回 {ok, applied, authority}；云端按 id 去重，断网重发不会重复扣。
    网络异常抛 LicenseCloudError —— 调用方把 items 入 pending 队列下次补报。
    """
    return _post("/api/license/spend", {"token": token, "items": items},
                 base_url, timeout, opener)


def daily_remote(token: str, items: list, base_url: Optional[str] = None,
                 timeout: float = 8.0,
                 opener: Optional[Callable] = None) -> dict[str, Any]:
    """每日用量上报（幂等）。items: [{res, n, id}]。

    同一账号在 App / 网页版共用同一份每日配额：本地 use_daily 只是预检缓存，
    真实用量以授权中心按账号累计为准。返回 {ok, applied, authority}；云端按 id
    去重，断网重发不会重复计。网络异常抛 LicenseCloudError —— 调用方把 items
    入 pending 队列下次补报。
    """
    return _post("/api/license/daily", {"token": token, "items": items},
                 base_url, timeout, opener)


def trial_claim_remote(token: str, op: str, mode: str = "once",
                       base_url: Optional[str] = None,
                       timeout: float = 8.0,
                       opener: Optional[Callable] = None) -> dict[str, Any]:
    """免费体验名额跨端原子领取。返回 {ok, claimed, already, authority}。

    already=False → 本端成功领取全局唯一名额（放行）；already=True → 另一台已领走
    （本端应拒绝）。网络异常抛 LicenseCloudError —— 调用方按 fail-open 处理
    （视为本端领取，离线不惩罚已付费/免费用户）。
    """
    return _post("/api/license/trial_claim",
                 {"token": token, "op": op, "mode": mode},
                 base_url, timeout, opener)


def cloud_quota_remote(token: str, lifetime: int = 0, daily: int = 0,
                       refund: bool = False,
                       resource: str = "cloud_commentary",
                       base_url: Optional[str] = None,
                       timeout: float = 8.0,
                       opener: Optional[Callable] = None) -> dict[str, Any]:
    """免费云端额度跨端原子扣减 / 退还 / 查询（2026-10-06）。

    🔴 这个端点存在的理由：两端原先把「终身 3 次云端额度」各记各的
    （桌面记本机 quota.json、网页记 use_daily("cloud") 的每日配额），
    换端/重装即重置。统一后由授权中心按**账号**记账，App / 网页共享一份。

    参数三态（服务端语义）：
      · lifetime/daily > 0 → 扣减
      · refund=True       → 退还（任务失败补偿）
      · 都不传             → 只查询余额（冷启动/心跳回灌用）

    返回 {ok, allowed, reason, applied, cloud_quota:{lifetime:{res:n},
    lifetime_remaining:{res:n}, lifetime_used(旧中心=总池 int), daily_auto_used, ...}}。
    allowed=False 表示额度已用尽（reason=lifetime_exhausted / daily_auto_exhausted），
    此时**不落账**。网络异常抛 LicenseCloudError —— 调用方按 fail-open 处理
    （沿用本机计数，绝不因断网把已付费会员拦在门外）。
    """
    return _post("/api/license/cloud_quota",
                 {"token": token, "lifetime": int(lifetime or 0),
                  "daily": int(daily or 0), "refund": bool(refund),
                  "resource": str(resource or "cloud_commentary")},
                 base_url, timeout, opener)


def set_password_remote(email: str, new_password: str, old_password: str = "",
                        token: str = "", base_url: Optional[str] = None,
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


def devices_remote(token: str, fp: str = "", base_url: Optional[str] = None,
                   timeout: float = 8.0,
                   opener: Optional[Callable] = None) -> dict[str, Any]:
    """我的设备列表（含 max_devices）。传 fp 时云端会标出哪台是本机。"""
    return _post("/api/license/devices", {"token": token, "fp": fp},
                 base_url, timeout, opener)


def unbind_remote(token: str, fp: str, base_url: Optional[str] = None,
                  timeout: float = 8.0,
                  opener: Optional[Callable] = None) -> dict[str, Any]:
    """登出/移除某台设备，腾位置给别人。"""
    return _post("/api/license/unbind", {"token": token, "fp": fp},
                 base_url, timeout, opener)


def check_remote(code: str, fingerprint: str = "", base_url: Optional[str] = None,
                 timeout: float = 8.0,
                 opener: Optional[Callable] = None) -> dict[str, Any]:
    """卡密状态查询（兼容保留）。网络故障抛 LicenseCloudError（调用方 fail-open）。"""
    return _post("/api/license/check", {"code": code, "fingerprint": fingerprint},
                 base_url, timeout, opener)


# ---- 支付（支付宝，内地 ECS pay.hanyuxz.top /api/pay/*）----
PAY_BASE = "https://pay.hanyuxz.top"


def pay_create_remote(token: str, plan_code: str, client: str = "",
                      base_url: str = PAY_BASE, timeout: float = 15.0,
                      opener: Optional[Callable] = None) -> dict[str, Any]:
    """下单：拿支付二维码 / 收银台地址。

    返回 {ok, order_id, mode, qr_png, qr, pay_url, amount, plan_code}。

    client 显式声明 'desktop' | 'mobile'：支付服务据此决定收银台形态；不传时
    支付服务按 User-Agent 兜底判定，而本客户端 UA 是 VDL-LicenseClient/2.0，
    会被判为 desktop —— 因此**由后端转发时必须显式传 client**，否则移动端拿不到 wap。
    """
    payload: dict[str, Any] = {"token": token, "plan_code": plan_code}
    if client:
        payload["client"] = client
    return _post("/api/pay/create", payload, base_url, timeout, opener)


def pay_query_remote(order_id: str, base_url: str = PAY_BASE,
                     timeout: float = 8.0,
                     opener: Optional[Callable] = None) -> dict[str, Any]:
    """订单状态轮询。返回 {ok, order_id, status: PENDING|PAID|GRANT_FAILED}。"""
    return _post("/api/pay/query", {"order_id": order_id},
                 base_url, timeout, opener)
