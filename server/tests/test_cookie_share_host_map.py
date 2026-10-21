#!/usr/bin/env python3
"""分享域 / 极速版域 → 主站登录域的 Cookie 映射（2026-10-21）。

## 为什么需要

`chenzhongtech.com`（快手极速版）、`iesdouyin.com`（抖音分享页）、`b23.tv`（B站短链）
这类域**自身不承载登录态**，浏览器里只有设备指纹：

    实测 Chrome Cookie 库（同一时刻、同一用户）
    .chenzhongtech.com → did, didv                         （无 passToken / webday7_st）
    .iesdouyin.com     → ttwid                             （无 sessionid / sid_tt）
    .kuaishou.com      → userId, kwssectoken, ...
    www.kuaishou.com   → kuaishou.server.webday7_st, ...
    id.kuaishou.com    → passToken                         ← 只在登录子域

若不换域取 Cookie，解析极速版/分享链接时带的是匿名设备指纹 ⇒ 低清晰度、
会员内容失败、部分链接直接解析不出。于是统一回溯到主站域。

## 锁什么

1. 映射表本身（含子域、大小写）；
2. `_cookie_domains_for_host` 同时给出「自身根域 + 主站」；
3. 非分享域**不得**被误映射（douyin.com / kuaishou.com 原样返回）；
4. B 站 CDN 映射（bilivideo.cn → bilibili.com）不得被这次改动破坏。

运行：cd server && python tests/test_cookie_share_host_map.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import downloader as D  # noqa: E402

_FAILED: list[str] = []


def check(name: str, cond: bool, extra=None) -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else f"  → {extra!r}"))
    if not cond:
        _FAILED.append(name)


def test_login_host() -> None:
    print("[1] _cookie_login_host：分享域/极速版域换到主站")
    cases = {
        "chenzhongtech.com": "kuaishou.com",
        "www.chenzhongtech.com": "kuaishou.com",
        "v.chenzhongtech.com": "kuaishou.com",
        "CHENZHONGTECH.COM": "kuaishou.com",
        "gifshow.com": "kuaishou.com",
        "iesdouyin.com": "douyin.com",
        "www.iesdouyin.com": "douyin.com",
        "b23.tv": "bilibili.com",
    }
    for host, want in cases.items():
        got = D._cookie_login_host(host)
        check(f"{host} → {want}", got == want, got)


def test_no_false_positive() -> None:
    print("[2] 主站/普通站不得被误映射")
    for host in ("douyin.com", "www.douyin.com", "kuaishou.com", "v.kuaishou.com",
                 "bilibili.com", "youtube.com", "xiaohongshu.com"):
        got = D._cookie_login_host(host)
        check(f"{host} 原样保留", got == host, got)


def test_domains_for_host() -> None:
    print("[3] _cookie_domains_for_host：自身根域 + 主站都在候选里")
    d = D._cookie_domains_for_host("v.chenzhongtech.com")
    check("含 chenzhongtech.com", "chenzhongtech.com" in d, d)
    check("含 kuaishou.com（取登录态用）", "kuaishou.com" in d, d)
    d = D._cookie_domains_for_host("www.iesdouyin.com")
    check("含 douyin.com", "douyin.com" in d, d)


def test_bili_cdn_kept() -> None:
    print("[4] B 站 CDN 回溯不得被破坏（回归）")
    check("bilivideo.cn → bilibili.com",
          D._cookie_login_host("upos-sz.bilivideo.cn") == "bilibili.com",
          D._cookie_login_host("upos-sz.bilivideo.cn"))
    check("bilivideo.com → bilibili.com",
          D._cookie_login_host("x.bilivideo.com") == "bilibili.com",
          D._cookie_login_host("x.bilivideo.com"))


def main() -> int:
    test_login_host()
    test_no_false_positive()
    test_domains_for_host()
    test_bili_cdn_kept()
    print()
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败：" + "；".join(_FAILED))
        return 1
    print("🎉 分享域→主站 Cookie 映射全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
