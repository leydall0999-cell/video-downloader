"""会员 store 决议隔离回归测试 —— Fix C（2026-10-04）。

背景（Root C）：`current_member_store(request)` 在「当前登录用户自己没有权益、
而全局匿名 store 有云端买的会员/积分」时，会回退到全局 store。这本是为「账号制把
权益落在全局」设计的善意兜底；但它**没有校验归属** —— 全局 store 的 meta.account.email
若属于上一个登录账号，当前账号就会读到别人的会员/积分（跨账号泄漏）。

真实事故（用户截图）：桌面端显示「会员到 2030 + 400 积分」，但云端对 181 号实为 0；
那 400/2030 其实是上一个账号 15014313254 的全局账本，被 181 号错读到。

Fix C 的契约：回退全局账本前，`gstore.meta.account.email` 必须 == 当前账号的注册邮箱
（经 `user_membership._email_of(uid)` 取），否则一律用该用户自己的 per-user store。

本文件钉死的不变量：
  1. ★ 跨账号隔离：全局账本属于 owner（含 400 积分），other 登录时**绝不得**读到 owner 的权益
     —— 返回 other 自己的 per-user store（空权益）。
  2. 同账号兜底仍生效：owner 登录时，因自己 per-user store 无权益而全局有，应回退到
     全局 store、看到 400 积分（Fix C 只加归属校验，不破坏合法的同账号兜底）。
  3. 映射正确：`_email_of(uid)` 能正确把 uid 解析回各自注册邮箱（否则归属校验恒为真/恒为假）。
  4. ★ 变异确认（断言后会红）：若去掉归属校验（`_email_of` 被改成恒返回全局归属邮箱），
     other 会泄漏 owner 的权益 —— 证明本回归测试确实能抓住 Root C 这类回退。

运行：
    cd server && ../.build_venv/bin/python tests/test_member_store_isolation.py
"""
import os
import sys
import tempfile
import types
import uuid
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 独立于 run_offline_tests.sh 运行时也走临时目录，绝不碰真实家目录
_CREATED_DIR = False
if not os.environ.get("VDL_DATA_DIR"):
    os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_store_iso_test_")
    _CREATED_DIR = True

# 铁律：测试数据目录不能是用户真实目录（会写坏真实会员/账号）
_REAL_DATA_DIR = (Path.home() / ".video-downloader").resolve()
assert Path(os.environ["VDL_DATA_DIR"]).resolve() != _REAL_DATA_DIR, \
    "测试数据目录不能是用户真实目录（会写坏真实会员/账号）"

import auth_store as au          # noqa: E402
import membership as ms          # noqa: E402
import user_membership as um     # noqa: E402


OWNER_EMAIL = f"owner-{uuid.uuid4().hex[:8]}@iso.test"   # 全局账本归属账号（含权益）
OTHER_EMAIL = f"other-{uuid.uuid4().hex[:8]}@iso.test"   # 旁观账号（应被隔离）


def _ensure_account(email: str) -> str:
    """幂等建号：已存在则直接返回其 uid（兼容 run_offline_tests.sh 复用 VDL_DATA_DIR）。"""
    data = au._load_users()
    uid = (data.get("by_identifier") or {}).get(email)
    if uid:
        return uid
    return au.create_user(email, "pw123456")


def _ensure_accounts() -> tuple[str, str]:
    return _ensure_account(OWNER_EMAIL), _ensure_account(OTHER_EMAIL)


def _fake_request(uid: str):
    """造一个带 Bearer token 的伪请求，get_current_user_id 能解出 uid。"""
    token = au.issue_token(uid)
    return types.SimpleNamespace(headers={"Authorization": f"Bearer {token}"})


def _make_global_store() -> "ms.MembershipStore":
    """造一个全局匿名 store：归属 owner 邮箱、含 400 永久积分（模拟「上一个人买的会员」）。"""
    # 独立路径，避免与任何磁盘残留状态耦合
    p = Path(os.environ["VDL_DATA_DIR"]) / f"iso_gstore_{uuid.uuid4().hex[:8]}.json"
    gstore = ms.MembershipStore(path=p)
    gstore._ensure_loaded()                       # 载入空状态
    gstore._state["meta"]["account"] = {"email": OWNER_EMAIL}
    gstore._state["permanent_credits"]["total"] = 400
    # 标记已加载：后续 current_member_store 内的 _ensure_loaded 是 no-op，不会把我注入的
    # 内存态覆盖回磁盘（该路径本就不存在，等价于空；显式置位更稳妥）。
    gstore._loaded = True
    return gstore


def _setup(uid_owner: str) -> "ms.MembershipStore":
    """把一个全局 store 塞进 app.member_store（current_member_store 经 import app 取它）。"""
    gstore = _make_global_store()
    # current_member_store 内部 `import app; app.member_store` —— 桩掉 app，避免离线拉起
    # 真实 app（fastapi 重依赖）；只给它一个 member_store 属性即可。
    sys.modules["app"] = types.SimpleNamespace(member_store=gstore)
    return gstore


def _reset_caches() -> None:
    um._STORE_CACHE.clear()
    um._RESOLVE_CACHE.clear()


# --------------------------------------------------------------------- 1
def test_email_mapping_is_correct():
    """`_email_of(uid)` 能把两个账号各自解析回注册邮箱（归属校验的前提）。"""
    uid_owner, uid_other = _ensure_accounts()
    assert uid_owner and uid_other, "前置失败：测试账号建不出来"

    assert um._email_of(uid_owner) == OWNER_EMAIL, (
        f"owner 的 uid 没映射回 {OWNER_EMAIL}，却得到 {um._email_of(uid_owner)!r}")
    assert um._email_of(uid_other) == OTHER_EMAIL, (
        f"other 的 uid 没映射回 {OTHER_EMAIL}，却得到 {um._email_of(uid_other)!r}")
    print("✅ _email_of 按 uid 正确解析各自注册邮箱（归属校验前提成立）")


# --------------------------------------------------------------------- 2
def test_cross_account_benefit_isolation():
    """★ 核心：全局账本属于 owner，other 登录绝不得读到 owner 的 400 积分（防跨账号泄漏）。"""
    uid_owner, uid_other = _ensure_accounts()
    gstore = _setup(uid_owner)
    _reset_caches()

    ustore = um.get_user_store(uid_other)          # other 自己的 per-user store（空权益）
    chosen = um.current_member_store(_fake_request(uid_other))

    # 必须返回 other 自己的 store，而不是全局 store
    assert chosen is ustore, (
        "跨账号泄漏：other 登录却拿到了全局 store（owner 的会员/积分）")
    # 且 other 看到的积分必须是 0，而不是泄漏来的 400
    assert chosen.status()["permanent_credits"] == 0, (
        f"跨账号泄漏：other 看到了 {chosen.status()['permanent_credits']} 积分（应为 0）")
    # 反向确认：全局 store 确实带着 400（说明「泄漏路径」客观存在，只是被归属校验挡住）
    assert gstore.status()["permanent_credits"] == 400, "前置异常：全局 store 的 400 积分丢了"
    print("✅ 跨账号隔离：other 登录返回自己的空 store，看不到 owner 的 400 积分（无泄漏）")


# --------------------------------------------------------------------- 3
def test_same_owner_fallback_still_works():
    """同账号兜底仍生效：owner 自己 per-user 无权益、全局有权益时，应回退看到 400。

    证明 Fix C 只加了归属校验，没有破坏「账号制把权益落在全局」这条合法路径。
    """
    uid_owner, uid_other = _ensure_accounts()
    gstore = _setup(uid_owner)
    _reset_caches()

    chosen = um.current_member_store(_fake_request(uid_owner))
    assert chosen is gstore, (
        "同账号兜底失效：owner 登录没回退到自己的全局 store（合法会员权益读不到）")
    assert chosen.status()["permanent_credits"] == 400, (
        f"同账号兜底失效：owner 看到的积分不是 400（实际 {chosen.status()['permanent_credits']}）")
    print("✅ 同账号兜底仍生效：owner 登录回退到自己的全局 store，看到 400 积分")


# --------------------------------------------------------------------- 4
def test_removing_ownership_check_reproduces_leak():
    """★ 变异确认（断言后会红）：若去掉归属校验，other 会泄漏 owner 的权益。

    Fix C 靠 `_email_of(uid)` 让 `same_owner` 在跨账号时为 False 来挡泄漏。这里把
    `_email_of` 桩成「恒返回全局归属邮箱」，模拟「归属校验被删」的最坏情况，断言此时
    other 会拿到全局 store（泄漏）。本用例存在的目的：证明 test 2 不是偶然通过——
    一旦归属校验回退，test 2 会变红，而本用例会先变绿（泄漏复现），二者共同锁死 Root C。
    """
    uid_owner, uid_other = _ensure_accounts()
    gstore = _setup(uid_owner)
    _reset_caches()

    real_email_of = um._email_of
    um._email_of = lambda uid: OWNER_EMAIL          # 模拟「归属校验被去掉」
    try:
        chosen = um.current_member_store(_fake_request(uid_other))
        assert chosen is gstore, (
            "变异不符预期：去掉归属校验后 other 仍没泄漏 —— 测试无法证明能抓住 Root C")
    finally:
        um._email_of = real_email_of                 # 必须恢复，避免污染其它用例
    print("✅ 变异确认：去掉归属校验后 other 立即泄漏 owner 权益 → 回归测试能有效抓住回退")


if __name__ == "__main__":
    test_email_mapping_is_correct()
    test_cross_account_benefit_isolation()
    test_same_owner_fallback_still_works()
    test_removing_ownership_check_reproduces_leak()
    # 清理自己造的临时数据目录（仅当本文件独立运行时创建）
    if _CREATED_DIR:
        import shutil
        shutil.rmtree(os.environ["VDL_DATA_DIR"], ignore_errors=True)
        assert not Path(os.environ["VDL_DATA_DIR"]).exists(), "临时数据目录没清掉（静默失败会留垃圾）"
    else:
        print(f"（数据目录由调用方提供，保留不动：{os.environ['VDL_DATA_DIR']}）")
    print("\n🎉 会员 store 决议隔离测试全部通过（4 项，含变异确认）")
