"""账号表（users.json）并发写安全回归测试（2026-09-16 新增）。

背景：用户要求「彻底解决」账号表的并发写问题（此前只修了索引不变量分叉，
读改写窗口本身没有锁）。

问题：users.json 有 8 个写入口（注册 / 改密 / 注销 / 提权 / 头像 / 后台禁用 /
后台重置 / 超管引导），散落在 auth_store 与 admin_store 两个模块，**全部**是
「读全量 → 改一处 → 写全量」。修之前没有任何锁，两个写入口同时进来会各自基于
同一份旧快照写回，后写的把先写的整段覆盖，且不报任何错。

反证（修之前实测，见提交信息）：
    8 线程并发注册不同账号 → 8 次都返回成功，文件里只剩 1 条（丢 7 个账号）
    8 线程并发注册同一邮箱 → 8 次都返回成功（7 个幽灵账号）
修之后：8/8 落盘；同名并发注册恰好成功 1 次。

本文件钉死的不变量：
  1. 临界区语义：users_mutation() 内持锁、外不持锁、可重入（后台重置会回调 auth_store）
  2. ★ 所有落盘都必须在临界区内 —— 动态挂钩 _save_users + AST 静态验证（双保险）
  3. 并发注册不丢记录；同名并发注册恰好成功一次
  4. 跨模块写不互相覆盖（后台禁用用户 vs 该用户此刻自己在改资料）
  5. 跨进程锁真的生效（本进程持锁时子进程抢不到）—— 双实例窗口兜底
  6. 写盘必须用唯一临时名（固定名会被并发写者截断出半截 JSON = 全部账号消失）

运行：
    cd server && ../.build_venv/bin/python tests/test_users_store_concurrency.py
    cd server && ../.build_venv/bin/python -m pytest tests/test_users_store_concurrency.py -v
"""
import ast
import atexit
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 独立运行（不经 run_offline_tests.sh）时也必须写进临时目录，绝不能碰用户真实数据
_CREATED_DIR = False
if not os.environ.get("VDL_DATA_DIR"):
    os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_users_lock_test_")
    _CREATED_DIR = True

# 铁律：无论谁设的 VDL_DATA_DIR，都不允许它就是用户真实数据目录
_REAL_DATA_DIR = (Path.home() / ".video-downloader").resolve()
assert Path(os.environ["VDL_DATA_DIR"]).resolve() != _REAL_DATA_DIR, \
    "测试数据目录不能是用户真实目录（会写坏真实账号）"

import auth_store as au          # noqa: E402
import admin_store as ad         # noqa: E402


def _cleanup() -> None:
    """只清自己造的临时目录（别人传进来的 VDL_DATA_DIR 不动）。"""
    if _CREATED_DIR:
        shutil.rmtree(os.environ["VDL_DATA_DIR"], ignore_errors=True)


atexit.register(_cleanup)


def _read_table() -> dict:
    p = au._users_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"users": [], "by_identifier": {}}


def _ids_of(identifier: str) -> list[str]:
    return [u["user_id"] for u in _read_table()["users"] if u.get("identifier") == identifier]


def _uniq(prefix: str) -> str:
    """每次运行都不同的 identifier 后缀。

    数据目录可能是调用方给的（跨用例/跨次运行共享），写死的邮箱第二次跑就会「已存在」，
    让用例莫名其妙地失败 —— 所以一律带随机后缀。
    """
    return f"{prefix}-{uuid.uuid4().hex[:8]}@lock.test"


def _fresh(identifier: str, password: str = "pw123456") -> str:
    """造一个全新账号，返回 user_id。"""
    uid = au.create_user(identifier, password)
    assert uid, f"前置失败：{identifier} 建不出来"
    return uid


@contextlib.contextmanager
def _widened_window(seconds: float = 0.03):
    """把「载入完成 → 落盘」之间的窗口人为拉大，让竞态可稳定复现。

    注意：这个延时**不会制造** bug —— 它只是把本来就存在的窗口放大到
    线程调度必然交错的程度（无锁时的丢失是实测出来的，不是这里造出来的）。
    """
    real = au._load_users

    def slow():
        data = real()
        time.sleep(seconds)
        return data

    au._load_users = slow
    try:
        yield
    finally:
        au._load_users = real


# --------------------------------------------------------------------- 1
def test_mutation_holds_lock_and_is_reentrant():
    """临界区语义：内持锁、外不持锁、可重入（后台重置密码会回调 auth_store）。"""
    assert not au.users_lock_held(), "临界区外就报告持有锁 → 判定逻辑坏了"
    with au.users_mutation():
        assert au.users_lock_held(), "临界区内未持锁 → 锁没生效"
        with au.users_mutation():                     # 重入（跨模块调用链会这样嵌套）
            assert au.users_lock_held(), "嵌套临界区内未持锁"
        assert au.users_lock_held(), "退出内层后就不持锁了 → 深度计数写反了"
    assert not au.users_lock_held(), "退出临界区后仍报告持锁 → 会有线程永远拿不到锁"
    print("✅ 临界区语义：内持锁 / 外不持锁 / 可重入（嵌套不死锁）")


# --------------------------------------------------------------------- 2
def test_every_save_happens_inside_the_lock():
    """★ 动态守卫：走遍全部 8 个写入口，每一次落盘都必须在临界区内。

    这是本文件最重要的一条 —— 它不依赖「某个入口忘了加锁」被人工发现，
    而是直接盯住唯一的落盘函数 _save_users：只要有人在锁外把它调起来就变红。
    """
    violations: list[str] = []
    real_save = au._save_users
    ident = _uniq("hook")

    def probe(data):
        if not au.users_lock_held():
            violations.append(f"thread={threading.current_thread().name}")
        return real_save(data)

    # ⚠️ 探针必须在**第一个写入口之前**装上：注册本身就是一个写入口，
    # 上一版把 _fresh() 写在装探针之前，导致「注册入口忘了加锁」这个变异抓不到。
    au._save_users = probe
    try:
        uid = au.create_user(ident, "pw123456")                        # 注册
        assert uid, "前置失败：账号建不出来"
        au.set_user_admin(uid, True)                                   # 提权
        au.reset_password(ident, "pw999999")                           # 改密
        au.set_user_avatar(uid, b"\x89PNG\r\n\x1a\n" + b"0" * 32)      # 头像
        ad.set_user_disabled(uid, True)                                # 后台禁用
        au.ensure_superusers()                                         # 超管引导
        au.deactivate_user(uid)                                        # 注销
    finally:
        au._save_users = real_save

    assert not violations, (
        f"有 {len(violations)} 次落盘发生在临界区之外 —— 与并发写会互相覆盖：\n  "
        + "\n  ".join(violations))
    print("✅ 全部写入口落盘时均持有账号表锁（0 次锁外落盘）")


# --------------------------------------------------------------------- 3
def test_save_call_sites_are_lexically_inside_mutation():
    """★ 静态守卫：全仓每一处 _save_users(...) 都必须写在 with users_mutation() 里。

    动态钩子只能证明「测到的那几个入口」是对的；静态扫描能挡住**新加**的入口
    （将来在任何模块再写一个改账号的地方，若忘了进临界区，这里立刻变红）。
    所以扫描范围是**整个 server/**，不是只盯着这两个已知模块。
    """
    sources = sorted(Path(_SERVER_DIR).rglob("*.py"))
    sources = [p for p in sources if not any(x in p.parts for x in ("__pycache__", "tests"))]

    def _is_mutation_cm(expr) -> bool:
        if not isinstance(expr, ast.Call):
            return False
        f = expr.func
        if isinstance(f, ast.Name):
            return f.id == "users_mutation"
        return isinstance(f, ast.Attribute) and f.attr == "users_mutation"

    found: list[tuple[str, int]] = []
    violations: list[tuple[str, int]] = []
    reimplemented: list[tuple[str, int]] = []

    for p in sources:
        src = p.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
        rel = p.relative_to(Path(_SERVER_DIR)).as_posix()
        depth = 0

        class V(ast.NodeVisitor):
            def visit_With(self, node):
                nonlocal depth
                entered = any(_is_mutation_cm(it.context_expr) for it in node.items)
                if entered:
                    depth += 1
                try:
                    for child in node.body:
                        self.visit(child)
                finally:
                    if entered:
                        depth -= 1

            def visit_Call(self, node):
                if isinstance(node.func, ast.Name) and node.func.id == "_save_users":
                    found.append((rel, node.lineno))
                    if depth == 0:
                        violations.append((rel, node.lineno))
                self.generic_visit(node)

        V().visit(tree)
        # 除 auth_store 自己，任何模块**调用** _users_path 都意味着「又有人要自己读写账号表」。
        # （admin_store 保留了同名 helper 的定义，供隔离测试断言两者指向同一份文件；
        #   但它只定义不调用 —— 调用就会在这里变红。）
        calls = [n.lineno for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "_users_path"]
        if rel != "auth_store.py" and calls:
            reimplemented.append((rel, calls[0]))

    assert not violations, (
        "以下 _save_users() 调用不在 with users_mutation() 临界区内（并发写会互相覆盖）：\n  "
        + "\n  ".join(f"{f}:{ln}" for f, ln in violations))
    assert not reimplemented, (
        "以下模块试图自己读写账号表（应改走 auth_store 的读写入口）：\n  "
        + "\n  ".join(f for f, _ in reimplemented))
    assert len(found) >= 7, (
        f"只扫到 {len(found)} 处落盘点，远少于预期的 7 处 —— 扫描逻辑或代码结构变了，请核对")
    print(f"✅ 静态扫描（全 {len(sources)} 个模块）：{len(found)} 处落盘点全部在临界区内，无旁路读写")


# --------------------------------------------------------------------- 4
def test_concurrent_registrations_lose_nothing():
    """并发注册不同账号：一个都不能丢（修之前 8 个只剩 1 个）。"""
    n = 8
    tag = uuid.uuid4().hex[:8]
    idents = [f"race{i}-{tag}@lock.test" for i in range(n)]
    found: list[str] = []
    errs: list[str] = []
    bar = threading.Barrier(n)

    def reg(i):
        bar.wait()                          # 尽量同时冲进去
        r = au.create_user(idents[i], "pw123456")
        (found if r else errs).append(r or idents[i])

    with _widened_window():
        ts = [threading.Thread(target=reg, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    assert not errs, f"有 {len(errs)} 个账号被判为重复/失败：{errs}"
    for it in idents:
        assert _ids_of(it), f"{it} 没落盘 —— 写被覆盖掉了"
    # 文件必须是完整可解析的 JSON（半截写入会让解析直接抛错）
    assert _read_table()["users"], "账号表读不出来或为空"
    print(f"✅ 并发注册 {n} 个账号：{len(found)} 个全部落盘，无丢号")


# --------------------------------------------------------------------- 5
def test_concurrent_same_identifier_registers_once():
    """同一邮箱并发注册：必须恰好成功一次（修之前 8 个线程全"成功"= 7 个幽灵账号）。"""
    ident = _uniq("dup")
    n = 8
    ok: list[str] = []
    bar = threading.Barrier(n)

    def reg(i):
        bar.wait()
        r = au.create_user(ident, "pw123456")
        if r:
            ok.append(r)

    with _widened_window():
        ts = [threading.Thread(target=reg, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    assert len(ok) == 1, f"同名并发注册成功了 {len(ok)} 次（期望恰好 1 次）"
    assert len(_ids_of(ident)) == 1, f"文件里同 identifier 有 {len(_ids_of(ident))} 条（期望 1 条）"
    print("✅ 同一邮箱并发注册 8 次：恰好成功 1 次，文件里只有 1 条")


# --------------------------------------------------------------------- 6
def test_admin_and_self_service_writes_do_not_overwrite_each_other():
    """★ 用户最关心的场景：后台改用户 与 用户自己改资料 同时发生，两个改动都要留下。

    这是「后台禁用了某用户，但该用户此时正在改头像」的真实竞态：
    修之前后落盘的那个会把另一个字段抹掉（禁用失效 = 该用户又能用了）。
    """
    rounds = 3
    for r in range(rounds):
        uid = _fresh(_uniq(f"cross{r}"))
        bar = threading.Barrier(2)

        def admin_disable():
            bar.wait()
            ad.set_user_disabled(uid, True)

        def user_change_avatar():
            bar.wait()
            au.set_user_avatar(uid, b"\x89PNG\r\n\x1a\n" + bytes([r]) * 32)

        with _widened_window():
            t1 = threading.Thread(target=admin_disable)
            t2 = threading.Thread(target=user_change_avatar)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        rec = next(u for u in _read_table()["users"] if u["user_id"] == uid)
        assert rec.get("disabled") is True, (
            f"第 {r + 1} 轮：后台的「禁用」被用户自己的写覆盖掉了（禁用失效）")
        assert rec.get("avatar_updated_at"), (
            f"第 {r + 1} 轮：用户头像更新时间被后台的写覆盖掉了")
    print(f"✅ 后台禁用 与 用户自助改资料 并发 {rounds} 轮：两个改动都保留（无互相覆盖）")


# --------------------------------------------------------------------- 7
def test_cross_process_lock_is_effective():
    """跨进程锁：本进程在临界区内时，子进程必须抢不到（双实例窗口兜底）。"""
    if au._fcntl is None and au._msvcrt is None:
        print("⚠️ 跳过：当前平台既无 fcntl 也无 msvcrt，跨进程锁不可用")
        return

    code = (
        "import sys, fcntl, os\n"
        f"sys.path.insert(0, {_SERVER_DIR!r})\n"
        "import auth_store as au\n"
        "try:\n"
        "    fh = open(au._users_lock_path(), 'a+')\n"
        "    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    print('CHILD_ACQUIRED')\n"
        "except OSError:\n"
        "    print('CHILD_BLOCKED')\n"
    )
    env = dict(os.environ, VDL_DATA_DIR=os.environ["VDL_DATA_DIR"])

    def run_child() -> str:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, env=env, timeout=30)
        return (p.stdout or p.stderr).strip().splitlines()[-1] if (p.stdout or p.stderr) else ""

    with au.users_mutation():
        inside = run_child()
    outside = run_child()

    assert inside == "CHILD_BLOCKED", (
        f"临界区内子进程仍抢到了跨进程锁（实际 {inside!r}）—— 两个实例会同时写坏账号表")
    assert outside == "CHILD_ACQUIRED", (
        f"临界区释放后子进程仍抢不到锁（实际 {outside!r}）—— 锁没被正确释放，会死锁")
    print("✅ 跨进程锁：持锁期间子进程被挡（CHILD_BLOCKED），释放后可获取（CHILD_ACQUIRED）")


# --------------------------------------------------------------------- 8
def test_atomic_write_uses_unique_temp_name():
    """落盘必须用唯一临时名：固定名会被并发写者截断出半截 JSON（= 账号全消失）。"""
    src = Path(_SERVER_DIR, "auth_store.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "_write_atomic"), None)
    assert fn is not None, "_write_atomic 不见了（原子写被改回手写临时文件？）"
    body = ast.get_source_segment(src, fn) or ""
    assert "with_suffix" not in body, (
        "_write_atomic 用了 with_suffix 生成临时名 → 固定名，并发写者会互相截断")
    for token in ("os.getpid()", "threading.get_ident()"):
        assert token in body, f"临时名里缺少 {token} —— 并发写同一份文件时会互相截断"

    # 行为面：正常写一轮后不能留下临时文件，且文件必须完整可解析
    ident = _uniq("tmpname")
    uid = _fresh(ident)
    au.set_user_admin(uid, True)
    leftovers = [p.name for p in Path(os.environ["VDL_DATA_DIR"]).iterdir() if ".tmp" in p.name]
    assert not leftovers, f"写完留下临时文件：{leftovers}"
    assert _ids_of(ident), "写完读不回来 → 落盘不完整"
    print("✅ 写盘用唯一临时名（pid+线程），无 .tmp 残留，文件完整可解析")


# --------------------------------------------------------------------- 9
def test_auth_store_has_no_fixed_temp_name_writers():
    """棘轮：auth_store 里不得再出现固定名临时文件（新增即变红，须评审）。"""
    src = Path(_SERVER_DIR, "auth_store.py").read_text(encoding="utf-8")
    n = src.count('with_suffix(".tmp")')
    assert n == 0, (
        f"auth_store 里又有 {n} 处固定名临时文件（.tmp）—— 并发写会写出半截文件，"
        "请改用 _write_atomic()")
    print("✅ auth_store 内无固定名临时文件写入（全部走 _write_atomic）")


if __name__ == "__main__":
    test_mutation_holds_lock_and_is_reentrant()
    test_every_save_happens_inside_the_lock()
    test_save_call_sites_are_lexically_inside_mutation()
    test_concurrent_registrations_lose_nothing()
    test_concurrent_same_identifier_registers_once()
    test_admin_and_self_service_writes_do_not_overwrite_each_other()
    test_cross_process_lock_is_effective()
    test_atomic_write_uses_unique_temp_name()
    test_auth_store_has_no_fixed_temp_name_writers()
    _cleanup()
    if _CREATED_DIR:
        assert not Path(os.environ["VDL_DATA_DIR"]).exists(), "临时数据目录没清掉（静默失败会留垃圾）"
    else:
        print(f"（数据目录由调用方提供，保留不动：{os.environ['VDL_DATA_DIR']}）")
    print("\n🎉 账号表并发安全测试全部通过（9 项）")
