"""两端云端额度口径统一守卫（2026-10-06 用户定档「两端统一成终身 3 次」）。

## 背景：同一个「云端免费 3 次」曾在两端是两套东西
- 桌面端：`server/quota.py` 的 **终身 3 次**（`consume_cloud_event`），本机
  `quota.json` + 授权中心原子记账（2026-10-06 上午做的跨端化）。
- 网页端：`app.py` 的 `cloud_quota_gate` → `store.use_daily("cloud")`，是
  **每日配额**（免费 3 次/日、日切重置）。

用户换端就白拿 ⇒ 定档统一为「终身 3 次 + 每日 auto 1 次」，两端都走授权中心
按**账号**记账（App / 网页共享一份）。

## 本守卫钉住
① 网页版必须已接入中心记账：`license_client.cloud_quota_remote` 存在且被
   `cloud_quota_count` 真正调用（不是只 import）。
② 网页版**不得**把云端额度只记在每日配额上（`use_daily("cloud")` 只能作为
   「拿不到中心时的 fail-open 兜底」，不能是主路径）。
③ 额度上限读 `quota.py` 常量（单一真源，不在 app.py 里另写 3/1）。
④ 取 token 必须走 `store.cloud_session()`（`cloud_link.py` 的既有范式）；
   不得用 `status()["account"]`（那是给前端的公开视图，**不含 token**）。
⑤ 会员判定用网页版**实际存在**的接口（它没有桌面端的 `is_download_active` /
   `_cloud_token`，那是桌面端 2026-10-06 才加的）。
"""
from __future__ import annotations

import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_SERVER = _HERE.parent

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  —— " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _func_src_text(src: str, name: str) -> str:
    """从**源码字符串**（非文件）里用 AST 取函数源码。"""
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _func_src(path: pathlib.Path, name: str) -> str:
    """用 AST 精确取某个函数的源码（**含其 docstring**）。

    🔴 为什么要 AST 而不用 `src.split('def X',1)[-1].split('\\ndef ',1)[0]`：
    字符串切分会把 docstring 里提到的**其它函数名**也带进来（实测
    `_cloud_quota_token` 的 docstring 里写了 `status()["account"]` 与
    `_cloud_token()`，导致「没用到这些」的断言误报）。
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
    return ""


def _code_only(fn_src: str) -> str:
    """去掉函数源码里的 docstring 与注释行，只留真正执行的代码。"""
    import ast
    try:
        tree = ast.parse(fn_src)
    except SyntaxError:
        return fn_src
    drop = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                for ln in range(body[0].lineno, body[0].end_lineno + 1):
                    drop.add(ln)
    out = []
    for i, ln in enumerate(fn_src.splitlines(), start=1):
        if i in drop:
            continue
        s = ln.strip()
        if s.startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def _use_daily_is_conditional(app_src: str) -> tuple[bool, str]:
    """本地兜底 `use_daily("cloud")` 是否**每处**都在条件分支 / except 内。

    🔴 判据演进（三轮才对，全是变异测试逼出来的）：
      ① 「缩进 ≥ 8」→ 被 `if False:` 绕过（死代码也缩进）；
      ② 「不在函数体顶层」→ 误报（`try` 本身**就是**异常兜底语义，正确形态正是
         「try 里问中心、except 里退回每日」）；
      ③ 「只在 except 里」→ 又太严（实际还有 `if legacy_daily` / `if not tok`
         / `if 中心异常` 三处条件兜底）。
      终版判据：**任何一处 use_daily 若位于函数体顶层且不在 if/except/if-False 内，
      才算违规**。这才是「兜底」的准确语义 —— 只要不是无条件执行的，就是兜底。
    """
    import ast
    tree = ast.parse(app_src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "cloud_quota_count"), None)
    if fn is None:
        return False, "cloud_quota_count 不存在"

    def _has_use_daily(node) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                nm = getattr(f, "attr", None) or getattr(f, "id", None)
                if nm == "use_daily":
                    for a in n.args:
                        if isinstance(a, ast.Constant) and a.value == "cloud":
                            return True
        return False

    def _guarded(node) -> bool:
        """该语句是否被 if / except / if-False 守着（即非无条件执行）。"""
        for n in ast.walk(node):
            if isinstance(n, (ast.If, ast.ExceptHandler)):
                # 命中 if 的 body/orelse，或 except 的 body
                for st in list(getattr(n, "body", [])) + list(getattr(n, "orelse", [])):
                    if _has_use_daily(st):
                        return True
                if isinstance(n, ast.ExceptHandler):
                    for st in n.body:
                        if _has_use_daily(st):
                            return True
        return False

    def _has_direct_call(node, want: str) -> bool:
        """该语句**自身**（不进入嵌套的 if/try）是否直接调用了 `want("cloud", …)`。"""
        for n in ast.walk(node):
            if isinstance(n, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
                continue                       # 嵌套分支不算「直接」
            if isinstance(n, ast.Call):
                f = n.func
                nm = getattr(f, "attr", None) or getattr(f, "id", None)
                if nm == want:
                    for a in n.args:
                        if isinstance(a, ast.Constant) and a.value == "cloud":
                            return True
        return False

    for stmt in fn.body:
        if isinstance(stmt, ast.Expr):
            continue                                   # docstring
        if not _has_use_daily(stmt):
            continue
        if isinstance(stmt, ast.Try):
            # 🔴 `try` 体内逐条判：**单条语句**（非复合）里直接出现 use_daily ⇒ 违规。
            #   早先用了递归式「是否被 if 守护」判定，被变异打脸两次（`if` 节点里
            #   会递归进其它分支，把无关的 use_daily 也算成「有守护」）。
            #   现在只看**直接子语句**：复合语句（if/try/for/while/with）自带分支，
            #   其内部的 use_daily 是条件兜底；单条语句里的 use_daily 是无条件的。
            for st in stmt.body:
                if isinstance(st, (ast.If, ast.Try, ast.For,
                                   ast.While, ast.With, ast.ExceptHandler)):
                    continue                          # 复合语句：内部是条件分支
                if _has_direct_call(st, "use_daily"):
                    return False, (f"use_daily 在 try 体内是**无条件单条语句**"
                                   f"（行 {st.lineno}）⇒ 每次都先记本地，跨端记账失效")
            continue
        # 顶层单条语句（无 if 守护）里出现 use_daily ⇒ 违规
        if _has_direct_call(stmt, "use_daily"):
            return False, (f"函数体顶层无条件 use_daily（{type(stmt).__name__}，"
                           f"行 {stmt.lineno}）")
    return True, "use_daily 全在条件/except 分支内（纯兜底）"


def _center_before_fallback(app_src: str) -> bool:
    """主路径是否「先问中心、再兜底本地」。

    🔴 按 **AST 语句顺序**判定，不用字符串 find：函数 docstring 里会提到
    `use_daily("cloud")` 作为说明，字符串比较会被它带偏（实测）。
    正确形态：`cloud_quota_remote(...)` 出现在任一 `use_daily("cloud")` **之前**
    （按源码行号比较；docstring 行已在 _func_src 里剥离范围之外）。
    """
    import ast
    src = app_src
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "cloud_quota_count"), None)
    if fn is None:
        return False
    doc = None
    body0 = fn.body
    if (body0 and isinstance(body0[0], ast.Expr)
            and isinstance(body0[0].value, ast.Constant)
            and isinstance(body0[0].value.value, str)):
        doc = body0[0]
    center_line, fallback_line = None, None
    for n in ast.walk(fn):
        if doc and doc.lineno <= getattr(n, "lineno", 0) <= doc.end_lineno:
            continue                                  # 跳过 docstring
        if isinstance(n, ast.Call):
            f = n.func
            nm = getattr(f, "attr", None) or getattr(f, "id", None)
            if nm == "use_daily":
                for a in n.args:
                    if isinstance(a, ast.Constant) and a.value == "cloud":
                        fallback_line = n.lineno if fallback_line is None else min(fallback_line, n.lineno)
            if nm == "cloud_quota_remote":
                center_line = n.lineno if center_line is None else min(center_line, n.lineno)
    if center_line is None:
        return False                       # 根本没有中心扣减 → 必红
    if fallback_line is None:
        return True                        # 没有兜底 → 更好
    return center_line < fallback_line


def main() -> None:
    print("=== 两端云端额度口径统一守卫（web-dev）===")

    app = (_SERVER / "app.py").read_text(encoding="utf-8")
    qsrc = (_SERVER / "quota.py").read_text(encoding="utf-8")
    lc = (_SERVER / "license_client.py").read_text(encoding="utf-8")
    mem = (_SERVER / "membership.py").read_text(encoding="utf-8")

    print("\n[A] 网页版已接入中心原子记账")
    check("license_client 有 cloud_quota_remote", "def cloud_quota_remote" in lc)
    cnt_code = _code_only(_func_src(_SERVER / "app.py", "cloud_quota_count"))
    check("cloud_quota_count 真正调用了它", "cloud_quota_remote(" in cnt_code,
          "只在别处调用 = 云端额度没走中心")
    check("app.py 有 _cloud_quota_remaining（只查不扣）",
          "def _cloud_quota_remaining" in app)
    check("app.py 有 _cloud_quota_token", "def _cloud_quota_token" in app)

    print("\n[B] 云端额度不能只记每日配额（use_daily 仅作 fail-open 兜底）")
    # 🔴 2026-10-06 拆池：resource 不再是字面量 "cloud"，而是 gate 携带的动态键
    # （cloud_commentary/convert/dewatermark/subtitle）。兜底判据改为「存在
    # use_daily 调用」+ AST 判定其全在条件分支内（_use_daily_is_conditional
    # 对动态参数恒真 —— 它只认字面量，字面量没了就只能靠下面这条主路径判据钉）。
    has_fallback = "use_daily(" in cnt_code
    check("有 use_daily 兜底分支（断网时不阻断用户）", has_fallback)
    ok, why = _use_daily_is_conditional(app)
    check("use_daily 只出现在兜底分支（不在函数体顶层）", ok, why)
    check("兜底原因写明是中心不可达",
          ("legacy_daily" in app or "无云端身份" in app or "中心异常" in app),
          "看不到「为什么退回每日配额」的说明")
    check("中心成功时不走 use_daily（主路径是原子扣减）",
          "cloud_quota_remote(tok, lifetime=n" in cnt_code,
          "没看到 lifetime 扣减调用 = 中心记账不是主路径")
    check("主路径扣减带 per-resource 键",
          "resource=resource" in cnt_code,
          "拆池后中心扣减必须带 resource（各功能独立终身额度）")
    # ⚠️ 曾试过「中心扣减必须出现在首个 use_daily 之前」这条判据，**已放弃**：
    # 正确代码里 `if gate.get("legacy_daily"): store.use_daily(...)` 本来就在前面
    # （那是「预检阶段就确认中心不可达」的兜底路径，先于中心扣减是必然的）。
    # 顺序判据因此会误报，改由上面「use_daily 全在条件分支内」+「主路径是原子扣减」
    # 两条共同保证语义 —— 那两条才是真正拦得住「退化成各记各的」。

    print("\n[C] 上限读 quota.py 常量，不在 app.py 另写数字")
    check("app.py 有 _cloud_quota_limits()", "def _cloud_quota_limits" in app)
    check("它从 quota.py 读常量",
          "from quota import LIFETIME_CLOUD_EVENTS" in app)
    check("quota.py 是唯一真源（终身 3 / 每日 auto 1）",
          "LIFETIME_CLOUD_EVENTS = 3" in qsrc and "DAILY_AUTO_RUNS = 1" in qsrc)

    print("\n[D] 取 token 走 cloud_session()，不用公开视图")
    tok_code = _code_only(_func_src(_SERVER / "app.py", "_cloud_quota_token"))
    check("用 store.cloud_session()", "cloud_session()" in tok_code)
    check("没用 status()['account']（不含 token 的公开视图）",
          'status().get("account"' not in tok_code
          and 'status()["account"]' not in tok_code,
          "公开视图没有 token，取不到会永远 fail-open")
    check("网页版 MembershipStore 确有 cloud_session", "def cloud_session" in mem)

    print("\n[E] 用网页版实际存在的接口（不得照抄桌面端）")
    app_code = _code_only(app)
    check("没调用桌面端独有的 store.is_download_active/is_ai_active",
          "store.is_download_active" not in app_code
          and "store.is_ai_active" not in app_code,
          "网页版 MembershipStore 没有这两个方法 → 会 AttributeError")
    check("会员判定走 _is_member_any（基于 status）", "def _is_member_any" in app)
    check("没调用桌面端独有的 store._cloud_token()",
          "store._cloud_token()" not in app_code,
          "网页版没有该方法")
    check("网页版确实没有 is_download_active（确认前提）",
          "def is_download_active" not in mem,
          "若网页版已加该方法，可简化 _is_member_any")

    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 网页端云端额度已走中心原子记账，与桌面端同一份（终身 3 次 + 每日 auto 1 次）")


if __name__ == "__main__":
    main()
