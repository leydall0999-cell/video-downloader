#!/usr/bin/env python3
"""一键生成 VDL Cookie 相关配置（支持任意需要登录态的平台）。

按「该站是否有 env 兜底读取点」分两条路产出：

  A. chrqj（提取器有 CHRQJ_COOKIE env 兜底）→ 输出可直接贴 Railway 的 env：
       CHRQJ_COOKIE=...        （登录态）
       VDL_COOKIE_ENC_KEY=...  （Fernet key，加密公共池，可选）

  B. 其他站（douyin/快手/bilibili/v.qq 等，走公共池）→ 验真后上报到 Railway 公共池
       （需 Railway 设了 VDL_COOKIE_SYNC_TOKEN，用 --sync-url/--sync-token 上报），
       网页版访客即可无感复用；不传 --sync-url 则打印等价 curl 命令供手动执行。

用法（在本机、用装了依赖的 venv 跑）：
  .build_venv/bin/python gen_cookie_env.py                              # chrqj → env
  .build_venv/bin/python gen_cookie_env.py --host douyin.com --sync-url https://hanyuxz.top --sync-token <TOKEN>
  .build_venv/bin/python gen_cookie_env.py --host chrqj.com --cookie-file cookies.txt   # 从文件读
  .build_venv/bin/python gen_cookie_env.py --host x.com --no-verify     # 跳过验真

说明：
  - 复用 server/downloader.get_browser_cookie_header 从本机浏览器自动读对应站 Cookie
    （含 Profile 定位 + 解密），读不到会提示先在浏览器登录该站。
  - 验真用 server/cookie_pool.verify_cookie（chrqj 走签名验真，其余走 yt-dlp 通用验真）。
"""
from __future__ import annotations

import os
import sys
import json
import argparse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(ROOT, "server")
for p in (ROOT, SERVER):
    if p not in sys.path:
        sys.path.insert(0, p)


def gen_fernet_key() -> str | None:
    """生成 Fernet key；cryptography 不可用则返回 None。"""
    try:
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()
    except Exception:
        return None


def load_browser_cookie(host: str, url: str) -> str | None:
    """复用下载层的浏览器 Cookie 自动提取（含 Profile 定位 + 解密）。"""
    try:
        import downloader
        return downloader.get_browser_cookie_header(host, url)
    except Exception as e:
        print(f"  [warn] 无法读浏览器 Cookie：{e}", file=sys.stderr)
        return None


def load_cookie_file(path: str) -> str | None:
    try:
        raw = open(path, encoding="utf-8").read().strip()
        if raw.startswith("Cookie:"):
            raw = raw[len("Cookie:"):].strip()
        return raw or None
    except Exception as e:
        print(f"  [warn] 读文件失败：{e}", file=sys.stderr)
        return None


def strip_sub(domain: str) -> str:
    """去 www/m 前缀，得到根域（复用 cookie_pool 逻辑，失败则简单降级）。"""
    try:
        import cookie_pool
        return cookie_pool._strip_sub(domain)
    except Exception:
        d = (domain or "").strip().lower()
        for p in ("https://", "http://", "//"):
            if d.startswith(p):
                d = d[len(p):]
        d = d.split("/")[0].split(":")[0]
        for sub in ("www.", "m."):
            if d.startswith(sub):
                d = d[len(sub):]
        return d


def verify_cookie(domain: str, header: str) -> bool | None:
    """按域分发验真（chrqj 签名 / 其余 yt-dlp 通用）。"""
    try:
        import cookie_pool
        return cookie_pool.verify_cookie(domain, header)
    except Exception as e:
        print(f"  [warn] 验真失败（网络/依赖）：{e}", file=sys.stderr)
        return None


def push_to_pool(sync_url: str, token: str, domain: str, header: str) -> tuple[int, dict]:
    """把 Cookie 上报到服务器公共池（POST /api/cookie/sync）。"""
    url = sync_url.rstrip("/") + "/api/cookie/sync"
    body = json.dumps({"token": token, "domain": domain, "cookie": header}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 VDL Cookie 配置（env / 公共池）")
    ap.add_argument("--host", default="chrqj.com", help="目标站点域名，默认 chrqj.com")
    ap.add_argument("--url", default=None, help="读 Cookie 用的页面 URL，默认 https://<host>/")
    ap.add_argument("--cookie-file", default=None, help="从文件读纯 Cookie 串（跳过浏览器自动提取）")
    ap.add_argument("--no-verify", action="store_true", help="跳过目标站验真")
    ap.add_argument("--sync-url", default=None, help="上报公共池的服务器地址（如 https://hanyuxz.top）")
    ap.add_argument("--sync-token", default=None, help="VDL_COOKIE_SYNC_TOKEN（上报公共池用，非 chrqj 站需要）")
    args = ap.parse_args()

    host = (args.host or "").strip().lower()
    url = args.url or f"https://{host}/"
    domain = strip_sub(host)
    is_chrqj = domain == "chrqj.com"

    print("=" * 62)
    print(" VDL Cookie 配置生成器")
    print(f" 目标站点：{host}" + ("（env 兜底）" if is_chrqj else "（走公共池）"))
    print("=" * 62)

    # 1) 取 Cookie
    header: str | None = None
    if args.cookie_file:
        header = load_cookie_file(args.cookie_file)
        print(f"\n[1] 从文件读取 Cookie：{'OK' if header else '失败'}")
    else:
        header = load_browser_cookie(host, url)
        print(f"\n[1] 从本机浏览器读取 {host} Cookie："
              f"{'OK' if header else '未检测到（请先在浏览器登录该站）'}")

    if not header:
        print(f"     提示：浏览器登录 {host} 后重跑，或 --cookie-file cookies.txt 手动给。")
        return 1

    # 2) 验真
    if not args.no_verify:
        ok = verify_cookie(domain, header)
        if ok is True:
            print("[2] 验真：✅ 有效")
        elif ok is False:
            print("[2] 验真：❌ 无效（该 Cookie 已失效/被拒，请重新登录后再取）")
            return 1
        else:
            print("[2] 验真：⚠️  无法判定（沙盒/离线无外网），已跳过；请部署后自行确认。")
    else:
        print("[2] 验真：已按 --no-verify 跳过")

    # 3) 产出（按站点类型分流）
    if is_chrqj:
        enc_key = gen_fernet_key()
        if enc_key:
            print("[3] 已生成 VDL_COOKIE_ENC_KEY（Fernet）")
        else:
            print("[3] ⚠️  本环境无 cryptography，无法生成 key；"
                  "可在装好依赖的 venv 里跑，或用：")
            print('      python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"')

        print("\n" + "=" * 62)
        print(" 复制以下键值到 Railway 环境变量（Variables）")
        print("=" * 62)
        print(f'\nCHRQJ_COOKIE={header}\n')
        if enc_key:
            print(f'VDL_COOKIE_ENC_KEY={enc_key}\n')
        print("# 可选：公共池空了发告警（企业微信/飞书/钉钉机器人 webhook）")
        print("# VDL_COOKIE_ALERT_WEBHOOK=<你的机器人 URL>\n")
        print("改完在 Railway 点 Deploy 重新部署生效。")
        return 0

    # 其他站：走公共池
    print(f"\n[3] {host} 无 env 兜底，走公共池（网页版访客无感复用）。")
    if args.sync_url:
        if not args.sync_token:
            print("❌ 上报公共池需要 --sync-token（Railway 的 VDL_COOKIE_SYNC_TOKEN）")
            return 1
        try:
            code, data = push_to_pool(args.sync_url, args.sync_token, domain, header)
            if code == 200 and data.get("ok"):
                print(f"✅ 已上报到 {args.sync_url} 公共池（verified={data.get('verified')}）")
                return 0
            print(f"❌ 上报失败 HTTP {code}：{data.get('detail', data)}")
            return 1
        except Exception as e:
            print(f"❌ 上报出错：{e}")
            return 1

    # 未给 --sync-url：打印等价 curl
    print("\n" + "=" * 62)
    print(" 等价 curl 命令（在能访问目标服务器的机器上执行）")
    print("=" * 62)
    cookie_esc = header.replace("'", "'\\''")
    print(f"\ncurl -sS -X POST '{ (args.sync_url or 'https://hanyuxz.top').rstrip('/') }/api/cookie/sync' \\")
    print("  -H 'Content-Type: application/json' \\")
    print(f"  -d '{{\"token\":\"<VDL_COOKIE_SYNC_TOKEN>\",\"domain\":\"{domain}\",\"cookie\":\"{cookie_esc}\"}}'\n")
    print("# 或直接加 --sync-url --sync-token 让本脚本代上报。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
