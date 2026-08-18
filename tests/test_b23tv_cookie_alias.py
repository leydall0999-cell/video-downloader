"""回归测试：b23.tv 短链应复用 bilibili.com 的云端 Cookie。

web 端用户常贴 b23.tv 短链，而云端公共池 / 本机缓存的 B站 Cookie 以
bilibili.com 域存储。若按 host=b23.tv 取 Cookie 会取空 → B站 412。
修复后 _base_options 对 b23.tv 回退到 bilibili.com 取 Cookie。
"""
import os
import sys
import json
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from downloader import _base_options


def _write_pool_cookie(tmp_path, header):
    """模拟云端公共池里存了一份 bilibili.com 的 Cookie。"""
    pool_dir = tmp_path / "cookie_pool"
    pool_dir.mkdir(parents=True, exist_ok=True)
    f = pool_dir / "bilibili.com.json"
    f.write_text(json.dumps({"cookies": [{"header": header, "ts": int(time.time())}]}))
    return pool_dir


def test_b23tv_uses_bilibili_pool_cookie(tmp_path, monkeypatch):
    header = "SESSDATA=fake_bili_cookie; bili_jct=xyz"
    pool_dir = _write_pool_cookie(tmp_path, header)
    monkeypatch.setenv("HOME", str(tmp_path))
    # 让本机缓存取不到，强制走公共池
    import cookie_cache as cc
    monkeypatch.setattr(cc, "get_cached_cookie_header", lambda h: None)
    import cookie_pool as cp
    cp._POOL_DIR = pool_dir

    opts = _base_options(host="b23.tv", cookie="")
    assert opts.get("http_headers", {}).get("Cookie") == header


def test_b23tv_with_manual_cookie_priority(tmp_path, monkeypatch):
    header = "SESSDATA=fake_bili_cookie"
    pool_dir = _write_pool_cookie(tmp_path, header)
    monkeypatch.setenv("HOME", str(tmp_path))
    import cookie_pool as cp
    cp._POOL_DIR = pool_dir

    manual = "SESSDATA=manual_cookie"
    opts = _base_options(host="b23.tv", cookie=manual)
    # 手动 Cookie 优先级最高，不被公共池覆盖
    assert opts["http_headers"]["Cookie"] == manual
