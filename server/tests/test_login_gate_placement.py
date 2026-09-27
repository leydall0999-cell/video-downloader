#!/usr/bin/env python3
"""登录门禁「挂载位置」守卫（纯离线，只读源码）。

背景（2026-09-27 用户实测反馈）：
    2026-09-26 上线了「全功能登录门禁」，把 20+ 个按钮都纳入捕获阶段拦截。
    结果用户随手点「解析链接」就弹登录框（此前只有「开始下载」才弹），
    观感是「只要一操作就跳出登录框」。
    用户要求回调门禁位置：**门禁只挂真正执行（会产出结果）的动作按钮**，
    前置步骤（解析、选文件）不弹登录。

本测试锁住这条契约，避免以后再被「顺手多挂几个入口」破坏：
  A. `web/app.js` 的 `_LOGIN_GATED_ACTIONS` 必须包含各功能的执行按钮；
  B. 该表**不得**包含前置步骤按钮（resolveBtn 解析 / shareAddBtn 选择文件）；
  C. 表里每个 id 都必须在 `web/index.html` 真实存在（改名后静默失效是隐形回归）；
  D. 后端 `server/app.py` 的 `_LOGIN_GATED_EXACT` 不得再拦 `/api/resolve`；
  E. 「选择文件」类动作的登录校验必须存在（扫码分享 upload 前），否则等于完全放开；
  F. 登录提示文案不能被 `openAuthModal()` 内部清空——必须先开弹窗再写文案。

运行：
    cd server && python tests/test_login_gate_placement.py
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_JS = REPO / "web" / "app.js"
INDEX_HTML = REPO / "web" / "index.html"
SERVER_APP = REPO / "server" / "app.py"

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def _extract_js_map(src: str, var_name: str) -> str:
    """取出 `var X = { ... };` 的对象字面量文本（花括号配平）。"""
    m = re.search(r"var\s+" + re.escape(var_name) + r"\s*=\s*\{", src)
    assert m, f"未找到 {var_name} 定义"
    i = src.index("{", m.start())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise AssertionError("花括号不配平")


def _js_map_keys(block: str):
    """对象字面量的键名（只取行首 `key:` 形式，跳过注释行）。"""
    keys = []
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("//") or ":" not in s:
            continue
        k = s.split(":", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", k):
            keys.append(k)
    return keys


def main():
    print("▶ 登录门禁挂载位置守卫（前置步骤不弹登录 / 执行动作必须弹）")
    app_js = APP_JS.read_text(encoding="utf-8")
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    server_app = SERVER_APP.read_text(encoding="utf-8")

    block = _extract_js_map(app_js, "_LOGIN_GATED_ACTIONS")
    keys = set(_js_map_keys(block))
    print(f"  · 前端门禁表共 {len(keys)} 项")

    print("\n[A] 执行动作必须仍在门禁内")
    for k, label in [
        ("downloadBtn", "开始下载"),
        ("batchBtn", "批量下载"),
        ("comGenerateScript", "解说-生成脚本"),
        ("subExtract", "字幕-提取字幕"),
        ("sbStartBtn", "字幕-开始提取"),
        ("comScriptRender", "解说-渲染成片"),
        ("cpStartAllBtn", "开始压缩"),
        ("matBtn", "开始抠图"),
        ("dwImgBtn", "去水印-图片"),
        ("processRun", "队列-开始处理"),
        ("cleanRun", "存储-立即清理"),
    ]:
        check(f"{label}（{k}）仍受登录门禁保护", k in keys)

    print("\n[B] 前置步骤不得再弹登录")
    for k, label in [
        ("resolveBtn", "解析链接"),
        ("shareAddBtn", "选择文件"),
    ]:
        check(f"{label}（{k}）已移出门禁表", k not in keys)

    print("\n[C] 门禁表里的 id 必须在 index.html 真实存在")
    missing = [k for k in sorted(keys) if f'id="{k}"' not in index_html]
    check("无「表里有、页面没有」的失效项", not missing, f"缺失：{missing}")

    print("\n[D] 后端名单同步：/api/resolve 不再要登录")
    m = re.search(r"_LOGIN_GATED_EXACT\s*=\s*\{", server_app)
    blk = server_app[m.start():server_app.index(")", m.start())]
    check("_LOGIN_GATED_EXACT 不含 /api/resolve", '"/api/resolve"' not in blk)
    check("_LOGIN_GATED_EXACT 仍含 /api/download", '"/api/download"' in blk)
    check("_LOGIN_GATED_EXACT 仍含 /api/commentary/script-only",
          '"/api/commentary/script-only"' in blk)

    print("\n[E] 「选择文件」类动作的登录校验没被一起删掉")
    check("扫码分享上传前有登录校验（shStart 内）", "需要登录后才能分享" in app_js)

    print("\n[F] 登录提示文案不被弹窗清空")
    body = app_js[app_js.index("function _notifyNeedLogin("):]
    body = body[:body.index("\n  }")]
    i_open = body.find("openAuthModal()")
    i_msg = body.find("_authMsg(msg")
    check("_notifyNeedLogin 先开弹窗、后写文案",
          0 <= i_open < i_msg, f"openAuthModal@{i_open} _authMsg@{i_msg}")

    print("")
    print("=========================================")
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
