"""授权中心基址解析（2026-09-22 迁 ECS）回归测试。

背景：授权中心自香港（hanyuxz.top → CF → 香港，实测 login 7.5s）迁国内 ECS
（8.138.223.3:8888，实测 ~86ms）。App 端 license_client 的 base 必须按
  env VDL_LICENSE_BASE → cloud_sync.json url → DEFAULT_BASE
三级解析，否则用户登录随机「云端未同步（read timed out）」。
"""
import importlib.util
import json
import os
import sys
import unittest.mock as m

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "license_client_standalone",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "license_client.py"))
lc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lc)

DEFAULT_BASE = "https://hanyuxz.top"


@pytest.fixture(autouse=True)
def _fresh_cache():
    lc._BASE_CACHE.clear()
    yield
    lc._BASE_CACHE.clear()


def _write_cloud_sync(tmp_path, url):
    home = tmp_path / "home"
    d = home / ".videodownloader"
    d.mkdir(parents=True)
    (d / "cloud_sync.json").write_text(json.dumps({"url": url}), encoding="utf-8")
    return str(home)


def test_default_reads_cloud_sync_url(tmp_path):
    home = _write_cloud_sync(tmp_path, "http://8.138.223.3:8888")
    with m.patch("os.path.expanduser", return_value=os.path.join(home, ".videodownloader", "..")):
        pass  # expanduser 拼路径方式不同，直接用 monkeypatch 更稳
    # 直接 monkeypatch expanduser 返回假 home
    with m.patch.object(lc.os.path, "expanduser", return_value=str(home)):
        assert lc.license_base() == "http://8.138.223.3:8888"


def test_env_overrides_everything(tmp_path, monkeypatch):
    home = _write_cloud_sync(tmp_path, "http://8.138.223.3:8888")
    monkeypatch.setenv("VDL_LICENSE_BASE", "http://override:9999")
    with m.patch.object(lc.os.path, "expanduser", return_value=str(home)):
        assert lc.license_base() == "http://override:9999"


def test_fallback_when_no_config(tmp_path, monkeypatch):
    monkeypatch.delenv("VDL_LICENSE_BASE", raising=False)
    with m.patch.object(lc.os.path, "expanduser", return_value=str(tmp_path / "nothing")):
        assert lc.license_base() == DEFAULT_BASE


def test_post_none_base_uses_license_base(monkeypatch):
    """_post 收到 base_url=None 时必须走 license_base()（所有账号函数的新默认）。"""
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        raise OSError("stop here")

    monkeypatch.setenv("VDL_LICENSE_BASE", "http://fake-license:8902")
    with pytest.raises(lc.LicenseCloudError):
        lc.login_remote("a@b.c", "pw", "fp", opener=fake_urlopen)
    assert captured["url"].startswith("http://fake-license:8902/api/license/login")


def test_no_trailing_double_slash():
    monkeypatch_url = "http://x:1/"
    os.environ["VDL_LICENSE_BASE"] = monkeypatch_url
    lc._BASE_CACHE.clear()
    try:
        assert lc.license_base() == "http://x:1"
    finally:
        os.environ.pop("VDL_LICENSE_BASE", None)
        lc._BASE_CACHE.clear()
