"""配额显示同源端到端守卫：后台改「每日免费」额度 → 个人中心接口实时跟随。

背景（2026-10-07 用户反馈「更改了为什么没有生效」）：
  后端有两套读取每日配额的逻辑——
    · 放行层 quota_state() 走 effective_daily_limits()（叠加后台覆盖）—— 真拦截已跟随；
    · 显示层 feature_usage_status()（个人中心「使用统计」）此前直读 FEATURE_USAGE_DEFS
      烘焙常量 —— 不跟随，导致「改了后台、功能按新额度拦、但表格仍显示旧值」的错位。
  修复：让显示层与放行层同读 effective_daily_limits()。

本守卫走**真实 HTTP 链路**（不 mock router，只 mock 登录/管理员校验与 store 注入）：
  POST /api/admin/config/plans  →  GET /api/account/profile
断言 usage_features 里各功能的 daily_limit 实时跟随后台覆盖（免费档 / 会员档 / 真拦截三处同源）。

运行：cd server && python tests/test_quota_display_e2e.py
"""
import os
import sys
import time
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

os.environ.setdefault("VDL_PLANS_CLOUD", "0")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

from fastapi import FastAPI
from fastapi.testclient import TestClient
import membership as M
import user_membership as UM
import routers.auth as A
import routers.admin as AD


def _build_client():
    # 每个用例独立数据目录，避免多个 MembershipStore 读写同一份状态互相污染
    os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_qe2e.")
    store = M.MembershipStore()
    UM.get_user_store = lambda uid: store          # 真实 store 注入（不 mock 业务逻辑）
    A._require_user = lambda req: "test_uid"       # 仅绕过登录校验
    AD.require_admin = lambda req: None            # 仅绕过管理员校验
    app = FastAPI()
    app.include_router(A.router)                   # 真实 router（含 /api/account/profile）
    app.include_router(AD.router)                  # 真实 router（含 /api/admin/config/plans）
    return TestClient(app), store


def _usage_limits(c):
    r = c.get("/api/account/profile?usage_period=today")
    assert r.status_code == 200, f"profile 非 200: {r.status_code}"
    j = r.json()
    assert "usage_features" in j, "接口未返回 usage_features"
    return {f["key"]: f["daily_limit"] for f in j["usage_features"]}


def test_free_tier_follows_override():
    """免费档：后台改 free_quota.daily_free_limits → 使用统计 + 真拦截 同时跟随。"""
    c, store = _build_client()
    r = c.post("/api/admin/config/plans", json={"free_quota": {
        "daily_free_limits": {"download": 3, "subtitle": 1, "convert_video": 3}}})
    assert r.status_code == 200 and r.json().get("ok"), "保存免费额度失败"

    lim = _usage_limits(c)
    assert lim["video_parse"] == 3, f"video_parse 应=3，实={lim.get('video_parse')}"
    assert lim["subtitle_extract"] == 1, f"subtitle_extract 应=1，实={lim.get('subtitle_extract')}"
    assert lim["convert_video"] == 3, f"convert_video 应=3，实={lim.get('convert_video')}"

    # 真拦截层也必须跟随（这是「改了后台、按新额度拦」的判定）
    assert store.quota_state("download")["limit"] == 3, "quota_state 拦截额度未跟随"
    print("✅ 免费档：后台改额度 → 使用统计 + 真拦截 同时跟随")


def test_member_tier_follows_override():
    """会员档：后台改 free_quota.daily_member_limits → 使用统计跟随。

    ⚠️ 构造会员态必须 active 且 expire_at 在未来，否则 status() 会判过期强制回免费档。
    """
    c, store = _build_client()
    store._ensure_loaded()  # 先把磁盘状态加载进 _state，否则首次 status() 会重载并覆盖下面的改动
    st = store._state
    st["download_member"]["active"] = True
    st["download_member"]["expire_at"] = time.time() + 86400 * 30
    r = c.post("/api/admin/config/plans", json={"free_quota": {
        "daily_member_limits": {"download": 800, "subtitle": 50}}})
    assert r.status_code == 200 and r.json().get("ok"), "保存会员额度失败"

    lim = _usage_limits(c)
    assert lim["video_parse"] == 800, f"会员档 video_parse 应=800，实={lim.get('video_parse')}"
    assert lim["subtitle_extract"] == 50, f"会员档 subtitle_extract 应=50，实={lim.get('subtitle_extract')}"
    print("✅ 会员档：后台改额度 → 使用统计跟随")


def main():
    failed = []
    for fn in (test_free_tier_follows_override, test_member_tier_follows_override):
        try:
            fn()
        except AssertionError as e:
            print(f"❌ {fn.__name__}: {e}")
            failed.append(fn.__name__)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            failed.append(fn.__name__)
    if failed:
        print(f"\n失败 {len(failed)} 项: {failed}")
        sys.exit(1)
    print("\n✅ 配额显示同源端到端全部通过（免费档/会员档/真拦截三处同源）")


if __name__ == "__main__":
    main()
