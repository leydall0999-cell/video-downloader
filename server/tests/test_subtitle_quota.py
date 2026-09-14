"""字幕提取模块会员日配额单测（2026-09-13：免费 2 次/日 → 会员无限）。

隔离 HOME 下 TestClient：
  1. 免费本地字幕提取 2 次/日成功计数 → 第 3 次 402 + MEMBER_QUOTA|（引导开会员）
  2. 激活下载会员 → 恢复本地字幕提取，member 档无限（不计日配额）
  3. 会员专享档位不在此测（字幕提取无会员专享引擎）——只验证日配额墙

运行（独立进程，HOME 隔离）：
    cd server && .build_venv/bin/python tests/test_subtitle_quota.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="vdl_sb_quota_")
os.environ["HOME"] = _TMP
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server_app.app, raise_server_exceptions=False)

# 假视频文件：仅需存在且后缀合法（extract 端点只校验后缀 + 配额；executor 已 mock 不真跑 whisper）
_VID = os.path.join(_TMP, "sample.mp4")
Path(_VID).write_text("fake", encoding="utf-8")


def _setup():
    """mock executor（不真跑 whisper）、限流；绕过本地路径白名单（测试用临时目录）。"""
    server_app.executor.submit = lambda *a, **k: None
    server_app._check_rate_limit = lambda request: None
    sb_mod = __import__("routers.subtitle", fromlist=["_resolve_safe_local_path"])
    sb_mod._resolve_safe_local_path = lambda p: Path(p)


def _reset_state():
    p = os.path.join(_TMP, ".video-downloader", "membership.json")
    if os.path.exists(p):
        os.remove(p)
    server_app.member_store._loaded = False


def _post_subtitle(model="small"):
    return client.post("/api/subtitle/extract",
                       json={"local_path": _VID, "model_size": model, "language": "", "fast": False})


def test_free_2_then_402():
    """免费本地字幕提取 2 次/日 → 第 3 次 402 + MEMBER_QUOTA|。"""
    _reset_state()
    _setup()
    for i in range(2):
        r = _post_subtitle()
        assert r.status_code == 200, f"第 {i+1} 次应 200，实际 {r.status_code}: {r.text[:200]}"
    used = server_app.member_store.status()["daily_usage"].get("subtitle", 0)
    assert used == 2, f"subtitle 用量应 2，实际 {used}"
    r = _post_subtitle()
    assert r.status_code == 402, f"第 3 次应 402，实际 {r.status_code}: {r.text[:200]}"
    detail = r.json().get("detail", "")
    assert detail.startswith("MEMBER_QUOTA|"), detail
    assert "2/日" in detail, detail
    print("✅ 免费本地字幕提取 2 次成功计数；第 3 次 402 + MEMBER_QUOTA| 引导开会员（2/日）")


def test_member_unblocks():
    """免费满 2 → 激活下载会员 → 恢复本地字幕提取，member 档无限（不计日配额）。"""
    _reset_state()
    _setup()
    for _ in range(2):
        assert _post_subtitle().status_code == 200
    assert _post_subtitle().status_code == 402
    r = server_app.member_store.activate("download_month")
    assert r["ok"]
    q = server_app.member_store.quota_state("subtitle")
    # 会员档：subtitle 不在 DAILY_QUOTA_LIMITS → 走 unknown 分支，恒 allowed（无限）
    assert q.get("allowed") is True, q
    # 会员再提交应放行，且日配额不被计入（无限）
    for _ in range(3):
        rr = _post_subtitle()
        assert rr.status_code == 200, f"会员应恢复：{rr.text[:200]}"
    used = server_app.member_store.status()["daily_usage"].get("subtitle", 0)
    assert used == 2, f"会员档不应累计字幕提取日配额，实际 {used}"
    print("✅ 免费满 2 → 激活下载会员 → 恢复本地字幕提取，member 档无限（日配额不累计）")


if __name__ == "__main__":
    test_free_2_then_402()
    test_member_unblocks()
    print("\n🎉 字幕提取会员日配额单测全部通过（2 项）")
    shutil.rmtree(_TMP, ignore_errors=True)
