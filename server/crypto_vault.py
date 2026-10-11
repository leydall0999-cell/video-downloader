"""本地媒体库保险箱：选中文件就地 AES-256-GCM 加密为 .vdlenc。

设计要点（安全）：
- 主密钥从密码派生：master = PBKDF2-HMAC-SHA256(pass, salt, 600k) → 64 字节。
  切分为 enc_key = master[:32]（AES-256 密钥，仅驻留内存）、verify = master[32:]（校验值）。
- vault.json 只存 {salt, verify, iterations}，**绝不存明文密码、也不存 enc_key**。
  攻击者拿到 vault.json 仍需密码才能算出 enc_key；拿到 .vdlenc 文件同样需要 enc_key。
- 每个加密文件独立随机 nonce_base；分块（1MB）加密，每块 nonce = nonce_base XOR 块序号，
  用 AESGCM 自带 16 字节认证标签，篡改即 InvalidTag 解密失败。
- 文件头（明文）：magic + nonce_base + 原名(utf-8,长度前缀) + 原类型(1字节)，
  便于扫描时显示原名与图标而不必解密内容。
- 解密播放为临时文件，播完即清（由调用方/守护线程清理）；原件加密成功后移入系统回收站保底，
  绝不静默硬删用户资产。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from pathlib import Path
from typing import Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"VDLENC1"
KDF_ITERS = 600_000
CHUNK = 1 << 20  # 1 MiB
NONCE_LEN = 12

KIND_CODE = {"video": 0, "audio": 1, "image": 2, "other": 3}
KIND_REV = {0: "video", 1: "audio", 2: "image", 3: "other"}


# --------------------------------------------------------------------------- #
# 密钥派生 / 校验
# --------------------------------------------------------------------------- #
def _derive_master(passphrase: bytes, salt: bytes, iters: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase, salt, iters, dklen=64)


def new_vault(passphrase: str) -> dict:
    """用新密码生成 vault 持久化数据（只含 salt + verify，不含密钥/明文）。"""
    salt = os.urandom(16)
    master = _derive_master(passphrase.encode("utf-8"), salt, KDF_ITERS)
    return {"salt": salt.hex(), "verify": master[32:].hex(), "iterations": KDF_ITERS}


def verify_passphrase(passphrase: str, vault: dict) -> bool:
    """校验密码是否匹配 vault。"""
    try:
        salt = bytes.fromhex(vault["salt"])
        verify = bytes.fromhex(vault["verify"])
        iters = int(vault["iterations"])
    except (KeyError, ValueError):
        return False
    master = _derive_master(passphrase.encode("utf-8"), salt, iters)
    return hmac.compare_digest(master[32:], verify)


def unlock_key(passphrase: str, vault: dict) -> bytes | None:
    """校验密码并返回内存密钥（32 字节）；失败返回 None。"""
    try:
        salt = bytes.fromhex(vault["salt"])
        iters = int(vault["iterations"])
    except (KeyError, ValueError):
        return None
    master = _derive_master(passphrase.encode("utf-8"), salt, iters)
    return master[:32]


# --------------------------------------------------------------------------- #
# 分块 nonce
# --------------------------------------------------------------------------- #
def _chunk_nonce(base: bytes, idx: int) -> bytes:
    base_int = int.from_bytes(base, "big")
    return (base_int ^ idx).to_bytes(NONCE_LEN, "big")


# --------------------------------------------------------------------------- #
# 文件加解密
# --------------------------------------------------------------------------- #
def encrypt_file(src: Path, dst: Path, key: bytes, orig_name: str, orig_kind: str) -> None:
    """把 src 加密写入 dst（含明文文件头：magic + nonce_base + 原名 + 原类型）。"""
    nonce_base = os.urandom(NONCE_LEN)
    kind_code = KIND_CODE.get(orig_kind, 3)
    name_b = orig_name.encode("utf-8")
    aes = AESGCM(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(MAGIC)
        fout.write(nonce_base)
        fout.write(struct.pack(">H", len(name_b)))
        fout.write(name_b)
        fout.write(bytes([kind_code]))
        idx = 0
        while True:
            data = fin.read(CHUNK)
            if not data:
                break
            ct = aes.encrypt(_chunk_nonce(nonce_base, idx), data, None)
            fout.write(struct.pack(">I", len(ct)))
            fout.write(ct)
            idx += 1


def decrypt_file(src: Path, dst: Path, key: bytes) -> Tuple[str, str]:
    """解密 src 到 dst，返回 (orig_name, orig_kind)。校验失败抛 InvalidTag。"""
    aes = AESGCM(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        if fin.read(len(MAGIC)) != MAGIC:
            raise ValueError("不是有效的加密文件")
        nonce_base = fin.read(NONCE_LEN)
        if len(nonce_base) != NONCE_LEN:
            raise ValueError("文件头损坏")
        (name_len,) = struct.unpack(">H", fin.read(2))
        orig_name = fin.read(name_len).decode("utf-8", "replace")
        kind_code = fin.read(1)
        orig_kind = KIND_REV.get(kind_code[0] if kind_code else 3, "other")
        idx = 0
        while True:
            head = fin.read(4)
            if not head:
                break
            (ct_len,) = struct.unpack(">I", head)
            ct = fin.read(ct_len)
            if len(ct) != ct_len:
                raise ValueError("文件截断")
            data = aes.decrypt(_chunk_nonce(nonce_base, idx), ct, None)
            fout.write(data)
            idx += 1
    return orig_name, orig_kind


def read_header(src: Path) -> Tuple[str, str, str]:
    """只读文件头，返回 (orig_name, orig_kind, orig_ext)，不解密内容。"""
    with open(src, "rb") as fin:
        if fin.read(len(MAGIC)) != MAGIC:
            raise ValueError("不是有效的加密文件")
        fin.read(NONCE_LEN)
        (name_len,) = struct.unpack(">H", fin.read(2))
        orig_name = fin.read(name_len).decode("utf-8", "replace")
        kind_code = fin.read(1)
        orig_kind = KIND_REV.get(kind_code[0] if kind_code else 3, "other")
    ext = Path(orig_name).suffix.lstrip(".").lower()
    return orig_name, orig_kind, ext


# --------------------------------------------------------------------------- #
# 随机访问解密（安全审计 2026-10-11 P1-7：解密播放不再落盘明文）
# --------------------------------------------------------------------------- #
# 分块 GCM 天然支持随机访问：每块的 nonce 只由 (nonce_base, 块序号) 决定，
# 块与块之间无链式依赖，故可以只解密「覆盖请求区间的那几块」。
# 关键性质：**除最后一块外，所有块的明文长度恒为 CHUNK** ⇒ 第 n 块在磁盘上的
# 偏移可由 `data_off + n * (4 + CHUNK + 16)` 直接算出，无需从头顺序扫描。
def _chunk_stride() -> int:
    """一个**完整**块在磁盘上占用的字节数：4 字节长度前缀 + 密文(CHUNK + 16 字节 GCM tag)。"""
    return 4 + CHUNK + 16


def _read_header_fh(fin) -> Tuple[bytes, str, str]:
    """从已打开的文件句柄读文件头，返回 (nonce_base, orig_name, orig_kind)；文件头之后即数据区。"""
    if fin.read(len(MAGIC)) != MAGIC:
        raise ValueError("不是有效的加密文件")
    nonce_base = fin.read(NONCE_LEN)
    if len(nonce_base) != NONCE_LEN:
        raise ValueError("文件头损坏")
    raw = fin.read(2)
    if len(raw) != 2:
        raise ValueError("文件头损坏")
    (name_len,) = struct.unpack(">H", raw)
    name_b = fin.read(name_len)
    if len(name_b) != name_len:
        raise ValueError("文件头损坏")
    kind_code = fin.read(1)
    if len(kind_code) != 1:
        raise ValueError("文件头损坏")
    return nonce_base, name_b.decode("utf-8", "replace"), KIND_REV.get(kind_code[0], "other")


def plaintext_size(src: Path) -> int:
    """加密文件的明文总长度（只跳读块长度前缀，**不解密**）。

    用于 HTTP `Content-Range` 的 total。每块只读 4 字节再 seek 跳过密文，
    1 GiB 文件约 1024 次 seek —— 可接受；调用方如需频繁查询请自行缓存。
    """
    total = 0
    with open(src, "rb") as fin:
        _read_header_fh(fin)
        while True:
            head = fin.read(4)
            if len(head) < 4:
                break
            (ct_len,) = struct.unpack(">I", head)
            if ct_len < 16:
                raise ValueError("块长度非法")
            total += ct_len - 16
            fin.seek(ct_len, os.SEEK_CUR)
    return total


def decrypt_range(src: Path, key: bytes, start: int, end: int) -> Tuple[bytes, str, str]:
    """解密并返回明文区间 [start, end]（**含两端点**），全程不落盘。

    返回 (data, orig_name, orig_kind)。`start` 落在明文末尾之后时 data 为空。
    只解密覆盖该区间的块，故大文件也能按 Range 边解边发（支持播放器拖动进度）。
    """
    if start < 0:
        start = 0
    stride = _chunk_stride()
    with open(src, "rb") as fin:
        nonce_base, orig_name, orig_kind = _read_header_fh(fin)
        data_off = fin.tell()
        if end < start:
            return b"", orig_name, orig_kind
        aes = AESGCM(key)
        out = bytearray()
        idx = start // CHUNK
        pos = idx * CHUNK                      # 该块在**明文**里的起始偏移
        while pos <= end:
            # 前置块恒为满块（只有最后一块可能短），故偏移可直接算
            fin.seek(data_off + idx * stride)
            head = fin.read(4)
            if len(head) < 4:
                break                          # 已越过文件末尾
            (ct_len,) = struct.unpack(">I", head)
            if ct_len < 16 or ct_len > CHUNK + 16:
                raise ValueError("块长度非法")
            ct = fin.read(ct_len)
            if len(ct) != ct_len:
                raise ValueError("文件截断")
            plain = aes.decrypt(_chunk_nonce(nonce_base, idx), ct, None)
            lo = max(start, pos) - pos
            hi = min(end, pos + len(plain) - 1) - pos
            if hi >= lo:
                out += plain[lo:hi + 1]
            if len(plain) < CHUNK:
                break                          # 最后一块，后面没有了
            pos += len(plain)
            idx += 1
        return bytes(out), orig_name, orig_kind
