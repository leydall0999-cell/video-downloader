#!/usr/bin/env python3
"""生成「视频工坊」自更新清单的 Ed25519 签名密钥对。

背景（安全审计 P0-1）：更新清单 latest.json 此前走未认证通道，sha256 与包同源，
中间人可同时替换 → 供应链 RCE。现在发布侧用私钥对 latest.json 原始字节签名，
客户端用内置公钥验签。

用法：
    python3 tools/gen_update_signing_key.py                 # 默认写到 ~/.vdl_update_signing_key.pem
    python3 tools/gen_update_signing_key.py --out ./key.pem # 指定私钥路径

输出：
    · 私钥 PEM（权限 0600）——**只放在发布机，绝不入库**
    · 公钥 base64 —— 填进 server/routers/system.py 的 UPDATE_MANIFEST_PUBKEY_B64，
      或发布/运行客户端时设置环境变量 VDL_UPDATE_PUBKEY

发布时：
    export VDL_UPDATE_SIGNING_KEY=~/.vdl_update_signing_key.pem
    bash desktop/publish_update.sh
"""
from __future__ import annotations

import argparse
import base64
import os
import stat
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 VDL 更新清单签名密钥对（Ed25519）")
    ap.add_argument("--out", default=str(Path.home() / ".vdl_update_signing_key.pem"),
                    help="私钥输出路径（默认 ~/.vdl_update_signing_key.pem）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的私钥文件")
    args = ap.parse_args()

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except Exception as e:  # noqa: BLE001
        print("需要 cryptography 库：pip install cryptography（%s）" % e, file=sys.stderr)
        return 2

    out = Path(args.out).expanduser()
    if out.exists() and not args.force:
        print("私钥已存在：%s\n如要覆盖请加 --force（覆盖后旧客户端将无法验证新签名）" % out,
              file=sys.stderr)
        return 1

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pem)
    try:
        os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass

    pub_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_b64 = base64.b64encode(pub_raw).decode("ascii")

    print("✅ 已生成 Ed25519 密钥对")
    print("   私钥（仅发布机保存，勿入库）：%s" % out)
    print()
    print("   公钥 base64（填进 server/routers/system.py 的 UPDATE_MANIFEST_PUBKEY_B64，")
    print("   或发布/运行时用环境变量 VDL_UPDATE_PUBKEY 指定）：")
    print()
    print("   %s" % pub_b64)
    print()
    print("   发布前设置：export VDL_UPDATE_SIGNING_KEY=%s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
