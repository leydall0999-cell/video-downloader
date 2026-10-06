"""云端处理账号级配额（2026-09-29 建，2026-10-06 拆池）回归测试。

背景
----
网页版云端处理功能（转码/拼接/图片·PDF 去水印/字幕识别/烧录/翻译）此前登录后
免费不限次。2026-09-29 建墙（resource="cloud" 总池）；2026-10-06 起**按功能
拆池**：cloud_convert / cloud_dewatermark / cloud_subtitle 各自独立计每日次数
（免费 3 次/日、会员 200 次/日），互不挤占。

  1. 配额表：免费 limit=3；会员（activate download_month）limit=200（逐键）；
  2. use_daily("cloud_convert")：3 次内 ok；第 4 次 ok=False + code=MEMBER_QUOTA +
     文案含「视频转码」（用户可读，不带机器前缀）；
  2b. 独立性：转码用尽后，去水印/字幕仍各自有完整 3 次（拆池核心语义）；
  3. cloud_quota_gate：匿名 403（文案引导登录）；超限 402 且发生在任务创建前；
  4. cloud_quota_count：任务成功创建后计数（与下载墙同语义，失败吞掉不回滚）；
  5. relay（global 节点）：cn 判 MEMBER_QUOTA → 402 / NO_AUTH → 403 /
     cn 不可达 → fail-open 放行；计数回派炸了不影响主链路；
  6. 会员引擎异常 fail-open（本地路径）；
  7. VDL_CLOUD_QUOTA_OFF=true → 全部放行（紧急停用开关）。

全程离线：store 用临时目录隔离，无网络。运行：cd server && python tests/test_cloud_quota.py
"""
import os
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_cloud_quota")
os.environ.setdefault("VDL_CLOUD_LINK", "0")
# 本地路径测试不允许 relay base 存在（否则 gate 会走回派分支真发网络请求）
for _k in ("VDL_QUOTA_RELAY_URL", "VDL_WORKER_URL"):
    os.environ.pop(_k, None)

import app as A  # noqa: E402
import user_membership as um  # noqa: E402
from membership import MembershipStore, FREE_DAILY_LIMITS, DAILY_QUOTA_LIMITS  # noqa: E402
from routers import convert as convert_rt  # noqa: E402
from pathlib import Path  # noqa: E402


class _StubRequest:
    def __init__(self, auth: str = ""):
        self.headers = {"Authorization": auth} if auth else {}
        self.query_params = {}


def _patch_stores(tmpdir: str):
    store = MembershipStore(path=Path(tmpdir) / "anon_member.json")
    A.member_store = store
    um._membership_dir = lambda: Path(tmpdir)
    um._STORE_CACHE.clear()
    return store


def _raise_http(e):
    return e


# --------------------------------------------------------------------------- #
# 1+2. 配额表与 use_daily 语义（免费 3/日、会员 200/日、超限文案）
# --------------------------------------------------------------------------- #
def test_free_quota_three_per_day():
    with tempfile.TemporaryDirectory() as d:
        _patch_stores(d)
        # 逐键断言（2026-10-06 拆池：每个功能独立 3 次/日）
        for res in ("cloud_convert", "cloud_dewatermark", "cloud_subtitle"):
            assert FREE_DAILY_LIMITS[res] == 3, f"免费 {res} 应为 3 次/日"
            assert DAILY_QUOTA_LIMITS[res] == 200, f"会员 {res} 应为 200 次/日"
        st = MembershipStore(path=Path(d) / "u_free.json")
        q = st.quota_state("cloud_convert")
        assert q["limit"] == 3 and q["tier"] == "free" and q["allowed"], q
        for i in range(3):
            r = st.use_daily("cloud_convert", n=1)
            assert r.get("ok"), r
            assert r["remaining"] == 2 - i, r
        r4 = st.use_daily("cloud_convert", n=1)
        assert not r4.get("ok") and r4.get("code") == "MEMBER_QUOTA", r4
        assert "视频转码" in r4["error"], f"文案须含「视频转码」: {r4['error']}"
        assert "MEMBER_QUOTA" not in r4["error"]
        q2 = st.quota_state("cloud_convert")
        assert q2["used"] == 3 and not q2["allowed"], q2
    print("✅ 免费视频转码 3 次/日：3 次内放行、第 4 次拒、文案含「视频转码」")


def test_resources_are_independent():
    """拆池核心语义：转码用尽后，去水印/字幕各自仍有完整的 3 次。"""
    with tempfile.TemporaryDirectory() as d:
        _patch_stores(d)
        st = MembershipStore(path=Path(d) / "u_indep.json")
        for _ in range(3):
            assert st.use_daily("cloud_convert", n=1)["ok"]
        assert not st.use_daily("cloud_convert", n=1)["ok"], "转码第 4 次应拒"
        for other in ("cloud_dewatermark", "cloud_subtitle"):
            q = st.quota_state(other)
            assert q["used"] == 0 and q["remaining"] == 3 and q["allowed"], q
            for _ in range(3):
                assert st.use_daily(other, n=1)["ok"]
            assert not st.use_daily(other, n=1)["ok"], f"{other} 第 4 次应拒"
        # 互不串台：转码仍拒、另两项也各自拒，但三者计数互不影响
        assert st.quota_state("cloud_convert")["used"] == 3
        assert st.quota_state("cloud_dewatermark")["used"] == 3
        assert st.quota_state("cloud_subtitle")["used"] == 3
    print("✅ 独立计次：转码用尽不影响去水印/字幕（各自 3 次/日独立扣）")


def test_member_quota_200():
    with tempfile.TemporaryDirectory() as d:
        _patch_stores(d)
        st = MembershipStore(path=Path(d) / "u_member.json")
        st.activate("download_month")
        q = st.quota_state("cloud_convert")
        assert q["limit"] == 200 and q["tier"] == "member", q
        r = st.use_daily("cloud_convert", n=1)
        assert r.get("ok") and r["remaining"] == 199, r
    print("✅ 会员视频转码 200 次/日（activate 后档位切换生效）")


# --------------------------------------------------------------------------- #
# 3+4. gate/count（cn 本地路径）：匿名 403 / 超限 402 / 成功创建后计数
# --------------------------------------------------------------------------- #
def test_gate_local_paths():
    with tempfile.TemporaryDirectory() as d:
        store = _patch_stores(d)
        # 匿名 → 403 引导登录
        raised = None
        try:
            A.cloud_quota_gate(_StubRequest(), resource="cloud_convert")
        except A.HTTPException as e:
            raised = e
        assert raised is not None and raised.status_code == 403, f"匿名应 403，实为 {raised}"
        assert "登录" in raised.detail, "403 文案必须引导登录"
        # 登录 + 未超限 → 放行 local
        from auth_store import issue_token
        token = issue_token("u_cloud_gate")
        req = _StubRequest(auth=f"Bearer {token}")
        gate = A.cloud_quota_gate(req, resource="cloud_convert")
        assert gate["mode"] == "local" and gate.get("store") is not None, gate
        # 计数 3 次后再 gate → 402（预检挡在任务创建前）
        user_store = um.get_user_store("u_cloud_gate")
        for _ in range(3):
            assert user_store.use_daily("cloud_convert", n=1)["ok"]
        raised2 = None
        try:
            A.cloud_quota_gate(req, resource="cloud_convert")
        except A.HTTPException as e:
            raised2 = e
        assert raised2 is not None and raised2.status_code == 402, f"超限应 402，实为 {raised2}"
        assert "视频转码" in raised2.detail, raised2.detail
        # count 失败吞掉（store 炸了不回滚不抛）
        gate_broken = {"mode": "local", "store": None, "resource": "cloud_convert"}
        orig = A.current_member_store
        A.current_member_store = lambda r: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            A.cloud_quota_count(req, gate_broken, n=1)  # 不应抛
        finally:
            A.current_member_store = orig
        assert store.quota_state("cloud_convert")["used"] == 0, "匿名池不应被污染"
    print("✅ gate 本地路径：匿名 403 / 超限 402（创建前拦截，文案含功能名）/ count 异常吞掉")


# --------------------------------------------------------------------------- #
# 5. relay（global 节点）路径
# --------------------------------------------------------------------------- #
def test_relay_mode():
    with tempfile.TemporaryDirectory() as d:
        _patch_stores(d)
        assert A.NODE_REGION != "cn", "测试环境应为 global 节点语义"
        os.environ["VDL_QUOTA_RELAY_URL"] = "http://cn-relay:18890"
        orig_relay = A._cloud_quota_relay
        try:
            # cn 判 MEMBER_QUOTA → 402
            A._cloud_quota_relay = lambda req, p: {
                "ok": False, "code": "MEMBER_QUOTA", "error": "今日免费云端处理次数已用尽"}
            raised = None
            try:
                A.cloud_quota_gate(_StubRequest())
            except A.HTTPException as e:
                raised = e
            assert raised is not None and raised.status_code == 402, raised
            # cn 判 NO_AUTH → 403
            A._cloud_quota_relay = lambda req, p: {
                "ok": False, "code": "NO_AUTH", "error": "请先登录账号"}
            raised = None
            try:
                A.cloud_quota_gate(_StubRequest())
            except A.HTTPException as e:
                raised = e
            assert raised is not None and raised.status_code == 403, raised
            # cn 不可达（None）→ fail-open relay 放行
            A._cloud_quota_relay = lambda req, p: None
            gate = A.cloud_quota_gate(_StubRequest())
            assert gate["mode"] == "relay", gate
            # 计数回派炸了 → 吞掉不抛
            def _boom(req, p):
                raise RuntimeError("network down")
            A._cloud_quota_relay = _boom
            A.cloud_quota_count(_StubRequest(), gate, n=1)  # 不应抛
        finally:
            A._cloud_quota_relay = orig_relay
            os.environ.pop("VDL_QUOTA_RELAY_URL", None)
    print("✅ relay：超限透传 402 / 匿名 403 / cn 不可达 fail-open / 计数失败吞掉")


# --------------------------------------------------------------------------- #
# 6. 会员引擎异常 fail-open（本地路径）
# --------------------------------------------------------------------------- #
def test_engine_error_fail_open():
    with tempfile.TemporaryDirectory() as d:
        _patch_stores(d)
        from auth_store import issue_token
        token = issue_token("u_cloud_failopen")

        def _boom():
            raise RuntimeError("disk on fire")
        orig = A.current_member_store
        A.current_member_store = lambda req: _boom()
        try:
            gate = A.cloud_quota_gate(_StubRequest(auth=f"Bearer {token}"))
            assert gate["mode"] == "local" and gate.get("store") is None, f"应 fail-open: {gate}"
        finally:
            A.current_member_store = orig
    print("✅ 会员引擎异常 fail-open：不挡已登录用户的云端功能")


# --------------------------------------------------------------------------- #
# 7. 紧急停用开关
# --------------------------------------------------------------------------- #
def test_kill_switch():
    orig = A._CLOUD_QUOTA_OFF
    try:
        A._CLOUD_QUOTA_OFF = True
        gate = A.cloud_quota_gate(_StubRequest())
        assert gate["mode"] == "off", gate
        A.cloud_quota_count(_StubRequest(), gate, n=1)  # off 模式不计数
    finally:
        A._CLOUD_QUOTA_OFF = orig
    print("✅ VDL_CLOUD_QUOTA_OFF 开关：mode=off 全放行不计数")


if __name__ == "__main__":
    test_free_quota_three_per_day()
    test_resources_are_independent()
    test_member_quota_200()
    test_gate_local_paths()
    test_relay_mode()
    test_engine_error_fail_open()
    test_kill_switch()
    print("\n全部通过 ✅（云端处理配额墙：表/独立计次/文案/gate/count/relay/fail-open/开关 共 7 例）")
