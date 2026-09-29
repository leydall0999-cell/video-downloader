#!/usr/bin/env python3
"""免重传重转（/api/convert/reconvert）回归测试。

背景（2026-09-29 用户两连问）
-----------------------------
1. 「重新编辑」最初复用旧 _uploadId 直接触发转码 → 必报「分片不完整（0/1）」，
   因为 /api/upload-chunk/finish 合并时逐片 p.unlink()，分片是一次性资源；
2. 改成强制重传后用户问「为什么要重新上传，能不能不重新上传」。

修复后的契约（本测试钉住）：
  1. finish 提交转码用 src_is_temp=False：**源文件（合并产物）在转码后保留**，
     job["src_path"] 登记其路径；
  2. POST /api/convert/reconvert 用旧 job_id + 新 target 可直接重转，不要求重新
     上传分片；新 job 的 src_path 指向同一份源文件（可链式重转），转码后源仍保留；
  3. 设备隔离：非创建者设备重转 → 404；
  4. 源文件被清理（2h TTL，app._cleanup_merged_upload_sources）后重转 → 410，
     前端据此回退为重新上传；
  5. TTL 清理只删合并源文件（up_<hex>.<ext>），不碰分片（up_*.pNNNN 归
     _cleanup_orphan_upload_parts 管）。

全程离线：限流/配额打桩放行，executor 换同步执行器，_run_convert 换假转码
（只登记产物文件、按 src_is_temp 决定是否删源），不打 ffmpeg、不碰网络。
运行：cd server && python tests/test_reconvert_source_reuse.py
"""
import os
import pathlib
import sys
import time

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 隔离数据目录：app 导入会建目录/写文件，绝不能落到真实用户目录
os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_reconvert")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---- 打桩：限流/配额放行；同步执行器；假转码（不跑 ffmpeg）----
server_app._check_rate_limit = lambda request: None
server_app._check_convert_quota = lambda request: (True, 0, 5)

# 2026-09-29 服务端功能门禁：本测试聚焦重转语义，打桩放行登录门禁
# （门禁契约由 test_feature_auth_gate.py 专测）。routers 与这里 import 的是同一模块对象。
import user_membership  # noqa: E402
user_membership.require_login_user = lambda request: "reconvert-test-user"


class _SyncExecutor:
    """同步执行器：submit 即运行，测试无需等待线程池。"""

    def submit(self, fn, *a, **k):
        fn(*a, **k)


server_app.executor = _SyncExecutor()


def _fake_run_convert(job_id, src, target, resolution, bitrate="", audio=True,
                      rotate=0, remux=False, src_is_temp=False,
                      audio_bitrate="", image_quality=0, resize=0,
                      flatten_alpha=True, is_image=False):
    """假转码：写产物 + 标记完成；src_is_temp=True 时删源（与真实契约一致）。"""
    job = server_app.CONVERT_JOBS.get(job_id)
    if not job:
        return
    out = pathlib.Path(job["out_path"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"fake-converted")
    job["status"] = "completed"
    job["progress"] = 100
    if src_is_temp:
        pathlib.Path(src).unlink(missing_ok=True)


server_app._run_convert = _fake_run_convert

DEV_A = {"X-Device-Id": "dev-test-a"}
DEV_B = {"X-Device-Id": "dev-test-b"}


def _upload_and_finish(target="flac", filename="song.mp3", device=DEV_A):
    """走真实分片上传 + finish 链路建一个已完成 job，返回 job_id。"""
    c = TestClient(server_app.app)
    up_id = "reconverttest01"
    r = c.post("/api/upload-chunk",
               data={"upload_id": up_id, "index": "0", "total": "1"},
               files={"file": ("part1", b"A" * 2048)}, headers=device)
    assert r.status_code == 200, f"分片上传非 200: {r.status_code} {r.text[:200]}"
    r = c.post("/api/upload-chunk/finish",
               data={"upload_id": up_id, "total": "1", "filename": filename,
                     "target": target, "audio": "true"},
               headers=device)
    assert r.status_code == 200, f"finish 非 200: {r.status_code} {r.text[:200]}"
    return r.json()["job_id"]


def test_finish_keeps_source():
    """finish 后源文件必须保留且登记 src_path（免重传的前提）。"""
    job_id = _upload_and_finish()
    job = server_app.CONVERT_JOBS[job_id]
    assert job.get("src_path"), f"job 未登记 src_path: {sorted(job)}"
    src = pathlib.Path(job["src_path"])
    assert src.exists(), "finish(src_is_temp=False) 后源文件应保留"
    # 分片已被消化（一次性资源）
    parts = list(pathlib.Path(str(server_app.UPLOAD_TMP)).glob(f"up_reconverttest01.p*"))
    assert not parts, f"分片应已被 finish 合并消化: {parts}"
    print("✅ finish 后源文件保留且登记 src_path，分片已消化")
    return job_id, src


def test_reconvert_reuses_source(job_id, src):
    """同设备免重传重转：新 job 复用同一源文件，转码后源仍在（可链式重转）。"""
    c = TestClient(server_app.app)
    r = c.post("/api/convert/reconvert",
               data={"job_id": job_id, "target": "wav", "audio_bitrate": ""},
               headers=DEV_A)
    assert r.status_code == 200, f"reconvert 非 200: {r.status_code} {r.text[:200]}"
    job2 = r.json()["job_id"]
    assert server_app.CONVERT_JOBS[job2]["src_path"] == str(src), "新 job 应指向同一源文件"
    assert server_app.CONVERT_JOBS[job2]["status"] == "completed"
    assert src.exists(), "重转后源文件仍应保留（链式重转）"
    print("✅ reconvert 复用源文件重转成功，源保留可链式重转")


def test_reconvert_device_isolation(job_id):
    """设备隔离：非创建者设备重转 → 404。"""
    c = TestClient(server_app.app)
    r = c.post("/api/convert/reconvert",
               data={"job_id": job_id, "target": "wav"}, headers=DEV_B)
    assert r.status_code == 404, f"跨设备重转应 404: {r.status_code} {r.text[:200]}"
    print("✅ reconvert 设备隔离生效（跨设备 404）")


def test_reconvert_after_source_gone(job_id, src):
    """源文件被 TTL 清理后重转 → 410（前端回退重传的信号）。"""
    c = TestClient(server_app.app)
    src.unlink(missing_ok=True)
    r = c.post("/api/convert/reconvert",
               data={"job_id": job_id, "target": "wav"}, headers=DEV_A)
    assert r.status_code == 410, f"源缺失应 410: {r.status_code} {r.text[:200]}"
    print("✅ 源文件清理后 reconvert 返回 410（前端回退重传）")


def test_cleanup_ttl_spares_parts():
    """TTL 清理只删过期合并源文件，不误删分片（分片归孤儿清理管）。"""
    old_src = server_app.UPLOAD_TMP / "up_oldsource01.mp4"
    old_part = server_app.UPLOAD_TMP / "up_orphan01.p3"
    old_src.write_bytes(b"x")
    old_part.write_bytes(b"x")
    stale = time.time() - 3 * 3600
    os.utime(old_src, (stale, stale))
    os.utime(old_part, (stale, stale))
    server_app._cleanup_merged_upload_sources(max_age=2 * 3600)
    assert not old_src.exists(), "过期合并源文件应被 TTL 清理"
    assert old_part.exists(), "分片不能被 _cleanup_merged_upload_sources 误删"
    old_part.unlink()
    print("✅ TTL 清理只删合并源文件，分片不误删")


if __name__ == "__main__":
    jid, src = test_finish_keeps_source()
    test_reconvert_reuses_source(jid, src)
    test_reconvert_device_isolation(jid)
    test_reconvert_after_source_gone(jid, src)
    test_cleanup_ttl_spares_parts()
    print("\n🎉 免重传重转回归测试全部通过 — finish 留源 / reconvert 复用 / 隔离 / 410 回退 / TTL 清理")
