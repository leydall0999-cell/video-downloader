"""torrent 路由集成测试（TestClient）：用 FakeLT 注入 torrent_mod.lt 覆盖管理逻辑。

运行：python3 tests/test_torrent_routes.py
（无需真实 libtorrent；真实内核的安装见 DESKTOP_FEATURES.md §2.10）
"""
import sys
import tempfile
from pathlib import Path

SERVER = str(Path(__file__).resolve().parent.parent / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

import torrent as torrent_mod
import fake_libtorrent
from fastapi.testclient import TestClient
import app as m


def check(name, cond):
    print(("PASS" if cond else "FAIL") + " " + name)
    if not cond:
        raise SystemExit(1)


# 注入 mock libtorrent，开启开关，准备隔离目录
torrent_mod.lt = fake_libtorrent.FakeLT()
m.TORRENT_ENABLED = True
TMP = Path(tempfile.mkdtemp())
m.torrent_manager = torrent_mod.TorrentManager(TMP)

client = TestClient(m.app)


def teardown():
    try:
        m.torrent_manager.stop()
    except Exception:
        pass


# 1) 添加 magnet
r = client.post("/api/torrents/add", json={"uri": "magnet:?xt=urn:btih:0123456789abcdef&dn=test"})
check("add 200", r.status_code == 200)
body = r.json()
tid = body.get("id")
check("add 返回 id", bool(tid))
check("add 名称非空", bool(body.get("name")))
check("add 初始状态 downloading", body.get("state") == "downloading")
check("add 含文件列表", isinstance(body.get("files"), list) and len(body["files"]) >= 1)

# 2) 列表包含
r = client.get("/api/torrents")
check("list 200", r.status_code == 200)
check("list 含该种子", any(t["id"] == tid for t in r.json()["items"]))

# 3) 暂停
r = client.post(f"/api/torrents/{tid}/pause")
check("pause 200", r.status_code == 200)
d = client.get(f"/api/torrents/{tid}").json()
check("pause 后 paused=True", d["paused"] is True)

# 4) 继续
r = client.post(f"/api/torrents/{tid}/resume")
check("resume 200", r.status_code == 200)
d = client.get(f"/api/torrents/{tid}").json()
check("resume 后 paused=False", d["paused"] is False)

# 5) 文件优先级（跳过第 0 个文件）
r = client.post(f"/api/torrents/{tid}/files", json={"priorities": {0: 0}})
check("files 200", r.status_code == 200)
d = client.get(f"/api/torrents/{tid}").json()
check("files[0] 被跳过", d["files"][0]["skipped"] is True)

# 6) 非法 uri 被拒
r = client.post("/api/torrents/add", json={"uri": "ftp://x/t.torrent"})
check("非法 uri 400", r.status_code == 400)

# 7) 移除
r = client.post(f"/api/torrents/{tid}/remove", json={"delete_files": False})
check("remove 200", r.status_code == 200)
check("remove 后 detail 404", client.get(f"/api/torrents/{tid}").status_code == 404)
check("remove 后列表为空", len(client.get("/api/torrents").json()["items"]) == 0)

# 8) 禁用时 404
m.TORRENT_ENABLED = False
torrent_mod.lt = None
check("禁用时 list 404", client.get("/api/torrents").status_code == 404)
# 恢复
torrent_mod.lt = fake_libtorrent.FakeLT()
m.TORRENT_ENABLED = True

# 9) /api/nodes 暴露 torrent
r = client.get("/api/nodes")
check("nodes 200", r.status_code == 200)
tor = r.json().get("torrent", {})
check("nodes.torrent.enabled", tor.get("enabled") is True)
check("nodes.torrent.available", tor.get("available") is True)

teardown()
print("\nALL TORRENT ROUTE TESTS PASSED")
