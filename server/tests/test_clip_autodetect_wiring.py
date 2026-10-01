#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面壳「剪贴板自动识别 → 一键下载」接线守卫（2026-10-02 用户需求）。

用户原话：「现在有点复杂,能不能在浏览器上点击选择完清晰度后,点击解析并下载,
app上就可以直接下载,想复制链接复制完在app上粘贴下载呢」。

做法：复制视频链接 → 切回 App → 自动弹提示条 →「下载」（直建任务）或
「解析并选择」（走原解析流程挑清晰度）。
读取剪贴板**必须**走 pywebview 原生桥（WKWebView 里 `navigator.clipboard.readText()`
与 writeText 一样不可靠——同 2026-10-01「复制链接没反应」是同一个坑）。

本测试对 desktop-app.js / desktop_launcher.py 做源码级断言（不跑浏览器、不动
用户剪贴板），钉住两端契约，防止「看着像做完了、其实某处没接上」。
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
    print("▶ 桌面壳「剪贴板自动识别 → 一键下载」接线")

    check("desktop-app.js 存在", JS.is_file(), str(JS))
    src = JS.read_text(encoding="utf-8")

    # ① watcher 本体
    check("定义了剪贴板 watcher", "initClipboardWatcher" in src)
    # ② 读取必须走原生桥，且不得改用网页 API
    check("读剪贴板走原生桥 api.read_clipboard()", "api.read_clipboard()" in src)
    check("不用 navigator.clipboard.readText（桌面壳里不可靠）",
          "navigator.clipboard.readText" not in src)
    # ③ 双通道触发（focus 在 WKWebView 里触发时机不稳，必须另有轮询兜底）
    check("有低频轮询（POLL_MS）", re.search(r"const POLL_MS = \d+", src) is not None)
    check("有 window focus 通道", "addEventListener('focus'" in src)
    check("有 visibilitychange 通道", "visibilitychange" in src)
    # ④ 链接判定：只认单个 http(s) URL、拒绝含空白（避免把整段文字当链接）
    check("定义了链接判定 asVideoUrl", "const asVideoUrl" in src)
    check("判定拒绝含空白/多行的文本", re.search(r"/\\s/\.test\(t\)", src) is not None)
    check("判定要求 http(s) 前缀", "^https?:\\/\\/\\S+$" in src)
    # ⑤ 提示条 UI
    check("提示条 DOM id 为 vdl-clip-bar", "'vdl-clip-bar'" in src)
    check("提示条含清晰度下拉 cb-q", "cb-q" in src)
    check("提示条有「下载」「解析并选择」两个动作", "cb-dl" in src and "cb-parse" in src)
    # ⑥ 一键下载：直调 /api/download 建任务，并切到下载页看进度
    check("「下载」直调 /api/download 建任务", "request('/api/download'" in src)
    check("建任务后切到下载页看进度", "switchView('download')" in src)
    # ⑦ 解析回退：填进首页输入框并提交
    check("「解析并选择」填进 urlInput", "getElementById('urlInput')" in src)
    check("「解析并选择」提交 resolveForm", "form.requestSubmit" in src)
    # ⑧ 自复制不能自弹（点「复制链接」后立刻弹自己的提示条会很烦）
    check("copyText 成功后标记自复制", "noteSelfCopied(text)" in src)
    check("watcher 跳过自复制链接", "isSelfCopied(url)" in src)
    # ⑨ 去重
    check("同一链接不重复弹（lastHandled 去重）", "url === lastHandled" in src)
    # ⑩ 纯 web 环境（浏览器开 127.0.0.1:8321）没有 pywebview.api → 不得启用
    check("无 pywebview.api 时不启用（纯 web 环境隔离）",
          re.search(r"typeof api\.read_clipboard === 'function'", src) is not None)

    # ⑪ 两端契约：Python 侧原生桥必须存在
    check("Python 侧 desktop_launcher.py 存在", DL.is_file(), str(DL))
    dl = DL.read_text(encoding="utf-8")
    check("VdlApi.read_clipboard 已定义", "def read_clipboard(self" in dl)
    m = re.search(r"def read_clipboard\(self.*?(?=\n    def )", dl, re.S)
    body = m.group(0) if m else ""
    check("read_clipboard 函数体定位成功", bool(body))
    check("read_clipboard 内 macOS 用 pbpaste", "pbpaste" in body)
    # 注意：docstring 里会**提到** "ERROR" 这个前缀（解释「故意不返回它」），
    # 所以这里断言的是「没有 return 该前缀的语句」，而不是「全文不含该词」。
    check("read_clipboard 内没有 return ERROR 前缀的语句（会被当成链接文本误判）",
          bool(body) and 'return "ERROR' not in body and 'return f"ERROR' not in body)

    print("")
    print("=========================================")
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
