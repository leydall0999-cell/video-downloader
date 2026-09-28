"""V1 下载配额墙（/api/download、/api/batch）回归测试。

背景（2026-09-28 用户报障）
---------------------------
网页版下载任务成功完成后，个人中心「今日使用」的 used 恒为 0 —— V1 方案 6.7 明明
定了「免费 10 次/日、任务成功创建才计费」。根因有两个（叠加导致限额形同虚设）：
  1. 服务端：会员引擎的 use_daily 从未被下载链路调用（/api/download 只有默认关闭的
     老 IP 配额），创建多少次都不计数；
  2. 前端：request() 不带 Authorization 头（只有 auth/me、member/status 带），
     服务端即使计数也只能落进全局匿名池，归因不到账号。

修复后的契约（本测试钉住）：
  1. cn/单节点：_member_quota_gate 用 current_member_store 预检（匿名→全局共享池，
     登录→该用户 store），_member_quota_count 在任务创建成功后计数（used 递增）；
  2. 超限：402（tier=free 文案含免费额度与会员额度），且 store.create 不被调用
     （配额墙在「点清晰度下载」处，不能先建任务再拒绝）；
  3. /api/download 响应带 member_quota {used, remaining}，前端/个人中心可对账；
  4. /api/batch：剩余不足时按剩余数截断（创建到额度耗尽为止，quota_exhausted=true），
     只按实际创建数计数；
  5. global 节点回派：gate 把 Authorization 原样回派 cn check_only 预检 ——
     cn 判 MEMBER_QUOTA → 402；cn 不可达 → fail-open（放行，不挡下载）；
     计数回派收到 NO_AUTH（匿名）→ 落本机全局匿名池；
  6. 会员引擎任何异常 fail-open：gate/count 异常绝不打断下载主链路。

全程离线：会员 store 用临时目录隔离，下载引擎（store/scheduler/downloader/parse_source）
全部打桩，不打任何网络。运行：cd server && python tests/test_member_download_quota.py
"""
import os
import sys
import tempfile
import types

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 隔离数据目录：app 导入会建 .auth_secret / stats.json，绝不能落到真实用户目录
os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_member_quota")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

import app as A  # noqa: E402
import user_membership as um  # noqa: E402
from membership import MembershipStore  # noqa: E402
from routers import core as core_rt  # noqa: E402
import routers.membership as member_rt  # noqa: E402


class _StubRequest:
    """最小请求桩：get_current_user_id / quota 路由只读 headers。"""

    def __init__(self, auth: str = ""):
        self.headers = {"Authorization": auth} if auth else {}
        self.query_params = {}


def _isolated_member_store() -> MembershipStore:
    """独立临时 store（不污染真实 / 全局匿名状态）。"""
    return MembershipStore(path=None)  # path=None → __post_init__ 落 default_state_path


def _patch_global_store(tmpdir: str) -> MembershipStore:
    """把全局匿名 store 与 user_membership 缓存都指到临时目录。"""
    from pathlib import Path
    store = MembershipStore(path=Path(tmpdir) / "anon_member.json")
    A.member_store = store
    um._membership_dir = lambda: Path(tmpdir)  # per-user 目录隔离
    um._STORE_CACHE.clear()
    return store


def _patch_engine_calls(store):
    """打桩下载引擎：记录 store.create / scheduler.submit 调用，解析恒成功。"""
    calls = {"created": 0, "submitted": 0}

    class _FakeTask:
        id = "t_fake_1"
        status = "pending"

    A.parse_source = lambda url: (url, types.SimpleNamespace(name="Test"))
    A._check_rate_limit = lambda req: None
    A._check_download_quota = lambda req: (False, 0, 10)
    A.downloader = types.SimpleNamespace(
        BEST_KEY="best", is_valid_quality=lambda q: True, quality_label=lambda q: q,
        run_download=lambda *a, **kw: None)
    A.store = types.SimpleNamespace(create=lambda **kw: (calls.__setitem__("created", calls["created"] + 1), _FakeTask())[1])
    A.scheduler = types.SimpleNamespace(submit=lambda *a, **kw: calls.__setitem__("submitted", calls["submitted"] + 1))
    return calls


# --------------------------------------------------------------------------- #
# 1. cn / 单节点：匿名共享池 gate + 创建成功后计数
# --------------------------------------------------------------------------- #
def test_anonymous_gate_then_count():
    with tempfile.TemporaryDirectory() as d:
        store = _patch_global_store(d)
        req = _StubRequest()
        gate = core_rt._member_quota_gate(req, need=1)
        assert gate["mode"] == "local" and gate.get("store") is store
        assert gate.get("remaining") == 10, f"免费档应为 10/日，实为 {gate.get('remaining')}"
        # 任务「创建成功」后计数 → used 递增、remaining 递减
        res = core_rt._member_quota_count(req, gate, n=1)
        assert res and res.get("ok") and res["used"] == 1 and res["remaining"] == 9
        q = store.quota_state("download")
        assert q["used"] == 1 and q["remaining"] == 9
    print("✅ 匿名共享池：gate 预检放行 + 创建成功后 used=1（修复前恒 0）")


# --------------------------------------------------------------------------- #
# 2. 超限 402，且 store.create 不被调用（先建后拒 = 白嫖）
# --------------------------------------------------------------------------- #
def test_exhausted_gate_blocks_before_create():
    with tempfile.TemporaryDirectory() as d:
        store = _patch_global_store(d)
        for _ in range(10):
            assert store.use_daily("download", n=1)["ok"]
        req = _StubRequest()
        calls = _patch_engine_calls(store)
        payload = A.DownloadRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7",
                                    quality="360")
        raised = None
        try:
            core_rt.create_download(payload, req)
        except A.HTTPException as e:
            raised = e
        assert raised is not None and raised.status_code == 402, f"应 402，实为 {raised}"
        assert "MEMBER_QUOTA" not in str(raised.detail)  # 文案给用户看，不带机器前缀
        assert calls["created"] == 0, "配额墙必须挡在 store.create 之前（不能先建任务再拒绝）"
    print("✅ 超限 402 且发生在创建任务之前（免费 10 次/日真正生效）")


# --------------------------------------------------------------------------- #
# 3. create_download 全链路：响应带 member_quota，账号归因（登录用户独立 store）
# --------------------------------------------------------------------------- #
def test_logged_in_download_counts_to_user():
    with tempfile.TemporaryDirectory() as d:
        _patch_global_store(d)
        from auth_store import issue_token
        token = issue_token("u_quota_test")
        req = _StubRequest(auth=f"Bearer {token}")
        calls = _patch_engine_calls(None)
        payload = A.DownloadRequest(url="https://www.bilibili.com/video/BV1GJ411x7h7",
                                    quality="360")
        resp = core_rt.create_download(payload, req)
        mq = resp.get("member_quota")
        assert mq and mq.get("used") == 1 and mq.get("remaining") == 9, f"member_quota 异常: {mq}"
        # 必须落到该用户自己的 store（而不是全局匿名池）
        st = um.get_user_store("u_quota_test").quota_state("download")
        assert st["used"] == 1, f"用户 store 应 used=1，实为 {st['used']}"
        assert A.member_store.quota_state("download")["used"] == 0, "匿名池不应被登录用户的下载污染"
        assert calls["created"] == 1 and calls["submitted"] == 1
    print("✅ 登录用户：下载计数归因到 per-user store（前端已默认带 Authorization）")


# --------------------------------------------------------------------------- #
# 4. /api/batch：剩余不足按剩余截断，只按实际创建数计数
# --------------------------------------------------------------------------- #
def test_batch_caps_at_remaining():
    with tempfile.TemporaryDirectory() as d:
        store = _patch_global_store(d)
        for _ in range(8):  # 已用 8 → 剩 2
            store.use_daily("download", n=1)
        req = _StubRequest()
        _patch_engine_calls(store)
        urls = [f"https://www.bilibili.com/video/BV1GJ411x7{i}" for i in range(5)]
        payload = core_rt.BatchRequest(urls=urls, quality="360")
        resp = core_rt.create_batch(payload, req)
        assert resp["count"] == 2, f"剩余 2 应只创建 2 个任务，实为 {resp['count']}"
        assert resp["quota_exhausted"] is True
        assert resp["member_quota"]["used"] == 10 and resp["member_quota"]["remaining"] == 0
        q = store.quota_state("download")
        assert q["used"] == 10, f"计数应恰好到 10（不超卖），实为 {q['used']}"
    print("✅ 批量：剩余不足截断创建 + 按实际创建数计数（不超卖）")


def test_batch_zero_remaining_rejects():
    with tempfile.TemporaryDirectory() as d:
        store = _patch_global_store(d)
        for _ in range(10):
            store.use_daily("download", n=1)
        req = _StubRequest()
        calls = _patch_engine_calls(store)
        payload = core_rt.BatchRequest(
            urls=[f"https://www.bilibili.com/video/BV1GJ411x7{i}" for i in range(3)],
            quality="360")
        raised = None
        try:
            core_rt.create_batch(payload, req)
        except A.HTTPException as e:
            raised = e
        assert raised is not None and raised.status_code == 402
        assert calls["created"] == 0
    print("✅ 批量：剩余 0 → 402 且一个任务都不建")


# --------------------------------------------------------------------------- #
# 5. global 节点回派：预检 402 / fail-open / 匿名计数落本机匿名池
# --------------------------------------------------------------------------- #
def test_relay_mode_gate_and_count():
    with tempfile.TemporaryDirectory() as d:
        store = _patch_global_store(d)
        assert A.NODE_REGION != "cn", "测试环境应为 global 节点语义（VDL_REGION 未设）"
        orig_base = core_rt._quota_relay_base
        orig_relay = core_rt._relay_member_quota
        try:
            core_rt._quota_relay_base = lambda: "http://cn-relay:18890"

            # 5a. cn 判 MEMBER_QUOTA → 402
            core_rt._relay_member_quota = lambda req, p: {
                "ok": False, "code": "MEMBER_QUOTA", "error": "今日免费下载次数已用尽"}
            raised = None
            try:
                core_rt._member_quota_gate(_StubRequest(), need=1)
            except A.HTTPException as e:
                raised = e
            assert raised is not None and raised.status_code == 402

            # 5b. cn 不可达（回派返回 None）→ fail-open 放行
            core_rt._relay_member_quota = lambda req, p: None
            gate = core_rt._member_quota_gate(_StubRequest(), need=1)
            assert gate["mode"] == "relay"

            # 5c. 计数回派 NO_AUTH（匿名）→ 落本机全局匿名池
            core_rt._relay_member_quota = lambda req, p: {
                "ok": False, "code": "NO_AUTH", "error": "请先登录账号"}
            res = core_rt._member_quota_count(_StubRequest(), gate, n=1)
            assert res and res.get("ok") and res["used"] == 1
            assert store.quota_state("download")["used"] == 1

            # 5d. 计数回派网络炸了 → 返回 None，绝不影响已创建的任务
            def _boom(req, p):
                raise RuntimeError("network down")
            core_rt._relay_member_quota = _boom
            assert core_rt._member_quota_count(_StubRequest(), gate, n=1) is None
        finally:
            core_rt._quota_relay_base = orig_base
            core_rt._relay_member_quota = orig_relay
    print("✅ global 回派：超限透传 402 / cn 不可达 fail-open / 匿名落本机匿名池")


# --------------------------------------------------------------------------- #
# 6. 会员引擎异常 fail-open（本地路径）
# --------------------------------------------------------------------------- #
def test_engine_error_fail_open():
    with tempfile.TemporaryDirectory() as d:
        _patch_global_store(d)

        def _boom():
            raise RuntimeError("disk on fire")
        orig = A.current_member_store  # 先存原函数再 patch，finally 原样还原
        A.current_member_store = lambda req: _boom()
        try:
            gate = core_rt._member_quota_gate(_StubRequest(), need=1)
            assert gate["mode"] == "local" and gate.get("store") is None, f"应 fail-open 放行: {gate}"
            # 计数失败同样吞掉（返回 None）
            assert core_rt._member_quota_count(_StubRequest(), gate, n=1) is None
        finally:
            A.current_member_store = orig
    print("✅ 会员引擎异常 fail-open：绝不打断下载主链路")


# --------------------------------------------------------------------------- #
# 7. quota/use 的 check_only（cn 侧回派端点）：只查不扣
# --------------------------------------------------------------------------- #
def test_quota_use_check_only():
    with tempfile.TemporaryDirectory() as d:
        _patch_global_store(d)
        from auth_store import issue_token
        token = issue_token("u_checkonly")
        req = _StubRequest(auth=f"Bearer {token}")
        r = member_rt.quota_use(req, {"resource": "download", "n": 1, "check_only": True})
        assert r.get("allowed") and r["used"] == 0, f"check_only 只查不扣: {r}"
        r2 = member_rt.quota_use(req, {"resource": "download", "n": 1})
        assert r2.get("ok") and r2["used"] == 1
        r3 = member_rt.quota_use(req, {"resource": "download", "n": 1, "check_only": True})
        assert r3["used"] == 1, "check_only 之后 used 不应再变"
    print("✅ quota/use check_only：只查不扣（hk 预检的 cn 侧端点）")


if __name__ == "__main__":
    test_anonymous_gate_then_count()
    test_exhausted_gate_blocks_before_create()
    test_logged_in_download_counts_to_user()
    test_batch_caps_at_remaining()
    test_batch_zero_remaining_rejects()
    test_relay_mode_gate_and_count()
    test_engine_error_fail_open()
    test_quota_use_check_only()
    print("\n全部通过 ✅（V1 下载配额墙：计数/归因/回派/fail-open 共 8 例）")
