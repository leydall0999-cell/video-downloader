"""crypto_vault 单元测试：密钥派生/校验、加解密往返（含大文件>分块、空文件）、错密码拒绝、read_header。

运行：python3 tests/test_crypto_unit.py
"""

import os
import sys
import tempfile
from pathlib import Path

SERVER = str(Path(__file__).resolve().parent.parent / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import crypto_vault as c  # noqa: E402


def check(name, cond):
    print(("PASS" if cond else "FAIL") + " " + name)
    if not cond:
        raise SystemExit(1)


def main():
    d = Path(tempfile.mkdtemp())

    # 1. 派生/校验：正确密码通过，错误密码拒绝
    vault = c.new_vault("p@ss-中文")
    check("verify ok", c.verify_passphrase("p@ss-中文", vault))
    check("verify bad", not c.verify_passphrase("wrong", vault))
    key = c.unlock_key("p@ss-中文", vault)
    check("unlock returns 32-byte key", isinstance(key, bytes) and len(key) == 32)
    wrong_key = c.unlock_key("wrong", vault)
    check("unlock wrong yields different key", isinstance(wrong_key, bytes) and wrong_key != key)

    # 2. vault 不存明文/密钥：只含 salt + verify + iterations
    check("vault has no plaintext", "p@ss-中文" not in str(vault))
    master = c._derive_master("p@ss-中文".encode(), bytes.fromhex(vault["salt"]), vault["iterations"])
    check("verify != enc_key (不泄露密钥)", bytes.fromhex(vault["verify"]) == master[32:] and bytes.fromhex(vault["verify"]) != master[:32])

    # 3. 小文件往返
    src = d / "a.mp4"
    src.write_bytes(b"hello world")
    dst = d / "a.mp4.vdlenc"
    c.encrypt_file(src, dst, key, src.name, "video")
    out = d / "dec_a.mp4"
    nm, kd = c.decrypt_file(dst, out, key)
    check("small roundtrip", out.read_bytes() == b"hello world" and nm == "a.mp4" and kd == "video")

    # 4. 大文件（>1 分块，非对齐）往返
    big = os.urandom(3 * 1024 * 1024 + 12345)
    src2 = d / "big.mp3"
    src2.write_bytes(big)
    dst2 = d / "big.mp3.vdlenc"
    c.encrypt_file(src2, dst2, key, src2.name, "audio")
    out2 = d / "dec_big.mp3"
    nm2, kd2 = c.decrypt_file(dst2, out2, key)
    check("big roundtrip", out2.read_bytes() == big and nm2 == "big.mp3" and kd2 == "audio")

    # 5. 错密码解密应失败（GCM 认证失败）
    bad = c.unlock_key("wrong", vault)
    try:
        c.decrypt_file(dst2, d / "fail.mp3", bad)
        check("wrong key rejected", False)
    except Exception:
        check("wrong key rejected", True)

    # 6. 空文件往返
    e = d / "empty.gif"
    e.write_bytes(b"")
    de = d / "empty.gif.vdlenc"
    c.encrypt_file(e, de, key, e.name, "image")
    oute = d / "dec_empty.gif"
    nme, kde, exte = c.read_header(de)
    c.decrypt_file(de, oute, key)
    check("empty roundtrip", oute.read_bytes() == b"")
    check("empty header", nme == "empty.gif" and kde == "image" and exte == "gif")

    # 7. read_header 不解密内容却还原原名/类型
    nm3, kd3, ext3 = c.read_header(dst2)
    check("read_header", nm3 == "big.mp3" and kd3 == "audio" and ext3 == "mp3")

    # 8. 损坏文件 read_header 抛错
    corrupt = d / "x.vdlenc"
    corrupt.write_bytes(b"NOTENCR" + os.urandom(10))
    try:
        c.read_header(corrupt)
        check("corrupt rejected", False)
    except Exception:
        check("corrupt rejected", True)

    print("\nALL_PASS crypto unit")


if __name__ == "__main__":
    main()
