"""下载统计的浏览器判定守卫（2026-10-14 接续待办 #7 精确化）。

`dl_stats.browser_from_ua` 决定「扩展/安装包下载」按浏览器分桶的口径。修前三处失真：
  ① 兜底两个分支**完全等价**（`if "chrome" in s …: return "chromium"` 与裸
     `return "chromium"`）⇒ 死代码，curl / 空 UA / 爬虫一律被记成 chromium，
     统计里凭空多一批「Chromium 用户」；
  ② 表里漏 `fxios/` ⇒ iOS Firefox（UA 带 Safari/）被记成 **safari**；
  ③ 缺 vivaldi / yandex / brave / opera_ios。

钉死：
  · 表驱动的各浏览器识别（含移动版）；
  · 顺序敏感两条（Edge 不许落 chrome、iOS Firefox 不许落 safari）—— 这两条是
    UA 表里最容易被后来者「随手加一行」破坏的；
  · 非浏览器落 unknown（不是 chromium）；
  · 源码棘轮：函数体内不得再有 `return "chromium"`，必须有 `return "unknown"`。

纯函数、零网络、零落盘（不调 record）。
"""
from __future__ import annotations

import os
import re
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import dl_stats as ds  # noqa: E402

# 真实 UA 取样（桌面 / iOS），保持原样大小写以顺带验证函数内部做了 lower()
_CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_CRIOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) CriOS/120.0.6099.101 Mobile/15E148 Safari/604.1")
_FXIOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) FxiOS/121.0 Mobile/15E148 Safari/605.1.15")

CASES = [
    (_CHROME, "chrome"),
    (_CHROME.replace("Chrome/120", "Edg/120 ") + " Edg/120.0.2210.91", "edge"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
     "firefox"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.1 Safari/605.1.15", "safari"),
    (_CRIOS, "chrome_ios"),
    (_FXIOS, "firefox_ios"),
    (_CHROME + " OPR/106.0.0.0", "opera"),
    (_CHROME + " Vivaldi/6.5", "vivaldi"),
    (_CHROME + " YaBrowser/23.11.0.0", "yandex"),
]


def test_recognises_each_browser():
    """① 各浏览器（含移动版）识别正确。"""
    for ua, want in CASES:
        got = ds.browser_from_ua(ua)
        assert got == want, f"UA 判定错误：期望 {want}，实际 {got}\n  UA={ua}"


def test_edge_is_not_misread_as_chrome():
    """② 顺序敏感之一：Edge 的 UA 同时含 Chrome/，必须先命中 edg/。"""
    ua = _CHROME + " Edg/120.0.2210.91"
    assert ds.browser_from_ua(ua) == "edge"


def test_ios_firefox_is_not_misread_as_safari():
    """② 顺序敏感之二（本轮真实缺陷）：iOS Firefox 的 UA 带 Safari/，漏 fxios/ 就落 safari。"""
    assert ds.browser_from_ua(_FXIOS) == "firefox_ios"
    assert "safari" in _FXIOS.lower(), "样例 UA 必须真的含 safari/，否则断言失去意义"


def test_non_browser_falls_back_to_unknown_not_chromium():
    """③ 认不出就是 unknown，不许冒充 chromium（原实现把所有非浏览器都记成 chromium）。"""
    for ua in ("curl/8.4.0", "", "python-requests/2.31.0", "Go-http-client/2.0"):
        assert ds.browser_from_ua(ua) == "unknown", f"{ua!r} 应落 unknown"


def test_is_case_insensitive():
    assert ds.browser_from_ua(_CHROME.upper()) == "chrome"


def test_no_chromium_fallback_left_in_source():
    """棘轮：函数体内不得再有 chromium 兜底（两个等价分支的死代码）。

    ⚠️ 必须用 AST **剥掉 docstring** 再扫代码：docstring 里为了说明「改了什么」
    引用了旧写法 `return "chromium"` 的字样，按纯文本扫会命中注释而误报。
    """
    import ast

    src = open(os.path.join(_SERVER_DIR, "dl_stats.py"), encoding="utf-8").read()
    fns = [n for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.FunctionDef) and n.name == "browser_from_ua"]
    assert len(fns) == 1, f"未唯一定位 browser_from_ua（命中 {len(fns)} 处）"
    nodes = fns[0].body
    if nodes and isinstance(nodes[0], ast.Expr) and isinstance(nodes[0].value, ast.Constant) \
            and isinstance(nodes[0].value.value, str):
        nodes = nodes[1:]                      # 去掉 docstring
    # ⚠️ 用 AST 取「返回的字面量集合」，别拿 unparse 出的文本匹配引号
    #    （ast.unparse 一律输出单引号，与源码里的双引号对不上，会假红）
    returned = {n.value.value
                for n in ast.walk(ast.Module(body=nodes, type_ignores=[]))
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)}
    assert "chromium" not in returned, f"chromium 兜底已废弃，仍在返回：{returned}"
    assert "unknown" in returned, f"未识别必须落 unknown，实际返回集：{returned}"
