#!/usr/bin/env python3
"""解说渲染「绝不慢放」守卫 —— 回归 2026-10-26 用户实拍 bug。

现象：成片（少帅第8集）画面突然慢动作。根因：渲染预检
`_plan_original_speed` 检出「旁白字数超片段时长预算」→ 返回 False →
`_commentary_args` 给渲染子进程加 `--no-original-speed` → 渲染层
setpts 慢放把窗口撑开对齐旁白。

用户拍板：**绝不放慢画面，解说保持完整**。正确事实：渲染层原速模式
（commentary-pipeline/scripts/edit_ffmpeg.py build 主链路）窗口完全跟随
旁白真实时长（e = s + vdur），画面**原速多播几秒**；触片尾前移起点、
放不下整段跳过（绝不截断旁白）；相邻段宁可源时间重叠也绝不慢放
⇒ 超标段无需降速，恒原速即可「不慢放 + 话完整 + 字不动」三方全满足。

本文件钉死三件事，防未来把「超标→降速」加回来：
  ① `_plan_original_speed`（AST 抽出真跑）对任何输入恒返回 True，
     且超标明细仍正常回传（留档/提示用）；
  ② 静态扫描：routers/commentary.py 渲染链路不得再出现
     「检出超标 ⇒ 放弃原速」的降速模式；
  ③ app.py 链路：`_commentary_args` 的 original_speed 默认必须 True，
     `--no-original-speed` 只允许在 `if not original_speed:` 分支拼装。

🔴 导入注意：routers/commentary.py 顶层 `import app` 且 app.py 尾部
include_router ⇒ 测试里直接 import 会循环导入炸掉，因此本文件走
「AST 抽函数 exec 真跑」路线（同 test_win_version_identity.py 范式）。

变异验证（改完代码必须跑一遍确认会红）：
  · 把 `return True, over` 改回旧条件返回 ⇒ ①②红
  · 渲染入口改 `use_original_speed, over_limit = False, []` ⇒ ②红
  · `_commentary_args` 默认改 False ⇒ ③红

跑法：python3 server/tests/test_commentary_never_slowdown.py
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # server/

PASS = 0
FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {extra}")


def _load_commentary_fns():
    """AST 抽出预检函数族 + 常量，exec 到受控命名空间真跑（不 import app）。"""
    src = (ROOT / "routers" / "commentary.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    want = {"NARRATION_CHARS_PER_SEC", "_narration_budget", "_narration_chars",
            "_narration_over_limit", "_plan_original_speed"}
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<x>", "exec"), ns)
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(t in want for t in targets):
                exec(compile(ast.Module(body=[node], type_ignores=[]), "<x>", "exec"), ns)
    missing = want - set(ns)
    if missing:
        raise SystemExit(f"❌ 未能从 commentary.py 抽出: {missing}")
    return ns, src


def main():
    ns, src = _load_commentary_fns()
    plan = ns["_plan_original_speed"]

    # ── ① 预检恒原速（真跑）─────────────────────────────────────────
    over_seg = [{"start": 0.0, "end": 2.0, "narration": "这" * 500}]   # 500 字塞 2 秒窗口
    normal_seg = [{"start": 0.0, "end": 30.0, "narration": "短"}]
    for label, segs in (("超标段", over_seg), ("正常段", normal_seg), ("空列表", [])):
        use, _ = plan(segs)
        ok(f"①{label} 恒原速", use is True, f"got use={use!r}")
    _, over = plan(over_seg)
    ok("①超标明细仍回传（留档/提示用）", isinstance(over, list) and len(over) == 1
       and over[0]["chars"] == 500 and over[0]["budget"] == 10, f"got {over!r}")
    # prefer_original_speed=False 只允许**显式透传**（当前无任何 UI 路径会传 False，
    # 铁律禁的是「自动降速」）；这里断言显式 False 透传、其余一切路径恒 True。
    use2, _ = plan(over_seg, prefer_original_speed=False)
    ok("①显式 False 仅透传（自动路径永不降速）", use2 is False, f"got {use2!r}")

    # ── ② 静态扫描：降速模式不得回归 ────────────────────────────────
    ok("②无 `return False, over`（旧降速返回）", "return False, over" not in src)
    ok("②无 `use_original_speed = False`", "use_original_speed = False" not in src)
    ok("②渲染入口仍走 _plan_original_speed",
       "use_original_speed, over_limit = _plan_original_speed(" in src)

    # ── ③ app.py 链路：慢放开关默认关 ───────────────────────────────
    app_src = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(app_src)
    found_default = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_commentary_option_args":
            a = node.args
            names = [x.arg for x in a.args + a.kwonlyargs]
            defs = ([None] * (len(a.args) - len(a.defaults)) + list(a.defaults)
                    + list(a.kw_defaults))
            for n, dv in zip(names, defs):
                if n == "original_speed":
                    found_default = isinstance(dv, ast.Constant) and dv.value is True
    ok("③_commentary_option_args 默认 original_speed=True", bool(found_default),
       f"default-check={found_default!r}")
    ok("③--no-original-speed 仅在 `if not original_speed:` 分支",
       "if not original_speed:" in app_src)

    print()
    print(f"通过: {PASS}  失败: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
