"""P0-2 越权回归测试：下载任务 / 订阅源 的归属隔离。

背景（安全审计 2026-10-11，缺陷 P0-2「下载任务跨用户越权」）：
  · `_require_task(task_id)` 只按 id 取任务、**不校验归属**；
    `/api/tasks` 更是直接 `store.list_all()` 返回**全站**任务。
  · 攻击者只要拿到（或枚举到）6~12 位 task_id，就能读标题、拉成片、
    取消/暂停/重试他人任务；`_commentary_title` 还能借他人 task_id 反查片名。
  · 其它作业类型（convert/compress/sr/subtitle）本就有 device 隔离，唯独下载链缺失。

修复口径：
  · 服务端解出的 uid（HMAC 签名 token）→ `u:<uid>`，客户端无法伪造；
  · 未登录 → `d:<device>`（X-Device-Id / ?device=）；
  · 历史遗留任务（owner_id 为空）保持可见，避免升级后老用户历史凭空消失；
  · 归属不符一律 **404**（不返回 403，避免 task_id 可枚举）。

设计约束：临时目录建 store、不联网、不写用户家目录、monkeypatch 恢复原值。
"""
import sys
import tempfile
import types
from pathlib import Path

_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
import tasks as tasks_mod  # noqa: E402
import subscriptions as subs_mod  # noqa: E402
from routers import subscriptions as subs_router  # noqa: E402


# ---------------------------------------------------------------- 辅助

class _Req:
    """最小请求替身：`_task_owner` 只用到 headers / query_params 的 .get()。"""

    def __init__(self, device: str = "", uid: str | None = None):
        self.headers = {"X-Device-Id": device} if device else {}
        self.query_params = {}
        self._uid = uid

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<Req device={self.headers.get('X-Device-Id')!r} uid={self._uid!r}>"


class _patch:
    """上下文管理器：临时替换一串属性，退出时逐一还原。"""

    def __init__(self, **attrs):
        self._attrs = attrs
        self._orig = {}

    def __enter__(self):
        for k, v in self._attrs.items():
            self._orig[k] = getattr(server_app, k)
            setattr(server_app, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(server_app, k, v)
        return False


def _isolated_stores():
    """在临时目录建独立的 tasks / subscriptions store（不碰真实状态文件）。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_p0_2_"))
    tstore = tasks_mod.TaskStore(tmp / "tasks", state_dir=tmp / "state")
    sstore = subs_mod.SubscriptionStore(tmp / "subs" / ".subscriptions.json")
    return tmp, tstore, sstore


def _mk_task(tstore, title: str, owner: str):
    return tstore.create(url="https://example.com/v", title=title,
                         platform="bilibili", quality="1080p", owner_id=owner)


def _is_404(exc) -> bool:
    return getattr(exc, "status_code", None) == 404


# ---------------------------------------------------------------- _task_owner

def test_task_owner_prefers_server_uid_over_device():
    """已登录时以服务端 uid 为准，客户端伪造的 X-Device-Id 不生效。"""
    with _patch(get_current_user_id=lambda req: "42"):
        # 通过 user_membership 模块解析
        import user_membership
        orig = user_membership.get_current_user_id
        user_membership.get_current_user_id = lambda req: "42"
        try:
            owner = server_app._task_owner(_Req(device="spoofed-device"))
        finally:
            user_membership.get_current_user_id = orig
    assert owner == "u:42", owner


def test_task_owner_falls_back_to_device_when_anonymous():
    with _patch(get_current_user_id=lambda req: None):
        import user_membership
        orig = user_membership.get_current_user_id
        user_membership.get_current_user_id = lambda req: None
        try:
            owner = server_app._task_owner(_Req(device="abc123"))
        finally:
            user_membership.get_current_user_id = orig
    assert owner == "d:abc123", owner


def test_task_owner_empty_when_no_identity():
    import user_membership
    orig = user_membership.get_current_user_id
    user_membership.get_current_user_id = lambda req: None
    try:
        assert server_app._task_owner(_Req()) == ""
    finally:
        user_membership.get_current_user_id = orig


# ---------------------------------------------------------------- 任务归属

def test_owned_task_blocks_cross_device_access():
    """他人任务 → 404；本人任务 → 放行。"""
    tmp, tstore, _ = _isolated_stores()
    with _patch(store=tstore):
        mine = _mk_task(tstore, "我的视频", "d:aaa")
        theirs = _mk_task(tstore, "别人的视频", "d:bbb")

        assert server_app._require_owned_task(mine.id, _Req(device="aaa")).id == mine.id

        try:
            server_app._require_owned_task(theirs.id, _Req(device="aaa"))
            raise AssertionError("跨设备访问他人任务应当被拒绝")
        except server_app.HTTPException as e:
            assert _is_404(e), e


def test_logged_in_user_cannot_reach_device_task():
    """已登录用户（u:*）不能访问匿名设备（d:*）任务。"""
    import user_membership
    tmp, tstore, _ = _isolated_stores()
    orig = user_membership.get_current_user_id
    try:
        with _patch(store=tstore):
            device_task = _mk_task(tstore, "设备任务", "d:aaa")
            user_membership.get_current_user_id = lambda req: "42"
            try:
                server_app._require_owned_task(device_task.id, _Req(uid="42"))
                raise AssertionError("登录用户不应能访问设备桶任务")
            except server_app.HTTPException as e:
                assert _is_404(e), e
    finally:
        user_membership.get_current_user_id = orig


def test_legacy_task_without_owner_still_visible():
    """升级前创建的任务（owner_id 为空）对任何人保持可见。"""
    tmp, tstore, _ = _isolated_stores()
    with _patch(store=tstore):
        legacy = _mk_task(tstore, "历史任务", "")
        assert server_app._require_owned_task(legacy.id, _Req(device="zzz")).id == legacy.id


def test_list_for_owner_filters():
    tmp, tstore, _ = _isolated_stores()
    _mk_task(tstore, "A", "d:aaa")
    _mk_task(tstore, "B", "d:bbb")
    _mk_task(tstore, "legacy", "")
    ids = {t.title for t in tstore.list_for_owner("d:aaa")}
    assert ids == {"A", "legacy"}, ids
    # list_all 仍返回全量（内部视图）
    assert len(tstore.list_all()) == 3


# ---------------------------------------------------------------- 订阅归属

def test_subscription_list_for_owner_and_router_404():
    tmp, _, sstore = _isolated_stores()
    mine = subs_mod.Subscription(
        id="s1", url="https://e.com/1", name="我的", platform="bilibili",
        quality_key="best", quality_label="最好", owner_id="d:aaa")
    theirs = subs_mod.Subscription(
        id="s2", url="https://e.com/2", name="别人的", platform="bilibili",
        quality_key="best", quality_label="最好", owner_id="d:bbb")
    sstore.add(mine)
    sstore.add(theirs)

    assert {s.id for s in sstore.list_for_owner("d:aaa")} == {"s1"}

    with _patch(sub_store=sstore):
        # 本人订阅：可解析
        assert subs_router._owned_or_404("s1", _Req(device="aaa")).id == "s1"
        # 他人订阅：404
        try:
            subs_router._owned_or_404("s2", _Req(device="aaa"))
            raise AssertionError("不应能读取他人订阅")
        except server_app.HTTPException as e:
            assert _is_404(e), e


def test_subscription_legacy_visible():
    tmp, _, sstore = _isolated_stores()
    sstore.add(subs_mod.Subscription(
        id="s9", url="https://e.com/9", name="历史", platform="bilibili",
        quality_key="best", quality_label="最好", owner_id=""))
    assert {s.id for s in sstore.list_for_owner("d:aaa")} == {"s9"}


# ---------------------------------------------------------------- 片名反查

def test_commentary_title_does_not_leak_others_task():
    """借他人 task_id 反查片名：拿不到标题，回退到源文件名，绝不泄漏。"""
    tmp, tstore, _ = _isolated_stores()
    with _patch(store=tstore):
        theirs = _mk_task(tstore, "他人机密片名", "d:bbb")
        payload = types.SimpleNamespace(task_id=theirs.id, title="", file_id="")

        got = server_app._commentary_title(payload, "/movies/我自己.mp4", _Req(device="aaa"))
        assert got != "他人机密片名", got
        assert got == "我自己", got


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
