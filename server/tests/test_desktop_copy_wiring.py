#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面壳「复制链接」接线守卫（2026-10-01 用户实测「复制链接没反应」）。

根因：web/js/desktop-app.js 的 .cp 按钮用 `navigator.clipboard.writeText`——
pywebview 的 WKWebView 里该 API 常为 undefined，且原代码写成
`navigator.clipboard && navigator.clipboard.writeText(it.url);` —— 直接短路，
**没有任何反馈**，用户点了以为功能坏了。

修法：优先走 pywebview 原生桥 `api.copy_to_clipboard`（Python 侧 pbcopy/clip/xclip），
成功/失败都给 toast，绝不静默。

本测试对 desktop-app.js 做源码级断言（不跑浏览器/不弹剪贴板），钉住两端契约：
  ① .cp 绑定必须调 copyText；
  ② copyText 必须「原生桥优先于 navigator.clipboard」；
  ③ 旧的无反馈短路写法必须已消失；
  ④ 失败必须可见（不静默）；
  ⑤ Python 侧 VdlApi.copy_to_clipboard 必须存在（否则桥调用永远 undefined）。
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
JS = REPO / "web" / "js" / "desktop-app.js"
DL = REPO / "desktop" / "desktop_launcher.py"

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


def main():
    print("▶ 桌面壳「复制链接」接线（原生剪贴板桥优先 + 失败不静默）")

    check("desktop-app.js 存在", JS.is_file(), str(JS))
    src = JS.read_text(encoding="utf-8")

    # ① .cp 绑定必须调 copyText
    check("「复制链接」按钮绑定 copyText",
          re.search(r"querySelector\('\.cp'\)\.addEventListener\('click', \(\) => copyText\(", src) is not None)

    # ② 旧的无反馈短路写法必须消失（这是本次 bug 的本体）
    check("旧的无反馈写法 `navigator.clipboard && navigator.clipboard.writeText(it.url);` 已移除",
          "navigator.clipboard && navigator.clipboard.writeText(it.url);" not in src)

    # ③ copyText 助手存在
    check("定义了 copyText 助手", re.search(r"const copyText = \(text, btn\) =>", src) is not None)

    # ④ 原生桥优先于 navigator.clipboard（顺序敏感：先桥、后网页 API）
    i_api = src.find("api.copy_to_clipboard")
    i_nav = src.find("navigator.clipboard.writeText(text)")
    check("copyText 走 pywebview 原生桥（api.copy_to_clipboard）", i_api != -1)
    check("原生桥调用位置早于 navigator.clipboard 回退",
          0 <= i_api < i_nav, f"api@{i_api} nav@{i_nav}")

    # ⑤ 失败必须可见（不静默）—— 用最少依赖的文案钉住
    check("复制失败有可见提示（不静默）", "复制失败，请手动选中链接复制" in src)
    check("复制成功有 toast 反馈", "已复制链接" in src)

    # ⑥ 两端契约：Python 侧桥必须存在，且 macOS 走 pbcopy
    check("Python 侧 desktop_launcher.py 存在", DL.is_file(), str(DL))
    dl = DL.read_text(encoding="utf-8")
    check("VdlApi.copy_to_clipboard 已定义", "def copy_to_clipboard(self" in dl)
    check("macOS 分支用 pbcopy", "pbcopy" in dl)
    check("异常时返回 ERROR 文本（不抛）", 'return f"ERROR: {exc}"' in dl)

    print("")
    print("=========================================")
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
