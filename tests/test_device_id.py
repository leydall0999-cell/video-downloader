"""device_id 单测（离线，不依赖真实系统 ID）。"""
import importlib.util
import sys
from pathlib import Path

import pytest

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "device_id", _here.parent / "server" / "device_id.py")
device_id = importlib.util.module_from_spec(_spec)
sys.modules["device_id"] = device_id
_spec.loader.exec_module(device_id)


def test_fingerprint_format_and_stability():
    fp1, strong = device_id.fingerprint()
    fp2, _ = device_id.fingerprint()
    assert device_id.is_fingerprint(fp1)
    assert fp1 == fp2                      # 进程内缓存，两次一致
    assert isinstance(strong, bool)


def test_digest_salts_source(monkeypatch):
    """同一源 ID 换 salt 指纹必须变（防原始 UUID 泄漏 + 跨产品撞库）。"""
    a = device_id._digest("TEST-SRC")
    monkeypatch.setattr(device_id, "_SALT", "other-line")
    b = device_id._digest("TEST-SRC")
    assert a != b
    assert device_id.is_fingerprint(a) and device_id.is_fingerprint(b)


def test_weak_source_marked(monkeypatch):
    """所有系统源取不到时走弱降级：指纹仍可用，但 strong 必须为 False。"""
    monkeypatch.setattr(device_id, "_macos_platform_uuid", lambda: None)
    monkeypatch.setattr(device_id, "_windows_machine_guid", lambda: None)
    monkeypatch.setattr(device_id, "_linux_machine_id", lambda: None)
    device_id.fingerprint.cache_clear()
    fp, strong = device_id.fingerprint()
    assert device_id.is_fingerprint(fp)
    assert strong is False
    with pytest.raises(RuntimeError):
        device_id.fingerprint_strict()
    device_id.fingerprint.cache_clear()


def test_strict_returns_when_strong(monkeypatch):
    monkeypatch.setattr(device_id, "_macos_platform_uuid", lambda: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
    device_id.fingerprint.cache_clear()
    fp, strong = device_id.fingerprint()
    assert strong is True
    assert device_id.fingerprint_strict() == fp
    device_id.fingerprint.cache_clear()


def test_real_fingerprint_on_this_machine():
    """本机真跑一次：macOS 上必须是 strong（真机 CI 语义，弱环境自动跳过）。"""
    fp, strong = device_id.fingerprint()
    assert device_id.is_fingerprint(fp)
    if sys.platform == "darwin":
        assert strong, "macOS 上应取到 IOPlatformUUID 强指纹"
