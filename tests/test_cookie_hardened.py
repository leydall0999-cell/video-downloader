"""强反爬站点 Cookie 支持范围测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import cookie_pool
import downloader


def test_youku_is_cookie_hardened():
    assert downloader.is_cookie_hardened_host("youku.com")
    assert downloader.is_cookie_hardened_host("www.youku.com")
    assert downloader.is_cookie_hardened_host("v.youku.com")
    assert not downloader.is_cookie_hardened_host("example.com")


def test_youku_allowed_in_public_pool():
    assert cookie_pool.is_allowed("youku.com")
    assert cookie_pool.is_allowed("www.youku.com")
    assert cookie_pool.is_allowed("v.youku.com")
