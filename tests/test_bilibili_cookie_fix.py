"""B站 Cookie 容错：用户粘贴裸 SESSDATA 值（无 SESSDATA= 前缀）时自动补前缀。

避免「明明填了 Cookie 还 403」：后端直接把字符串当 Cookie 头发，裸值会被 B站 当无效 Cookie。
覆盖：
  A. B站 裸值  → 自动补 SESSDATA=
  B. B站 短链 b23.tv 裸值 → 同样补
  C. B站 已带 SESSDATA= → 保持原样
  D. 非 B站 裸值（如 douyin）→ 不瞎猜键名，保持原样（仅 B站 做了特定处理）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import downloader  # noqa: E402


def _cookie_header(host: str, cookie: str) -> str:
    opts = downloader._base_options(host=host, cookie=cookie)
    return opts.get("http_headers", {}).get("Cookie", "")


def test_bilibili_bare_value_gets_sessdata_prefix():
    header = _cookie_header("www.bilibili.com", "47e9736e,1802507376,abc")
    assert header == "SESSDATA=47e9736e,1802507376,abc"


def test_bilibili_short_link_bare_value_gets_sessdata_prefix():
    header = _cookie_header("b23.tv", "47e9736e,1802507376,abc")
    assert header == "SESSDATA=47e9736e,1802507376,abc"


def test_bilibili_with_prefix_unchanged():
    header = _cookie_header("www.bilibili.com", "SESSDATA=47e9736e,1802507376,abc")
    assert header == "SESSDATA=47e9736e,1802507376,abc"


def test_non_bilibili_bare_value_not_mangled():
    # 其他平台键名各异，不做猜测，保持原样（由用户负责格式）
    header = _cookie_header("www.douyin.com", "some_raw_token_value")
    assert header == "some_raw_token_value"


if __name__ == "__main__":
    test_bilibili_bare_value_gets_sessdata_prefix()
    test_bilibili_short_link_bare_value_gets_sessdata_prefix()
    test_bilibili_with_prefix_unchanged()
    test_non_bilibili_bare_value_not_mangled()
    print("✅ B站 Cookie 容错测试通过")
