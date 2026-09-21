"""VDL 卡密云端客户端（App 侧 → 授权中心）。

授权中心部署在香港机（https://hanyuxz.top/api/license/*，见 deploy/license_server.py）。
App 端**不做**卡密验签（secret 不下发），激活 = 把卡密+设备指纹交给云端裁决：
  redeem → {ok, plan_code, bound_at}  → 本地 membership.activate(plan_code, via="license")
  check  → {known, status, matches}   → 启动时校验卡密是否被作废

可注入性：base_url / 请求函数均可替换，单测无需真实网络。
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
        headers={"Content-Type": "application/json", "User-Agent": "VDL-LicenseClient/1.0"},
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


def redeem_remote(code: str, fingerprint: str, base_url: str = DEFAULT_BASE,
                  timeout: float = 12.0,
                  opener: Optional[Callable] = None) -> dict[str, Any]:
    """卡密激活。云端业务失败返回 {"ok": False, "error", "code"}；网络故障抛 LicenseCloudError。"""
    return _post("/api/license/redeem", {"code": code, "fingerprint": fingerprint},
                 base_url, timeout, opener)


def check_remote(code: str, fingerprint: str, base_url: str = DEFAULT_BASE,
                 timeout: float = 8.0,
                 opener: Optional[Callable] = None) -> dict[str, Any]:
    """卡密状态查询（作废检测）。网络故障抛 LicenseCloudError（调用方 fail-open）。"""
    return _post("/api/license/check", {"code": code, "fingerprint": fingerprint},
                 base_url, timeout, opener)
