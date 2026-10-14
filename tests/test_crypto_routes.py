"""crypto 路由集成测试（TestClient，web 适用节）：status/set-pass/unlock 错对/禁用 404。

安全审计 2026-10-14（F-13/I2 收尾）裁剪：原 5-9 节（encrypt/encfile/decrypt/lock）
深度耦合 /api/library —— **library 是桌面专属，web 有意不挂载**（app.py 剥离注释），
在 web 永远无法运行，故裁掉；crypto 逻辑本体由 test_crypto_unit.py 覆盖，
本文件保住 web 的 crypto 路由级回归（status/set-pass/unlock/开关）。

自给自足：import 后直接置 m.CRYPTO_ENABLED=True，无需外部环境变量。
运行：python3 tests/test_crypto_routes.py
"""

import os
import sys
import tempfile
from pathlib import Path

SERVER = str(Path(__file__).resolve().parent.parent / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import app as m  # noqa: E402

m.CRYPTO_ENABLED = True  # 与 torrents 测试同款：动态开关直接置位，无需 VDL_CRYPTO_ENABLED


def check(name, cond):
    print(("PASS" if cond else "FAIL") + " " + name)
    if not cond:
        raise SystemExit(1)


def setup_module():
    TMP = Path(tempfile.mkdtemp())
    m.DOWNLOAD_DIR = TMP
    m.VAULT_TMP = TMP / ".vault_tmp"
    m.VAULT_TMP.mkdir(parents=True, exist_ok=True)
    m.VAULT_PATH = Path(tempfile.mktemp(suffix=".json"))
    if m.VAULT_PATH.exists():
        m.VAULT_PATH.unlink()
    # 清空内存密钥
    m.VAULT_KEY = None
    return TMP


def main():
    TMP = setup_module()
    from fastapi.testclient import TestClient
    client = TestClient(m.app)

    # 1. 初始 status
    r = client.get("/api/crypto/status")
    check("status init", r.status_code == 200 and r.json()["has_pass"] is False and r.json()["locked"] is True)

    # 2. set-pass 不一致 → 400
    r = client.post("/api/crypto/set-pass", json={"passwd": "pw123", "confirm": "xx"})
    check("set-pass mismatch 400", r.status_code == 400)

    # 3. set-pass 成功 → 自动解锁
    r = client.post("/api/crypto/set-pass", json={"passwd": "pw123", "confirm": "pw123"})
    check("set-pass ok auto-unlock", r.status_code == 200 and r.json()["locked"] is False)
    r = client.get("/api/crypto/status")
    check("status after set", r.json()["has_pass"] is True and r.json()["locked"] is False)

    # 4. unlock 错密码 → 401
    r = client.post("/api/crypto/unlock", json={"passwd": "bad"})
    check("unlock wrong 401", r.status_code == 401)

    # 5. 禁用保险箱 → 404（web 剥离守卫同款动态开关）
    m.CRYPTO_ENABLED = False
    r = client.get("/api/crypto/status")
    check("disabled 404", r.status_code == 404)
    m.CRYPTO_ENABLED = True

    print("\nALL_PASS crypto routes (web 适用节)")


if __name__ == "__main__":
    main()
