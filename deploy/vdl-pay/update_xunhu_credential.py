#!/usr/bin/env python3
"""更新虎皮椒（xunhupay）渠道凭据 —— 用于商户主体升级/重开商户号后换 appid·appsecret。

⚠️ 只在 VPS（8.138.223.3）上跑，不需要也不应该在本机跑。

设计要点（都是踩过的坑）：
1. **先备份再改**：备份带时间戳、保留 600 权限；失败自动回滚并重启。
2. **改完必须 restart vdl-pay**：凭据是进程启动时读进内存的，改文件不重启不生效。
3. **重启后必须打 healthz**：`/api/pay/healthz` 的 `channel` 应为 `xunhupay`。
   注意 healthz 只反映环境变量 VDL_PAY_CHANNEL，**不证明新 appid 能用** ——
   真正证明要再建一单（见 --smoke）。
4. **权限必须是 600**：文件含 appsecret。
5. **--smoke 会真实向虎皮椒建一单**（1 天卡 ¥1.90，不付款、会自然超时）。
   这是唯一能证明新凭据真的能下单的方式；代价是订单库多一条 PENDING。

用法：
    # 只查看当前生效凭据（脱敏）
    python3 update_xunhu_credential.py --verify-only

    # 只换 appid（重开商户号时通常两个都换）
    python3 update_xunhu_credential.py --appid 201906188457 --yes

    # 两个都换 + 改完立刻建单验证
    python3 update_xunhu_credential.py --appid XXX --secret YYY --yes --smoke
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

CFG_DEFAULT = "/opt/vdl-license/xunhupay.json"
PAY_BASE = "http://127.0.0.1:8903"
UNIT = "vdl-pay"


def sh(cmd: list[str], check: bool = True) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"命令失败 {cmd}: {p.stderr.strip()}")
    return p.returncode, (p.stdout or "").strip()


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def mask(v: str) -> str:
    if not isinstance(v, str):
        return repr(v)
    return f"{v[:4]}…(len={len(v)})" if len(v) > 6 else v


def verify_only(path: str) -> int:
    d = load(path)
    print(f"== 当前凭据文件 {path} ==")
    for k in ("appid", "appsecret", "gateway", "notify_base"):
        print(f"   {k:<12} {mask(d.get(k, ''))}")
    code, out = sh(["systemctl", "show", UNIT, "-p", "MainPID", "-p", "ActiveState", "--value"],
                   check=False)
    print(f"== {UNIT} 状态 ==\n{out}")
    try:
        with urllib.request.urlopen(f"{PAY_BASE}/api/pay/healthz", timeout=8) as r:
            print("== healthz ==\n  " + r.read().decode("utf-8")[:300])
    except Exception as e:  # noqa: BLE001
        print(f"== healthz 失败: {e} ==")
        return 1
    return 0


def healthz_ok() -> bool:
    for _ in range(10):
        try:
            with urllib.request.urlopen(f"{PAY_BASE}/api/pay/healthz", timeout=5) as r:
                d = json.loads(r.read().decode("utf-8"))
                if d.get("ok") and d.get("channel") == "xunhupay":
                    return True
        except Exception:  # noqa: BLE001, S112
            pass
        time.sleep(1.5)
    return False


def smoke_order() -> None:
    """真实建一单（不付款），证明新凭据可用。"""
    env = sh(["systemctl", "show", UNIT, "-p", "Environment", "--value"])[1]
    secret = ""
    for kv in env.split():
        if "=" in kv:
            k, v = kv.split("=", 1)
            os.environ[k] = v
            if k == "VDL_LICENSE_SECRET":
                secret = v
    if not secret:
        print("!! 取不到 VDL_LICENSE_SECRET，跳过下单冒烟")
        return
    import base64
    import hmac

    def b64u(s: str) -> str:
        return base64.urlsafe_b64encode(s.encode("utf-8")).rstrip(b"=").decode("ascii")

    now = time.time()
    raw = f"smoketest@vdl.local|{int(now)}|{int(now + 600)}"
    sig = hmac.new(secret.encode(), raw.encode(), "sha256").hexdigest()[:32]
    token = b64u(f"{raw}|{sig}")
    body = json.dumps({"token": token, "plan_code": "download_1day",
                       "client": "desktop"}).encode()
    req = urllib.request.Request(f"{PAY_BASE}/api/pay/create", data=body,
                                 headers={"Content-Type": "application/json"})
    print("== 下单冒烟（不付款，将自然超时）==")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8"))
            print(f"   HTTP 200 order_id={d.get('order_id')} amount={d.get('amount')} "
                  f"mode={d.get('mode')}")
            print("   ✅ 新凭据可正常下单")
    except urllib.error.HTTPError as e:
        print(f"   HTTP {e.code}: {e.read().decode('utf-8')[:400]}")
        print("   ❌ 新凭据下单失败 —— 请核对 appid/appsecret，必要时回滚")
    except Exception as e:  # noqa: BLE001
        print(f"   请求异常: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default=CFG_DEFAULT)
    ap.add_argument("--appid")
    ap.add_argument("--secret")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    ap.add_argument("--smoke", action="store_true", help="改完真实建一单验证")
    ap.add_argument("--verify-only", action="store_true")
    a = ap.parse_args()

    if a.verify_only:
        return verify_only(a.cfg)

    if not a.appid and not a.secret:
        print("!! 至少给 --appid 或 --secret 之一（或 --verify-only 只看不改）")
        return 2

    cfg = a.cfg
    old = load(cfg)

    if a.appid and not re.fullmatch(r"\d{6,20}", a.appid):
        print(f"!! appid 形似非法（应全数字）: {a.appid}")
        return 2
    if a.secret and not re.fullmatch(r"[0-9a-fA-F]{32}", a.secret):
        print(f"!! appsecret 形似非法（应 32 位 hex）: len={len(a.secret)}")
        return 2

    print("== 变更预览 ==")
    print(f"   appid     {mask(old.get('appid',''))} -> {mask(a.appid) if a.appid else '(不变)'}")
    print(f"   appsecret {mask(old.get('appsecret',''))} -> "
          f"{mask(a.secret) if a.secret else '(不变)'}")
    if not a.yes:
        ans = input("确认写入并重启 vdl-pay？[y/N] ").strip().lower()
        if ans != "y":
            print("已取消")
            return 0

    ts = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{cfg}.bak-{ts}"
    shutil.copy2(cfg, bak)
    os.chmod(bak, 0o600)
    print(f"== 已备份 -> {bak} ==")

    new = dict(old)
    if a.appid:
        new["appid"] = a.appid
    if a.secret:
        new["appsecret"] = a.secret
    try:
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
        os.chmod(cfg, 0o600)
        print("== 已写入（权限 600）==")

        print(f"== restart {UNIT} ==")
        sh(["systemctl", "restart", UNIT])
        time.sleep(3)

        if healthz_ok():
            print("== healthz ✅ ok / channel=xunhupay ==")
        else:
            raise RuntimeError("healthz 未通过")

        if a.smoke:
            smoke_order()
        print(f"\n✅ 完成。回滚命令：cp {bak} {cfg} && systemctl restart {UNIT}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"!! 失败：{e}\n== 自动回滚 ==")
        shutil.copy2(bak, cfg)
        os.chmod(cfg, 0o600)
        sh(["systemctl", "restart", UNIT], check=False)
        print(f"已回滚到 {bak} 并重启")
        return 1


if __name__ == "__main__":
    sys.exit(main())
