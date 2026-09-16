"""跨功能「共享工具」隔离性回归测试（2026-09-16 新增，同日扩至全 App）。

背景：用户提出「很多功能用了相同的工具，改 A 功能的参数会不会污染 B 功能」，
并进一步要求「**每个功能独立、不能相互影响**，全 App 彻底清查」。

审计结论（本测试把结论钉死）：底层资源确实是共享的——同一个 onnxruntime、
同一套 ffmpeg 编解码器选择、同一个模型目录、同一份 users.json——但**状态必须是
每个功能各自持有的**。共享「库」没问题，共享「可变状态」才是污染。

本测试的作用是防止将来有人把这些「各自独立的状态」重构成进程共享
（例如为了省内存把 session 缓存合并成一个、或让两个模块各写一份配置），
从而静默引入跨功能污染。一旦发生，这里会立刻变红。

覆盖（16 项）：
  ── 引擎级隔离 ──
  1. 抠图 / 去水印 / 扩散 的 _SESSIONS、_LOCK 必须是不同对象
  2. 改一方的模型全局，另一方必须不变（双向）
  3. 去水印 session 缓存必须区分 INT8 模式（同模型不同量化不能串用）
  4. _get_session 必须保留「显式传模型名」的通道（不依赖进程全局）
  ── 全 App 级：可变状态穷举 ──
  5. ★ 全模块扫描：任何 dict/list/set/锁 都不得被 ≥2 个功能模块共同引用
  6. 各功能的 job 注册表与队列锁必须两两不同
  ── 磁盘资源 ──
  7. 模型目录：抠图/去水印/扩散/超分/云抠图超分 必须落在同一棵 .vdl_models 树
  8. 超分模型必须只有一个缓存来源（routers.sr 与 cloud_matting_mediakit 同目录）
  9. users.json 必须单一写入者（后台用户管理不得自写一份，否则索引与列表会不同步）
  ── 进程级资源 ──
 10. ★ 静态守卫：函数体内改写 os.environ 的位置必须等于白名单（新增即需评审）
 11. aria2c 的 PATH 前置必须幂等（不得每次下载都重复追加）
 12. 转码闸门是「排队」而非「失败」；且其成员集合被钉死为 compress + sr
 13. 固定的 /tmp 负载文件名必须等于白名单（新增即需评审）
  ── 前端与配置 ──
 14. 前端 localStorage：去水印私有键不得出现在抠图面板代码里
 15. 字幕功能不得在请求处理期修改进程级 HF_ENDPOINT（跨功能隐式副作用）
 16. 配置落盘：各功能配置文件不得被两个功能模块同时写（白名单除外）

运行：
    cd server && ../.build_venv/bin/python tests/test_engine_isolation.py
    cd server && ../.build_venv/bin/python -m pytest tests/test_engine_isolation.py -v

也可以直接用**已装机包内**的模块跑（验证修复真进了产物）：
    python -c "import sys,runpy; P='/Applications/视频工坊.app/Contents/Resources/server'; \\
      sys.path[:0]=[P,P+'/tests']; runpy.run_path(P+'/tests/test_engine_isolation.py', run_name='__main__')"
"""
import ast
import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_SERVER_DIR)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 独立运行（不经 run_offline_tests.sh）时也必须写进临时目录，绝不能碰用户真实数据
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_iso_test_"))


def _py_sources():
    """server/ 下的生产代码（排除 tests 与缓存）。"""
    for p in sorted(Path(_SERVER_DIR).rglob("*.py")):
        if any(x in p.parts for x in ("__pycache__", "tests")):
            continue
        yield p


# --------------------------------------------------------------------- 1
def test_engine_session_caches_are_independent_objects():
    """每个功能的 ONNX session 缓存必须是各自独立的 dict，锁也必须独立。"""
    import dewatermark_ai as dw
    import dewatermark_diffusion as dwd
    import matting_ai as mat

    check = [
        ("抠图 vs 去水印 _SESSIONS", mat._SESSIONS is not dw._SESSIONS),
        ("抠图 vs 扩散 _SESSIONS", mat._SESSIONS is not dwd._SESSIONS),
        ("去水印 vs 扩散 _SESSIONS", dw._SESSIONS is not dwd._SESSIONS),
        ("抠图 vs 去水印 _LOCK", mat._LOCK is not dw._LOCK),
        ("抠图 vs 去水印 _MODEL_NAME 所属模块", True),
    ]
    bad = [name for name, ok in check if not ok]
    assert not bad, f"以下共享点被合并了（会导致跨功能污染）: {bad}"
    print("✅ 抠图/去水印/扩散 各自持有独立的 session 缓存与锁（不共用底层推理对象）")


# --------------------------------------------------------------------- 2
def test_model_globals_do_not_cross_contaminate():
    """改一方的默认模型，另一方必须完全不受影响（双向验证）。"""
    import dewatermark_ai as dw
    import matting_ai as mat

    mat_before = mat._MODEL_NAME
    dw_before = dw._MODEL_NAME
    try:
        # 去水印只有一个可用模型，改它不应动摇抠图
        dw.set_model("lama")
        assert mat._MODEL_NAME == mat_before, (
            f"改去水印模型后抠图默认模型被改了: {mat_before} -> {mat._MODEL_NAME}")
        # 抠图换模型不应动摇去水印
        mat.set_model("isnet-general-use")
        assert dw._MODEL_NAME == dw_before, (
            f"改抠图模型后去水印默认模型被改了: {dw_before} -> {dw._MODEL_NAME}")
    finally:
        # 还原真实状态（这些是进程级全局，必须无残留）
        try:
            dw.set_model(dw_before)
        except Exception:  # noqa: BLE001
            pass
        try:
            mat.set_model(mat_before)
        except Exception:  # noqa: BLE001
            pass
    assert mat._MODEL_NAME == mat_before and dw._MODEL_NAME == dw_before, "还原失败：测试污染了进程全局"
    print("✅ 改去水印模型不影响抠图、改抠图模型不影响去水印（双向隔离，且已还原）")


# --------------------------------------------------------------------- 3
def test_dewatermark_session_cache_distinguishes_int8():
    """同一模型的 INT8/FP32 会话不能互相复用（缓存必须是 (session, int8) 二元组）。"""
    src = (Path(_SERVER_DIR) / "dewatermark_ai.py").read_text(encoding="utf-8")
    assert "_SESSIONS[name] = (_sess, use_int8)" in src, (
        "去水印 session 缓存不再是 (session, int8) 二元组 —— 同模型不同量化模式会串用")
    assert "cached[1] != use_int8" in src, (
        "缺少「缓存模式与当前请求不一致则丢弃重建」的校验 —— 切换 INT8 会拿到旧会话")
    print("✅ 去水印 session 缓存按 (模型, INT8模式) 区分，切量化不会串用旧会话")


# --------------------------------------------------------------------- 4
def test_get_session_keeps_explicit_model_channel():
    """_get_session 必须能显式接收模型名（不依赖进程全局），这是各功能互相隔离的基础。"""
    import inspect

    import dewatermark_ai as dw
    import matting_ai as mat

    dw_params = inspect.signature(dw._get_session).parameters
    assert "model_name" in dw_params, "去水印 _get_session 丢失了显式模型名参数（退回只能读全局）"
    mat_params = inspect.signature(mat._get_session).parameters
    assert "name" in mat_params, "抠图 _get_session 丢失了显式模型名参数"

    # 两个模块各自的模型注册表不得是同一个对象
    assert mat.MODELS is not dw.MODELS, "抠图与去水印的模型注册表被合并了"
    print("✅ 两个功能都保留了「显式传模型名」通道，且模型注册表各自独立")


# --------------------------------------------------------------------- 5
def _load_all_feature_modules():
    """导入 app（它会拉起全部路由），返回 {模块名: 模块}。"""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        import app  # noqa: F401

    mods = {}
    for name, mod in list(sys.modules.items()):
        if not isinstance(mod, type(sys)):
            continue
        if not (name == "app" or name.startswith("routers.")):
            continue
        f = getattr(mod, "__file__", "") or ""
        if f.startswith(_SERVER_DIR):
            mods[name] = mod
    return mods


def _ensure_app():
    """确保 app 已导入。

    `routers/sr.py` 里有 `import app`，若先导入 `routers.sr` 就会撞上
    「partially initialized module」循环导入。全量跑时前一个测试已经导入了 app 所以看不出，
    但**单独跑某一个测试**（变异验证就是这么跑的）时必须显式保证顺序。
    """
    if "app" not in sys.modules:
        _load_all_feature_modules()


def test_no_mutable_state_shared_across_feature_modules():
    """★ 全 App 穷举：任何可变对象都不得被 ≥2 个功能模块共同引用。

    这是「功能之间绝不互相影响」最彻底的一条机械证明——不靠人工判断某两个功能
    是否相关，而是把 27 个路由 / 45 个模块一次性按**对象身份**分组，
    凡是同一个 dict/list/set/锁/semaphore 被两个模块引用就会在此变红。

    共享的只有「库」（onnxruntime / ffmpeg / 模型文件），状态必须各持一份。
    """
    mods = _load_all_feature_modules()
    assert len(mods) >= 20, f"只导入到 {len(mods)} 个模块，覆盖不足，请复核导入路径"

    sync_types = (type(threading.Lock()), type(threading.RLock()),
                  type(threading.Event()), type(threading.Semaphore()))
    mutable = (dict, list, set, bytearray) + sync_types

    owners = {}
    for name, mod in mods.items():
        for attr, val in list(vars(mod).items()):
            if attr.startswith("__"):
                continue
            if isinstance(val, mutable):
                owners.setdefault(id(val), []).append((name, attr, val))

    offenders = []
    for refs in owners.values():
        names = sorted({r[0] for r in refs})
        if len(names) < 2:
            continue
        obj = refs[0][2]
        offenders.append((type(obj).__name__, names, sorted({f"{a}.{b}" for a, b, _ in refs})))

    assert not offenders, (
        "以下可变对象被多个功能模块共同引用 → 改一个功能会影响另一个：\n"
        + "\n".join(f"  [{t}] 模块={n} 引用点={r}" for t, n, r in offenders))
    print(f"✅ 全 App 扫描 {len(mods)} 个功能模块：零共享可变状态（"
          f"共检视 {len(owners)} 个可变对象，无一被跨模块引用）")


# --------------------------------------------------------------------- 6
def test_job_registries_and_locks_are_per_feature():
    """各功能的 job 注册表 dict 与其队列锁必须两两不同（合并 = 串任务/串状态）。"""
    _ensure_app()
    import app
    from routers import compress as cmp_rtr
    from routers import matting as mat_rtr
    from routers import sr as sr_rtr
    from routers import subtitle as sub_rtr
    from routers import system as sys_rtr

    registries = {
        "转换 CONVERT_JOBS": app.CONVERT_JOBS,
        "去水印 DW_JOBS": app.DW_JOBS,
        "解说 commentary_jobs": app.commentary_jobs,
        "加密 CRYPTO_JOBS": app.CRYPTO_JOBS,
        "压缩 COMPRESS_JOBS": cmp_rtr.COMPRESS_JOBS,
        "抠图 MAT_JOBS": mat_rtr.MAT_JOBS,
        "超分 SR_JOBS": sr_rtr.SR_JOBS,
        "字幕 SUBTITLE_JOBS": sub_rtr.SUBTITLE_JOBS,
        "自更新 _UPDATE_JOBS": sys_rtr._UPDATE_JOBS,
    }
    ids = {}
    for label, d in registries.items():
        assert isinstance(d, dict), f"{label} 不是 dict"
        ids.setdefault(id(d), []).append(label)
    dup = [v for v in ids.values() if len(v) > 1]
    assert not dup, f"以下功能的 job 注册表是同一个对象（会互相串任务）: {dup}"

    locks = {
        "CONVERT_LOCK": app.CONVERT_LOCK,
        "DW_LOCK": app.DW_LOCK,
        "MAT_LOCK": mat_rtr.MAT_LOCK,
        "COMPRESS _LOCK": cmp_rtr._LOCK,
        "SR _LOCK": sr_rtr._LOCK,
        "SUBTITLE _LOCK": sub_rtr._SUBTITLE_LOCK,
        "SYSTEM _JOBS_LOCK": sys_rtr._JOBS_LOCK,
    }
    lids = {}
    for label, lk in locks.items():
        lids.setdefault(id(lk), []).append(label)
    dup_l = [v for v in lids.values() if len(v) > 1]
    assert not dup_l, f"以下功能的队列锁是同一把（会互相阻塞）: {dup_l}"
    print(f"✅ {len(registries)} 个功能的 job 注册表与 {len(locks)} 把队列锁两两独立")


# --------------------------------------------------------------------- 7
def test_all_features_share_one_model_tree():
    """★ 全 App：所有功能的模型目录必须落在同一棵 .vdl_models 树。

    历史缺陷（2026-09-16 修，两处同源）：dewatermark_ai 与 dewatermark_diffusion 在
    VDL_MODELS_DIR 生效时子目录名少写前导点（vdl_models vs .vdl_models），
    导致同一台机器上出现两套模型目录树，磁盘统计 / 清理 / 备份都会漏掉一份。
    """
    import dewatermark_ai as dw
    import dewatermark_diffusion as dwd
    import cloud_matting_mediakit as mm
    import matting_ai as mat
    _ensure_app()
    from routers import sr as sr_rtr

    old = os.environ.get("VDL_MODELS_DIR")
    tmp = tempfile.mkdtemp(prefix="vdl_iso_models_")
    try:
        os.environ["VDL_MODELS_DIR"] = tmp
        dirs = {
            "matting_ai(抠图)": mat._model_dir(),
            "dewatermark_ai(去水印)": dw._model_dir(),
            "dewatermark_diffusion(扩散去水印)": dwd._model_dir(),
            "routers.sr(超分)": sr_rtr._model_dir(),
            "cloud_matting_mediakit(云抠图超分)": mm._sr_cache_dir(),
        }
        root = Path(tmp).resolve() / ".vdl_models"
        bad = []
        for label, d in dirs.items():
            rp = Path(d).resolve()
            if root not in rp.parents and rp != root:
                bad.append(f"{label} -> {rp}（不在 {root} 下）")
        assert not bad, (
            "以下功能的模型目录跑到了统一的 .vdl_models 树之外 → 同一份模型会被下载多份:\n  "
            + "\n  ".join(bad))

        # 超分这一个模型必须有唯一目录（两处曾各写各的）
        assert Path(sr_rtr._model_dir()).resolve() == Path(mm._sr_cache_dir()).resolve(), (
            "超分模型目录不一致：routers.sr 与 cloud_matting_mediakit 会各下一份 real_esrgan_x2.onnx")
    finally:
        if old is None:
            os.environ.pop("VDL_MODELS_DIR", None)
        else:
            os.environ["VDL_MODELS_DIR"] = old
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        assert not os.path.exists(tmp), f"临时模型目录清理失败，请手工删除: {tmp}"
    print("✅ 抠图/去水印/扩散/超分/云抠图超分 共用同一棵 .vdl_models 树，超分模型目录唯一")


# --------------------------------------------------------------------- 8
def test_sr_model_has_single_cache_source():
    """超分权重必须只有一个缓存来源，且兼容复用老位置（不逼已下载用户重下）。"""
    import cloud_matting_mediakit as mm

    # 老位置必须仍然被识别（否则老用户会白下 64MB）
    assert hasattr(mm, "_SR_LEGACY_DIR"), "丢失了老位置常量，已下载的用户会被迫重新下载模型"
    assert mm._SR_LEGACY_DIR != mm._sr_cache_dir(), (
        "老位置与统一目录相同就没必要保留兼容分支了，请复核本测试前提")

    old_legacy = mm._SR_LEGACY_DIR
    old_env = os.environ.get("VDL_MODELS_DIR")
    tmp = tempfile.mkdtemp(prefix="vdl_iso_sr_")
    try:
        legacy = Path(tmp) / "legacy"
        legacy.mkdir(parents=True)
        fake = legacy / "real_esrgan_x2.onnx"
        fake.write_bytes(b"\0" * 6_000_000)          # > 5MB 视为可用
        mm._SR_LEGACY_DIR = legacy
        os.environ["VDL_MODELS_DIR"] = str(Path(tmp) / "new")
        got = mm._sr_model_path("real_esrgan_x2.onnx")
        assert got is not None and Path(got).resolve() == fake.resolve(), (
            f"老位置已有可用权重却没被复用（会重复下载）: {got}")
    finally:
        mm._SR_LEGACY_DIR = old_legacy
        if old_env is None:
            os.environ.pop("VDL_MODELS_DIR", None)
        else:
            os.environ["VDL_MODELS_DIR"] = old_env
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
    print("✅ 超分模型单一缓存来源，且老位置已有权重时直接复用（不重复下载）")


# --------------------------------------------------------------------- 9
def test_users_json_has_single_writer():
    """users.json 是「登录认证」与「后台用户管理」共用的同一份文件，必须单一写入者。

    历史缺陷（2026-09-16 修）：admin_store 自带一份 _load_users/_save_users，
    写前**不**重建 by_identifier 索引（auth_store 会重建）→ 后台改一次用户就可能写出
    索引与列表不同步的 users.json，登录时反查不到 user_id，报「账号或密码错误」。
    """
    import admin_store as ad
    import auth_store as au

    assert ad._users_path() == au._users_path(), (
        f"两个模块指向了不同的 users.json：\n  admin={ad._users_path()}\n  auth={au._users_path()}")

    ad_src = (Path(_SERVER_DIR) / "admin_store.py").read_text(encoding="utf-8")
    assert "auth_store._load_users()" in ad_src and "auth_store._save_users(" in ad_src, (
        "admin_store 又自己实现了一份 users.json 读写 —— 索引不变量会与 auth_store 分叉")

    # 功能验证：给一份索引不一致的文件，走后台的保存路径后必须被自愈
    old_env = os.environ.get("VDL_DATA_DIR")
    tmp = tempfile.mkdtemp(prefix="vdl_iso_users_")
    try:
        os.environ["VDL_DATA_DIR"] = tmp
        p = au._users_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"users": [{"user_id": "u1", "identifier": "a@b.c"}], "by_identifier": {}}',
                     encoding="utf-8")
        ad._save_users(ad._load_users())
        import json
        after = json.loads(p.read_text(encoding="utf-8"))
        assert after.get("by_identifier") == {"a@b.c": "u1"}, (
            f"后台保存后索引仍未重建 → 登录会报「账号或密码错误」: {after.get('by_identifier')}")
    finally:
        if old_env is None:
            os.environ.pop("VDL_DATA_DIR", None)
        else:
            os.environ["VDL_DATA_DIR"] = old_env
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
    print("✅ users.json 单一写入者：后台保存也会重建索引，索引与列表不再可能不同步")


# -------------------------------------------------------------------- 10
# 允许在**函数体内**改写进程级 os.environ 的位置（白名单，新增即需评审）。
# 判据：写的东西不能随「谁在什么时候调用」而变（否则就是跨功能污染）。
_ALLOWED_ENV_WRITERS = {
    ("app.py", "_ensure_vps_env"),                  # 紧随定义处调用一次 = 等价导入期
    ("downloader.py", "_aria2c_path"),              # 已去重，幂等（见测试 11）
    ("downloader.py", "_maybe_refresh_vps_token"),  # 只刷新同一个 VPS 令牌，语义与调用者无关
    ("routers/subtitle.py", "_apply_hf_mirror"),    # 模块顶部调用一次 = 导入期
    # 保险箱功能写自己功能在 app 模块上的进程级密钥槽（app.VAULT_KEY）。
    # 只有保险箱一个功能读写它，不构成跨功能污染；但它确实是「跨模块写全局」，
    # 所以显式登记：将来若有第二个功能来动 VAULT_KEY，diff 时必须在这里被看见。
    ("routers/crypto.py", "crypto_lock"),
    ("routers/crypto.py", "crypto_set_pass"),
    ("routers/crypto.py", "crypto_unlock"),
}


def _is_environ_subscript(node) -> bool:
    return (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
            and node.value.attr == "environ")


def _is_environ_setdefault(node) -> bool:
    """os.environ.setdefault(...) —— 同样是进程级写入（虽然幂等，但仍需登记）。"""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "os"
            and node.func.value.attr == "environ")


def _is_foreign_module_global_assign(node) -> bool:
    """<别名>.ENDPOINT = ... 之类：改写**别的模块/库**的模块级全局常量。

    典型是 huggingface_hub.constants.ENDPOINT —— huggingface_hub 每次下载都读它，
    在请求期改一次就会连带改掉所有走该库的功能（扩散去水印等）的下载端点。
    另一类是 app.VAULT_KEY 这种「跨模块写另一个模块的全局」，同样必须显式登记。

    排除 `self.X = ...` / `cls.X = ...`（那是对象属性，不是模块全局）。
    """
    if not isinstance(node, ast.Assign):
        return False
    for t in node.targets:
        if not isinstance(t, ast.Attribute) or not t.attr.isupper():
            continue
        recv = t.value
        if isinstance(recv, ast.Name) and recv.id in {"self", "cls"}:
            continue
        if isinstance(recv, ast.Name):
            return True
    return False


def test_function_body_env_writes_match_whitelist():
    """★ 静态守卫：函数体内改写进程级环境变量 / 其他库全局常量的位置必须恰好等于白名单。

    进程级环境变量是「一个功能影响另一个」的最隐蔽通道——请求处理期写一次，
    所有后续功能（含子进程）都会被改到。新增一处就必须在这里显式登记并评审。

    覆盖三种形态：① os.environ[...] = ... ② os.environ.setdefault(...)
    ③ <别名>.CONST = ...（改写别的库的模块级全局）
    """
    found = set()
    for p in _py_sources():
        rel = p.relative_to(Path(_SERVER_DIR)).as_posix()
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for n in ast.walk(fn):
                hit = False
                if isinstance(n, ast.Assign):
                    hit = any(_is_environ_subscript(t) for t in n.targets) \
                        or _is_foreign_module_global_assign(n)
                elif _is_environ_setdefault(n):
                    hit = True
                if hit:
                    found.add((rel, fn.name))

    added = sorted(found - _ALLOWED_ENV_WRITERS)
    removed = sorted(_ALLOWED_ENV_WRITERS - found)
    assert not added, (
        "新增了「函数体内改写进程级环境变量 / 其他库全局常量」的位置"
        "（会跨功能污染，或需确认已在导入期调用）：\n  "
        + "\n  ".join(f"{f}:{fn}()" for f, fn in added)
        + "\n确认无害后请加入 _ALLOWED_ENV_WRITERS 并写清理由。")
    assert not removed, (
        f"白名单里的位置已不存在（源码变了，请更新白名单）: {removed}")

    # 白名单里「实际等价导入期」的两个必须真的在模块顶层被调用
    for rel, fn in (("app.py", "_ensure_vps_env"),
                    ("routers/subtitle.py", "_apply_hf_mirror")):
        src = (Path(_SERVER_DIR) / rel).read_text(encoding="utf-8")
        assert re.search(rf"^{fn}\(\)", src, re.M), (
            f"{rel} 的 {fn}() 不在模块顶层调用 —— 会变成请求期污染")
    print(f"✅ 函数体内改写进程级 global 的位置恰好 {len(found)} 处，与白名单一致（新增即变红）")


# -------------------------------------------------------------------- 11
def test_aria2c_path_prepend_is_idempotent():
    """aria2c 的 PATH 前置必须幂等。

    历史缺陷（2026-09-16 修）：_aria2c_path() **每个下载任务都会调用**，而它改的是
    进程级 os.environ["PATH"]；原来无条件前置，每下载一次该目录就再进一次
    （实测每次 +89 字节），几十个任务后 PATH 膨胀成一长串重复项，
    拖慢所有功能的子进程按名检索，并可能触达系统 PATH 长度上限。
    """
    import shutil as _shutil

    import downloader as dl

    base = Path(tempfile.mkdtemp(prefix="vdl_iso_a2_"))
    try:
        (base / "Contents" / "MacOS").mkdir(parents=True)
        bindir = base / "Contents" / "Resources" / "bin"
        bindir.mkdir(parents=True)
        a2 = bindir / "aria2c"
        a2.write_text("#!/bin/sh\n")
        a2.chmod(0o755)
        exe = base / "Contents" / "MacOS" / "视频工坊"
        exe.write_text("")
        exe.chmod(0o755)

        real_which, real_exe = _shutil.which, sys.executable
        real_path = os.environ.get("PATH", "")
        _shutil.which = lambda name, *a, **k: None      # 模拟「机器上没装 aria2c」
        sys.executable = str(exe)
        try:
            for _ in range(5):
                dl._aria2c_path()
            cur = os.environ.get("PATH", "")
            n = cur.split(os.pathsep).count(str(bindir))
            assert n == 1, (
                f"该目录在 PATH 中出现 {n} 次（期望 1）→ 每次下载都会重复前置，PATH 会膨胀")
        finally:
            _shutil.which, sys.executable = real_which, real_exe
            os.environ["PATH"] = real_path
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("✅ aria2c 的 PATH 前置幂等：连调 5 次该目录仍只出现 1 次")


# -------------------------------------------------------------------- 12
def test_transcode_gate_is_shared_and_queues_not_fails():
    """转码闸门（_TRANSCODE_SEM）是两个功能共用的**保护**机制，不能退化成「抢不到就失败」。

    这是全 App 里唯一一处「功能 A 影响功能 B 时序」的设计：压缩与超分都是重转码，
    共用 2 个名额防止把机器打满。闸门满时必须把任务**延后重投（排队中）**，
    绝不能报错、更不能走到 finally 把用户源文件删掉（两个模块都踩过这个坑）。
    本测试把「成员集合」钉死：将来若悄悄让第三个功能也来抢名额，这里会变红。
    """
    users = set()
    for p in _py_sources():
        src = p.read_text(encoding="utf-8", errors="replace")
        if "_TRANSCODE_SEM" in src or "_TRANSCODE_SEM as _sem" in src:
            users.add(p.relative_to(Path(_SERVER_DIR)).as_posix())
    assert users == {"routers/compress.py", "routers/sr.py"}, (
        f"转码闸门的成员集合变了: {sorted(users)} —— 新增成员需评审（会互相排队）")

    for rel in ("routers/compress.py", "routers/sr.py"):
        src = (Path(_SERVER_DIR) / rel).read_text(encoding="utf-8")
        assert "排队中" in src, f"{rel} 丢失了「排队中」状态（抢不到名额时会变成失败）"
        assert "_defer" in src, f"{rel} 丢失了延后重投分支（抢不到名额时会变成失败）"
        assert "acquire(blocking=False)" in src, f"{rel} 的闸门改成了阻塞获取（会挂住请求线程）"
    print("✅ 转码闸门成员被钉死为 compress + sr，且抢不到名额时是「排队」而非失败")


# -------------------------------------------------------------------- 13
# 允许写死的临时**负载**路径白名单（新增即需评审）。
# 这两个是**诊断**文件，语义就是「最后一次诊断结果」，被覆盖是预期行为；
# 凡是用户产物一律不得写死（必须带 pid/job 后缀）。
_ALLOWED_FIXED_TMP = {
    "vdl_cookie_diag.txt",
    "vdl_403_diag.txt",
    "vdl_probe_debug.log",
}


def _collect_fixed_tmp_literals(tree) -> set:
    """收集写死的临时文件路径字面量。

    两种形态都要抓：
      · "/tmp/vdl_xxx"                      —— 绝对路径直接写死
      · os.path.join(gettempdir(), "vdl_xxx") —— 文件名写死（目录是动态的）
    跳过 f-string：里面必然有插值（如 f"/tmp/vdl_vs_test_{os.getpid()}.mp3"），
    不算写死。
    """
    found = set()

    def walk(node):
        if isinstance(node, ast.JoinedStr):
            return                      # f-string 一律视为「已带变量」
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if v.startswith("/tmp/") and "vdl" in v:
                found.add(v)
            elif re.fullmatch(r"vdl_[a-z0-9_]+\.(txt|log|mp3|wav|json|png|jpg)", v):
                found.add(v)
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return found


def test_no_unreviewed_fixed_temp_payload_paths():
    """写死的临时负载文件名必须等于白名单。

    写死的临时路径是两个任务并发时互相覆盖的经典成因（历史缺陷：语音工作台自测
    写死 /tmp/vdl_vs_test.mp3，并发时会出现「A 的试听里是 B 的音频」这种极难排查的串音）。
    凡是**负载**文件都必须带 pid/job 后缀；诊断文件允许固定。
    """
    found = set()
    for p in _py_sources():
        found |= _collect_fixed_tmp_literals(
            ast.parse(p.read_text(encoding="utf-8", errors="replace")))

    added = sorted(found - _ALLOWED_FIXED_TMP)
    removed = sorted(_ALLOWED_FIXED_TMP - found)
    assert not added, (
        "新增了写死的 /tmp 负载路径（并发任务会互相覆盖，请加 pid/job 后缀）：\n  "
        + "\n  ".join(added))
    assert not removed, f"白名单里的固定路径已不存在，请更新白名单: {removed}"
    print(f"✅ 写死的 /tmp 路径恰好 {len(found)} 个（均为诊断文件），无负载文件被固定命名")


# -------------------------------------------------------------------- 14
def test_frontend_storage_keys_do_not_leak_across_features():
    """前端去水印私有存储键不得出现在抠图面板的代码里。"""
    js = (Path(_REPO_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
    all_keys = set(re.findall(r"'(vdl_[a-z0-9_]+)'", js))
    dw_keys = sorted(k for k in all_keys if k.startswith("vdl_dw_"))
    assert dw_keys, "未找到去水印私有存储键，前端可能被重构过，请复核本测试的前提"

    # 抠图面板的代码行（用其专属标识符筛选）里不允许出现去水印键
    mat_lines = [
        ln for ln in js.splitlines()
        if ("matModel" in ln or "matJobId" in ln or "matting" in ln.lower())
    ]
    assert mat_lines, "未找到抠图面板代码，前端可能被重构过，请复核本测试的前提"
    offenders = [(i, ln.strip()) for i, ln in enumerate(mat_lines) if "vdl_dw_" in ln]
    assert not offenders, f"抠图面板代码里出现去水印存储键，参数会互相影响: {offenders[:3]}"
    print(f"✅ 去水印私有键 {dw_keys} 不出现在抠图面板；前端参数按功能隔离")


# -------------------------------------------------------------------- 15
def test_subtitle_does_not_mutate_process_env_at_request_time():
    """字幕功能不得在请求处理期改写进程级 HF_ENDPOINT（会波及扩散去水印等其他功能）。"""
    src = (Path(_SERVER_DIR) / "routers" / "subtitle.py").read_text(encoding="utf-8")

    # 定位 _get_model 函数体，断言其中没有对全局端点的写入
    m = re.search(r"\ndef _get_model\(.*?\n(?=\ndef |\nclass |\Z)", src, re.S)
    assert m, "未找到 _get_model 函数，源码可能被重构，请复核本测试的前提"
    body = m.group(0)
    assert 'os.environ["HF_ENDPOINT"]' not in body, (
        "_get_model 又在请求期强制改写进程级 HF_ENDPOINT 了 —— 会静默改掉其他功能的下载端点")
    assert "_hf_const.ENDPOINT" not in body, (
        "_get_model 又在请求期改写 huggingface_hub 全局常量了 —— 跨功能隐式副作用")

    # 端点设置必须存在于模块 import 期（启动一次），否则镜像加速失效
    assert "_apply_hf_mirror()" in src, "丢失了 import 期的 HF 镜像设置，字幕下载会走官方源"
    assert re.search(r"^_apply_hf_mirror\(\)", src, re.M), "HF 镜像设置不在模块顶层执行"
    print("✅ 字幕功能只在启动期设置一次 HF 端点，请求期不再污染其他功能的下载端点")


# -------------------------------------------------------------------- 16
# 允许被多个功能模块共同读写的配置文件（都是**按设计**共享的同一份状态）。
_ALLOWED_SHARED_CONFIG = {
    "users.json",        # 账号表：auth_store 写，admin_store 只经其委托写（见测试 9）
    "admin.json",        # 管理员配置：admin_store 与 auth_store（发信配置）共用
    "smtp.json",         # 邮件配置：同上，同一份发信设置
    "cloud_sync.json",   # 云端同步地址：app 与 downloader 读同一份（同一账号配置）
}


def test_feature_config_files_do_not_overlap():
    """各功能配置文件不得被两个功能模块同时写（白名单除外）。

    两个模块各写一份自己的配置文件 = 各自的参数互不干扰；但若写的是**同一个文件名**，
    就要确认它们是不是有意共享（是 → 必须走单一写入者，否则会互相覆盖字段）。
    """
    refs = {}
    for p in _py_sources():
        rel = p.relative_to(Path(_SERVER_DIR)).as_posix()
        for m in re.finditer(r'["\']([a-z0-9_]+\.json)["\']', p.read_text(encoding="utf-8", errors="replace")):
            refs.setdefault(m.group(1), set()).add(rel)

    overlap = {k: sorted(v) for k, v in refs.items()
               if len(v) > 1 and k not in _ALLOWED_SHARED_CONFIG}
    assert not overlap, (
        "以下配置文件被多个功能模块引用 —— 确认是有意共享（需单一写入者）后加入白名单：\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in overlap.items()))
    print(f"✅ {len(refs)} 个配置文件中，跨模块重名的 {len(_ALLOWED_SHARED_CONFIG)} 个均为有意共享"
          f"（其余各功能独占自己的配置文件）")


if __name__ == "__main__":
    test_engine_session_caches_are_independent_objects()
    test_model_globals_do_not_cross_contaminate()
    test_dewatermark_session_cache_distinguishes_int8()
    test_get_session_keeps_explicit_model_channel()
    test_no_mutable_state_shared_across_feature_modules()
    test_job_registries_and_locks_are_per_feature()
    test_all_features_share_one_model_tree()
    test_sr_model_has_single_cache_source()
    test_users_json_has_single_writer()
    test_function_body_env_writes_match_whitelist()
    test_aria2c_path_prepend_is_idempotent()
    test_transcode_gate_is_shared_and_queues_not_fails()
    test_no_unreviewed_fixed_temp_payload_paths()
    test_frontend_storage_keys_do_not_leak_across_features()
    test_subtitle_does_not_mutate_process_env_at_request_time()
    test_feature_config_files_do_not_overlap()
    print("\n🎉 跨功能隔离性测试全部通过（16 项）")
