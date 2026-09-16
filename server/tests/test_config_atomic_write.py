"""配置/状态文件「原子落盘 + 读改写串行」回归测试（2026-09-16 新增）。

背景（承接账号表那次「彻底解决」）：全仓有多处「读全量 → 改一处 → 写全量」的
JSON 落盘，此前用**固定名**临时文件（`xxx.json.tmp`）甚至是**直接 write_text**。
两者的失效方式都已实测复现，不是理论担忧：

① 固定名临时文件：两个写者共用同一个临时路径 → A 打开截断 → B 打开再截断 →
   A 先 replace（此时文件里是 B 写了一半的内容）→ 目标文件解析失败 = **整份配置消失**。
   本文件 `test_fixed_temp_name_tears_under_concurrency` 是这条的**可复现反证**：
   实测并发读者会拿到 JSONDecodeError，写者线程还会因临时文件被别人 rename 走而 ENOENT。
② 直接 write_text：先截断原文件再写。进程被杀就留半截；更贵的是**并发读者**读到半截后
   按「空状态」处理 —— `quota.json` 一旦这样，免费云端额度**整体复原**（变现漏洞）。

覆盖（17 项）：
  atomic_io 契约与并发不撕裂 / 固定名反证 / 唯一临时名保留扩展名
  全仓 AST 棘轮：不得再出现「固定名 .tmp」
  已知状态文件必须走 atomic_io（源码名单）
  quota：并发扣减恰好封顶（线程 + 多进程）/ 源码级原子写与临界区断言 / 两份同源
  membership / voice_studio_config / llm_config / stats 落盘隔离验证
  全程不碰真实 ~/.video-downloader

运行：
    cd server && python tests/test_config_atomic_write.py
    cd server && python -m pytest tests/test_config_atomic_write.py -v
"""
import ast
import atexit
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import atomic_io  # noqa: E402

_TMP_ROOT = tempfile.mkdtemp(prefix="vdl_atomic_test_")
_REAL_DATA_DIR = (Path.home() / ".video-downloader").resolve()
_REAL_SNAPSHOT = None
_CREATED_REAL = False


def _snapshot_real() -> set:
    try:
        return {p.name for p in _REAL_DATA_DIR.iterdir()}
    except OSError:
        return set()


def _cleanup() -> None:
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    # 清理失败要能被察觉（静默失败会在临时目录留垃圾，本仓踩过）
    assert not os.path.exists(_TMP_ROOT), f"临时目录未清理干净：{_TMP_ROOT}"


atexit.register(_cleanup)


def _tmpdir(tag: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"{tag}_", dir=_TMP_ROOT))
    return d


def _snippet(src: str, name: str) -> str:
    """取某函数的源码片段（含装饰器）；找不到返回 ""。"""
    lines = src.splitlines()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            start = min([d.lineno for d in n.decorator_list] + [n.lineno])
            return "\n".join(lines[start - 1: n.end_lineno])
    return ""


# ───────────────────────────────────────────────────────────────────────── #
# 1. atomic_io 契约
# ───────────────────────────────────────────────────────────────────────── #
def test_atomic_write_contract():
    d = _tmpdir("contract")
    p = d / "cfg.json"
    atomic_io.atomic_write_json(p, {"a": 1, "中文": "值"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "中文": "值"}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600, (
        f"权限应为 0600（凭据文件不允许因 umask 放宽），实际 {oct(stat.S_IMODE(p.stat().st_mode))}")
    assert [f.name for f in d.iterdir()] == ["cfg.json"], "原子写不得残留临时文件"

    # umask 全开也不能把权限放宽
    old = os.umask(0)
    try:
        atomic_io.atomic_write_json(p, {"b": 2})
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, "umask=0 下权限被放宽了"
    finally:
        os.umask(old)

    # 「恰好等于请求的 mode」而不是「≤ mode」：umask 只会剥位，光靠 os.open(mode)
    # 拿不到比 umask 更宽的结果 —— 这一条专门钉住显式 chmod 那一行。
    p2 = d / "exact.json"
    old = os.umask(0o077)
    try:
        atomic_io.atomic_write_text(p2, "{}", mode=0o640)
        got = stat.S_IMODE(p2.stat().st_mode)
        assert got == 0o640, f"权限应恰好等于请求的 mode 0640（umask 只该被忽略），实际 {oct(got)}"
    finally:
        os.umask(old)

    # 父目录不存在要能自动建（原实现各写各的 mkdir，统一进 helper 后不能丢）
    deep = d / "x" / "y" / "z.json"
    atomic_io.atomic_write_json(deep, {"ok": True})
    assert deep.is_file()
    print("✅ 原子写契约：内容正确 / 权限 0600（umask 无关）/ 无临时残留 / 自动建父目录")


def test_unique_temp_path_keeps_extension_and_dir():
    d = _tmpdir("uniquetmp")
    target = d / "cover.jpg"
    a = atomic_io.unique_temp_path(target)
    b = atomic_io.unique_temp_path(target)
    assert a != b, "临时名必须唯一（同进程连续两次都不能撞）"
    assert a.parent == target.parent, "临时文件必须同目录，否则 os.replace 不是原子的"
    assert a.name.endswith(".jpg"), f"必须保留扩展名（ffmpeg 靠它猜封装格式）：{a.name}"
    print("✅ 唯一临时名：唯一 / 同目录 / 保留扩展名（供 ffmpeg、urlretrieve 这类外部写者使用）")


def test_atomic_write_survives_concurrent_writers():
    """核心断言：并发写者 + 并发读者，永远读不到「写一半」的文件。

    ⚠️ **必须同时采集写者异常**：固定名临时文件下，A 把临时文件 rename 走之后
    B 的 `os.replace` 会直接 ENOENT 崩掉 —— 只看读者是否撕裂会漏掉这条（实测过）。
    """
    d = _tmpdir("concurrent")
    p = d / "state.json"
    pad = "x" * 40000
    torn: list = []
    writer_err: list = []
    stop = threading.Event()

    def writer(i: int) -> None:
        try:
            for k in range(40):
                atomic_io.atomic_write_json(p, {"who": i, "k": k, "pad": pad}, indent=None)
        except Exception as e:  # noqa: BLE001
            writer_err.append(repr(e))

    def reader() -> None:
        while not stop.is_set():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                assert len(data.get("pad", "")) == 40000, "读到了不完整的 payload"
            except Exception as e:  # noqa: BLE001
                torn.append(repr(e))

    atomic_io.atomic_write_json(p, {"who": -1, "k": -1, "pad": pad}, indent=None)
    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    ts = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stop.set()
    rt.join(timeout=5)
    assert not torn, f"并发读到了 {len(torn)} 次撕裂文件：{torn[:3]}"
    assert not writer_err, f"并发写者报错 {len(writer_err)} 次（临时文件被别人换走）：{writer_err[:2]}"
    assert [f.name for f in d.iterdir()] == ["state.json"], "并发写后有临时文件残留"
    print("✅ 并发写（6 线程 × 40 轮）+ 并发读：0 撕裂、0 写者报错、0 残留")


def test_fixed_temp_name_tears_under_concurrency():
    """反证：证明上面那条测试不是空壳 —— 退回固定名临时文件必须变红。

    这就是本次要清掉的隐患本体：写者会因临时文件被别人 rename 走而报错，
    读者会拿到半截 JSON。两条都不依赖时序巧合，放大窗口后稳定复现。
    """
    d = _tmpdir("fixedname")
    p = d / "state.json"
    pad = "x" * 40000
    torn: list = []
    writer_err: list = []
    stop = threading.Event()

    def fixed_write(payload: dict) -> None:
        tmp = p.with_name(p.name + ".tmp")      # ← 固定名（旧写法）
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
            fh.flush()
            time.sleep(0.002)                   # 只放大窗口，不改语义
        os.replace(tmp, p)

    def writer(i: int) -> None:
        for k in range(40):
            try:
                fixed_write({"who": i, "k": k, "pad": pad})
            except Exception as e:  # noqa: BLE001
                writer_err.append(repr(e))

    def reader() -> None:
        while not stop.is_set():
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                torn.append(repr(e))

    fixed_write({"init": 1})
    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    ts = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stop.set()
    rt.join(timeout=5)
    assert torn or writer_err, (
        "固定名临时文件竟然没坏 —— 说明并发测试本身没测到点上（本用例是反证，必须变红）")
    print(f"✅ 反证成立：固定名临时文件下 读者撕裂 {len(torn)} 次 / 写者报错 {len(writer_err)} 次")


# ───────────────────────────────────────────────────────────────────────── #
# 2. 全仓静态棘轮
# ───────────────────────────────────────────────────────────────────────── #
def test_repo_has_no_fixed_temp_name_writers():
    """AST 棘轮：全仓不得再出现「固定名临时文件」。

    固定名是这次清查的隐患本体，靠人肉 review 挡不住回归，用棘轮钉住。

    判据 = 该表达式里出现 `.tmp` 字面量，**且**没有任何「唯一名」证据
    （`os.getpid()` / `threading.get_ident()`）。两种写法都抓
    （`with_suffix(".tmp")` 与 `with_name(name + ".tmp")`）—— 只挡前者能被绕过去。

    ⚠️ 两个必须避开的误报（都实际踩过）：
      · `auth_store` 的 `f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp"` —— 唯一名，
        但 `.tmp` 出现在 f-string 里；只看字面量会误判 → 用 `ast.unparse` 后检查唯一名证据。
      · `ydlp_update` 的 `with_name("yt_dlp_tmp")` —— 是**目录**名，不含 `.tmp`（点 + tmp），
        按 `".tmp"` 而不是 `"tmp"` 匹配即可排除。
      · 本文件自己的反证用例故意写固定名 → 扫描时跳过 tests 目录。
    """
    offenders = []
    for dp, dn, fn in os.walk(_SERVER_DIR):
        if "__pycache__" in dp or os.path.basename(dp) == "tests":
            continue
        for f in fn:
            if not f.endswith(".py"):
                continue
            fp = os.path.join(dp, f)
            try:
                tree = ast.parse(open(fp, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for n in ast.walk(tree):
                if not (isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr in ("with_suffix", "with_name")):
                    continue
                for a in n.args:
                    text = ast.unparse(a)
                    if ".tmp" not in text:
                        continue
                    if "getpid" in text or "get_ident" in text:
                        continue        # 有唯一名证据
                    offenders.append(f"{os.path.relpath(fp, _SERVER_DIR)}:{n.lineno}  {text[:80]}")
    assert not offenders, (
        "仍有固定名临时文件写盘（并发写者会互相截断 → 整份配置消失）：\n  "
        + "\n  ".join(offenders)
        + "\n请改用 atomic_io.atomic_write_json / atomic_write_text / unique_temp_path")
    print("✅ 全仓 AST 棘轮：0 处固定名 .tmp 临时文件（with_suffix / with_name 两种写法都挡）")


def test_known_state_files_route_through_atomic_io():
    """源码名单：这些「读改写共享文件」必须走 atomic_io（防止有人改回手写）。"""
    mods = {
        "membership.py": ["_save_state", "save_plan_overrides"],
        "stats.py": ["_save"],
        "subscriptions.py": ["_save"],
        "retention.py": ["_save"],
        "cookie_pool.py": ["_save"],
        "cookie_cache.py": ["_save"],
        "llm_config.py": ["save_llm_config", "save_managed_config"],
        "vision_config.py": ["save_vision_config"],
        "commentary_config.py": ["save_commentary_config"],
        "cloud_matting_config.py": ["save_cloud_matting_config"],
        "voice_studio_config.py": ["save_voice_studio_config"],
        "admin_store.py": ["save_smtp_accounts"],
    }
    missing = []
    for f, fns in mods.items():
        src = open(os.path.join(_SERVER_DIR, f), encoding="utf-8").read()
        assert "import atomic_io" in src, f"{f} 没有 import atomic_io"
        for fn in fns:
            body = _snippet(src, fn)
            assert body, f"{f}::{fn} 源码片段没找到（函数被改名？棘轮前提失效）"
            if "atomic_write_" not in body:
                missing.append(f"{f}::{fn}")
    # app.py 的保险箱索引走 atomic_write_text（带 _chmod_600 钩子）
    app_src = open(os.path.join(_SERVER_DIR, "app.py"), encoding="utf-8").read()
    vault = _snippet(app_src, "_vault_save")
    assert "atomic_write_text" in vault and "atomic_io.mutation(VAULT_PATH)" in vault, (
        "保险箱索引落盘没走原子写 + 临界区")
    for f, fns in (("routers/support.py", ["_write_threads"]),
                   ("library.py", ["get_thumbnail"]),
                   ("dewatermark_ai.py", ["_ensure_model"])):
        src = open(os.path.join(_SERVER_DIR, f), encoding="utf-8").read()
        for fn in fns:
            body = _snippet(src, fn)
            if not body:
                continue
            assert ("atomic_write_" in body or "unique_temp_path" in body), \
                f"{f}::{fn} 落盘未走 atomic_io"
    assert not missing, "以下写入口没走 atomic_io：\n  " + "\n  ".join(missing)
    print(f"✅ 已知状态文件全部走 atomic_io（{len(mods)} 个模块 / "
          f"{sum(len(v) for v in mods.values())} 个写入口 + 保险箱 + 缩略图 + 模型下载）")


def test_read_modify_write_paths_hold_mutation_lock():
    """读改写必须在临界区内：只锁落盘等于没锁。"""
    expect = {
        "membership.py": ("_save_state", "atomic_io.mutation(path)"),
        "voice_studio_config.py": ("save_voice_studio_config", "atomic_io.mutation(cp)"),
    }
    for f, (fn, needle) in expect.items():
        src = open(os.path.join(_SERVER_DIR, f), encoding="utf-8").read()
        body = _snippet(src, fn)
        assert needle in body, f"{f}::{fn} 未在临界区内完成读改写（缺 {needle}）"
    print("✅ 会员状态 / 语音工坊配置：读改写整段持锁")


# ───────────────────────────────────────────────────────────────────────── #
# 3. quota：唯一跨进程写者，本次最高风险项
# ───────────────────────────────────────────────────────────────────────── #
def _quota_mods():
    import quota as q
    return q


def test_quota_concurrent_consume_caps_exactly():
    """额度只有 3，20 个线程各扣 1 次 → 必须**恰好**成功 3 次。

    没有临界区时，多个线程会各自读到同一份旧状态、各自写回 1 次 → 成功次数远超额度；
    `_load` 读到半截还会把计数清零（额度复原）。
    """
    q = _quota_mods()
    d = _tmpdir("quota_threads")
    results: list = []
    lock = threading.Lock()

    def w() -> None:
        # 每个线程一个独立实例：模拟 `routers/quota.py` 每请求新建 manager
        r = q.QuotaManager(base_dir=d).consume_cloud_event()
        with lock:
            results.append(r)

    ts = [threading.Thread(target=w) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    used = json.loads((d / "quota.json").read_text(encoding="utf-8"))["lifetime_cloud_used"]
    assert sum(results) == q.LIFETIME_CLOUD_EVENTS, (
        f"并发扣减成功 {sum(results)} 次，应恰好 {q.LIFETIME_CLOUD_EVENTS} 次（超额放行=变现漏洞）")
    assert used == q.LIFETIME_CLOUD_EVENTS, f"落盘计数 {used} 与实际放行数不一致"
    assert not [f for f in d.iterdir() if f.name.endswith(".tmp")], "有临时文件残留"
    print(f"✅ quota 并发扣减：20 线程 → 恰好成功 {q.LIFETIME_CLOUD_EVENTS} 次，落盘一致、无残留")


def test_quota_cross_process_lock_serializes():
    """quota.json 有两个**进程**写者（worker 扣 / 父进程退）→ 必须有跨进程锁。"""
    q = _quota_mods()
    d = _tmpdir("quota_procs")
    code = (
        "import sys\n"
        f"sys.path.insert(0, {_SERVER_DIR!r})\n"
        "from quota import QuotaManager\n"
        "n = 0\n"
        "for _ in range(3):\n"
        f"    if QuotaManager(base_dir={str(d)!r}).consume_cloud_event():\n"
        "        n += 1\n"
        "print(n)\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(4)]
    outs, errs = [], []
    for pp in procs:
        o, e = pp.communicate(timeout=60)
        outs.append(o.strip())
        if pp.returncode != 0:
            errs.append(e.strip()[-400:])
    assert not errs, f"子进程失败：{errs}"
    total = sum(int(x) for x in outs if x.isdigit())
    used = json.loads((d / "quota.json").read_text(encoding="utf-8"))["lifetime_cloud_used"]
    assert total == q.LIFETIME_CLOUD_EVENTS, (
        f"4 进程共放行 {total} 次（各进程自报 {outs}），应恰好 {q.LIFETIME_CLOUD_EVENTS} 次")
    assert used == q.LIFETIME_CLOUD_EVENTS, f"落盘计数 {used} 与放行数不一致"
    assert (d / ".quota.lock").exists(), "跨进程锁文件没建出来（flock 没生效？）"
    print(f"✅ quota 跨进程：4 进程共放行 {q.LIFETIME_CLOUD_EVENTS} 次，落盘一致，.quota.lock 已建")


def test_quota_cross_process_lock_blocks_rival():
    """跨进程锁语义：**持有期间抢不到 + 释放之后抢得到**，缺一条就测不出锁。

    （只看「并发扣减不超额」不够 —— 那个窗口很小，可能碰巧不出错。）
    """
    q = _quota_mods()
    d = _tmpdir("quota_flock")
    lock_path = d / ".quota.lock"
    child = (
        "import sys\n"
        f"sys.path.insert(0, {_SERVER_DIR!r})\n"
        "import quota as q\n"
        f"fh = q._flock_acquire({str(lock_path)!r}, timeout=0.6)\n"
        "print('CHILD_ACQUIRED' if fh else 'CHILD_BLOCKED')\n"
        "if fh:\n"
        "    q._flock_release(fh)\n"
    )

    def run_child() -> str:
        pp = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=60)
        return (pp.stdout or "").strip()

    fh = q._flock_acquire(lock_path, timeout=1.0)
    assert fh is not None, "父进程自己都抢不到锁（锁实现坏了）"
    try:
        while_blocked = run_child()
    finally:
        q._flock_release(fh)
    after = run_child()
    assert while_blocked == "CHILD_BLOCKED", f"持有期间子进程竟然抢到了锁：{while_blocked}"
    assert after == "CHILD_ACQUIRED", f"释放之后子进程仍抢不到（锁没放/泄漏）：{after}"
    print("✅ quota 跨进程锁：持有期间子进程被挡住，释放后可获取")


def test_quota_source_is_atomic_and_locked():
    """源码级棘轮：quota 的落盘与临界区形态不许退化。"""
    src = open(os.path.join(_SERVER_DIR, "quota.py"), encoding="utf-8").read()
    wa = _snippet(src, "_write_atomic")
    assert wa, "_write_atomic 不见了（原子写被拿掉了？）"
    assert "with_suffix" not in wa, "_write_atomic 用了 with_suffix 生成临时名 → 又变固定名"
    for token in ("os.getpid()", "threading.get_ident()", "os.replace("):
        assert token in wa, f"_write_atomic 缺 {token}"
    assert "self.path.write_text(" not in src, "quota 又出现就地 write_text（读到半截 → 额度复原）"
    for fn in ("consume_cloud_event", "consume_daily_auto", "refund"):
        body = _snippet(src, fn)
        assert body, f"{fn} 没找到"
        assert "with self._mutation():" in body, f"{fn} 未进临界区（读改写在锁外=没锁）"
    mut = _snippet(src, "_mutation")
    for token in ("_path_lock(", "_flock_acquire(", "_flock_release("):
        assert token in mut, f"_mutation 缺 {token}"
    print("✅ quota 源码棘轮：唯一临时名原子写 + 三个写入口全在进程内/跨进程临界区内")


def test_quota_dual_copy_identical():
    """与解说管线 `scripts/quota.py` 逐字节同源（两个仓库各持一份，diff 校验守护）。"""
    candidates = [
        os.environ.get("VDL_PIPELINE_QUOTA", ""),
        os.path.join(os.path.dirname(_SERVER_DIR), "commentary", "scripts", "quota.py"),
        "/Users/suixindelang/WorkBuddy/问问题/commentary-pipeline/scripts/quota.py",
    ]
    other = next((p for p in candidates if p and os.path.isfile(p)), "")
    if not other:
        print("⚠️ 跳过：未找到管线侧 scripts/quota.py")
        return
    a = open(os.path.join(_SERVER_DIR, "quota.py"), "rb").read()
    b = open(other, "rb").read()
    assert a == b, f"两份 quota.py 已分叉：{other}（文件必须逐字节一致，否则两进程行为不同）"
    print("✅ 两份 quota.py 逐字节一致（server/ 与管线 scripts/）")


# ───────────────────────────────────────────────────────────────────────── #
# 4. 其余状态文件：真实落盘验证（全部注入临时路径，不碰家目录）
# ───────────────────────────────────────────────────────────────────────── #
def test_membership_save_state_is_atomic():
    d = _tmpdir("membership")
    import membership as M

    p = d / "membership.json"
    state = M._empty_state()
    torn: list = []
    writer_err: list = []
    stop = threading.Event()

    def writer(i: int) -> None:
        try:
            st = M._empty_state()
            st["permanent_credits"] = {"n": i, "pad": "y" * 20000}
            for _ in range(30):
                M._save_state(p, st)
        except Exception as e:  # noqa: BLE001
            writer_err.append(repr(e))

    def reader() -> None:
        while not stop.is_set():
            try:
                # ⚠️ 必须按**原始文件**解析，不能走 _load_state：后者解析失败会静默回落空态，
                # 于是「文件被写坏」在测试里永远看不见（实测过，属于空壳断言）。
                data = json.loads(p.read_text(encoding="utf-8"))
                assert len(data["permanent_credits"]["pad"]) == 20000, "读到了不完整的会员状态"
            except Exception as e:  # noqa: BLE001
                torn.append(repr(e))

    M._save_state(p, state)
    # 起始文件也要带 pad，否则读者在「写入前」读到的是另一种合法内容 → 误报
    seed = M._empty_state()
    seed["permanent_credits"] = {"n": -1, "pad": "y" * 20000}
    M._save_state(p, seed)
    rt = threading.Thread(target=reader, daemon=True)
    rt.start()
    ts = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stop.set()
    rt.join(timeout=5)
    assert not torn, f"会员状态被读到撕裂 {len(torn)} 次：{torn[:2]}"
    assert not writer_err, f"会员状态写者报错 {len(writer_err)} 次：{writer_err[:2]}"
    got = M._load_state(p)
    assert got["permanent_credits"]["pad"] == "y" * 20000, "会员状态内容不完整"
    assert not [f for f in d.iterdir() if f.name.endswith(".tmp")], "有临时文件残留"
    print("✅ 会员状态（用户付费数据）并发落盘：0 撕裂、0 写者报错、内容完整、无残留")


def test_voice_studio_config_merges_under_lock():
    """并发保存**不同字段** → 两个字段都必须留下（锁真的包住了「读基底→合并」）。"""
    d = _tmpdir("voicecfg")
    import voice_studio_config as V

    real_path = V._config_path
    V._config_path = lambda: d / "voice_studio_config.json"   # type: ignore[assignment]
    try:
        keys = [k for k in V.DEFAULTS][:2]
        assert len(keys) == 2, "DEFAULTS 至少要有两个键才能测「并发保存不互相覆盖」"
        barrier = threading.Barrier(2)
        real_write = atomic_io.atomic_write_json

        def slow_write(path, obj, **kw):
            try:
                barrier.wait(timeout=0.4)      # 没有锁时两个线程会同时走到这里
            except Exception:  # noqa: BLE001
                pass
            return real_write(path, obj, **kw)

        orig = V.atomic_io.atomic_write_json
        V.atomic_io.atomic_write_json = slow_write   # type: ignore[assignment]
        try:
            errs: list = []

            def save(k, v) -> None:
                try:
                    V.save_voice_studio_config({k: v})
                except Exception as e:  # noqa: BLE001
                    errs.append(repr(e))

            t1 = threading.Thread(target=save, args=(keys[0], "AAA"))
            t2 = threading.Thread(target=save, args=(keys[1], "BBB"))
            t1.start(); t2.start(); t1.join(); t2.join()
        finally:
            V.atomic_io.atomic_write_json = orig        # type: ignore[assignment]
        assert not errs, f"保存报错：{errs}"
        cfg = json.loads((d / "voice_studio_config.json").read_text(encoding="utf-8"))
        assert cfg.get(keys[0]) == "AAA", f"并发保存丢了 {keys[0]}：{cfg.get(keys[0])!r}"
        assert cfg.get(keys[1]) == "BBB", f"并发保存丢了 {keys[1]}：{cfg.get(keys[1])!r}"
        print(f"✅ 语音工坊配置并发保存：{keys[0]}/{keys[1]} 两个字段都保留（读合并写在锁内）")
    finally:
        V._config_path = real_path                    # type: ignore[assignment]


def test_llm_config_save_atomic_on_real_path():
    d = _tmpdir("llmcfg")
    import llm_config as L

    real_path = L._config_path
    L._config_path = lambda: d / "llm_config.json"    # type: ignore[assignment]
    try:
        L.save_llm_config({"provider": "deepseek", "api_key": "sk-test", "model": "m"})
        got = json.loads((d / "llm_config.json").read_text(encoding="utf-8"))
        assert got["api_key"] == "sk-test"
        mode = stat.S_IMODE((d / "llm_config.json").stat().st_mode)
        assert mode == 0o600, f"含 Key 的配置必须 0600，实际 {oct(mode)}"
        assert [f.name for f in d.iterdir()] == ["llm_config.json"], "残留临时文件"
    finally:
        L._config_path = real_path                    # type: ignore[assignment]
    print("✅ LLM 配置（含 Key）落盘：内容完整 / 0600 / 无残留")


def test_stats_and_subscriptions_atomic():
    d = _tmpdir("stats_subs")
    import stats as S
    import subscriptions as Sub

    sp = d / "stats.json"
    S._save(sp, S._empty_state())
    assert json.loads(sp.read_text(encoding="utf-8")) == S._empty_state()

    store = Sub.SubscriptionStore(d / "subs.json")
    store._save()
    assert (d / "subs.json").is_file()
    assert json.loads((d / "subs.json").read_text(encoding="utf-8")) == {"subscriptions": {}}
    assert not [f for f in d.iterdir() if f.name.endswith(".tmp")], "有临时文件残留"
    print("✅ 统计 / 订阅监控落盘：走原子写、无残留")


def test_home_dir_untouched():
    """硬约束：本测试文件绝不向真实 ~/.video-downloader 写任何东西。"""
    now = _snapshot_real()
    added = now - (_REAL_SNAPSHOT or set())
    assert not added, f"测试污染了用户数据目录：多出 {sorted(added)}"
    print("✅ 未向真实 ~/.video-downloader 写入任何文件")


if __name__ == "__main__":
    _REAL_SNAPSHOT = _snapshot_real()
    test_atomic_write_contract()
    test_unique_temp_path_keeps_extension_and_dir()
    test_atomic_write_survives_concurrent_writers()
    test_fixed_temp_name_tears_under_concurrency()
    test_repo_has_no_fixed_temp_name_writers()
    test_known_state_files_route_through_atomic_io()
    test_read_modify_write_paths_hold_mutation_lock()
    test_quota_concurrent_consume_caps_exactly()
    test_quota_cross_process_lock_serializes()
    test_quota_cross_process_lock_blocks_rival()
    test_quota_source_is_atomic_and_locked()
    test_quota_dual_copy_identical()
    test_membership_save_state_is_atomic()
    test_voice_studio_config_merges_under_lock()
    test_llm_config_save_atomic_on_real_path()
    test_stats_and_subscriptions_atomic()
    test_home_dir_untouched()
    print("\n🎉 配置/状态文件原子写测试全部通过（17 项）")
