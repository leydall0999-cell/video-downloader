"""订阅追更也计「下载」额度 —— 回归守卫（2026-10-20）。

背景（用户 2026-10-20 晚截图质疑）：
  「使用统计」里「视频下载」和「订阅追更」两行数字永远一模一样，因为
  `FEATURE_USAGE_DEFS` 里两行 `resource` 同为 `"download"` —— 共用一份额度是设计。
  但**拦截面**当时是单向的：
    · `/api/download`、`/api/batch`、图片下载 → 每条 `use_daily("download", 1)` ✅
    · `_run_subscription_check()`（订阅自动追更）→ **一条都没计** ❌
  后果：免费档 10 次/日可被「订阅追更」完全绕过；且订阅产生的任务没有 usage_ref，
  不进后台「使用详情」。本测试钉住修复后的行为：
    1. 每条新视频扣 1 次下载额度，任务挂 usage_ref（结束后能回写成败）；
    2. 额度用尽立即停止本轮，剩余新视频**不进基线**（下一轮 check 会重试）；
    3. 取不到账本 / 计额度抛异常时 fail-open（放行下载，绝不中断整轮追更）；
    4. 归属还原：`u:<uid>` → 该账号 store，`d:<device>` / 空 → 全局匿名 store。

运行：cd server && ../.build_venv/bin/python tests/test_subscription_download_quota.py
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_subq."))
os.environ.setdefault("VDL_CLOUD_LINK", "0")     # 不联网、不上云
os.environ.setdefault("VDL_PLANS_CLOUD", "0")

import app as A            # noqa: E402
import membership as M     # noqa: E402
import user_membership as UM  # noqa: E402

_SEQ = [0]


def _fresh_store(tag: str) -> M.MembershipStore:
    _SEQ[0] += 1
    p = Path(tempfile.mkdtemp(prefix=f"vdl_subq_{tag}{_SEQ[0]}.")) / "membership.json"
    return M.MembershipStore(path=p)


def _videos(n: int):
    return [{"id": f"v{i}", "url": f"https://example.com/v{i}", "title": f"第{i}集"}
            for i in range(n)]


class _FakeStore:
    def __init__(self):
        self.created = []

    def create(self, **kw):
        t = SimpleNamespace(id=f"task{len(self.created) + 1}", **kw)
        self.created.append(t)
        return t


class _FakeSubStore:
    def __init__(self):
        self.updates = []

    def update(self, sid, **kw):
        self.updates.append((sid, kw))


def _run_check(member_store, videos, owner_id="", sub_id="sub1",
               last_ids=None, raise_in_use_daily=False):
    """跑一次真实的 _run_subscription_check，外部依赖全部替换为假件。"""
    fake_tasks = _FakeStore()
    fake_subs = _FakeSubStore()
    sub = SimpleNamespace(id=sub_id, url="https://example.com/channel", cookie="", proxy="",
                          quality_key=A.downloader.BEST_KEY, last_video_ids=list(last_ids or []),
                          owner_id=owner_id)

    def _parse(u):
        return (u, SimpleNamespace(name="测试平台"))

    real = member_store
    if raise_in_use_daily:
        real.use_daily = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("账本坏了"))

    with patch.object(A.subs_mod, "probe_channel", lambda *a, **k: videos), \
         patch.object(A, "parse_source", _parse), \
         patch.object(A, "store", fake_tasks), \
         patch.object(A, "scheduler", SimpleNamespace(submit=lambda *a, **k: None)), \
         patch.object(A, "sub_store", fake_subs), \
         patch.object(A, "_member_store_for_owner", lambda owner: real):
        result = A._run_subscription_check(sub)
    return result, fake_tasks, fake_subs, real


def case_methodical_download_counts_one_each():
    st = _fresh_store("basic")
    result, tasks, subs_store, _ = _run_check(st, _videos(3))
    assert result["quota_exhausted"] is False, f"额度够用却被标用尽：{result}"
    assert len(tasks.created) == 3, f"应创建 3 个任务，实际 {len(tasks.created)}"
    used = st.status()["daily_usage"].get("download", 0)
    assert used == 3, f"下载额度应记 3 次，实际 {used}"
    refs = [getattr(t, "usage_ref", "") for t in tasks.created]
    assert all(refs), f"每个任务都必须挂 usage_ref（否则不进使用详情/无法回写成败）：{refs}"
    assert all(getattr(t, "_usage_store", None) is st for t in tasks.created), "任务必须挂上账本对象"
    assert len(subs_store.updates) == 1, "基线必须只更新一次"
    baseline = subs_store.updates[0][1]["last_video_ids"]
    assert sorted(baseline) == ["v0", "v1", "v2"], f"基线应含 3 条，实际 {baseline}"
    print("✅ 每条订阅下载各计 1 次下载额度 + 挂 usage_ref + 进基线")


def case_exhausted_stops_and_keeps_retryable():
    st = _fresh_store("exhaust")
    # 免费档 download 上限 10/日：先手动用掉 8 次，只剩 2 次
    for _ in range(8):
        assert st.use_daily("download", 1)["ok"] is True
    result, tasks, subs_store, _ = _run_check(st, _videos(5))
    assert result["quota_exhausted"] is True, "额度不足必须标记 quota_exhausted"
    assert len(tasks.created) == 2, f"只剩 2 次额度，应创建 2 个任务，实际 {len(tasks.created)}"
    assert st.status()["daily_usage"]["download"] == 10, "额度必须扣满且不超限"
    baseline = subs_store.updates[0][1]["last_video_ids"]
    assert sorted(baseline) == ["v0", "v1"], (
        f"未下载的 3 条绝不能进基线（否则下次永不重试），实际 {baseline}")
    assert result["task_ids"] and len(result["task_ids"]) == 2, result
    print("✅ 额度用尽即停：只下剩额度的条数，其余不进基线、下轮重试")


def case_fail_open_on_store_error():
    st = _fresh_store("boom")
    result, tasks, _, _ = _run_check(st, _videos(3), raise_in_use_daily=True)
    assert result["quota_exhausted"] is False, "账本异常不得被当成额度用尽"
    assert len(tasks.created) == 3, f"账本异常必须 fail-open（3 个任务），实际 {len(tasks.created)}"
    print("✅ 计额度抛异常 → fail-open 放行，不中断整轮追更")


def case_no_store_means_fail_open():
    result, tasks, _, _ = _run_check("SENTINEL", _videos(2))
    # member_store="SENTINEL" 会原样返回，调用 use_daily 会炸 → 走异常分支
    assert len(tasks.created) == 2, "取不到可用账本时不得阻断下载"
    print("✅ 账本不可用 → 仍照常下载（不因记账挂掉追更）")


def case_owner_prefix_mapping():
    global_store = A.member_store
    assert UM.store_for_owner("") is global_store, "空归属应落到全局匿名 store"
    assert UM.store_for_owner("d:MacBook-ABC123") is global_store, "设备归属应落到全局匿名 store"
    per_user = UM.store_for_owner("u:testuser001")
    assert str(per_user.path).endswith("testuser001.json"), (
        f"u:<uid> 必须落到该账号的 per-user 账本，实际 {per_user.path}")
    assert per_user is not global_store, "账号 store 不能等于全局匿名 store"
    print("✅ 归属还原正确：u:<uid> → 账号账本；d:<device> / 空 → 全局匿名账本")


def case_member_tier_not_blocked():
    """会员档 1000/日：批量追更 30 条也必须全部放行（回归「会员被误拦」）。"""
    st = _fresh_store("member")
    with patch.object(M, "effective_daily_limits", lambda: ({"download": 1000}, {"download": 10})), \
         patch.object(st, "_is_download_active", lambda: True):
        result, tasks, _, _ = _run_check(st, _videos(30))
    assert result["quota_exhausted"] is False, "会员档 30 条不该触顶"
    assert len(tasks.created) == 30, f"会员档应放行 30 条，实际 {len(tasks.created)}"
    print("✅ 会员档 1000/日 不被误拦（30 条全部放行）")


def main():
    os.environ["VDL_PLANS_CLOUD"] = "0"
    case_methodical_download_counts_one_each()
    case_exhausted_stops_and_keeps_retryable()
    case_fail_open_on_store_error()
    case_no_store_means_fail_open()
    case_owner_prefix_mapping()
    case_member_tier_not_blocked()
    print("\n✅ 订阅追更计入下载额度回归全部通过（计数 / 触顶停 / fail-open / 归属 / 会员不误拦）")


if __name__ == "__main__":
    main()
