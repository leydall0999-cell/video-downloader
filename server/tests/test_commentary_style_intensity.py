"""解说「风格强度」链路离线回归测试（2026-09-21 新增）。

背景：本轮给「解说」加「风格强度」滑杆（0~100，默认 65），要求它从
  前端 payload → CommentaryRequest → _commentary_option_args（CLI）→ 管线 process.py/llm_script
整条贯通。实现时 **CommentaryRequest 漏加 style_intensity 字段**，而
`routers/commentary.py` 在 JSON 请求路径上直接访问 `payload.style_intensity`：

  - pyflakes / `test_static_undefined_names.py` **查不到**这类「属性在模型里不存在」
    （它只查未定义**名**，不查 Pydantic 字段）；
  - 现有离线套件也不覆盖 —— 于是「本地全绿，但前端主路径一发请求就 AttributeError」。

实测复现：探针 `A.CommentaryRequest.model_fields` 里没有 `style_intensity`，
而 router 第 411/980 行已经在读它。本测试把这条链路、以及
「payload 引用的字段必须真实存在」这一整类缺陷钉成断言。

覆盖：
  1. CommentaryRequest.style_intensity 存在 / 默认 65 / 界 [0,100]（崩溃 bug 的直接守卫）
  2. _commentary_option_args 按风格产出 `--style <s> --style-intensity <n>`
  3. 强度两端（30 / 85）产出不同 CLI（滑杆真的改变行为，不是摆设）
  4. ★ AST 棘轮：routers 里每处 `payload.X` 都必须是该函数绑定模型里真实存在的字段
  5. 源码断言：CommentaryRequest 的两条 JSON 路径都把 style_intensity 传下去

运行：
    cd server && VDL_DATA_DIR=/tmp/vdl_si_test python tests/test_commentary_style_intensity.py
    cd server && VDL_DATA_DIR=/tmp/vdl_si_test python -m pytest tests/test_commentary_style_intensity.py -v
"""
import ast
import os
import sys

# VDL_DATA_DIR 必须在 import app 之前定好，否则会去写真实 ~/.video-downloader
os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_si_test")

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)          # noqa: E402

import app as server_app                     # noqa: E402  （必须早于 routers，避免循环导入）
from routers import commentary as _cr        # noqa: E402

_ROUTER_PATH = os.path.join(_SERVER_DIR, "routers", "commentary.py")
_APP_PATH = os.path.join(_SERVER_DIR, "app.py")


def _pair(args, flag):
    """取 `--flag value` 的相邻对；不存在返回 None。"""
    if flag in args:
        i = args.index(flag)
        return (args[i], args[i + 1]) if i + 1 < len(args) else None
    return None


def test_model_has_style_intensity():
    """★ 本 bug 的直接守卫：字段必须存在，且默认/边界正确。"""
    fields = server_app.CommentaryRequest.model_fields
    assert "style_intensity" in fields, (
        "CommentaryRequest 必须有 style_intensity 字段 —— "
        "否则 routers/commentary.py 的 payload.style_intensity 会在 JSON 请求路径上 AttributeError")
    f = fields["style_intensity"]
    assert f.default == 65, f"默认强度应为 65，实际 {f.default}"
    meta = {type(m).__name__: m for m in f.metadata}
    assert "Ge" in meta and getattr(meta["Ge"], "ge", None) == 0, "强度下界应为 0"
    assert "Le" in meta and getattr(meta["Le"], "le", None) == 100, "强度上界应为 100"
    print("✅ CommentaryRequest.style_intensity 存在、默认 65、界 [0,100]")


def test_cli_emits_style_intensity():
    """滑杆必须真的翻成 CLI 参数，否则管线根本收不到。"""
    args = server_app._commentary_option_args(style="funny", style_intensity=85)
    assert _pair(args, "--style") == ("--style", "funny"), args
    assert _pair(args, "--style-intensity") == ("--style-intensity", "85"), args
    print("✅ --style funny --style-intensity 85 按预期拼出")


def test_intensity_endpoints_differ():
    """两端强度产出不同 CLI —— 证明是「可调」而非写死。"""
    lo = server_app._commentary_option_args(style="funny", style_intensity=30)
    hi = server_app._commentary_option_args(style="funny", style_intensity=85)
    assert _pair(lo, "--style-intensity") == ("--style-intensity", "30"), lo
    assert _pair(hi, "--style-intensity") == ("--style-intensity", "85"), hi
    assert lo != hi, "30 与 85 应产出不同命令行"
    print("✅ 强度 30 / 85 产出不同 CLI（滑杆真能改变行为）")


def _router_payload_models():
    """解析 router：每个函数 `payload` 参数注解的模型名，及其函数体内 payload.X 的引用。"""
    tree = ast.parse(open(_ROUTER_PATH, encoding="utf-8").read())
    out = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        model = None
        for a in fn.args.args:
            if a.arg != "payload" or a.annotation is None:
                continue
            ann = a.annotation
            if isinstance(ann, ast.Attribute):        # app.CommentaryRequest
                model = ann.attr
            elif isinstance(ann, ast.Name):           # CommentaryRequest
                model = ann.id
        if not model:
            continue
        used = set()
        for n in ast.walk(fn):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                    and n.value.id == "payload"):
                used.add(n.attr)
        out.append((fn.name, model, used))
    return out


def _app_class_fields():
    tree = ast.parse(open(_APP_PATH, encoding="utf-8").read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names = {st.target.id for st in node.body
                     if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)}
            if names:
                out[node.name] = names
    return out


def test_payload_fields_all_exist():
    """★ AST 棘轮：`payload.X` 引用的字段必须真实存在于该函数绑定的模型里。

    这类「字段名打错 / 忘记给模型加字段」缺陷 pyflakes 抓不到，只有运行到那行才炸。
    本断言在静态阶段就挡住，且对**将来新增**的 payload 引用同样生效。
    """
    models = _app_class_fields()
    usages = _router_payload_models()
    assert usages, "未解析到任何 payload 端点，前提失效（router 结构变了？）"
    problems = []
    for fn_name, model, used in usages:
        fields = models.get(model)
        assert fields is not None, f"{fn_name} 的 payload 模型 {model} 未在 app.py 找到"
        missing = sorted(used - fields - {"model_dump", "dict", "json"})   # 排除方法名
        for attr in missing:
            problems.append(f"{fn_name}: payload.{attr} 在 {model} 中不存在")
    assert not problems, "存在幽灵字段引用（运行时会 AttributeError）：\n  " + "\n  ".join(problems)
    print(f"✅ AST 棘轮：{len(usages)} 个 payload 端点的字段引用全部真实存在")


def test_router_forwards_intensity_on_both_paths():
    """源码断言：两条 JSON 路径 + 一条 form 路径都必须把强度传下去。"""
    src = open(_ROUTER_PATH, encoding="utf-8").read()
    n_json = src.count("style_intensity=payload.style_intensity,")
    assert n_json >= 2, f"JSON 路径应有 ≥2 处透传 style_intensity，实际 {n_json}"
    assert "style_intensity=style_intensity" in src, "form 路径应透传 style_intensity"
    assert "style_intensity: int = app.Form(65)" in src, "form 路径应有 style_intensity Form 参数"
    print(f"✅ router 透传齐备（JSON {n_json} 处 + form 1 处）")


if __name__ == "__main__":
    test_model_has_style_intensity()
    test_cli_emits_style_intensity()
    test_intensity_endpoints_differ()
    test_payload_fields_all_exist()
    test_router_forwards_intensity_on_both_paths()
    print("\n🎉 解说风格强度链路测试全部通过（5 项）")
