"""P0-4 回归测试：云额度不得被用户自行回退。

背景（安全审计 2026-10-11，缺陷 P0-4「云额度可被用户自行回退」）：
  `deploy/license_server.py:cloud_quota_impl` 是两端共用的账号级免费云端额度记账
  （终身 3 次 / 每日 1 次，单次真实大模型调用成本 ¥0.04~0.19）。修复前有两个口子：
    ① 负值守卫写成 `life_n < 0 **and** day_n < 0` —— 只传**一个**负数（如
       `lifetime=-3`）即绕过守卫，而 `max(0, used + (-3))` 会把已用数回退
       ⇒ 用户自行把「终身免费次数」刷回满额，可无限白嫖；
    ② `refund=True` 由**客户端**直接传入即生效 ⇒ 消费 → 退还 → 再消费，循环套利。

修复口径（见 deploy/license_server.py）：
  · 任一负值即 400（`or` 而非 `and`）；
  · `refund` 仅当 `allow_refund=True`（管理端动作 `cloud_quota_refund`）才受理，
    客户端传 refund 一律 403；
  · 退还必须携带 `event_id`（原消费事件号）且**一次性**（重复 → 409）；
  · 扣减时服务端生成 `event_id` 随响应返回，使「退还」可核验、天然有界；
  · 每次额度变更写审计流水（来源 client/trusted、事件号、金额、IP）。

设计约束：VDL_LICENSE_DATA 指向临时目录，绝不碰真实 ~/.vdl-license。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="vdl_p0_4_")
os.environ["VDL_LICENSE_DATA"] = str(Path(_TMP) / "cards.json")
os.environ["VDL_LICENSE_SECRET"] = "test-secret-for-p0-4"
os.environ["VDL_LICENSE_ADMIN_TOKEN"] = "test-admin-token"

_DEPLOY_DIR = str(Path(__file__).resolve().parent.parent.parent / "deploy")
if _DEPLOY_DIR not in sys.path:
    sys.path.insert(0, _DEPLOY_DIR)

import license_server as ls  # noqa: E402

SECRET = ls.SECRET
RES = "cloud_commentary"


def _state_with_user(uid: str = "u_p04@example.com") -> tuple[dict, str]:
    """构造一个含该账号的 state 与有效 token（走模块自身的 make_token）。"""
    now = time.time()
    state: dict = {}
    ls._users(state)[uid] = {"email": uid, "created_at": now, "cloud_quota": {}}
    token = ls.make_token(uid, SECRET, now)
    return state, token


def _err(exc) -> tuple[int, str]:
    return int(getattr(exc, "status", 0)), str(getattr(exc, "code", ""))


# ---------------------------------------------------------------- 客户端不得退还

def test_client_refund_forbidden():
    state, token = _state_with_user()
    try:
        ls.cloud_quota_impl(state, token, time.time(), SECRET,
                            lifetime=1, refund=True, resource=RES, ip="1.2.3.4")
        raise AssertionError("客户端传 refund 应被拒绝")
    except ls.ApiError as e:
        status, code = _err(e)
        assert status == 403 and code == "REFUND_FORBIDDEN", (status, code)


def test_client_single_negative_value_rejected():
    """回归：只传一个负数即可绕过旧守卫（`and` → `or` 的根因）。"""
    for kw in ({"lifetime": -3}, {"daily": -1}, {"lifetime": -1, "daily": 0}):
        state, token = _state_with_user()
        try:
            ls.cloud_quota_impl(state, token, time.time(), SECRET,
                                resource=RES, ip="1.2.3.4", **kw)
            raise AssertionError(f"负值 {kw} 应被拒绝")
        except ls.ApiError as e:
            status, code = _err(e)
            assert status == 400 and code == "BAD_BODY", (kw, status, code)


def test_client_consume_then_refund_cannot_reset_usage():
    """端到端：客户端先消费再试图退还 —— 退还被拒，用量不回退。"""
    state, token = _state_with_user()
    now = time.time()
    r = ls.cloud_quota_impl(state, token, now, SECRET, lifetime=3, resource=RES, ip="1.2.3.4")
    used = r["cloud_quota"]["lifetime"][RES]
    assert used == 3, r

    try:
        ls.cloud_quota_impl(state, token, now, SECRET, lifetime=3, refund=True,
                            resource=RES, ip="1.2.3.4")
        raise AssertionError("客户端退还应被拒绝")
    except ls.ApiError:
        pass
    after = ls.cloud_quota_impl(state, token, now, SECRET, resource=RES)["cloud_quota"]
    assert after["lifetime"][RES] == 3, f"用量被回退了：{after}"


# ---------------------------------------------------------------- 消费事件号

def test_consume_returns_event_id_and_enforces_limit():
    state, token = _state_with_user()
    now = time.time()
    r1 = ls.cloud_quota_impl(state, token, now, SECRET, lifetime=3, resource=RES, ip="1.2.3.4")
    assert r1["allowed"] is True
    assert r1["event_id"], "扣减应返回消费事件号（供受信端凭它退还）"

    r2 = ls.cloud_quota_impl(state, token, now, SECRET, lifetime=1, resource=RES, ip="1.2.3.4")
    assert r2["allowed"] is False and r2["reason"] == "lifetime_exhausted", r2


# ---------------------------------------------------------------- 受信退还

def test_trusted_refund_requires_event_id():
    state, token = _state_with_user()
    now = time.time()
    ls.cloud_quota_impl(state, token, now, SECRET, lifetime=1, resource=RES, ip="1.2.3.4")
    try:
        ls.cloud_quota_impl(state, token, now, SECRET, lifetime=1, refund=True,
                            resource=RES, ip="1.2.3.4", allow_refund=True)
        raise AssertionError("受信退还未带 event_id 应被拒绝")
    except ls.ApiError as e:
        status, code = _err(e)
        assert status == 400 and code == "BAD_BODY", (status, code)


def test_trusted_refund_is_one_time_and_bounded():
    """受信端凭事件号退还：一次性、且不会退成负数。"""
    state, token = _state_with_user()
    now = time.time()
    r = ls.cloud_quota_impl(state, token, now, SECRET, lifetime=3, resource=RES, ip="1.2.3.4")
    ev = r["event_id"]

    rf = ls.cloud_quota_impl(state, token, now, SECRET, lifetime=3, refund=True,
                             resource=RES, ip="10.0.0.1", allow_refund=True, event_id=ev)
    assert rf["refunded"] is True
    assert rf["cloud_quota"]["lifetime"][RES] == 0, rf

    # 同一事件重复退还 → 409（否则可反复把额度退回负数 / 重复回补）
    try:
        ls.cloud_quota_impl(state, token, now, SECRET, lifetime=3, refund=True,
                            resource=RES, ip="10.0.0.1", allow_refund=True, event_id=ev)
        raise AssertionError("重复退还同一消费事件应被拒绝")
    except ls.ApiError as e:
        status, code = _err(e)
        assert status == 409 and code == "REFUND_DUPLICATE", (status, code)

    # 超额退还也不得把已用数压成负数
    r2 = ls.cloud_quota_impl(state, token, now, SECRET, lifetime=1, resource=RES, ip="1.2.3.4")
    rf2 = ls.cloud_quota_impl(state, token, now, SECRET, lifetime=99, refund=True,
                              resource=RES, ip="10.0.0.1", allow_refund=True,
                              event_id=r2["event_id"])
    assert rf2["cloud_quota"]["lifetime"][RES] >= 0, rf2


# ---------------------------------------------------------------- 审计流水

def test_audit_log_records_source_and_event():
    state, token = _state_with_user()
    now = time.time()
    r = ls.cloud_quota_impl(state, token, now, SECRET, lifetime=1, resource=RES, ip="1.2.3.4")
    ls.cloud_quota_impl(state, token, now, SECRET, lifetime=1, refund=True,
                        resource=RES, ip="10.0.0.1", allow_refund=True,
                        event_id=r["event_id"])

    evs = [e for e in state.get("ev_ring", []) if e.get("kind") == "cloud_quota"]
    assert len(evs) >= 2, evs
    client_ev = next(e for e in evs if e.get("source") == "client")
    trusted_ev = next(e for e in evs if e.get("source") == "trusted")
    assert client_ev["event_id"] == r["event_id"]
    assert client_ev["ip"] == "1.2.3.4"
    assert trusted_ev["refund"] is True
    assert trusted_ev["ip"] == "10.0.0.1"


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
