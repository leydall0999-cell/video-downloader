#!/usr/bin/env python3
"""一键生成 VDL Cookie 相关环境变量。

产出（复制即可贴到 Railway `radiant-art/web` 的环境变量）：
  CHRQJ_COOKIE        —— chrqj 登录态（纯 "a=b; c=d" 串，无 "Cookie:" 前缀）
  VDL_COOKIE_ENC_KEY  —— Fernet key（用于加密公共池存储，可选但建议）
  VDL_COOKIE_ALERT_WEBHOOK —— 公共池空了发告警（可选，本脚本只提示、不生成）

用法（在本机、用装了依赖的 venv 跑）：
  .build_venv/bin/python gen_cookie_env.py
  .build_venv/bin/python gen_cookie_env.py --host chrqj.com --url https://www.chrqj.com/
  .build_venv/bin/python gen_cookie_env.py --cookie-file cookies.txt   # 不想读浏览器时，从文件读纯串

说明：
  - 优先复用 server/downloader.get_browser_cookie_header 从本机浏览器自动读 chrqj Cookie；
    读不到时会提示你先在浏览器登录 chrqj.com。
  - 拿到 Cookie 后用 server/cookie_pool.verify_chrqj 真调一次验真，无效则明确告诉你。
"""
from __future__ import annotations

import os
import sys
import argparse

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


def verify(header: str) -> bool | None:
    try:
        import cookie_pool
        return cookie_pool.verify_chrqj(header)
    except Exception as e:
        print(f"  [warn] 验真失败（网络/依赖）：{e}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 VDL Cookie 环境变量")
    ap.add_argument("--host", default="chrqj.com")
    ap.add_argument("--url", default="https://www.chrqj.com/")
    ap.add_argument("--cookie-file", default=None, help="从文件读纯 Cookie 串（跳过浏览器自动提取）")
    ap.add_argument("--no-verify", action="store_true", help="跳过目标站验真")
    args = ap.parse_args()

    print("=" * 62)
    print(" VDL Cookie 环境变量生成器")
    print("=" * 62)

    # 1) 取 Cookie
    header: str | None = None
    if args.cookie_file:
        header = load_cookie_file(args.cookie_file)
        print(f"\n[1] 从文件读取 Cookie：{'OK' if header else '失败'}")
    else:
        header = load_browser_cookie(args.host, args.url)
        print(f"\n[1] 从本机浏览器读取 {args.host} Cookie："
              f"{'OK' if header else '未检测到（请先在浏览器登录该站）'}")

    if not header:
        print("     提示：浏览器登录 chrqj.com 后重跑，或 --cookie-file cookies.txt 手动给。")
        return 1

    # 2) 验真
    if not args.no_verify:
        ok = verify(header)
        if ok is True:
            print("[2] 验真：✅ 有效（已真调 chrqj 签名接口确认）")
        elif ok is False:
            print("[2] 验真：❌ 无效（该 Cookie 已失效/被拒，请重新登录后再取）")
            return 1
        else:
            print("[2] 验真：⚠️  无法判定（沙盒/离线无外网），已跳过；请部署后自行确认。")
    else:
        print("[2] 验真：已按 --no-verify 跳过")

    # 3) 生成加密 key
    enc_key = gen_fernet_key()
    if enc_key:
        print("[3] 已生成 VDL_COOKIE_ENC_KEY（Fernet）")
    else:
        print("[3] ⚠️  本环境无 cryptography，无法生成 key；"
              "请在装好依赖的 venv 里跑，或用下面命令生成：")
        print("      python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")

    # 4) 输出
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


if __name__ == "__main__":
    raise SystemExit(main())
