"""P0-3 回归测试：旧 mock 支付路径不得免费发权益。

背景（安全审计 2026-10-11，缺陷 P0-3「旧 mock 支付路径可免费发权益」）：
  · `routers/payment.py:_provider()` 默认 `"mock"`；
  · `payment_core.MockProvider.simulate_paid` 的唯一守卫是
    `isinstance(self.provider, MockProvider)` —— 无管理员/本机/环境校验；
  · `POST /api/cloud/pay/simulate_paid` 直接把订单翻成已付并触发 `activate()`。
  ⇒ 任何能访问该端口的人，只要 POST 一个自己下单得到的 order_id，就能白拿
    会员天数 / AI 积分（二者都是计费资产）。
  另：`_grant_with` 原先用**回调请求者**的 store 发放，与订单归属无关 ——
    拿到他人 order_id 即可把权益发到自己账上。

修复口径（见 routers/payment.py + membership.py）：
  · 旧 mock 通道加**双重开关**：`VDL_PAY_MOCK=1`（默认关）**且**请求来自本机回环，
    缺一即 404（不暴露端点存在）；真线上收款走 /api/pay/*（转发 VPS），与此无关；
  · `_grant_with` 改为按**订单记录里的 user_id**（`get_user_store(owner)`）发放；
  · `MembershipStore.activate(order_id=...)` 加订单级幂等键，同一笔支付只发一次。

设计约束：VDL_DATA_DIR 指向临时目录，绝不碰真实 ~/.video-downloader。
"""
import os
import sys
import tempfile
import types
from pathlib import Path

_TMP_DATA = tempfile.mkdtemp(prefix="vdl_p0_3_")
os.environ["VDL_DATA_DIR"] = _TMP_DATA

_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from routers import payment as pay  # noqa: E402


def _req(host: str = "127.0.0.1", headers=None):
    """最小请求替身。

    安全审计 2026-10-11（P3-1）后 `_is_loopback` 走 `app._is_local_direct`，
    要求「socket 对端环回 **且未携带任何代理头**」，故替身必须带 headers 字段
    （旧替身只有 client，会被判成「经过代理」而 fail-closed）。
    """
    return types.SimpleNamespace(client=types.SimpleNamespace(host=host),
                                 headers=dict(headers or {}))


def _env(**kv):
    class _Ctx:
        def __enter__(self):
            self._old = {k: os.environ.get(k) for k in kv}
            for k, v in kv.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return self

        def __exit__(self, *exc):
            for k, v in self._old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            return False

    return _Ctx()


def _is_404(exc) -> bool:
    return getattr(exc, "status_code", None) == 404


# ---------------------------------------------------------------- 双重开关

def test_mock_pay_disabled_by_default():
    """未设 VDL_PAY_MOCK 时，即使来自本机也一律拒绝。"""
    with _env(VDL_PAY_MOCK=None):
        assert pay._mock_pay_allowed(_req("127.0.0.1")) is False
        try:
            pay._require_mock_pay(_req("127.0.0.1"))
            raise AssertionError("默认配置下旧 mock 通道应被拒绝")
        except pay.HTTPException as e:
            assert _is_404(e), e


def test_mock_pay_requires_loopback():
    """开了开关但从公网地址调用仍被拒。"""
    with _env(VDL_PAY_MOCK="1"):
        assert pay._mock_pay_allowed(_req("203.0.113.7")) is False
        assert pay._mock_pay_allowed(_req("127.0.0.1")) is True
        assert pay._mock_pay_allowed(_req("::1")) is True
        assert pay._mock_pay_allowed(None) is False     # 拿不到请求对象 → 拒


def test_mock_pay_loopback_cannot_be_spoofed():
    """安全审计 2026-10-11（P3-1）：伪造代理头不能把自己说成本机。"""
    with _env(VDL_PAY_MOCK="1"):
        for hdr in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip"):
            assert pay._mock_pay_allowed(_req("127.0.0.1", {hdr: "127.0.0.1"})) is False, hdr


def test_simulate_paid_404_without_switch():
    """端到端：默认配置下 simulate_paid 直接 404（不是「订单不存在」这类业务错）。"""
    with _env(VDL_PAY_MOCK=None):
        try:
            pay.pay_simulate({"order_id": "VDLFAKE0000000001"}, _req("127.0.0.1"))
            raise AssertionError("simulate_paid 应当 404")
        except pay.HTTPException as e:
            assert _is_404(e), e


# ---------------------------------------------------------------- 发放归属

def test_grant_goes_to_order_owner_not_caller():
    """权益必须发给**订单归属账号**，与谁触发回调无关。"""
    with _env(VDL_PAY_MOCK="1"):
        svc = pay._service()
        order = svc.create("u_owner_A", "download_1day")
        oid = order["order_id"]

        # 由「另一个人」的请求触发付款
        res = pay.pay_simulate({"order_id": oid}, _req("127.0.0.1"))
        assert res.get("ok"), res
        assert res.get("status") == "PAID", res
        assert res.get("granted") is True, res

        from user_membership import get_user_store
        owner_st = get_user_store("u_owner_A").status()
        other_st = get_user_store("u_owner_B").status()
        assert owner_st["download_member"]["active"] is True, "权益没有发给订单归属账号"
        assert other_st["download_member"]["active"] is False, "权益被错误地发给了调用者"


def test_grant_rejects_order_without_owner():
    """订单记录缺 user_id 时必须拒绝发放（不能退化成「发给调用者」）。"""
    with _env(VDL_PAY_MOCK="1"):
        svc = pay._service()
        oid = "VDLORPHAN00000001"
        svc.store.save({"order_id": oid, "plan_code": "download_1day",
                        "plan_name": "x", "amount": 1.9, "status": "PENDING",
                        "created_at": 0.0, "granted": False, "granted_at": 0.0})
        r = pay._grant_with(_req("127.0.0.1"), "download_1day", oid)
        assert r.get("ok") is False, r
        assert r.get("code") == "ORDER_NO_OWNER", r


# ---------------------------------------------------------------- 幂等

def test_activate_is_idempotent_by_order_id():
    """同一 order_id 重复发放：第二次直接返回既有结果，权益不叠加。"""
    from membership import MembershipStore
    st = MembershipStore(path=Path(_TMP_DATA) / "idem_member.json")

    first = st.activate("download_7day", via="payment", order_id="VDLIDEM000000001")
    assert first.get("ok") and not first.get("idempotent")
    exp1 = st.status()["download_member"]["expire_at"]

    second = st.activate("download_7day", via="payment", order_id="VDLIDEM000000001")
    assert second.get("ok") is True
    assert second.get("idempotent") is True, "重复发放未被幂等拦下"
    exp2 = st.status()["download_member"]["expire_at"]
    assert exp2 == exp1, f"重复发放叠加了会员时长：{exp1} → {exp2}"


def test_activate_without_order_id_still_stacks():
    """不传 order_id（如后台手动发放）时保持原语义：可以连续叠加。"""
    from membership import MembershipStore
    st = MembershipStore(path=Path(_TMP_DATA) / "stack_member.json")
    st.activate("download_7day", via="admin")
    exp1 = st.status()["download_member"]["expire_at"]
    st.activate("download_7day", via="admin")
    exp2 = st.status()["download_member"]["expire_at"]
    assert exp2 > exp1, "无订单号的发放不应被幂等拦下"


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
