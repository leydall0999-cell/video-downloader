#!/usr/bin/env python3
"""火山「云端抠图」权限自诊断（服务端运行，零凭据外泄）。

用法：  python3 /opt/vdl-gateway/verify_cv.py
作用：  1) 先用 AK/SK 调 STS GetCallerIdentity —— 证明这把钥匙本身有效，并打印身份；
        2) 再分别探测两条上游通道：
             A｜AI MediaKit（Bearer Key）  —— 通用软 alpha 抠图 / 画质增强
             B｜视觉智能 CV（SigV4 签名）  —— GeneralSegment 像素级分割
        只打印 HTTP 状态码与错误码，**绝不打印 AK / SK / API Key 的任何字符**。
退出码：0 = 至少一条通道可用   1 = 两条都被拒（会区分「缺权限」与「服务未开通」）
        2 = 金库无凭据 / 网络异常
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(os.environ.get("VDL_GATEWAY_HOME", "/opt/vdl-gateway"))
MATTING_FILE = BASE / "matting.json"
MEDIAKIT_HOST = os.environ.get("VDL_MEDIAKIT_HOST", "mediakit.cn-beijing.volces.com")


def _load() -> dict:
    cfg = {}
    try:
        cfg = json.loads(MATTING_FILE.read_text())
    except Exception:
        pass
    for env, key in (("VDL_GW_MAT_AK", "ak"), ("VDL_GW_MAT_SK", "sk"),
                     ("VDL_GW_MAT_MEDIAKIT_KEY", "mediakit_api_key")):
        v = (os.environ.get(env) or "").strip()
        if v:
            cfg[key] = v
    return cfg


def _mask_len(s: str) -> str:
    s = (s or "").strip()
    return f"非空({len(s)} 字符)" if s else "空"


# ── SigV4（视觉智能 CV / IAM / STS）────────────────────────────────────────
def _sigv4(service: str, action: str, version: str, ak: str, sk: str,
           region: str = "cn-north-1", body: bytes = b"{}",
           ct: str = "application/json") -> tuple[int, str]:
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
    k = hmac.new(sk.encode(), sd.encode(), hashlib.sha256).digest()
    k = hmac.new(k, region.encode(), hashlib.sha256).digest()
    k = hmac.new(k, service.encode(), hashlib.sha256).digest()
    k = hmac.new(k, b"request", hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    hdr = {"Host": host, "X-Date": xd, "X-Content-Sha256": ph, "Content-Type": ct,
           "Authorization": f"HMAC-SHA256 Credential={ak}/{scope}, "
                            f"SignedHeaders={sh}, Signature={sig}"}
    req = urllib.request.Request(f"https://{host}/?{q}", data=body, headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _err_of(body: str) -> str:
    try:
        d = json.loads(body)
        if isinstance(d, dict):
            if d.get("code") is not None:
                return f"{d.get('code')} {d.get('message') or d.get('Message') or ''}"
            md = d.get("ResponseMetadata") or {}
            e = md.get("Error") or {}
            return f"{e.get('Code','')} {e.get('Message','')}"
    except Exception:
        pass
    return (body or "")[:120]


def main() -> int:
    cfg = _load()
    ak = (cfg.get("ak") or "").strip()
    sk = (cfg.get("sk") or "").strip()
    mk = (cfg.get("mediakit_api_key") or "").strip()

    print("=" * 62)
    print("火山「云端抠图」权限自诊断（不打印任何密钥字符）")
    print("=" * 62)
    print(f"金库文件: {MATTING_FILE}")
    print(f"  ak              : {_mask_len(ak)}")
    print(f"  sk              : {_mask_len(sk)}")
    print(f"  mediakit_api_key: {_mask_len(mk)}")
    print()

    if not (ak and sk) and not mk:
        print("❌ 金库里既没有 AK/SK 也没有 MediaKit Key，无法诊断")
        return 2

    any_ok = False

    # ① 身份（只有 AK/SK 才能查）
    if ak and sk:
        c, b = _sigv4("sts", "GetCallerIdentity", "2018-01-01", ak, sk)
        print(f"[1] 身份验证 STS GetCallerIdentity → HTTP {c}")
        if c == 200:
            try:
                r = (json.loads(b).get("Result") or json.loads(b))
                print(f"    账号 AccountId : {r.get('AccountId')}")
                print(f"    身份 Trn       : {r.get('Trn') or r.get('Arn')}")
                print("    ⇒ 这把 AK/SK **本身有效**（签名实现正确）")
            except Exception:
                print("    ⇒ 200，但响应解析异常（不影响结论）")
        else:
            print(f"    ⇒ {_err_of(b)}   【钥匙无效或已禁用】")
        print()

        # ② 视觉智能 CV
        c2, b2 = _sigv4("cv", "GeneralSegment", "2020-08-26", ak, sk)
        print(f"[2] 通道B｜视觉智能 CV GeneralSegment → HTTP {c2}")
        if c2 == 200:
            print("    ✅ 可用")
            any_ok = True
        else:
            code_msg = _err_of(b2)
            print(f"    ❌ {code_msg}")
            if "50400" in code_msg or "Access Denied" in code_msg:
                print("    ⇒ 签名没问题（上面 STS 已证），是**子用户缺 CV 权限 / 服务未开通**")
                print("      控制台：访问控制 → 用户 → VDL → 添加权限 CVFullAccess；")
                print("      若仍报同样错 → 视觉智能控制台点「开通服务」。")
        print()
    else:
        print("[1][2] 无 AK/SK，跳过身份验证与视觉智能 CV 检测")
        print()

    # ③ MediaKit
    if mk:
        url = f"https://{MEDIAKIT_HOST}/api/v1/tools-sync/request-media-upload-url"
        req = urllib.request.Request(url, data=b"{}", method="POST", headers={
            "Authorization": f"Bearer {mk}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                c3, b3 = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            c3, b3 = e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            c3, b3 = -1, f"{type(e).__name__}: {e}"
        print(f"[3] 通道A｜AI MediaKit 申请上传地址 → HTTP {c3}")
        if c3 == 200:
            print("    ✅ 可用（MediaKit 已开通且 Key 有效）")
            any_ok = True
        elif c3 in (401, 403):
            print(f"    ❌ {_err_of(b3)}  ⇒ Key 无效 / 或该服务未开通")
        elif c3 == -1:
            print(f"    ⚠️ 网络异常：{b3[:120]}")
        else:
            print(f"    ⚠️ HTTP {c3} → {_err_of(b3)}")
    else:
        print("[3] 通道A｜AI MediaKit：金库无 Key，跳过")
        print("    （控制台 → AI MediaKit → 基础配置 → API Key，粘给我即可装进金库）")

    print()
    print("=" * 62)
    if any_ok:
        print("✅ 结论：至少一条通道可用，云端抠图可以工作")
        return 0
    print("❌ 结论：两条通道都不可用。按上面的提示在火山控制台处理后再跑一次。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
