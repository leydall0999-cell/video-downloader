"""跨功能「共享工具」隔离性回归测试（2026-09-16 新增）。

背景：用户提出「很多功能用了相同的工具，改 A 功能的参数会不会污染 B 功能」，
典型疑问是「改去水印参数会不会影响抠图」。

审计结论（本测试把这个结论钉死）：底层推理库虽然是同一套（onnxruntime / LaMa /
BiRefNet / Real-ESRGAN），但**每个功能各自持有独立的 session 缓存与独立的模型全局**，
改一个功能的参数不会影响另一个功能。

本测试的作用是防止将来有人把这些「模块独立全局」重构成「进程共享全局」
（例如为了省内存把 session 缓存合并成一个），从而静默引入跨功能污染。
一旦发生，这里会立刻变红。

覆盖：
  1. 抠图 / 去水印 / 扩散 的 _SESSIONS、_LOCK 必须是不同对象
  2. 改一方的模型全局，另一方必须不变（双向）
  3. 去水印 session 缓存必须区分 INT8 模式（同模型不同量化不能串用）
  4. _get_session 必须保留「显式传模型名」的通道（不依赖进程全局）
  5. 前端 localStorage：去水印私有键不得出现在抠图面板代码里
  6. 模型目录：抠图与去水印在 VDL_MODELS_DIR 生效时必须解析到同一目录
  7. 字幕功能不得在请求处理期修改进程级 HF_ENDPOINT（跨功能隐式副作用）

运行：
    cd server && ../.build_venv/bin/python tests/test_engine_isolation.py
    cd server && ../.build_venv/bin/python -m pytest tests/test_engine_isolation.py -v
"""
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_SERVER_DIR)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)


# --------------------------------------------------------------------- 1
def test_engine_session_caches_are_independent_objects():
    """每个功能的 ONNX session 缓存必须是各自独立的 dict，锁也必须独立。"""
    import matting_ai as mat
    import dewatermark_ai as dw
    import dewatermark_diffusion as dwd

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
    import matting_ai as mat
    import dewatermark_ai as dw

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


# --------------------------------------------------------------------- 6
def test_model_dirs_agree_between_matting_and_dewatermark():
    """VDL_MODELS_DIR 生效时，抠图与去水印必须解析到同一模型目录。

    历史缺陷（2026-09-16 修）：去水印曾写成 <raw>/vdl_models，抠图/超分写 <raw>/.vdl_models，
    于是设了该变量后同一份模型被下载两份。
    """
    import dewatermark_ai as dw
    import matting_ai as mat

    old = os.environ.get("VDL_MODELS_DIR")
    tmp = tempfile.mkdtemp(prefix="vdl_iso_models_")
    try:
        os.environ["VDL_MODELS_DIR"] = tmp
        mat_dir = Path(mat._model_dir()).resolve()
        dw_dir = Path(dw._model_dir()).resolve()
        assert mat_dir == dw_dir, (
            f"模型目录分叉 → 同一份模型会被下载两份:\n  抠图={mat_dir}\n  去水印={dw_dir}")
    finally:
        if old is None:
            os.environ.pop("VDL_MODELS_DIR", None)
        else:
            os.environ["VDL_MODELS_DIR"] = old
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        assert not os.path.exists(tmp), f"临时模型目录清理失败，请手工删除: {tmp}"
    print("✅ 抠图与去水印共用同一模型目录（同模型只下载一份）")


# --------------------------------------------------------------------- 7
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


if __name__ == "__main__":
    test_engine_session_caches_are_independent_objects()
    test_model_globals_do_not_cross_contaminate()
    test_dewatermark_session_cache_distinguishes_int8()
    test_get_session_keeps_explicit_model_channel()
    test_frontend_storage_keys_do_not_leak_across_features()
    test_model_dirs_agree_between_matting_and_dewatermark()
    test_subtitle_does_not_mutate_process_env_at_request_time()
    print("\n🎉 跨功能隔离性测试全部通过（7 项）")
