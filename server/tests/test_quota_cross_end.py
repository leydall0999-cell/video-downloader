"""跨端云端免费额度记账守卫（2026-10-06）。

🔴 这个守卫钉住的是一个真实变现漏洞：桌面端原先把「终身 3 次免费云端额度」
记在**本机** `~/.video-downloader/quota.json`，授权中心完全不知道 ⇒ 免费用户
重装系统 / 换台电脑 → 计数归零 → 又白嫖 3 次真实云端大模型调用
（commentary_llm 单次真实成本 ¥0.04~0.19），且可无限重复。

修复口径（用户 2026-10-06 拍板）：
  · 保留 3 次，改成**跨端共享**（中心是唯一真源，本机降级为缓存）
  · 断网 / 中心不可达 → **fail-open**（沿用本机计数，不误伤已付费与断网用户）

本文件用假 center（内存里的 HTTP 替身）验证两侧语义，不发真实网络请求。
中心侧实现本身的原子性/退还/日切由 /tmp/vdl_center/test_cloud_quota_center.py
与线上真实 HTTP 验证覆盖。

运行：python3 test_quota_cross_end.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SERVER = _HERE.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

import quota  # noqa: E402  (路径插入后导入，读取 DEFAULT_CLOUD_RESOURCE)

# 🔴 隔离真实 plans.json：生产环境把 daily_auto 配成了 0（共享护栏已关），
# 否则 daily_auto_remaining() 恒为 0，本用例「次日恢复」的前提不成立。
# 指向一个无 plans.json 的临时目录 → cloud_quota_limits 落回代码默认 daily_auto=1。
os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_cq_datadir_")


class FakeCenter:
    """授权中心 cloud_quota 端点的内存替身。

    只实现客户端真正依赖的契约：
      · 扣减原子（超限 → allowed=False 且不落账）
      · 退还 clamp 到 0
      · 北京时间日切（daily 归零、lifetime 保留）
      · 账号隔离
    """

    LIFETIME_LIMIT = 3
    DAILY_LIMIT = 1

    def __init__(self, day: str = "2026-10-06"):
        self.users: dict[str, dict] = {}
        self.day = day
        self.calls = 0

    def _acct(self, tok: str) -> dict:
        return self.users.setdefault(tok, {"lifetime": 0, "daily": 0, "day": self.day})

    def _roll(self, a: dict) -> None:
        if a["day"] != self.day:
            a["day"] = self.day
            a["daily"] = 0

    def post(self, token: str, lifetime: int = 0, daily: int = 0,
             refund: bool = False, _raise: bool = False, **kw) -> dict:
        """模拟 license_client.cloud_quota_remote。_raise=True 模拟网络异常。

        🔴 2026-10-06 拆池：客户端现在会多传 `resource`（按功能拆键），本替身只跟踪
        共享单池（所有用例都用 cloud_commentary），故用 **kw 吸收并忽略 resource。
        """
        self.calls += 1
        if _raise:
            raise OSError("network down")
        if not token:
            return {"ok": False, "error": "NO_TOKEN"}
        a = self._acct(token)
        self._roll(a)
        life_n = -abs(lifetime) if refund else int(lifetime)
        day_n = -abs(daily) if refund else int(daily)

        if life_n > 0 and a["lifetime"] + life_n > self.LIFETIME_LIMIT:
            return {"ok": True, "allowed": False, "reason": "lifetime_exhausted",
                    "cloud_quota": self._view(a)}
        if day_n > 0 and a["daily"] + day_n > self.DAILY_LIMIT:
            return {"ok": True, "allowed": False, "reason": "daily_auto_exhausted",
                    "cloud_quota": self._view(a)}

        a["lifetime"] = max(0, a["lifetime"] + life_n)
        a["daily"] = max(0, a["daily"] + day_n)
        return {"ok": True, "allowed": True, "refunded": bool(refund),
                "cloud_quota": self._view(a)}

    def _view(self, a: dict) -> dict:
        return {"lifetime_used": a["lifetime"], "lifetime_limit": self.LIFETIME_LIMIT,
                "lifetime_remaining": max(0, self.LIFETIME_LIMIT - a["lifetime"]),
                "daily_auto_used": a["daily"], "daily_auto_limit": self.DAILY_LIMIT,
                "daily_auto_remaining": max(0, self.DAILY_LIMIT - a["daily"]),
                "date": self.day}


def _mgr(base: str, token_fn=None, is_member: bool = False, now_fn=None):
    """构造注入假中心的 QuotaManager（monkeypatch license_client）。"""
    import quota
    import license_client

    def _fake_post(tok, lifetime=0, daily=0, refund=False, **kw):
        return CENTER.post(tok, lifetime=lifetime, daily=daily, refund=refund,
                           _raise=kw.pop("_raise", False))

    license_client.cloud_quota_remote = _fake_post
    return quota.QuotaManager(base_dir=base, is_member_fn=lambda: is_member,
                              token_fn=token_fn or (lambda: "TOK-A"),
                              now_fn=now_fn)


CENTER: FakeCenter = FakeCenter()          # 由各用例重置
TOK = {"A": "TOK-A", "B": "TOK-B"}


def _fresh() -> str:
    """干净的 base_dir + 全新中心状态。

    🔴 必须同时清 Keychain 影子：它是**系统级**的、不随临时目录销毁，
    会跨用例泄漏（上一用例用满 3 次 → 下一用例一开始就 remaining=0）。
    这与 credential_store 当年把真实账号 token 写进 Keychain 是同一类事故。
    """
    global CENTER
    CENTER = FakeCenter()
    _cleanup_keychain()
    d = tempfile.mkdtemp(prefix="vdl_cq_")
    _mgr(d)                                  # 触发 monkeypatch 绑定到新 CENTER
    return d


def _used(d: str) -> tuple[int, int]:
    """读本机 quota.json 计数。文件不存在 = 从未消费过（= 0）。

    ⚠️ 必须在缺文件时返回 0 而不是抛错：这正是原漏洞的场景 —— 新端/重装后
    本机根本没有 quota.json，计数天然归零。

    🔴 2026-10-06 拆池：终身计数已按功能拆进 `cloud_lifetime` 字典，这里读
    `cloud_commentary`（桌面唯一云端功能，consume_cloud_event 默认 resource）。
    """
    p = Path(d) / "quota.json"
    if not p.exists():
        return 0, 0
    st = json.loads(p.read_text(encoding="utf-8"))
    cl = st.get("cloud_lifetime") or {}
    return int(cl.get(quota.DEFAULT_CLOUD_RESOURCE, 0)), int(st.get("daily_auto_used", 0))


def _cleanup_keychain():
    """删掉本测试写入的 Keychain 影子条目（service 带 .test 后缀）。

    Keychain 是**系统级**的，不随临时目录销毁 —— 不清理会污染后续测试，
    也会在用户机器上留垃圾条目。
    """
    import subprocess as _sp
    import quota as _Q
    try:
        _sp.run(["security", "delete-generic-password", "-s", _Q._keychain_service(),
                 "-a", _Q._KEYCHAIN_ACCOUNT], capture_output=True, timeout=5)
    except Exception:
        pass


# ── 用例 ───────────────────────────────────────────────────────────────── #
def test_cloud_is_authoritative_across_ends():
    """核心回归：A 端用满 3 次后，「另一端」（同账号新会话）必须被拒。

    这正是原漏洞：计数只在本机，换端即归零。修复后中心记账，跨端共享同一份。
    """
    d = _fresh()
    q = _mgr(d)
    for _ in range(3):
        assert q.consume_cloud_event() is True
    assert CENTER.users[TOK["A"]]["lifetime"] == 3

    # 模拟「换电脑」：全新 base_dir（本机计数归零）+ 同一账号 token
    d2 = tempfile.mkdtemp(prefix="vdl_cq_other_")
    q2 = _mgr(d2)
    assert _used(d2)[0] == 0, "新端本机计数确实是 0（原漏洞前提）"
    assert q2.consume_cloud_event() is False, "新端必须被中心拒绝（漏洞已堵）"
    assert CENTER.users[TOK["A"]]["lifetime"] == 3, "被拒时中心计数不得变化"


def test_local_file_is_cache_not_source():
    """本机 quota.json 降级为缓存：中心是真源，本机被覆盖回灌。"""
    d = _fresh()
    # 伪造「本机计数比中心大」的篡改状态
    Path(d, "quota.json").write_text(json.dumps(
        {"cloud_lifetime": {quota.DEFAULT_CLOUD_RESOURCE: 0}, "daily_auto_used": 0},
        ensure_ascii=False), encoding="utf-8")
    q = _mgr(d)
    for _ in range(2):
        q.consume_cloud_event()
    life, _ = _used(d)
    assert life == 2, "本机应跟随中心（2 次）"
    # 直接把本机改回 0（模拟手改 JSON），下次消费应被中心纠正回 2→3
    st = json.loads(Path(d, "quota.json").read_text(encoding="utf-8"))
    st.setdefault("cloud_lifetime", {})[quota.DEFAULT_CLOUD_RESOURCE] = 0
    Path(d, "quota.json").write_text(json.dumps(st), encoding="utf-8")
    assert q.consume_cloud_event() is True
    assert _used(d)[0] == 3, "中心说 2 → 扣成 3，本机被纠正（覆盖而非累加本机脏值）"


def test_failopen_when_center_unreachable():
    """断网 fail-open：沿用本机计数，绝不把用户误伤（旧口径一致）。"""
    d = _fresh()
    import license_client

    def _boom(*a, **kw):
        raise OSError("network down")

    license_client.cloud_quota_remote = _boom
    import importlib
    import quota
    importlib.reload(quota) if False else None
    q = quota.QuotaManager(base_dir=d, is_member_fn=lambda: False,
                           token_fn=lambda: TOK["A"])
    for i in range(3):
        assert q.consume_cloud_event() is True, f"断网第{i+1}次应放行"
    assert q.consume_cloud_event() is False, "断网用满 3 次后也应拒绝（本地计数生效）"
    assert _used(d)[0] == 3


def test_failopen_when_not_logged_in():
    """未登录云端账号（本机专属用户）→ fail-open 走本机，不被中心拦。"""
    d = _fresh()
    q = _mgr(d, token_fn=lambda: "")          # 无 token
    assert CENTER.calls == 0, "无 token 不该打网络（省一次无谓请求）"
    for _ in range(3):
        assert q.consume_cloud_event() is True
    assert q.consume_cloud_event() is False


def test_refund_goes_to_center():
    """失败退还必须同步中心，否则用户换端后额度被永久扣光。"""
    d = _fresh()
    q = _mgr(d)
    q.consume_cloud_event()
    assert CENTER.users[TOK["A"]]["lifetime"] == 1
    q.refund(lifetime=1)
    assert CENTER.users[TOK["A"]]["lifetime"] == 0, "退还已同步到中心"
    assert _used(d)[0] == 0, "本机缓存也回到 0"

    # 退还后另一端应能再扣
    d2 = tempfile.mkdtemp(prefix="vdl_cq_refund_")
    q2 = _mgr(d2)
    assert q2.consume_cloud_event() is True, "退还后跨端可再扣（退还真的生效了）"


def test_accounts_isolated():
    """账号隔离：B 账号不受 A 账号耗尽影响（不能串号）。

    ⚠️ A / B 必须是**各自独立的 base_dir** —— 一台机器同一时刻只有一个云端
    账号，本机缓存目录天然是单账号的。若让两个 token 共用同一目录，本机缓存
    会互相覆盖，那是测试自造的不可能场景，不是产品缺陷。
    """
    dA = _fresh()
    qa = _mgr(dA, token_fn=lambda: TOK["A"])
    for _ in range(3):
        assert qa.consume_cloud_event() is True
    assert CENTER.users[TOK["A"]]["lifetime"] == 3

    dB = tempfile.mkdtemp(prefix="vdl_cq_b_")
    qb = _mgr(dB, token_fn=lambda: TOK["B"])
    assert _used(dB)[0] == 0
    assert qb.consume_cloud_event() is True, "B 不受 A 影响"
    assert CENTER.users[TOK["B"]]["lifetime"] == 1, "B 中心计数独立=1"
    assert _used(dB)[0] == 1, "B 本机缓存显示自己的用量"


def test_daily_auto_shared_and_rolls_over():
    """每日 auto 额度也跨端共享，且按日切重置。"""
    d = _fresh()
    q = _mgr(d)
    assert q.consume_daily_auto() is True
    assert q.consume_daily_auto() is False, "同日第二次被拒"
    # 换端：另一端当天也应被拒（跨端共享同一份每日额度）
    d2 = tempfile.mkdtemp(prefix="vdl_cq_daily_")
    q2 = _mgr(d2)
    assert q2.consume_daily_auto() is False, "跨端共享每日额度（当天已用）"
    assert _used(d2)[1] == 1, "被拒时本机缓存已回灌为中心值（当天 1 次）"

    # 换日：中心日切 → 恢复放行。
    # ⚠️ 必须用**同一个 q2**（本机缓存里存着昨天的 daily_date），才能验证
    # 「本机惰性日切」与「中心日切」不打架。
    CENTER.day = "2026-10-07"
    assert q2.consume_daily_auto() is True, "次日恢复"
    assert CENTER.users[TOK["A"]]["daily"] == 1, "中心次日重置后记 1 次"
    # 本机以「云端下发的日切日期」为准重置：计数回到 0（当日第 1 次还没在本机记）。
    # 断言 remaining 而不是绝对计数 —— 本机时区与云端北京日界可能差一天，
    # 记的绝对值会在 0/1 间跳，但「今日可用」这个语义恒为 1。
    assert q2.daily_auto_remaining() == 1, "次日本机显示今日可用 1 次"


def test_member_bypasses_center():
    """会员不受云端额度限制，也不该白打中心（is_member 短路在最前）。"""
    d = _fresh()
    q = _mgr(d, is_member=True)
    for _ in range(10):
        assert q.consume_cloud_event() is True
    assert q.consume_daily_auto() is True
    assert CENTER.calls == 0, "会员完全不打中心"
    assert _used(d)[0] == 0, "会员不落本机计数"


def test_concurrent_consume_never_exceeds_limit():
    """并发扣减不许超发：中心原子 + 本机 flock 双保险，最多放行 3 次。"""
    d = _fresh()
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        q = _mgr(d)                            # 每请求新建实例（真实形态）
        ok = q.consume_cloud_event()
        with lock:
            results.append(ok)

    ths = [threading.Thread(target=worker) for _ in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert sum(1 for r in results if r) == 3, f"只应放行 3 次，实际 {sum(results)}"
    assert CENTER.users[TOK["A"]]["lifetime"] == 3, "中心计数=3，未超发"


def test_stale_local_cache_never_falsely_denies():
    """🔴 防「假拒回归」：本机缓存显示已用尽，但中心仍有余量时，必须放行。

    这条钉住的是 consume_* 里**本地前置检查的位置**：早期版本在函数开头就
    `if remaining() <= 0: return False`，于是本机一旦记满（哪怕中心还有余额，
    或只是跨了日切边界），就直接拒绝而**根本不问中心**。

    两种真实触发场景：
      ① 日切：本地 daily_date 停在昨天 → 次日本机算出 remaining=0，中心已放行；
      ② 另一台机器用掉了额度又退回，本机仍是旧的满值。
    """
    d = _fresh()
    q = _mgr(d)
    # 本机记满终身 3 次
    for _ in range(3):
        assert q.consume_cloud_event() is True
    # 场景②：中心退还 2 次（余额回到 2），但本机缓存仍停在 3（已用尽）
    q.refund(lifetime=2)
    # 强制本机回到「已用尽」的脏状态（模拟旧缓存 / 手改 JSON）
    st = json.loads(Path(d, "quota.json").read_text(encoding="utf-8"))
    st.setdefault("cloud_lifetime", {})[quota.DEFAULT_CLOUD_RESOURCE] = 3
    Path(d, "quota.json").write_text(json.dumps(st), encoding="utf-8")
    assert q.lifetime_cloud_remaining() == 0, "本机确实显示用尽"

    # 关键：中心还有 2 次，本机不该把它当拒绝依据
    assert q.consume_cloud_event() is True, "本机脏缓存不得造成假拒（须问中心）"
    # 中心真值：退款后 used=1 → 这次扣到 2；本机被覆盖回 2（不是本机脏值 3+1）
    assert CENTER.users[TOK["A"]]["lifetime"] == 2, "中心计数=2"
    assert _used(d)[0] == 2, "本机已被中心真值纠正（3 → 2）"

    # 场景①：日切边界 —— 本机 daily_date 停在昨天，中心已日切
    d2 = tempfile.mkdtemp(prefix="vdl_cq_stale_day_")
    q2 = _mgr(d2)
    assert q2.consume_daily_auto() is True
    # 手工把本机日切日期改成昨天、计数改满（伪造过期脏状态）
    st2 = json.loads(Path(d2, "quota.json").read_text(encoding="utf-8"))
    st2["daily_date"] = "2026-10-05"
    st2["daily_auto_used"] = 1
    Path(d2, "quota.json").write_text(json.dumps(st2), encoding="utf-8")
    CENTER.day = "2026-10-07"
    assert q2.consume_daily_auto() is True, "跨日脏缓存不得造成假拒"


def test_shadow_count_survives_file_deletion():
    """🔴 防「删文件重置」：Keychain 影子计数必须让 quota.json 可删也不可重置。

    实测过的口子（修复前）：联网把 3 次用满（中心记 3）→ 断网 → 删掉
    quota.json → 计数归零 → 又白嫖 3 次。断网时没有中心可问，本机文件是
    唯一依据，而它是**可删的普通文件**。

    本用例用真实 Keychain（service 带 .test 后缀，与真实条目隔离）跑完整链路：
    用满 → 删文件 → 断网 → 仍必须拒绝。
    """
    d = _fresh()
    q = _mgr(d)
    for _ in range(3):
        assert q.consume_cloud_event() is True
    assert q.lifetime_cloud_remaining() == 0

    # 断网 + 删掉 quota.json（模拟重装系统 / 手动清理）
    import license_client

    def _boom(*a, **k):
        raise OSError("network down")
    license_client.cloud_quota_remote = _boom
    os.remove(os.path.join(d, "quota.json"))
    assert not os.path.exists(os.path.join(d, "quota.json")), "文件确已删除"

    q2 = _mgr(d)
    assert q2.lifetime_cloud_remaining() == 0, "影子计数应让 remaining 仍为 0"
    assert q2.consume_cloud_event() is False, "删文件 + 断网也不得放行（白嫖口子已堵）"
    _cleanup_keychain()


def test_shadow_uses_max_not_trust():
    """影子与文件取**较大值**：手改小文件不能把已用额度改回来。"""
    d = _fresh()
    q = _mgr(d)
    for _ in range(2):
        assert q.consume_cloud_event() is True
    # 手动把本机改小（篡改尝试）
    p = os.path.join(d, "quota.json")
    st = json.loads(open(p, encoding="utf-8").read())
    st.setdefault("cloud_lifetime", {})[quota.DEFAULT_CLOUD_RESOURCE] = 0
    open(p, "w", encoding="utf-8").write(json.dumps(st))
    q2 = _mgr(d)
    assert q2.lifetime_cloud_remaining() == 1, "取较大值：仍认已用 2 次，不被改回 3"
    _cleanup_keychain()


def test_keychain_unavailable_is_soft():
    """Keychain 不可用时**静默回退**纯文件口径，绝不让加固失败变成功能不可用。"""
    d = _fresh()
    import quota as Q
    orig_read, orig_write = Q._keychain_read, Q._keychain_write
    try:
        Q._keychain_read = lambda: None      # 模拟非 macOS / 条目不存在
        Q._keychain_write = lambda st: None
        q = _mgr(d)
        for i in range(3):
            assert q.consume_cloud_event() is True, f"回退模式下第{i+1}次应放行"
        assert q.consume_cloud_event() is False, "回退模式下仍受本机 3 次限制"
    finally:
        Q._keychain_read, Q._keychain_write = orig_read, orig_write
        _cleanup_keychain()


def test_both_copies_byte_identical():
    """server/quota.py 与管线 scripts/quota.py 必须逐字节同源。

    管线侧扣额度、子进程退款都读它那份；不同源就会出现「父进程退了一次、
    子进程那份没退」或口径漂移。
    """
    import subprocess
    pipe = Path("/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline/scripts/quota.py")
    if not pipe.exists():
        print("     (跳过：管线副本不在本机)")
        return
    a = ( _SERVER / "quota.py").read_bytes()
    b = pipe.read_bytes()
    assert a == b, "两份 quota.py 不再同源，必须 cp server/quota.py 过去"


def test_pipeline_injects_cloud_token():
    """管线必须读 VDL_CLOUD_TOKEN 注入 token，否则独立进程拿不到云端身份。"""
    llm = Path("/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline/scripts/llm_script.py")
    if not llm.exists():
        print("     (跳过：管线不在本机)")
        return
    src = llm.read_text(encoding="utf-8")
    assert "VDL_CLOUD_TOKEN" in src, "llm_script.py 未读取 VDL_CLOUD_TOKEN"
    assert "set_token_fn" in src, "llm_script.py 未调用 set_token_fn（token 注入断链）"
    # server 端也必须注入这个 env
    app_py = (_SERVER / "app.py").read_text(encoding="utf-8")
    assert 'run_env["VDL_CLOUD_TOKEN"]' in app_py, "app.py 未把 token 注入子进程 env"


def test_login_syncs_cloud_balance():
    """登录后必须回灌本机缓存，否则面板显示「还剩 3 次」= 用户以为没修。"""
    src = (_SERVER / "routers" / "cloud_account.py").read_text(encoding="utf-8")
    assert "sync_from_cloud" in src, "_after_login 未挂云端额度回灌"


def main():
    tests = [
        test_cloud_is_authoritative_across_ends,
        test_local_file_is_cache_not_source,
        test_failopen_when_center_unreachable,
        test_failopen_when_not_logged_in,
        test_refund_goes_to_center,
        test_accounts_isolated,
        test_daily_auto_shared_and_rolls_over,
        test_daily_auto_is_per_resource_and_stable,
        test_center_limit_override_corrects_false_reject,
        test_member_bypasses_center,
        test_concurrent_consume_never_exceeds_limit,
        test_stale_local_cache_never_falsely_denies,
        test_shadow_count_survives_file_deletion,
        test_shadow_uses_max_not_trust,
        test_keychain_unavailable_is_soft,
        test_both_copies_byte_identical,
        test_pipeline_injects_cloud_token,
        test_login_syncs_cloud_balance,
    ]
    for t in tests:
        t()
        print(f"  ✅ {t.__name__}")
    print(f"✅ 跨端云端额度守卫全过（{len(tests)} 项）")


def test_daily_auto_is_per_resource_and_stable():
    """🔴 2026-10-06 自查：daily_auto 必须 per-resource，且**反复重读不漂移**。

    背景：中心把每日 auto 改成 per-resource 后，客户端若仍存标量，就会把
    「8 个键的总和」当成「某一个功能今天用了几次」⇒ 管理员把 daily_auto 配成
    ≥1 时，任一功能用过一次就会让**所有**功能假拒（实测复现）。
    另一个已踩的坑：`daily_auto_used` 是派生总和，若拿它做迁移会反过来冲掉刚写好的
    per-resource 字典（转码用 1 次后重读，cloud_commentary 变成 1）。
    """
    q = _mgr(_fresh())
    import time as _t
    import quota as _q
    today = _t.strftime("%Y-%m-%d")
    # 中心回灌：两个功能各用 1 次
    view = {"daily": {"cloud_convert": 1, "cloud_subtitle_burn": 1},
            "daily_auto_used": 2, "date": today,
            "lifetime": {r: 0 for r in _q.CLOUD_RESOURCES}}
    assert q._apply_cloud_view(view, "cloud_convert")
    da = q._state()["daily_auto"]
    assert da["cloud_convert"] == 1 and da["cloud_subtitle_burn"] == 1, da
    assert da["cloud_commentary"] == 0, f"未使用的功能不该有计数: {da}"
    # 反复重读必须稳定（迁移优先级：字典 > 标量）
    for i in range(5):
        assert q._state()["daily_auto"] == da, f"第 {i + 1} 次重读漂移"
    print("✅ daily_auto per-resource，反复重读不漂移")


def test_center_limit_override_corrects_false_reject():
    """🔴 2026-10-06 覆盖层引入的假拒：中心放宽上限后本机必须跟着纠正。

    管理员在**另一台机器**把某功能的终身次数从 3 放宽到 5，本机 plans.json 仍是
    旧值、本机已用量 3 ⇒ 只读本机会判 0 → 用户明明还剩 2 次却被拒。
    修法：`lifetime_cloud_remaining(confirm=True)` 在**将被拒的那一刻**问一次中心
    （平时 remaining>0 零网络开销），并把中心的 lifetime/lifetime_limits 回灌本机。

    同时钉住两条边界：
      · 中心不可达 → 保持本机判定（不凭空放行，与改动前一致，不是回归）
      · 回灌后本机缓存即真值 → 后续调用 confirm=False 也应得到正确余量
    """
    import time as _t
    import quota as _q
    RES = "cloud_commentary"
    today = _t.strftime("%Y-%m-%d")
    d = _fresh()
    p = Path(d) / "quota.json"
    p.write_text(json.dumps({
        "cloud_lifetime": {r: (3 if r == RES else 0) for r in _q.CLOUD_RESOURCES},
        "daily_auto": {r: 0 for r in _q.CLOUD_RESOURCES},
        "daily_auto_used": 0, "daily_date": today,
    }), encoding="utf-8")
    q = _mgr(d)

    # ① 本机旧口径判 0
    assert q.lifetime_cloud_remaining(RES, confirm=False) == 0, "本机口径应判 0"

    # ② 中心不可达 → 不得凭空放行（fail-safe，不是 fail-open）
    orig = q._cloud
    q._cloud = lambda **kw: None
    assert q.lifetime_cloud_remaining(RES) == 0, "中心不可达时不该放行"

    # ③ 中心放宽到 5 → 纠正为 2（假拒修复）
    cq = {"lifetime": {r: (3 if r == RES else 0) for r in _q.CLOUD_RESOURCES},
          "lifetime_limits": {r: (5 if r == RES else 3) for r in _q.CLOUD_RESOURCES},
          "date": today}
    q._cloud = lambda **kw: (q._apply_cloud_view(cq, RES), cq)[1]
    assert q.lifetime_cloud_remaining(RES) == 2, "中心放宽后假拒未纠正"

    # ④ 回灌落缓存 → 之后 confirm=False 也是真值
    q._cloud = orig
    assert q.lifetime_cloud_remaining(RES, confirm=False) == 2, "中心真值未落本机缓存"

    # ⑤ 别的功能不被连累
    assert q.lifetime_cloud_remaining("cloud_convert", confirm=False) == 3
    print("✅ 中心覆盖值纠正假拒：不可达不回归 / 纠正生效 / 落缓存 / 不连累")

if __name__ == "__main__":
    main()
