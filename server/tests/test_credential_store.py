"""server/tests/test_credential_store.py —— 账号 token 凭据存储回归测试（离线）。

钉死三件事：
1. token 优先存 Keychain；读回一致。
2. **磁盘 JSON 里不再有明文 token**（这是本加固的核心目的 —— 拷走文件也用不了），
   但内存态仍有 token（否则 6 处 `acc.get("token")` 读取点会全部掉登录）。
3. 重新加载（模拟 App 重启）能把 token 从 Keychain 注回内存。

⚠️ 会真实写 macOS Keychain（用一次性测试账号，结尾必清理）；数据目录走
VDL_DATA_DIR 隔离，绝不写真实家目录。
"""
import os
import sys
import tempfile

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

# 一次性测试账号：用不可能与真实用户冲突的域名
TEST_EMAIL = "vdl-credtest-do-not-use@invalid.local"
TEST_TOKEN = "tok-cred-test-123456"


def _isolate():
    tmp = tempfile.mkdtemp(prefix="vdl_cred_")
    os.environ["VDL_DATA_DIR"] = tmp
    return tmp


def test_set_then_get_roundtrip():
    _isolate()
    import credential_store as cs
    try:
        where = cs.set_token(TEST_EMAIL, TEST_TOKEN)
        assert where in ("keychain", "file"), f"存储失败: {where}"
        got = cs.get_token(TEST_EMAIL)
        assert got == TEST_TOKEN, f"读回不一致: {got!r}"
    finally:
        cs.delete_token(TEST_EMAIL)


def test_resolve_token_reads_keychain_when_json_empty():
    """acc 里没有明文 token 时，按 email 从 Keychain 解析出来。"""
    _isolate()
    import credential_store as cs
    try:
        cs.set_token(TEST_EMAIL, TEST_TOKEN)
        acc = {"email": TEST_EMAIL, "token": ""}
        assert cs.resolve_token(acc) == TEST_TOKEN
    finally:
        cs.delete_token(TEST_EMAIL)


def test_persist_strips_plaintext_but_memory_keeps_token():
    """核心：磁盘不留明文，内存仍有 token。"""
    tmp = _isolate()
    import credential_store as cs
    from membership import MembershipStore
    from pathlib import Path
    p = Path(tmp) / "m.json"
    try:
        cs.set_token(TEST_EMAIL, TEST_TOKEN)
        st = MembershipStore(path=p)
        st.save_account(TEST_EMAIL, TEST_TOKEN, fp="fp-test")
        # 内存态：token 在（各读取点靠它，不能空）
        acc = (st._state.get("meta") or {}).get("account") or {}
        assert acc.get("token") == TEST_TOKEN, "内存态 token 丢失 → 会掉登录"
        # 磁盘：Keychain 态下不得有明文
        raw = p.read_text(encoding="utf-8")
        if acc.get("token_store") == "keychain":
            assert TEST_TOKEN not in raw, "磁盘 JSON 仍含明文 token（加固无效）"
    finally:
        cs.delete_token(TEST_EMAIL)


def test_reload_injects_token_back():
    """模拟 App 重启：新实例从 Keychain 把 token 注回内存。"""
    tmp = _isolate()
    import credential_store as cs
    from membership import MembershipStore
    from pathlib import Path
    p = Path(tmp) / "m.json"
    try:
        cs.set_token(TEST_EMAIL, TEST_TOKEN)
        st = MembershipStore(path=p)
        st.save_account(TEST_EMAIL, TEST_TOKEN, fp="fp-test")
        acc1 = (st._state.get("meta") or {}).get("account") or {}
        if acc1.get("token_store") != "keychain":
            return  # 非 macOS 回落态：无 Keychain 可注入，跳过
        st2 = MembershipStore(path=p)
        st2._ensure_loaded()
        acc2 = (st2._state.get("meta") or {}).get("account") or {}
        assert acc2.get("token") == TEST_TOKEN, "重启后 token 未注回 → 掉登录"
    finally:
        cs.delete_token(TEST_EMAIL)


def test_delete_removes_token():
    _isolate()
    import credential_store as cs
    cs.set_token(TEST_EMAIL, TEST_TOKEN)
    cs.delete_token(TEST_EMAIL)
    assert cs.get_token(TEST_EMAIL) == "", "删除后仍读到 token"


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
        print(f"✅ {fn.__name__}")
    print("ALL CREDENTIAL STORE TESTS PASSED")
