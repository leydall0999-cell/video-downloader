#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""火山「视觉智能 / 抠图」权限自诊断（只读，不改任何配置）

用途：主账号给子用户授权后，跑一次即可确认云端抠图是否真的通了。
用法：  python3 /opt/vdl-gateway/verify_cv.py
退出码：0 = 抠图可用；1 = 仍被拒（会说明是权限还是服务未开通）；2 = 凭据缺失/网络异常

安全：只输出状态码与错误码，**绝不打印 AK/SK**。
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path("/opt/vdl-gateway")
CFG = HOME / "matting.json"

# 1x1 红点 PNG（最小合法输入，只为验证接口可达与权限，不产生真实费用以外的开销）
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def _H(k: bytes, m: str) -> bytes:
    return hmac.new(k, m.encode(), hashlib.sha256).digest()


def _call(service: str, action: str, version: str, body: bytes = b"{}",
          ct: str = "application/json", region: str = "cn-north-1"):
    host = f"{service}.volcengineapi.com"
    q = f"Action={action}&Version={version}"
    t = datetime.datetime.now(datetime.timezone.utc)
    xd, sd = t.strftime("%Y%m%dT%H%M%SZ"), t.strftime("%Y%m%d")
    ph = hashlib.sha256(body).hexdigest()
    sh = "content-type;host;x-content-sha256;x-date"
    ch = f"content-type:{ct}\nhost:{host}\nx-content-sha256:{ph}\nx-date:{xd}\n"
    cr = "\n".join(["POST", "/", q, ch, sh, ph])
    scope = f"{sd}/{region}/{service}/request"
    sts = "\n".join(["HMAC-SHA256", xd, scope, hashlib.sha256(cr.encode()).hexdigest()])
    k = _H(_H(sk.encode(), sd), region)
    k = _H(k, service)
    k = _H(k, "request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    hdr = {
        "Host": host, "X-Date": xd, "X-Content-Sha256": ph, "Content-Type": ct,
        "Authorization": f"HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={sh}, Signature={sig}",
    }
    req = urllib.request.Request(f"https://{host}/?{q}", data=body, headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def _err(body: str) -> tuple[str, str]:
    """从火山响应里取 (错误码, 说明)；兼容 OpenAPI 与 CV 两种错误封装。"""
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return "", body[:160]
    e = (d.get("ResponseMetadata") or {}).get("Error") or {}
    if e.get("Code"):
        return str(e.get("Code")), str(e.get("Message") or "")[:160]
    if d.get("code"):
        return str(d.get("code")), str(d.get("message") or "")[:160]
    return "", ""


def main() -> int:
    global ak, sk
    if not CFG.exists():
        print(f"❌ 金库凭据文件不存在: {CFG}")
        return 2
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    ak = (cfg.get("ak") or "").strip()
    sk = (cfg.get("sk") or "").strip()
    if not ak or not sk:
        print("❌ 金库里没有火山 AK/SK")
        return 2
    print(f"金库凭据: AK={ak[:6]}…(长度{len(ak)})  SK=(长度{len(sk)})   [值不打印]")
    print()

    # ① 身份（证明钥匙有效）
    c, b = _call("sts", "GetCallerIdentity", "2018-01-01")
    print(f"① 身份校验  HTTP {c}")
    if c == 200:
        r = json.loads(b).get("Result") or {}
        print(f"   账号 {r.get('AccountId')} | {r.get('Trn')} | 类型 {r.get('IdentityType')}")
        print("   → 钥匙本身有效（能过身份校验）")
    else:
        code, msg = _err(b)
        print(f"   ✗ {code} {msg}")
        print("   → 钥匙无效或被禁用，需换一把 AK/SK")
        return 2

    # ② 抠图（GeneralSegment）
    import urllib.parse
    body = ("image_base64=" + urllib.parse.quote(base64.b64encode(_PNG).decode(), safe="")
            + "&return_foreground_image=1&refine=1").encode()
    c, b = _call("cv", "GeneralSegment", "2020-08-26",
                 body=body, ct="application/x-www-form-urlencoded")
    print(f"\n② 抠图接口 GeneralSegment  HTTP {c}")
    code, msg = _err(b)
    if c == 200 and not code:
        try:
            d = json.loads(b)
            data = d.get("data") or (d.get("Result") or {}).get("Data") or {}
            fg = data.get("foreground_image") or data.get("foreground") or ""
            print(f"   ✅ 可用！返回前景图 base64 长度 {len(fg)}")
            print("   → 云端抠图已通，App 里即可正常使用")
        except Exception:  # noqa: BLE001
            print(f"   ✅ 可用（返回前 120 字）: {b[:120]}")
        return 0

    print(f"   ✗ 错误码 {code or c}: {msg}")
    if code == "50400" or "Access Denied" in msg:
        print("   → 该子用户缺少『视觉智能』权限，或服务未开通。")
        print("     处理：主账号 → 访问控制 → 用户 → 该子用户 → 添加权限 → 搜索 CVFullAccess → 提交")
        print("     若授权后仍报此错，则需在『视觉智能』控制台点『开通服务』。")
    else:
        print("   → 非权限问题，按上面错误码查火山文档或提工单。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
