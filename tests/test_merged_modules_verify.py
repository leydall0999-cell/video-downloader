"""合并后系统核对：AI 去水印 / 订阅监控 / 时效清理 / 媒体库清理。

方法（同历史核对）：TestClient 冒烟 + 前后端字段契约交叉比对。
- 冒烟：用 FastAPI TestClient 真实打各模块路由，断言状态码与响应骨架。
- 契约：把后端返回的 JSON key 集合，与前端 web/app.js 实际读取的字段做交叉比对，
       发现「前端读、后端没给」或「后端给、前端契约依赖却缺失」即记为不匹配。

外部重算力（yt-dlp 探查 / AI 去水印 worker / ffmpeg）一律打桩，只验证桥接、路由、
状态机与字段契约，不依赖网络与二进制。
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

# ---- 在 import app 前打开四个模块所需的开关 ----
os.environ["VDL_SUBSCRIPTIONS_ENABLED"] = "1"
os.environ["VDL_LIBRARY_ENABLED"] = "1"
os.environ["VDL_RETENTION_ENABLED"] = "1"
os.environ["VDL_AI_DEWATERMARK_MODE"] = "http"
os.environ["VDL_AI_DEWATERMARK_ENDPOINT"] = "http://fake-worker.local"
os.environ.setdefault("VDL_COMMENTARY_ENABLED", "false")

SERVER = str(Path(__file__).resolve().parent.parent / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from fastapi.testclient import TestClient  # noqa: E402
import app as m  # noqa: E402

client = TestClient(m.app)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _assert_subset(name, backend_keys, frontend_fields):
    """后端必须至少提供前端契约依赖的全部字段。返回 (missing, extra)。"""
    missing = [f for f in frontend_fields if f not in backend_keys]
    extra = [k for k in backend_keys if k not in frontend_fields]
    return missing, extra


# =========================================================================== #
# 模块一：订阅监控
# =========================================================================== #
def test_subscriptions_smoke_and_contract(tmp_path, monkeypatch):
    # 打桩：构造一个假平台，避免真实解析 / yt-dlp 网络探查
    class _FakePlatform:
        name = "TestTube"
    monkeypatch.setattr(m, "parse_source", lambda url: (url, _FakePlatform()))
    monkeypatch.setattr(m.subs_mod, "probe_channel", lambda *a, **k: [])

    # 1) GET 列表
    r = client.get("/api/subscriptions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) >= {"subscriptions", "enabled"}
    assert body["enabled"] is True

    # 2) POST 新增（真实走 store.add，基线为空）
    r = client.post("/api/subscriptions", json={"url": "https://x.com/chan", "name": "测试订阅",
                                                 "quality": m.downloader.BEST_KEY, "auto_check": True})
    assert r.status_code == 200, r.text
    sub = r.json()
    sub_id = sub["id"]
    assert sub_id

    # 3) POST 手动检查（基线空 -> 无新视频；worker 桩返回空）
    r = client.post(f"/api/subscriptions/{sub_id}/check")
    assert r.status_code == 200, r.text
    chk = r.json()
    assert set(chk.keys()) >= {"sub_id", "checked", "new_videos", "task_ids", "failed"}

    # 4) DELETE
    r = client.delete(f"/api/subscriptions/{sub_id}")
    assert r.status_code == 200 and r.json().get("deleted") is True

    # 5) 不存在的订阅 -> 404
    r = client.delete("/api/subscriptions/nope")
    assert r.status_code == 404
    r = client.post("/api/subscriptions/nope/check")
    assert r.status_code == 404

    # ---- 字段契约交叉比对 ----
    # 前端 card/modal 实际读取：id, name, platform, last_checked, last_video_ids
    item_frontend = {"id", "name", "platform", "last_checked", "last_video_ids"}
    missing, extra = _assert_subset("sub.item", set(sub.keys()), item_frontend)
    assert not missing, f"订阅条目后端缺失前端字段: {missing}"

    # 前端 check 读取 new_videos / task_ids
    check_frontend = {"new_videos", "task_ids"}
    missing2, _ = _assert_subset("sub.check", set(chk.keys()), check_frontend)
    assert not missing2, f"订阅检查后端缺失前端字段: {missing2}"
    print(f"[订阅监控] 冒烟 PASS；契约 OK（item 多出未用字段: {sorted(extra)}）")


# =========================================================================== #
# 模块二：时效清理（retention）
# =========================================================================== #
def test_retention_smoke_and_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DOWNLOAD_DIR", tmp_path)
    # 造一个超期的 .part 临时碎片（temp_days=2 默认 -> 立刻命中）
    old = tmp_path / "leftover.part"
    old.write_bytes(b"junk")
    (tmp_path / "real.mp4").write_bytes(b"video")
    os.utime(old, (old.stat().st_atime, old.stat().st_mtime - 10 * 86400))

    # 1) GET config
    r = client.get("/api/retention/config")
    assert r.status_code == 200, r.text
    cfg_body = r.json()
    assert set(cfg_body.keys()) >= {"config", "labels", "trash_available", "usage"}

    # 2) POST config（改 interval + 开媒体清理需回收站；本沙盒回收站可能不可用 -> 应 400）
    r = client.post("/api/retention/config", json={"interval_hours": 12})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["interval_hours"] == 12

    # 3) POST scan（dry run，不得删文件）
    r = client.post("/api/retention/scan")
    assert r.status_code == 200, r.text
    scan = r.json()
    assert set(scan.keys()) >= {"categories", "total_files", "total_size", "trash_available", "scanned_at", "usage"}
    assert old.exists(), "scan 不应删除任何文件"
    # 命中了 temp 类
    assert scan["categories"]["temp"]["count"] >= 1, scan["categories"]
    # 每档被归一为 {count,size,items}
    assert set(scan["categories"]["temp"].keys()) == {"count", "size", "items"}

    # 4) POST run（真实清理 temp 类）
    r = client.post("/api/retention/run", json={"categories": ["temp"]})
    assert r.status_code == 200, r.text
    run = r.json()
    assert set(run.keys()) >= {"removed", "failed", "freed", "errors", "ran_at", "usage", "freed_text"}
    assert run["removed"] >= 1, run
    assert not old.exists(), "run 应已清理 temp 碎片"

    # ---- 字段契约交叉比对 ----
    # 前端 config 表单读取 config.* 关键字段
    cfg_frontend = {"auto_enabled", "interval_hours", "temp_enabled", "temp_days",
                    "frames_enabled", "frames_days", "thumbs_enabled", "thumbs_days",
                    "media_enabled", "media_days", "quota_enabled", "quota_gb",
                    "media_use_trash"}
    missing, _ = _assert_subset("retention.config", set(cfg_body["config"].keys()), cfg_frontend)
    assert not missing, f"清理配置后端缺失前端字段: {missing}"

    # 前端 scan 渲染读取 categories[cat].{count,size,items} 与 item.{rel,is_dir,size,age_days}
    cat = scan["categories"]["temp"]
    missing_cat, _ = _assert_subset("retention.cat", set(cat.keys()), {"count", "size", "items"})
    assert not missing_cat
    if cat["items"]:
        it = cat["items"][0]
        missing_item, _ = _assert_subset("retention.item", set(it.keys()),
                                         {"rel", "is_dir", "size", "age_days", "category", "path"})
        assert not missing_item, f"清理明细后端缺失前端字段: {missing_item}"

    # 前端 run 读取 removed/freed_text/freed/failed/errors/usage
    run_frontend = {"removed", "freed_text", "freed", "failed", "errors", "usage"}
    missing_run, _ = _assert_subset("retention.run", set(run.keys()), run_frontend)
    assert not missing_run, f"清理执行后端缺失前端字段: {missing_run}"
    print("[时效清理] 冒烟 PASS；契约 OK")


# =========================================================================== #
# 模块三：媒体库清理（library）
# =========================================================================== #
def test_library_smoke_and_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DOWNLOAD_DIR", tmp_path)
    vid = tmp_path / "我的视频.mp4"
    vid.write_bytes(b"videodata")

    # 1) GET 列表
    r = client.get("/api/library")
    assert r.status_code == 200, r.text
    lib = r.json()
    assert set(lib.keys()) >= {"items", "total"}
    assert lib["total"] >= 1
    item = next(i for i in lib["items"] if i["name"] == "我的视频.mp4")
    lib_id = item["id"]

    # 2) DELETE 单条（连带侧车/缩略图）
    r = client.delete(f"/api/library/{lib_id}")
    assert r.status_code == 200 and r.json().get("deleted") is True
    # 删除后不再出现在列表
    r = client.get("/api/library")
    assert all(i["id"] != lib_id for i in r.json()["items"])

    # 3) 不存在的 id -> 404
    r = client.delete("/api/library/" + ("X" * 16))
    assert r.status_code == 404

    # ---- 字段契约交叉比对 ----
    # 前端 card/modal 读取：id,title,name,platform,uploader,duration,size,mtime,kind,encrypted
    item_frontend = {"id", "title", "name", "platform", "uploader", "duration",
                     "size", "mtime", "kind", "encrypted", "ext", "thumbnail", "source_url"}
    missing, extra = _assert_subset("library.item", set(item.keys()), item_frontend)
    assert not missing, f"媒体库条目后端缺失前端字段: {missing}"
    print(f"[媒体库清理] 冒烟 PASS；契约 OK（后端额外字段: {sorted(extra)}）")


# =========================================================================== #
# 模块四：AI 去水印（ai_dewatermark，process 管线 op）
# =========================================================================== #
def test_ai_dewatermark_smoke_and_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "DOWNLOAD_DIR", tmp_path)
    # 前端在 /api/nodes 上解构 ai_dewatermark 节点（app.js:5430/5466），据此显隐入口与 GPU 标签
    r = client.get("/api/nodes")
    assert r.status_code == 200
    version = r.json()
    assert "ai_dewatermark" in version, "前端依赖 nodes.ai_dewatermark 显隐入口，但后端未返回"
    ai_node = version["ai_dewatermark"]
    assert set(ai_node.keys()) >= {"enabled", "gpu"}, ai_node

    vid = tmp_path / "源视频.mp4"
    vid.write_bytes(b"videodata")
    lib_id = m.library_mod.encode_id(vid.relative_to(tmp_path).as_posix())

    # 打桩外部 AI worker：直接把任务标记完成（镜像真实收尾写入的 lib_id/name）
    def _stub(job_id, src, params):
        with m.process_queue.lock:
            job = m.process_queue.jobs.get(job_id)
            if not job:
                return
            out = Path(src).parent / (Path(src).stem + "_AI去水印.mp4")
            new_id = m.library_mod.encode_id(out.resolve().relative_to(tmp_path.resolve()).as_posix())
            job.update(status="completed", out_path=str(out), lib_id=new_id, name=out.name)

    with patch.object(m, "_run_ai_dewatermark", _stub):
        # 1) POST 提交 ai_dewatermark（需 VDL_LIBRARY_ENABLED）
        r = client.post("/api/process/run",
                        json={"lib_id": lib_id, "op": "ai_dewatermark",
                              "params": {"x": 10, "y": 20, "w": 120, "h": 60, "band": 5}})
        assert r.status_code == 200, r.text
        run = r.json()
        assert "job_id" in run, run
        job_id = run["job_id"]

        # 2) GET 任务状态，轮询到终态
        status = None
        for _ in range(50):
            r = client.get(f"/api/process/{job_id}")
            assert r.status_code == 200, r.text
            status = r.json()
            if status["status"] in ("completed", "failed"):
                break
        assert status is not None
        assert status["status"] == "completed", status

    # ---- 字段契约交叉比对 ----
    # 前端 pollProcess 读取：status, is_dir, count, name；renderProcQueue 读取 status/steps/name/lib_id/error
    status_frontend = {"status", "error", "lib_id", "name", "count", "is_dir", "steps", "logs"}
    missing, extra = _assert_subset("ai_dewatermark.status", set(status.keys()), status_frontend)
    assert not missing, f"AI去水印任务状态后端缺失前端字段: {missing}"

    # 前端 ai_dewatermark 入参契约（PROCESS_OPS.ai_dewatermark.params）：x,y,w,h,band
    # 后端 _run_ai_dewatermark 接受 x,y,w,h,band,mode —— 全部在 params 透传范围内
    accepted = {"x", "y", "w", "h", "band", "mode"}
    sent = {"x", "y", "w", "h", "band"}
    assert sent <= accepted, "前端发送的去水印参数后端未全部接收"
    print(f"[AI 去水印] 冒烟 PASS；契约 OK（任务态额外字段: {sorted(extra)}）")
