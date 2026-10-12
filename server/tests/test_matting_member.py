"""抠图模块会员模式单测（2026-09-13：Layer1 日配额墙 + Layer2 会员专享引擎）。

隔离 HOME 下 TestClient：
  1. 引擎标记：birefnet-portrait/hr/isnet/sam 标 member_only；birefnet-matting 保持免费
  2. 一键抠图纯按 AI 积分：有积分连发 12 次全过（**无 8 次/日 上限**），日配额计数恒 0
  3. 激活会员**不再**解锁「抠图 500 次/日」—— 配额表已无 matting 键
  4. 会员专享引擎（birefnet-portrait）免费用户直调 → 402 拦截；会员 → 放行
  5. /api/matting/models 列表：免费隐藏 member_only 引擎，会员可见（HR 仍受 RAM 隐藏）
  6. 本机 / 云端抠图都不再写 daily_usage.matting（日配额整条移除）

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
# 2026-09-30：membership._base_dir() 起优先认 VDL_DATA_DIR（run_offline_tests.sh 会设成固定的
# /tmp/vdl_offline_data）。只隔离 HOME 时会员状态会落到那个**共享**目录 → 本文件的 _reset_state()
# 形同虚设、免费日配额跨轮次累计，同一进程内第二个用例必 402。故把 VDL_DATA_DIR 对齐到 _TMP。
os.environ["VDL_DATA_DIR"] = os.path.join(_TMP, ".video-downloader")

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
    # 🔴 2026-10-11 定档：本机重算力计入积分，且**取消每日免费次数**。本文件现在
    #   验的是「没有 8 次/日 这道墙」⇒ 先给账号补足积分，把积分因素排除掉。
    #   零积分被拦由 test_ai_free_trial / test_membership 单独钉，此处不复测。
    server_app.member_store.add_credits(100000, reason="test_matting_member_setup")


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


def test_local_matting_needs_credits_no_daily_cap():
    """🔴 2026-10-11 定档：一键抠图取消每日免费次数，纯按 AI 积分计费。

    钉两件事：
      1) 有积分 → 连发 12 次全部 200（远超旧的 8 次/日 上限也不拦）；
      2) `daily_usage.matting` 始终为 0 —— 日配额墙已整条移除，不再计数。
    """
    _reset_state()
    _setup()
    for i in range(12):
        r = _post_matting(model="birefnet-general")
        assert r.status_code == 200, (
            f"有积分时第 {i+1} 次应 200（已无 8/日 上限），实际 {r.status_code}: {r.text[:200]}")
    used = server_app.member_store.status()["daily_usage"].get("matting", 0)
    assert used == 0, f"抠图已取消日配额，daily_usage.matting 应为 0，实际 {used}"
    print("✅ 一键抠图：有积分连发 12 次全过（无 8/日 上限），日配额计数恒 0")


def test_member_no_longer_unlocks_daily_matting():
    """🔴 2026-10-11：会员**不再**解锁「抠图 500 次/日」—— 抠图改纯积分，无日配额。

    反向钉死：旧行为是激活会员把 matting 日配额抬到 500。现在 `quota_state("matting")`
    必须是「未知资源、fail-open」，会员与否都一样 —— 拦不拦只看积分。
    """
    _reset_state()
    _setup()
    r = server_app.member_store.activate("download_month")
    assert r["ok"]
    q = server_app.member_store.quota_state("matting")
    assert q["unknown"] is True, f"抠图已无日配额 ⇒ 应为未知资源，实际 {q}"
    assert q["allowed"] is True
    r = _post_matting(model="birefnet-general")
    assert r.status_code == 200, f"会员 + 有积分应放行：{r.text[:200]}"
    print("✅ 会员不再解锁抠图日配额（改纯积分）；有积分即放行")


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


def test_matting_writes_no_daily_usage():
    """🔴 2026-10-11：抠图（本机 / 云端）都不再写 `daily_usage.matting`。

    旧行为：本地抠图每次 `use_daily("matting", 1)`。现在日配额整条移除 ⇒ 无论
    force_cloud 与否，计数器都必须是 0（有积分时两条路径都 200）。
    """
    _reset_state()
    _setup()
    _post_matting(force_cloud=True)
    for _ in range(3):
        assert _post_matting(model="birefnet-general").status_code == 200
    used = server_app.member_store.status()["daily_usage"].get("matting", 0)
    assert used == 0, f"抠图已取消日配额，daily_usage.matting 应为 0，实际 {used}"
    print("✅ 抠图（本机 / 云端）均不再写日配额计数；只走 AI 积分")


if __name__ == "__main__":
    test_engine_flags()
    test_local_matting_needs_credits_no_daily_cap()
    test_member_no_longer_unlocks_daily_matting()
    test_member_only_engine_gate()
    test_models_list_filter()
    test_matting_writes_no_daily_usage()
    print("\n🎉 抠图会员模式单测全部通过（6 项）")
    shutil.rmtree(_TMP, ignore_errors=True)
