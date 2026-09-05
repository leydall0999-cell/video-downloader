"""下载模块会员配额 E2E（2026-09-06 语义：解析放开，墙在「点清晰度下载」）。

隔离 HOME 下 import 整个 app（TestClient）：
  1. 解析不再设配额墙 —— 连续多次解析均 200，不受 10 次限制
  2. 免费下载 10 次/日成功计数 → 第 11 次 402 + MEMBER_QUOTA| 前缀（引导开会员）
  3. 下载任务创建失败不烧配额（无效清晰度 400 / 非法链接异常）
  4. 免费满额后激活下载会员 → 恢复下载且档位 member（limit 1000）

运行（独立进程，HOME 隔离）：
    cd server && python tests/test_member_quota_e2e.py
    .build_venv/bin/python -m pytest tests/test_member_quota_e2e.py -v
"""
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="vdl_dl_quota_e2e_")
os.environ["HOME"] = _TMP  # 必须在 import app 之前，让会员状态文件落在隔离目录
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server_app.app, raise_server_exceptions=False)

_FAKE_INFO = {
    "id": "f1", "title": "demo", "uploader": "u", "duration": 10,
    "webpage_url": "https://www.bilibili.com/video/BV1xx411c7mD", "extractor_key": "BiliBili",
    "formats": [{"height": 1080, "vcodec": "avc1", "ext": "mp4", "filesize": 1000},
                {"height": 720, "vcodec": "avc1", "ext": "mp4", "filesize": 800},
                {"height": None, "vcodec": "none", "ext": "m4a", "acodec": "mp4a", "filesize": 50}],
    "url": "https://example.com/v.mp4", "is_live": False, "thumbnail": "",
}
_URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def _setup():
    """mock probe（解析）、scheduler.submit（下载不真跑）与限流（避免 429 干扰配额断言）。"""
    server_app.downloader.probe = types.MethodType(
        lambda self, url, cookie="", proxy="": dict(_FAKE_INFO), server_app.downloader)
    server_app.scheduler.submit = lambda *a, **k: None
    server_app._check_rate_limit = lambda request: None


def _reset_state():
    p = os.path.join(_TMP, ".video-downloader", "membership.json")
    if os.path.exists(p):
        os.remove(p)
    server_app.member_store._loaded = False


def test_resolve_no_quota_wall():
    """解析放开：连续 12 次全部成功（不再被 10 次/日限制打断）。"""
    _reset_state()
    _setup()
    for i in range(12):
        r = client.post("/api/resolve", json={"url": _URL})
        assert r.status_code == 200, f"第 {i+1} 次解析应 200，实际 {r.status_code}: {r.text[:120]}"
    used = server_app.member_store.status()["daily_usage"].get("download", 0)
    assert used == 0, "解析不应消耗下载配额"
    print("✅ 解析放开：连续 12 次成功且不消耗下载配额")


def test_download_free_10_then_402():
    _reset_state()
    _setup()
    body = {"url": _URL, "quality": "1080", "title": "demo", "cookie": "", "proxy": ""}
    for i in range(10):
        r = client.post("/api/download", json=body)
        assert r.status_code == 200, f"第 {i+1} 次下载应 200，实际 {r.status_code}: {r.text[:200]}"
        q = r.json().get("quota") or {}
        assert q.get("free_used") == i + 1, f"free_used 应 {i+1}，实际 {q}"
    # 第 11 次 → 402 + MEMBER_QUOTA| 前缀 + 引导文案
    r = client.post("/api/download", json=body)
    assert r.status_code == 402, f"第 11 次应 402，实际 {r.status_code}"
    detail = r.json().get("detail", "")
    assert detail.startswith("MEMBER_QUOTA|"), detail
    assert "开通下载会员可解锁 1000 次/日" in detail, detail
    print("✅ 免费下载 10 次成功计数；第 11 次 402 + MEMBER_QUOTA| 引导开会员")


def test_download_failure_does_not_consume():
    _reset_state()
    _setup()
    # 无效清晰度：gate 通过后 400，不烧配额
    r = client.post("/api/download", json={"url": _URL, "quality": "99999", "title": "", "cookie": "", "proxy": ""})
    assert r.status_code == 400, r.status_code
    used = server_app.member_store.status()["daily_usage"].get("download", 0)
    assert used == 0, f"无效清晰度不应烧配额，实际 used={used}"
    # 非法链接：parse_source 抛错（500），不烧配额
    r = client.post("/api/download", json={"url": "not a url", "quality": "1080", "title": "", "cookie": "", "proxy": ""})
    assert r.status_code in (400, 500)
    used = server_app.member_store.status()["daily_usage"].get("download", 0)
    assert used == 0, f"非法链接不应烧配额，实际 used={used}"
    # 正常下载恢复后照常计费
    r = client.post("/api/download", json={"url": _URL, "quality": "1080", "title": "", "cookie": "", "proxy": ""})
    assert r.status_code == 200
    assert server_app.member_store.status()["daily_usage"].get("download", 0) == 1
    print("✅ 下载失败（无效清晰度/非法链接）不烧配额；恢复后照常计费")


def test_activation_unblocks_download():
    _reset_state()
    _setup()
    body = {"url": _URL, "quality": "1080", "title": "", "cookie": "", "proxy": ""}
    for _ in range(10):
        assert client.post("/api/download", json=body).status_code == 200
    assert client.post("/api/download", json=body).status_code == 402
    # 激活下载会员
    r = server_app.member_store.activate("download_month")
    assert r["ok"]
    q = server_app.member_store.quota_state("download")
    assert q["tier"] == "member" and q["limit"] == 1000
    r = client.post("/api/download", json=body)
    assert r.status_code == 200, f"激活后应恢复下载：{r.text[:200]}"
    print("✅ 免费满 10 → 激活下载会员 → 恢复下载，member 档 1000/日")


if __name__ == "__main__":
    test_resolve_no_quota_wall()
    test_download_free_10_then_402()
    test_download_failure_does_not_consume()
    test_activation_unblocks_download()
    print("\n🎉 下载模块配额 E2E 全部通过（4 项）")
    shutil.rmtree(_TMP, ignore_errors=True)
