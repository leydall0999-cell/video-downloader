"""cookie_cache 单元测试：权限 / 命中过期 / 路径穿越 / 清除。"""
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import cookie_cache
import downloader


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    importlib.reload(cookie_cache)
    monkeypatch.setattr(downloader, "get_browser_cookie_header", lambda h, u: "sess=1")
    return cookie_cache


def test_save_file_perms_600(fresh, tmp_path):
    assert fresh.get_cached_cookie_header("v.qq.com") == "sess=1"
    f = tmp_path / ".videodownloader" / "cookies" / "v.qq.com.json"
    assert f.exists()
    mode = oct(f.stat().st_mode & 0o777)
    assert mode == "0o600", f"cookie 文件权限应为 600，实际 {mode}"


def test_cache_dir_perms_700(fresh, tmp_path):
    fresh.get_cached_cookie_header("v.qq.com")  # 触发建目录
    d = tmp_path / ".videodownloader" / "cookies"
    mode = oct(d.stat().st_mode & 0o777)
    assert mode == "0o700", f"cookie 目录权限应为 700，实际 {mode}（755 会泄露登录足迹）"


def test_hit_then_expiry(fresh, tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake(h, u):
        calls["n"] += 1
        return "sess=x"

    monkeypatch.setattr(downloader, "get_browser_cookie_header", fake)
    assert fresh.get_cached_cookie_header("a.com") == "sess=x"
    assert calls["n"] == 1  # 首次实时解密
    assert fresh.get_cached_cookie_header("a.com") == "sess=x"
    assert calls["n"] == 1  # 二次命中缓存，不再解密
    f = tmp_path / ".videodownloader" / "cookies" / "a.com.json"
    data = json.loads(f.read_text())
    data["ts"] = 0  # 制造过期
    f.write_text(json.dumps(data))
    assert fresh.get_cached_cookie_header("a.com") == "sess=x"
    assert calls["n"] == 2  # 过期后重新解密


def test_path_traversal_host_safe(fresh, tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "get_browser_cookie_header", lambda h, u: "x=1")
    fresh.get_cached_cookie_header("evil.com/../../../../etc/passwd")
    names = {p.name for p in (tmp_path / ".videodownloader" / "cookies").glob("*.json")}
    assert names == {"evil.com_.._.._.._.._etc_passwd.json"}
    assert not (tmp_path / "etc").exists()


def test_clear(fresh, tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "get_browser_cookie_header", lambda h, u: "x=1")
    fresh.get_cached_cookie_header("a.com")
    fresh.get_cached_cookie_header("b.com")
    assert fresh.clear_cookie_cache() == 2
    assert not list((tmp_path / ".videodownloader" / "cookies").glob("*..json"))
