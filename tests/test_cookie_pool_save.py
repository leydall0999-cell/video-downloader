"""公共 Cookie 池 _save 加密往返回归测试。

背景：vps-pull 每次写入同一个 B站 Cookie，add_cookie 去重应命中；但旧 _save
实现用 c.get("header") 取明文，对加密记录（仅 header_enc 键）取到空串后走
else 分支把密文重写成空明文条目，导致：
1. 历史健康记录逐批变空，get_cookie 读不到稳定 Cookie；
2. 去重失败（空条目解密为 "" 不等于 header），记录无限膨胀。

本组测试锁定该回归。
"""
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import cookie_pool


def _valid_fernet_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def test_save_keeps_existing_encrypted_entry(monkeypatch, tmp_path):
    """_save 重写文件时，已有加密记录（header_enc）不得被重写成空明文条目。"""
    monkeypatch.setattr(cookie_pool, "_POOL_DIR", Path(tmp_path))
    monkeypatch.setenv("VDL_COOKIE_ENC_KEY", _valid_fernet_key())
    assert cookie_pool._cipher() is not None

    # 1. 首次 add：写加密记录
    assert cookie_pool.add_cookie("bilibili.com", "SESSDATA=abc; bili_jct=xyz", source="vps-pull")
    data = json.loads(cookie_pool._pool_file("bilibili.com").read_text())
    assert len(data["cookies"]) == 1
    assert "header_enc" in data["cookies"][0]

    # 2. 模拟后续 pull：同一 header 再次 add → 去重命中，刷新 ts，不新增
    assert cookie_pool.add_cookie("bilibili.com", "SESSDATA=abc; bili_jct=xyz", source="vps-pull")
    data = json.loads(cookie_pool._pool_file("bilibili.com").read_text())
    assert len(data["cookies"]) == 1, "去重应命中，不新增记录"
    # 关键断言：已有加密记录没有被重写成空条目
    assert "header_enc" in data["cookies"][0]
    assert data["cookies"][0]["header_enc"]

    # 3. get 应稳定返回
    got = cookie_pool.get_cookie("bilibili.com")
    assert got == "SESSDATA=abc; bili_jct=xyz"


def test_save_drops_empty_entries(monkeypatch, tmp_path):
    """历史遗留的双空条目应在 _save 时被清理，不无限膨胀。"""
    monkeypatch.setattr(cookie_pool, "_POOL_DIR", Path(tmp_path))
    monkeypatch.setenv("VDL_COOKIE_ENC_KEY", _valid_fernet_key())
    f = cookie_pool._pool_file("bilibili.com")
    f.write_text(json.dumps({"cookies": [
        {"ts": 1, "source": "old", "header": ""},       # 空明文
        {"ts": 2, "source": "old", "header_enc": ""},   # 空密文
    ]}))
    assert cookie_pool.add_cookie("bilibili.com", "SESSDATA=new", source="vps-pull")
    data = json.loads(f.read_text())
    assert len(data["cookies"]) == 1
    assert cookie_pool._decrypt_item(data["cookies"][0]) == "SESSDATA=new"


def test_save_without_cipher_keeps_plaintext(monkeypatch, tmp_path):
    """无加密 key 时明文条目照常保留（老环境兼容）。"""
    monkeypatch.setattr(cookie_pool, "_POOL_DIR", Path(tmp_path))
    monkeypatch.delenv("VDL_COOKIE_ENC_KEY", raising=False)
    assert cookie_pool._cipher() is None
    assert cookie_pool.add_cookie("bilibili.com", "SESSDATA=plain", source="vps-pull")
    got = cookie_pool.get_cookie("bilibili.com")
    assert got == "SESSDATA=plain"
