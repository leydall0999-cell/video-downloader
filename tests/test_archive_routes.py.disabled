"""归档网盘 · 路由集成测试（pytest 版，避免 import 期执行全套请求）。

覆盖：
- /api/nodes 开关
- /api/archive/config 读取（脱敏，无明文）/ 写入（私网 WebDAV 放行、非法 scheme 拒绝、删本地回收站不可用时拒绝）
- /api/archive/scan（只算不传、类型筛选、去重）
- /api/archive/run（执行 + 记录 + 重复跳过）/ status / cancel / forget
- 关闭归档时各路由返回 404

注意：本文件不依赖 import 期环境变量——归档路由在 app.py 中始终挂载，是否可用由运行时
ARCHIVE_ENABLED 全局变量（_require_archive）判定，因此通过 fixture 在运行时开启归档即可，
不受其他测试先导入 app 的影响，也不污染全局环境变量 / 不启动归档 watchdog 线程。
所有请求都在测试函数内（而非 import 期）执行，避免 pytest 收集阶段副作用与全局状态污染。
"""
import os
import sys
import time
import json
import tempfile
from pathlib import Path

SERVER = str(Path(__file__).resolve().parent.parent / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import pytest

import app as m
import archive as archive_mod
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def env():
    """搭建临时归档环境，并在测试结束后还原被改写的全局状态。"""
    TMP = Path(tempfile.mkdtemp(prefix="arc_test_"))
    old_download = m.DOWNLOAD_DIR
    old_enabled = m.ARCHIVE_ENABLED
    old_store = getattr(m, "archive_store", None)
    old_upload = m._webdav_provider.upload

    m.DOWNLOAD_DIR = TMP
    m.archive_store = archive_mod.ArchiveStore(TMP / "archive.json")
    m.ARCHIVE_ENABLED = True
    client = TestClient(m.app)

    yield {"client": client, "tmp": TMP}

    m.DOWNLOAD_DIR = old_download
    m.ARCHIVE_ENABLED = old_enabled
    m.archive_store = old_store
    m._webdav_provider.upload = old_upload


def _mk_file(tmp, name, size=2048, age_days=5.0, meta=None):
    p = tmp / name
    p.write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    if meta:
        (tmp / (name.rsplit(".", 1)[0] + ".vdlmeta.json")).write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8"
        )


def test_archive_routes(env):
    client = env["client"]
    tmp = env["tmp"]

    # 1. /api/nodes 开关
    r = client.get("/api/nodes")
    assert r.status_code == 200 and r.json()["archive"]["enabled"] is True

    # 2. 关闭归档时路由 404
    old = m.ARCHIVE_ENABLED
    m.ARCHIVE_ENABLED = False
    try:
        assert client.get("/api/archive/config").status_code == 404
        assert client.post("/api/archive/scan").status_code == 404
    finally:
        m.ARCHIVE_ENABLED = old

    # 3. 读取配置（脱敏，无明文）
    r = client.get("/api/archive/config")
    j = r.json()
    assert r.status_code == 200 and "config" in j
    assert "tokens" in j and "default_template" in j
    assert "pass" not in j["creds"].get("webdav", {})

    # 4. 写入 WebDAV 凭据（私网地址应放行）
    body = {"webdav": {"url": "http://192.168.1.50:5006/dav", "user": "nas", "pass": "S3cretPass23"}}
    r = client.post("/api/archive/config", json=body)
    assert r.status_code == 200
    assert r.json().get("configured") is True
    wd = r.json()["creds"]["webdav"]
    assert "pass" not in wd and wd.get("pass_set") is True
    assert wd.get("pass_masked", "").startswith("S3") and "****" in wd["pass_masked"]

    # 5. 留空沿用旧密码
    r = client.post("/api/archive/config", json={"webdav": {"url": "http://192.168.1.50:5006/dav", "user": "nas", "pass": ""}})
    assert r.json()["creds"]["webdav"].get("pass_set") is True

    # 6. 非法 scheme 被拒
    r = client.post("/api/archive/config", json={"webdav": {"url": "ftp://evil/x", "user": "u", "pass": "p"}})
    assert r.status_code == 400

    # 7. 回收站不可用时拒绝开删本地
    real_trash = m.retention_mod.trash_available
    m.retention_mod.trash_available = lambda: False
    try:
        r = client.post("/api/archive/config", json={"delete_after": True})
        assert r.status_code == 400
    finally:
        m.retention_mod.trash_available = real_trash

    # 8. scan：只算不传
    _mk_file(tmp, "a.mp4", meta={"title": "电影", "platform": "youtube", "uploader": "张三"})
    _mk_file(tmp, "b.mp3", meta={"title": "歌", "platform": "bilibili", "uploader": "李四"})
    _mk_file(tmp, "c.jpg")
    r = client.post("/api/archive/scan")
    j = r.json()
    assert r.status_code == 200
    assert "c.jpg" not in [i["name"] for i in j["items"]]
    assert {"a.mp4", "b.mp3"} <= {i["name"] for i in j["items"]}
    assert all(i.get("dest") for i in j["items"])
    assert j.get("configured") is True

    # 9. run：执行 + 记录 + 去重
    uploaded = []

    def fake_upload(path, dest, creds, progress=None):
        uploaded.append(str(path))
        if progress:
            progress(10, 100)
            progress(100, 100)
        return f"/remote/{dest}"

    m._webdav_provider.upload = fake_upload
    r = client.post("/api/archive/run", json={})
    assert r.status_code == 200
    job_id = r.json().get("job_id")
    total = r.json().get("total", 0)
    assert bool(job_id) and total > 0

    final = None
    for _ in range(60):
        s = client.get(f"/api/archive/status/{job_id}").json()
        if s.get("status") in ("completed", "canceled", "failed"):
            final = s
            break
        time.sleep(0.1)
    assert final is not None
    assert final["uploaded"] > 0
    assert len(uploaded) == final["uploaded"]

    r2 = client.post("/api/archive/scan")
    assert r2.json()["count"] == 0

    # 10. forget 后重新可归档
    r = client.post("/api/archive/forget", json={"rel": ""})
    assert r.json().get("cleared", 0) > 0
    r3 = client.post("/api/archive/scan")
    assert r3.json()["count"] > 0

    # 11. cancel：取消进行中的任务
    slow_uploads = []

    def slow_upload(path, dest, creds, progress=None):
        slow_uploads.append(str(path))
        time.sleep(0.4)
        return f"/remote/{dest}"

    m._webdav_provider.upload = slow_upload
    r = client.post("/api/archive/run", json={})
    jid2 = r.json()["job_id"]
    time.sleep(0.05)
    c = client.post(f"/api/archive/cancel/{jid2}")
    assert c.status_code == 200 and c.json().get("canceling") is True
    final2 = None
    for _ in range(80):
        s = client.get(f"/api/archive/status/{jid2}").json()
        if s.get("status") in ("completed", "canceled", "failed"):
            final2 = s
            break
        time.sleep(0.1)
    assert final2 is not None and final2["status"] == "canceled"
    assert final2["uploaded"] < final2.get("total", 0)

    # 12. 无待归档时 run -> 409
    m.archive_store.update(include_video=False, include_audio=False, include_image=False)
    r = client.post("/api/archive/run", json={})
    assert r.status_code == 409
