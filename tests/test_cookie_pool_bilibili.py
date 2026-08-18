#!/usr/bin/env python3
"""验证 bilibili.com 被纳入公共 Cookie 池白名单。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import cookie_pool


def test_bilibili_in_root_domains():
    roots = cookie_pool._root_domains()
    assert "bilibili.com" in roots, f"bilibili.com 不在白名单：{roots}"


def test_bilibili_allowed_with_www():
    assert cookie_pool.is_allowed("www.bilibili.com") is True
    assert cookie_pool.is_allowed("bilibili.com") is True


def test_bilibili_subdomain_fallback():
    # www.bilibili.com 不在白名单，但 get_cookie 会按候选域回退到 bilibili.com 池文件
    header = "SESSDATA=subdomain"
    cookie_pool.add_cookie("bilibili.com", header)
    got = cookie_pool.get_cookie("www.bilibili.com")
    assert got == header, f"www.bilibili.com 未回退到 bilibili.com 池: {got}"
    f = cookie_pool._pool_file("bilibili.com")
    if f.exists():
        f.unlink()


def test_bilibili_add_and_get_cookie():
    header = "SESSDATA=testvalue; bili_jct=another"
    assert cookie_pool.add_cookie("www.bilibili.com", header) is True
    got = cookie_pool.get_cookie("www.bilibili.com")
    assert got == header, f"get_cookie 返回不一致: {got}"
    # 清理
    f = cookie_pool._pool_file("bilibili.com")
    if f.exists():
        f.unlink()


if __name__ == "__main__":
    test_bilibili_in_root_domains()
    test_bilibili_allowed_with_www()
    test_bilibili_subdomain_fallback()
    test_bilibili_add_and_get_cookie()
    print("✅ B站 Cookie 池白名单测试通过")
