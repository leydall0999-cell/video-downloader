"""下载模块会员配额 E2E（resolve：免费 10/日 → 会员 1000/日）。

在隔离 HOME 下 import 整个 app（TestClient），mock downloader.probe 避免真网络。
覆盖：
  1. 免费解析成功逐次计数，第 11 次返回 402 + MEMBER_QUOTA| 前缀
  2. 解析失败/异常不消耗配额（成功后计费的语义保证）
  3. 免费满额后激活下载会员 → 恢复解析且档位升 member（limit 1000）

运行（独立进程，HOME 隔离）：
    cd server && python tests/test_member_quota_e2e.py
    .build_venv/bin/python -m pytest tests/test_member_quota_e2e.py -v
"""
import os
import shutil
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="vdl_resolve_e2e_")
os.environ["HOME"] = _TMP  # 必须在 import app 之前，让会员状态文件落在隔离目录
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server_app.app, raise_server_exceptions=False)  # 服务端 500 返回响应而非 raise

_FAKE_INFO = {
    "id": "f1", "title": "demo", "uploader": "u", "duration": 10,
    "webpage_url": "https://example.com/v", "extractor_key": "Generic",
    "formats": [], "url": "https://example.com/v.mp4", "is_live": False, "thumbnail": "",
}


def _set_probe_ok():
    server_app.downloader.probe = types.MethodType(
        lambda self, url, cookie="", proxy="": dict(_FAKE_INFO), server_app.downloader)


def _reset_state():
    """清空会员状态文件，回到免费 0 用量。"""
    p = os.path.join(_TMP, ".video-downloader", "membership.json")
    if os.path.exists(p):
        os.remove(p)
    server_app.member_store._loaded = False  # 强制下次重读


def test_free_10_then_402():
    _reset_state()
    _set_probe_ok()
    for i in range(10):
        r = client.post("/api/resolve", json={"url": "https://example.com/v"})
        assert r.status_code == 200, f"第 {i+1} 次免费解析应成功，实际 {r.status_code}: {r.text[:200]}"
    s = server_app.member_store.status()
    assert s["daily_usage"]["resolve"] == 10, "免费成功 10 次后 used 应为 10"
    # 第 11 次 → 402 + MEMBER_QUOTA| 前缀 + 引导文案
    r = client.post("/api/resolve", json={"url": "https://example.com/v"})
    assert r.status_code == 402, f"第 11 次应 402，实际 {r.status_code}"
    detail = r.json().get("detail", "")
    assert detail.startswith("MEMBER_QUOTA|"), detail
    assert "开通下载会员可解锁 1000 次/日" in detail, detail
    # 拒绝不计数
    assert server_app.member_store.status()["daily_usage"]["resolve"] == 10
    print("✅ 免费 10 次成功计数；第 11 次 402 + MEMBER_QUOTA| 引导；拒绝不计数")


def test_failure_does_not_consume():
    _reset_state()
    # probe 抛异常 → 解析失败（500），配额不得消耗
    def boom(self, url, cookie="", proxy=""):
        raise RuntimeError("simulated probe failure")
    server_app.downloader.probe = types.MethodType(boom, server_app.downloader)
    r = client.post("/api/resolve", json={"url": "https://example.com/v"})
    assert r.status_code == 500
    used = server_app.member_store.status()["daily_usage"]["resolve"]
    assert used == 0, f"解析失败不应消耗配额，实际 used={used}"
    # 恢复 OK 后仍可解析（配额还在）
    _set_probe_ok()
    r = client.post("/api/resolve", json={"url": "https://example.com/v"})
    assert r.status_code == 200
    assert server_app.member_store.status()["daily_usage"]["resolve"] == 1
    print("✅ 解析失败不烧配额；恢复后照常计费")


def test_activation_unblocks():
    _reset_state()
    _set_probe_ok()
    for _ in range(10):
        assert client.post("/api/resolve", json={"url": "https://example.com/v"}).status_code == 200
    # 满额后被拒
    assert client.post("/api/resolve", json={"url": "https://example.com/v"}).status_code == 402
    # 激活下载会员
    r = server_app.member_store.activate("download_month")
    assert r["ok"]
    q = server_app.member_store.quota_state("resolve")
    assert q["tier"] == "member" and q["limit"] == 1000
    # 恢复解析
    r = client.post("/api/resolve", json={"url": "https://example.com/v"})
    assert r.status_code == 200, f"激活后应恢复解析：{r.text[:200]}"
    print("✅ 免费满额 → 激活下载会员 → 恢复解析，档位 member 1000/日")


if __name__ == "__main__":
    test_free_10_then_402()
    test_failure_does_not_consume()
    test_activation_unblocks()
    print("\n🎉 下载模块会员配额 E2E 全部通过（3 项）")
    shutil.rmtree(_TMP, ignore_errors=True)
