"""更新清单签名校验测试（安全审计 P0-1，2026-10-11）。

背景：更新清单 latest.json 与安装包 sha256 此前都取自同一条未认证通道，
攻击者可在网络路径上同时替换清单与包、校验随之自洽 → 供应链 RCE。
修复后：发布侧用 Ed25519 私钥对 latest.json 原始字节签名，客户端用公钥验签，
验签不通过一律拒绝更新（fail-closed）。

本测试锁住它的契约：
  1. 正确签名 → 通过
  2. 篡改清单内容（签名不变）→ 拒绝
  3. 签名错误 / 缺签名文件 → 拒绝
  4. 未配置公钥且未放行 → 拒绝（fail-closed）
  5. 显式 VDL_UPDATE_ALLOW_UNSIGNED=1 → 放行（仅应急）

运行：cd server && python tests/test_update_manifest_signature.py
"""
import base64
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import importlib  # noqa: E402

sysmod = importlib.import_module("routers.system")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402


def _keypair():
    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    return key, pub_b64


def _sig_b64(key, raw: bytes) -> str:
    return base64.b64encode(key.sign(raw)).decode("ascii")


_RAW = b'{"version": "9.9.9", "sha256": "deadbeef", "url": "http://evil/x.zip"}'


def test_valid_signature_passes():
    key, pub = _keypair()
    assert sysmod._verify_manifest_signature(_RAW, _sig_b64(key, _RAW), pub) is True
    print("✅ 正确签名通过")


def test_tampered_manifest_rejected():
    """关键用例：只改清单内容、沿用旧签名 —— 必须拒绝（这正是被利用的攻击形态）。"""
    key, pub = _keypair()
    sig = _sig_b64(key, _RAW)
    tampered = _RAW.replace(b"evil", b"good")
    assert sysmod._verify_manifest_signature(tampered, sig, pub) is False
    print("✅ 篡改清单被拒绝")


def test_wrong_key_or_sig_rejected():
    key, pub = _keypair()
    other, _ = _keypair()
    assert sysmod._verify_manifest_signature(_RAW, _sig_b64(other, _RAW), pub) is False
    assert sysmod._verify_manifest_signature(_RAW, "not-base64!!", pub) is False
    assert sysmod._verify_manifest_signature(_RAW, "", pub) is False
    print("✅ 错误密钥 / 非法签名被拒绝")


def _patch_fetch(raw: bytes, sig: bytes | None):
    def fake(url: str, timeout: int = 15) -> bytes:
        if url.endswith(sysmod.UPDATE_SIG_SUFFIX):
            if sig is None:
                raise OSError("404")
            return sig
        return raw
    sysmod._http_get_bytes = fake


def test_fetch_manifest_ok_with_signature():
    key, pub = _keypair()
    os.environ["VDL_UPDATE_PUBKEY"] = pub
    _patch_fetch(_RAW, _sig_b64(key, _RAW).encode())
    try:
        data = sysmod._fetch_manifest()
        assert data["version"] == "9.9.9"
        print("✅ 验签通过后正常返回清单")
    finally:
        os.environ.pop("VDL_UPDATE_PUBKEY", None)


def test_fetch_manifest_rejects_missing_sig():
    key, pub = _keypair()
    os.environ["VDL_UPDATE_PUBKEY"] = pub
    _patch_fetch(_RAW, None)  # 签名文件缺失
    try:
        try:
            sysmod._fetch_manifest()
            raise AssertionError("缺签名文件时应拒绝")
        except sysmod.ManifestError:
            pass
        print("✅ 缺签名文件被拒绝")
    finally:
        os.environ.pop("VDL_UPDATE_PUBKEY", None)


def test_fetch_manifest_fail_closed_without_pubkey():
    """未配置公钥且未放行 → 拒绝（防止悄悄退回不验签的旧行为）。"""
    os.environ.pop("VDL_UPDATE_PUBKEY", None)
    old_allow = sysmod.UPDATE_ALLOW_UNSIGNED
    old_pub = sysmod.UPDATE_MANIFEST_PUBKEY_B64
    sysmod.UPDATE_ALLOW_UNSIGNED = False
    sysmod.UPDATE_MANIFEST_PUBKEY_B64 = ""
    _patch_fetch(_RAW, None)
    try:
        try:
            sysmod._fetch_manifest()
            raise AssertionError("未配置公钥时应 fail-closed")
        except sysmod.ManifestError:
            pass
        print("✅ 未配置公钥时 fail-closed")
    finally:
        sysmod.UPDATE_ALLOW_UNSIGNED = old_allow
        sysmod.UPDATE_MANIFEST_PUBKEY_B64 = old_pub


def test_fetch_manifest_escape_hatch():
    """显式 VDL_UPDATE_ALLOW_UNSIGNED=1 → 放行（应急通道）。"""
    os.environ.pop("VDL_UPDATE_PUBKEY", None)
    old_allow = sysmod.UPDATE_ALLOW_UNSIGNED
    old_pub = sysmod.UPDATE_MANIFEST_PUBKEY_B64
    sysmod.UPDATE_ALLOW_UNSIGNED = True
    sysmod.UPDATE_MANIFEST_PUBKEY_B64 = ""
    _patch_fetch(_RAW, None)
    try:
        data = sysmod._fetch_manifest()
        assert data["version"] == "9.9.9"
        print("✅ 应急放行通道生效")
    finally:
        sysmod.UPDATE_ALLOW_UNSIGNED = old_allow
        sysmod.UPDATE_MANIFEST_PUBKEY_B64 = old_pub


if __name__ == "__main__":
    test_valid_signature_passes()
    test_tampered_manifest_rejected()
    test_wrong_key_or_sig_rejected()
    test_fetch_manifest_ok_with_signature()
    test_fetch_manifest_rejects_missing_sig()
    test_fetch_manifest_fail_closed_without_pubkey()
    test_fetch_manifest_escape_hatch()
    print("\n🎉 更新清单签名校验测试全部通过 — 明文通道被篡改将无法通过验签。")
