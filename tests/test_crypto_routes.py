"""crypto 路由集成测试（TestClient）：status/set-pass/unlock错对/encrypt移回收站+库识别/
decrypt还原/lock清密钥/encfile播放内容一致/禁用 404。

运行：VDL_CRYPTO_ENABLED=1 VDL_VAULT_CONFIG=/tmp/vault_test.json python3 tests/test_crypto_routes.py
"""

import os
import sys
import time
import tempfile
from pathlib import Path

SERVER = str(Path(__file__).resolve().parent.parent / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import app as m  # noqa: E402


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
    global VAULT_KEY  # noqa
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

    # 5. 放一个媒体文件并加密
    f = TMP / "clip.mp4"
    orig = os.urandom(5000)
    f.write_bytes(orig)
    items = client.get("/api/library").json()["items"]
    check("lib has plain item", len(items) == 1 and items[0]["encrypted"] is False)
    lid = items[0]["id"]
    r = client.post("/api/crypto/encrypt", json={"lib_ids": [lid]})
    check("encrypt submit", r.status_code == 200 and "job_id" in r.json())
    jid = r.json()["job_id"]
    time.sleep(1.5)
    j = client.get(f"/api/crypto/job/{jid}").json()
    check("encrypt job completed", j["status"] == "completed" and j["errors"] == [])

    # 6. 原件移回收站，.vdlenc 在，库识别为加密
    check("orig moved to trash", not f.exists())
    check("vdlenc exists", (TMP / "clip.mp4.vdlenc").exists())
    items = client.get("/api/library").json()["items"]
    enc = [i for i in items if i["encrypted"]]
    check("lib shows encrypted", len(enc) == 1 and enc[0]["name"] == "clip.mp4" and enc[0]["kind"] == "video")

    # 7. encfile 播放内容 == 原文（且 Range 支持）
    elid = enc[0]["id"]
    r = client.get(f"/api/library/encfile/{elid}")
    check("encfile 200 + content matches", r.status_code == 200 and r.content == orig)

    # 8. decrypt 还原
    r = client.post("/api/crypto/decrypt", json={"lib_ids": [elid]})
    check("decrypt submit", r.status_code == 200 and "job_id" in r.json())
    jid2 = r.json()["job_id"]
    time.sleep(1.5)
    j2 = client.get(f"/api/crypto/job/{jid2}").json()
    check("decrypt job completed + restored", j2["status"] == "completed" and j2["errors"] == [] and f.exists() and not (TMP / "clip.mp4.vdlenc").exists())

    # 9. lock 后 encfile → 423
    client.post("/api/crypto/lock")
    r = client.get(f"/api/library/encfile/{elid}")
    check("encfile locked 423", r.status_code == 423)

    # 10. 禁用保险箱 → 404
    m.CRYPTO_ENABLED = False
    r = client.get("/api/crypto/status")
    check("disabled 404", r.status_code == 404)
    m.CRYPTO_ENABLED = True

    print("\nALL_PASS crypto routes")


if __name__ == "__main__":
    main()
