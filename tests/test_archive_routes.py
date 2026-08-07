"""归档网盘 · 路由集成测试（TestClient）。

覆盖：
- /api/nodes 开关
- /api/archive/config 读取（脱敏，无明文）/ 写入（私网 WebDAV 放行、非法 scheme 拒绝、删本地回收站不可用时拒绝）
- /api/archive/scan（只算不传、类型筛选、去重）
- /api/archive/run（执行 + 记录 + 重复跳过）/ status / cancel / forget
- 关闭归档时各路由返回 404
"""
import os, sys, time, pathlib, json, tempfile
from pathlib import Path

# 必须在 import app 之前定好环境
os.environ["VDL_ARCHIVE_ENABLED"] = "1"

SERVER = str(pathlib.Path(__file__).resolve().parent.parent / 'server')
sys.path.insert(0, SERVER)

import app as m
import archive as archive_mod

ok = []
def check(label, cond):
    ok.append(bool(cond))
    print(f'  {"PASS" if cond else "FAIL"}  {label}')

# 临时下载目录 + 临时归档配置，避免污染本机
TMP = Path(tempfile.mkdtemp(prefix="arc_test_"))
m.DOWNLOAD_DIR = TMP
CFG = TMP / "archive.json"
m.archive_store = archive_mod.ArchiveStore(CFG)

from fastapi.testclient import TestClient
client = TestClient(m.app)

def mk_file(name, size=2048, age_days=5.0, meta=None):
    p = TMP / name
    p.write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(p, (t, t))
    if meta:
        (TMP / (name.rsplit(".", 1)[0] + ".vdlmeta.json")).write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")

print("=== 1. /api/nodes 开关 ===")
r = client.get("/api/nodes")
check("nodes.archive.enabled = True", r.status_code == 200 and r.json()["archive"]["enabled"] is True)

print("=== 2. 关闭归档时路由 404 ===")
old = m.ARCHIVE_ENABLED
m.ARCHIVE_ENABLED = False
check("GET config -> 404", client.get("/api/archive/config").status_code == 404)
check("POST scan -> 404", client.post("/api/archive/scan").status_code == 404)
m.ARCHIVE_ENABLED = old

print("=== 3. 读取配置（脱敏，无明文） ===")
r = client.get("/api/archive/config")
j = r.json()
check("200 + 有 config", r.status_code == 200 and "config" in j)
check("返回 tokens / default_template", "tokens" in j and "default_template" in j)
check("creds 不含明文 pass 键", "pass" not in j["creds"].get("webdav", {}))

print("=== 4. 写入 WebDAV 凭据（私网地址应放行） ===")
body = {"webdav": {"url": "http://192.168.1.50:5006/dav", "user": "nas", "pass": "S3cretPass23"}}
r = client.post("/api/archive/config", json=body)
check("私网 WebDAV 写入 200", r.status_code == 200)
check("configured = True", r.json().get("configured") is True)
wd = r.json()["creds"]["webdav"]
check("pass 不回显明文", "pass" not in wd and wd.get("pass_set") is True)
check("pass 被脱敏", wd.get("pass_masked", "").startswith("S3") and "****" in wd["pass_masked"])

print("=== 5. 留空沿用旧密码 ===")
r = client.post("/api/archive/config", json={"webdav": {"url": "http://192.168.1.50:5006/dav", "user": "nas", "pass": ""}})
check("留空后仍 pass_set", r.json()["creds"]["webdav"].get("pass_set") is True)

print("=== 6. 非法 scheme 被拒 ===")
r = client.post("/api/archive/config", json={"webdav": {"url": "ftp://evil/x", "user": "u", "pass": "p"}})
check("ftp scheme -> 400", r.status_code == 400)

print("=== 7. 回收站不可用时拒绝开删本地 ===")
real_trash = m.retention_mod.trash_available
m.retention_mod.trash_available = lambda: False
r = client.post("/api/archive/config", json={"delete_after": True})
check("delete_after + 无回收站 -> 400", r.status_code == 400)
m.retention_mod.trash_available = real_trash

print("=== 8. scan：只算不传 ===")
mk_file("a.mp4", meta={"title": "电影", "platform": "youtube", "uploader": "张三"})
mk_file("b.mp3", meta={"title": "歌", "platform": "bilibili", "uploader": "李四"})
mk_file("c.jpg")          # image 默认不传
r = client.post("/api/archive/scan")
j = r.json()
check("scan 200", r.status_code == 200)
check("不含图片", "c.jpg" not in [i["name"] for i in j["items"]])
check("video+audio 入选", {"a.mp4", "b.mp3"} <= {i["name"] for i in j["items"]})
check("带目标路径 dest", all(i.get("dest") for i in j["items"]))
check("configured=True", j.get("configured") is True)

print("=== 9. run：执行 + 记录 + 去重 ===")
uploaded = []
def fake_upload(path, dest, creds, progress=None):
    uploaded.append(str(path))
    if progress:
        progress(10, 100); progress(100, 100)
    return f"/remote/{dest}"
m._webdav_provider.upload = fake_upload
r = client.post("/api/archive/run", json={})
check("run 200", r.status_code == 200)
job_id = r.json().get("job_id")
total = r.json().get("total", 0)
check("有 job_id 且 total>0", bool(job_id) and total > 0)

# 轮询状态
final = None
for _ in range(60):
    s = client.get(f"/api/archive/status/{job_id}").json()
    if s.get("status") in ("completed", "canceled", "failed"):
        final = s; break
    time.sleep(0.1)
check("任务结束", final is not None)
check("上传成功计数>0", final["uploaded"] > 0)
check("uploader 被调用", len(uploaded) == final["uploaded"])

# 去重：再 scan 应空
r2 = client.post("/api/archive/scan")
check("已归档后 scan 为空（去重）", r2.json()["count"] == 0)

print("=== 10. forget 后重新可归档 ===")
r = client.post("/api/archive/forget", json={"rel": ""})
check("forget 清空记录", r.json().get("cleared", 0) > 0)
r3 = client.post("/api/archive/scan")
check("forget 后 scan 又有文件", r3.json()["count"] > 0)

print("=== 11. cancel：取消进行中的任务 ===")
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
check("cancel 返回 canceling", c.status_code == 200 and c.json().get("canceling") is True)
final2 = None
for _ in range(80):
    s = client.get(f"/api/archive/status/{jid2}").json()
    if s.get("status") in ("completed", "canceled", "failed"):
        final2 = s; break
    time.sleep(0.1)
check("任务被取消", final2 is not None and final2["status"] == "canceled")
check("取消后并非全部上传", final2["uploaded"] < final2.get("total", 0))

print("=== 12. 无待归档时 run -> 409 ===")
m.archive_store.update(include_video=False, include_audio=False, include_image=False)
r = client.post("/api/archive/run", json={})
check("无待归档 run -> 409", r.status_code == 409)

print("\n" + ("ALL_PASS" if all(ok) else f"SOME_FAIL ({ok.count(False)} 项失败)"))
sys.exit(0 if all(ok) else 1)
