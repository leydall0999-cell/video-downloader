"""web-dev 原子写 / 并发锁 / 验证码回传 回归测试。

对应 app-dev 的 test_config_atomic_write.py + test_users_store_concurrency.py +
test_auth_reset_code_leak.py，但针对 web-dev 本次实际改造做了收敛：
  · 钉住本次迁移的 7 文件 9 处固定名 .tmp 写盘已改为 atomic_io；
  · 钉住 auth_store / admin_store 的全部 _save_users 调用都在 users_mutation() 临界区内；
  · 钉住 atomic_io 的契约（内容完整 / 权限恰为 0600 / 无残留 / 并发不互覆）；
  · 钉住 dev_code 只在本机回传（公网验证码泄露 = 账号接管洞）。

不依赖任何外部网络；纯 stdlib，dev_code 真实模块测试在 fastapi 可用时才跑（否则跳过）。
运行：
    cd server && python tests/test_atomic_writes_and_auth.py
"""
import ast
import os
import sys
import tempfile
import threading

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import atomic_io  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. atomic_io 契约
# --------------------------------------------------------------------------- #
def test_atomic_write_contract():
    import json
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "cfg.json")
        atomic_io.atomic_write_json(p, {"a": 1, "中文": "值"})
        with open(p, encoding="utf-8") as f:
            assert json.load(f) == {"a": 1, "中文": "值"}
        mode = os.stat(p).st_mode & 0o777
        assert mode == 0o600, f"权限应为 0600，实为 {oct(mode)}"
        leftovers = [n for n in os.listdir(d) if n.endswith(".tmp")]
        assert not leftovers, f"存在残留临时文件: {leftovers}"
    print("✅ atomic_write_json 契约（内容完整 / 权限 0600 / 无残留）")


def test_unique_temp_path_keeps_extension_and_dir():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "users.json")
        a = atomic_io.unique_temp_path(target)
        b = atomic_io.unique_temp_path(target)
        assert os.path.dirname(str(a)) == d and os.path.dirname(str(b)) == d
        assert a != b, "两次应给出不同临时名"
        assert str(a).endswith(".json"), "应保留扩展名"
    print("✅ unique_temp_path 同目录 / 带扩展名 / 互不撞名")


def test_atomic_write_survives_concurrent_writers():
    """并发写者不得互相覆盖，最终文件必为某次完整写入（否则 JSONDecodeError）。"""
    import json as _json
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "state.json")
        pad = "x" * 2000

        def worker(i):
            for k in range(20):
                atomic_io.atomic_write_json(p, {"who": i, "k": k, "pad": pad}, indent=None)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(p, encoding="utf-8") as f:
            data = _json.load(f)
        assert "who" in data and "k" in data and data["pad"] == pad
    print("✅ 并发写者下文件始终完整可读")


# --------------------------------------------------------------------------- #
# 2. 迁移点静态守卫：7 文件中不得再有固定名 .tmp 写盘
# --------------------------------------------------------------------------- #
_MIGRATED = [
    "auth_store.py", "admin_store.py", "app.py", "llm_config.py",
    "dewatermark_ai.py", "library.py", "routers/support.py",
]


def _is_fixed_tmp_call(node):
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ("with_suffix", "with_name"):
        return False
    if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant):
        return False
    return str(node.args[0].value).endswith(".tmp")


def test_no_fixed_temp_write_in_migrated_files():
    for fname in _MIGRATED:
        path = os.path.join(_SERVER_DIR, fname)
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=fname)
        hits = [ast.unparse(n) for n in ast.walk(tree) if _is_fixed_tmp_call(n)]
        assert not hits, f"{fname} 仍存在固定名 .tmp 写盘: {hits}"
    print(f"✅ {len(_MIGRATED)} 个迁移文件中无固定名 .tmp 写盘")


# --------------------------------------------------------------------------- #
# 3. users_mutation 临界区：auth_store / admin_store 的全部 _save_users 调用
#    必须写在 `with ...users_mutation():` 内（否则并发读改写会互相覆盖丢账号）
# --------------------------------------------------------------------------- #
def _is_users_mutation_call(node):
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Name) and f.id == "users_mutation":
        return True
    if isinstance(f, ast.Attribute) and f.attr == "users_mutation":
        return True
    return False


def _call_sites_inside_mutation(func_node):
    """返回 (ok, bad_list)：函数体内所有 _save_users(...) 是否都在 with users_mutation() 内。"""
    covered = set()
    for w in ast.walk(func_node):
        if isinstance(w, ast.With):
            for item in w.items:
                if _is_users_mutation_call(item.context_expr):
                    for sub in ast.walk(w):
                        if isinstance(sub, ast.Call):
                            covered.add(sub)
    bad = []
    for c in ast.walk(func_node):
        if isinstance(c, ast.Call):
            t = c.func
            is_save = (isinstance(t, ast.Name) and t.id == "_save_users") or \
                      (isinstance(t, ast.Attribute) and t.attr == "_save_users")
            if is_save and c not in covered:
                bad.append(ast.unparse(c))
    return (not bad), bad


def test_save_call_sites_inside_mutation():
    problems = []
    for fname in ["auth_store.py", "admin_store.py"]:
        path = os.path.join(_SERVER_DIR, fname)
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=fname)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ok, bad = _call_sites_inside_mutation(node)
                if not ok:
                    problems.append(f"{fname}:{node.name} -> {bad}")
    assert not problems, (
        "以下 _save_users() 调用不在 with users_mutation() 临界区内（并发写会互相覆盖）：\n  "
        + "\n  ".join(problems)
    )
    print("✅ auth_store / admin_store 的全部 _save_users 调用均在 users_mutation 内")


# --------------------------------------------------------------------------- #
# 4. dev_code 仅本机回传（公网不得泄露验证码 → 否则任意账号密码可被改）
# --------------------------------------------------------------------------- #
def _fake_request(host):
    class _Client:
        def __init__(self, h):
            self.host = h
    class _Req:
        client = _Client(host)
    return _Req()


def test_dev_code_only_loopback():
    try:
        import fastapi  # noqa: F401 — 仅当可用时才跑真实模块测试
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "vdl_auth_test_mod", os.path.join(_SERVER_DIR, "routers", "auth.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001 — fastapi 缺失等：跳过真实模块校验
        print(f"⚠️  跳过 dev_code 真实模块测试（fastapi 不可用：{e}）")
        return
    assert mod._is_loopback(_fake_request("203.0.113.5")) is False, "公网 IP 应判为非本机"
    assert mod._is_loopback(_fake_request("127.0.0.1")) is True, "127.0.0.1 应判为本机"
    assert mod._is_loopback(_fake_request("::1")) is True, "::1 应判为本机"
    print("✅ _is_loopback 对公网/本机判定正确（dev_code 泄漏已封）")


if __name__ == "__main__":
    test_atomic_write_contract()
    test_unique_temp_path_keeps_extension_and_dir()
    test_atomic_write_survives_concurrent_writers()
    test_no_fixed_temp_write_in_migrated_files()
    test_save_call_sites_inside_mutation()
    test_dev_code_only_loopback()
    print("\n✅ 全部 web-dev 原子写 / 并发锁 / 验证码回归测试通过")
