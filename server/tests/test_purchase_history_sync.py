# -*- coding: utf-8 -*-
"""守卫：云端购买记录必须落到本机「购买记录」（且**不重复叠加会员天数**）。

背景（2026-10-09 用户报障）：「购买记录没有显示购买过」——在网页版付款成功后，
个人中心的购买记录仍是空的。

根因：网页/云端付款由**授权中心**发放权益，本机通过 `apply_cloud_authoritative`
用云端权威快照对齐。但该快照只带会员**字段**（member_until_dl / ai_credits_left /
perm_credits …），**不带流水**；而「购买记录」是读本机 `meta.history` 的
（`server/routers/auth.py::account_profile`）⇒ 权威路径永远不写 history，
用户看到「买过但记录里没有」。

修复：`apply_cloud_authoritative` 把云端 `purchases`（id/plan_code/at）按 id
**幂等**并入 `meta.history`。关键约束有两条，本守卫逐条钉住：

  1) 幂等：同一个快照对账多次，购买记录不重复、不增长；
  2) **绝不**因此重复加天数：权益效果已在快照字段里体现，若顺手调 activate()
     会把会员到期时间重复叠加（多送时长）—— 这是本条修复最容易犯的错。
  3) 与老兜底路径 `apply_cloud_purchases` 共用去重表，两条路互不重复落户。

所有用例都在 VDL_DATA_DIR 临时目录内进行，绝不写真实家目录。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

_tmp = tempfile.mkdtemp(prefix="vdl_hist_sync_guard_")
os.environ["VDL_DATA_DIR"] = _tmp
os.environ["VDL_CLOUD_LINK"] = "0"
os.environ["VDL_PLANS_CLOUD"] = "0"

from membership import MembershipStore  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  \u2705 " if cond else "  \u274c ") + name)
    if not cond:
        FAILS.append(name)


def _purchases_of(store: MembershipStore) -> list[dict]:
    """与 routers/auth.py::account_profile 一致的「购买记录」口径。

    生产链路里 profile 先调 store.status()（内部 _ensure_loaded）再读 _state；
    这里同样要先触发惰性加载，否则读到的永远是空 _state。
    """
    store._ensure_loaded()
    raw = (store._state.get("meta", {}) or {}).get("history", []) or []
    out = [dict(h) for h in raw if h.get("type") not in ("spend", "admin_adjust")]
    out.sort(key=lambda x: x.get("at", 0), reverse=True)
    return out


def _snapshot(dl_until: float, purchases: list[dict]) -> dict:
    return {
        "authority": {
            "v": 1,
            "member_until_dl": dl_until,
            "member_until_ai": 0,
            "ai_credits_left": 0,
            "perm_credits": 0,
            "banned": False,
        },
        "purchases": purchases,
    }


def test_cloud_purchases_land_in_history() -> None:
    print("\n[A] 云端购买记录落户本机 history")
    p = pathlib.Path(_tmp) / "a.json"
    st = MembershipStore(path=p)
    now = st._now()
    dl_until = now + 86400.0                      # 快照：VIP会员 1 天
    acc = _snapshot(dl_until, [
        {"id": "buy-1", "plan_code": "download_1day", "at": now - 10},
        {"id": "buy-2", "plan_code": "download_3day", "at": now - 5},
    ])
    r = st.apply_cloud_authoritative(acc)
    check("apply_cloud_authoritative 成功", bool(r.get("ok")))

    got = _purchases_of(st)
    check("购买记录出现 2 条", len(got) == 2)
    codes = {g.get("code") for g in got}
    check("两条 code 正确", codes == {"download_1day", "download_3day"})
    check("带 purchase_id（可去重）",
          {g.get("purchase_id") for g in got} == {"buy-1", "buy-2"})
    check("持久化到磁盘（重开仍是 2 条）",
          len(_purchases_of(MembershipStore(path=p))) == 2)


def test_idempotent_no_duplicates() -> None:
    print("\n[B] 反复对账不重复落户")
    p = pathlib.Path(_tmp) / "b.json"
    st = MembershipStore(path=p)
    now = st._now()
    acc = _snapshot(now + 86400.0, [
        {"id": "same-1", "plan_code": "download_1day", "at": now - 10},
    ])
    for _ in range(4):
        st.apply_cloud_authoritative(acc)
    got = _purchases_of(st)
    check("4 次对账后仍只有 1 条", len(got) == 1, )
    check("history 总数未膨胀",
          len((st._state.get("meta") or {}).get("history") or []) == 1)


def test_membership_days_not_double_counted() -> None:
    print("\n[C] 🔴 落户购买记录不得重复叠加会员天数")
    p = pathlib.Path(_tmp) / "c.json"
    st = MembershipStore(path=p)
    now = st._now()
    dl_until = now + 86400.0
    acc = _snapshot(dl_until, [
        {"id": "buy-x", "plan_code": "download_1day", "at": now - 10},
    ])
    st.apply_cloud_authoritative(acc)
    after1 = float(st._state["download_member"]["expire_at"])
    check("到期时间 == 快照值（未额外加 1 天）",
          abs(after1 - dl_until) < 0.001,
          )

    # 再对账两次：若实现里顺手调了 activate()，这里会一路膨胀
    st.apply_cloud_authoritative(acc)
    st.apply_cloud_authoritative(acc)
    after3 = float(st._state["download_member"]["expire_at"])
    check("第三次对账后到期时间依然不变",
          abs(after3 - dl_until) < 0.001)
    check("到期时间严格等于快照（差值 0）",
          abs(after3 - dl_until) < 0.001)


def test_shares_dedup_table_with_fallback_path() -> None:
    print("\n[D] 与兜底路径共用去重表（不重复落户）")
    p = pathlib.Path(_tmp) / "d.json"
    st = MembershipStore(path=p)
    now = st._now()
    # 权威路径先落一条
    st.apply_cloud_authoritative(_snapshot(now + 86400.0, [
        {"id": "dup-1", "plan_code": "download_1day", "at": now - 10},
    ]))
    check("权威路径已落 1 条", len(_purchases_of(st)) == 1)
    # 老服务端兜底路径拿到同一条 → 不得再加
    st.apply_cloud_purchases([{"id": "dup-1", "plan_code": "download_1day"}])
    check("兜底路径不再重复落户", len(_purchases_of(st)) == 1)


def test_old_snapshot_without_purchases_is_safe() -> None:
    print("\n[E] 快照无 purchases 字段时不报错、不清空既有记录")
    p = pathlib.Path(_tmp) / "e.json"
    st = MembershipStore(path=p)
    now = st._now()
    st.apply_cloud_authoritative(_snapshot(now + 86400.0, [
        {"id": "keep-1", "plan_code": "download_1day", "at": now - 10},
    ]))
    check("先有 1 条", len(_purchases_of(st)) == 1)
    # 老服务端快照：purchases 缺失
    acc = _snapshot(now + 172800.0, [])
    acc.pop("purchases")
    r = st.apply_cloud_authoritative(acc)
    check("无 purchases 字段也成功", bool(r.get("ok")))
    check("既有购买记录未被清空", len(_purchases_of(st)) == 1)


def main() -> int:
    print("=" * 52)
    print("购买记录落户守卫（云端 purchases → 本机 meta.history）")
    print("=" * 52)
    test_cloud_purchases_land_in_history()
    test_idempotent_no_duplicates()
    test_membership_days_not_double_counted()
    test_shares_dedup_table_with_fallback_path()
    test_old_snapshot_without_purchases_is_safe()
    print("\n" + "=" * 52)
    if FAILS:
        print("\u274c 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("\u2705 购买记录落户守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
