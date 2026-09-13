"""抠图模块会员模式单测（2026-09-13：Layer1 日配额墙 + Layer2 会员专享引擎）。

隔离 HOME 下 TestClient：
  1. 引擎标记：birefnet-portrait/hr/isnet/sam 标 member_only；birefnet-matting 保持免费
  2. 免费本地抠图 8 次/日成功计数 → 第 9 次 402 + MEMBER_QUOTA|（引导开会员）
  3. 激活会员 → 恢复本地抠图，档位 member（limit 500）
  4. 会员专享引擎（birefnet-portrait）免费用户直调 → 402 拦截；会员 → 放行
  5. /api/matting/models 列表：免费隐藏 member_only 引擎，会员可见（HR 仍受 RAM 隐藏）
  6. 云端火山抠图走积分、不占日配额（force_cloud=1 不计 matting 用量）

运行（独立进程，HOME 隔离）：
    cd server && .build_venv/bin/python tests/test_matting_member.py
"""
import io
import os
import shutil
import sys
import tempfile

from PIL import Image

_TMP = tempfile.mkdtemp(prefix="vdl_mat_member_")
os.environ["HOME"] = _TMP
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
import matting_ai as mat_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server_app.app, raise_server_exceptions=False)

# 1x1 白底 PNG，作为上传样本（suffix 合法即可，不会真跑抠图——executor 已 mock）
_IMG = io.BytesIO()
Image.new("RGB", (4, 4), (255, 255, 255)).save(_IMG, format="PNG")
_IMG.seek(0)
_IMG_BYTES = _IMG.getvalue()


def _setup():
    """mock executor（不真跑抠图）、限流、matting.available 恒 True，纯测门禁。"""
    server_app.executor.submit = lambda *a, **k: None
    server_app._check_rate_limit = lambda request: None
    mat_mod.available = lambda: True


def _reset_state():
    p = os.path.join(_TMP, ".video-downloader", "membership.json")
    if os.path.exists(p):
        os.remove(p)
    server_app.member_store._loaded = False


def _post_matting(model=None, force_cloud=False):
    files = {"file": ("t.png", _IMG_BYTES, "image/png")}
    data = {}
    if model:
        data["model"] = model
    if force_cloud:
        data["force_cloud"] = "1"
    return client.post("/api/matting/image", files=files, data=data)


def test_engine_flags():
    """引擎标记正确性：4 个高级引擎 member_only，birefnet-matting 免费。"""
    _setup()
    for eng in ("birefnet-portrait", "birefnet-hr-matting", "isnet-general-use", "sam-matting"):
        assert mat_mod.MODELS[eng].get("member_only") is True, f"{eng} 应标 member_only"
    assert mat_mod.MODELS["birefnet-matting"].get("member_only") is not True, "birefnet-matting 应保持免费"
    # list_models 暴露字段
    names = {m["name"]: m for m in mat_mod.list_models()}
    assert names["birefnet-portrait"]["member_only"] is True
    assert names["birefnet-matting"].get("member_only") is not True
    print("✅ 引擎标记：4 高级引擎 member_only；birefnet-matting 免费；list_models 已暴露")


def test_free_local_8_then_402():
    """免费本地抠图 8 次/日 → 第 9 次 402 + MEMBER_QUOTA|。"""
    _reset_state()
    _setup()
    for i in range(8):
        r = _post_matting(model="birefnet-general")
        assert r.status_code == 200, f"第 {i+1} 次应 200，实际 {r.status_code}: {r.text[:200]}"
    used = server_app.member_store.status()["daily_usage"].get("matting", 0)
    assert used == 8, f"matting 用量应 8，实际 {used}"
    r = _post_matting(model="birefnet-general")
    assert r.status_code == 402, f"第 9 次应 402，实际 {r.status_code}"
    detail = r.json().get("detail", "")
    assert detail.startswith("MEMBER_QUOTA|"), detail
    assert "500 次/日" in detail, detail
    print("✅ 免费本地抠图 8 次成功计数；第 9 次 402 + MEMBER_QUOTA| 引导开会员（500/日）")


def test_member_unblocks_local():
    """免费满 8 → 激活下载会员 → 恢复本地抠图，member 档 500/日。"""
    _reset_state()
    _setup()
    for _ in range(8):
        assert _post_matting(model="birefnet-general").status_code == 200
    assert _post_matting(model="birefnet-general").status_code == 402
    r = server_app.member_store.activate("download_month")
    assert r["ok"]
    q = server_app.member_store.quota_state("matting")
    assert q["tier"] == "member" and q["limit"] == 500, q
    r = _post_matting(model="birefnet-general")
    assert r.status_code == 200, f"激活后应恢复：{r.text[:200]}"
    print("✅ 免费满 8 → 激活下载会员 → 恢复本地抠图，member 档 500/日")


def test_member_only_engine_gate():
    """会员专享引擎：免费用户直调 birefnet-portrait → 402；会员 → 放行。"""
    _reset_state()
    _setup()
    # 免费：先确保有配额（但引擎门禁优先于配额，应在第 1 次就拦）
    r = _post_matting(model="birefnet-portrait")
    assert r.status_code == 402, f"免费用户选会员引擎应 402，实际 {r.status_code}: {r.text[:200]}"
    assert "会员专享" in r.json().get("detail", ""), r.json().get("detail")
    # 会员：激活后放行
    server_app.member_store.activate("download_month")
    r = _post_matting(model="birefnet-portrait")
    assert r.status_code == 200, f"会员选 portrait 应放行，实际 {r.status_code}: {r.text[:200]}"
    print("✅ 会员专享引擎：免费 402 拦截（会员专享文案）；会员放行")


def test_models_list_filter():
    """/api/matting/models：免费隐藏 member_only 引擎；会员可见（HR 仍受 RAM 隐藏）。"""
    _reset_state()
    _setup()
    free = client.get("/api/matting/models").json()["models"]
    free_names = {m["name"] for m in free}
    assert "birefnet-portrait" not in free_names, "免费应隐藏 portrait"
    assert "isnet-general-use" not in free_names, "免费应隐藏 isnet"
    assert "sam-matting" not in free_names, "免费应隐藏 sam"
    assert "birefnet-matting" in free_names, "免费应可见 birefnet-matting"
    # 会员
    server_app.member_store.activate("download_month")
    mem = client.get("/api/matting/models").json()["models"]
    mem_names = {m["name"] for m in mem}
    assert "birefnet-portrait" in mem_names, "会员应可见 portrait"
    assert "isnet-general-use" in mem_names, "会员应可见 isnet"
    assert "sam-matting" in mem_names, "会员应可见 sam"
    print("✅ 模型列表：免费隐藏 4 高级引擎、会员可见；birefnet-matting 两档均可见")


def test_cloud_not_counted_in_daily():
    """云端火山抠图走积分、不占本地日配额（force_cloud=1，8GB 无积分应 402 但 matting 用量不变）。"""
    _reset_state()
    _setup()
    before = server_app.member_store.status()["daily_usage"].get("matting", 0)
    # 本机无 AI 积分 → 云端门禁 402（积分不足），但本地 matting 日配额不应被消耗
    r = _post_matting(force_cloud=True)
    after = server_app.member_store.status()["daily_usage"].get("matting", 0)
    assert after == before, f"云端抠图不应占本地日配额，before={before} after={after}"
    # 本地仍可用 8 次
    for _ in range(8):
        assert _post_matting(model="birefnet-general").status_code == 200
    assert server_app.member_store.status()["daily_usage"].get("matting", 0) == before + 8
    print("✅ 云端抠图走积分不计本地日配额；本地 8 次额度独立可用")


if __name__ == "__main__":
    test_engine_flags()
    test_free_local_8_then_402()
    test_member_unblocks_local()
    test_member_only_engine_gate()
    test_models_list_filter()
    test_cloud_not_counted_in_daily()
    print("\n🎉 抠图会员模式单测全部通过（6 项）")
    shutil.rmtree(_TMP, ignore_errors=True)
