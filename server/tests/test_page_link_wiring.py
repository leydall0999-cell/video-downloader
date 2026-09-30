#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「生成网页 → 在线链接」接线守卫（纯离线，只读源码）。

背景（2026-09-30 用户实测）：
    用户要的是**在线链接**（能发人、点开就看），而「生成网页」原先只产出一个本地 HTML 文件 ——
    结果区给的是**本机路径**，用户把这串路径发给别人，对方当然打不开
    （「为什么别人打不开」就是这条来的）。
    改成「生成后自动上传分享节点换链接」之后，链路上任何一环被改名/删掉都会**静默失效**
    —— 页面不报错，只是没有链接，用户又回到"只能发文件"的老路。

本测试把两端接线一起钉住：
  A. 分享节点必须认识 HTML，并把 /s/<sid> 渲染成页面原件（不是「请下载」空壳）；
  B. 前端结果区必须是在线链接 + 二维码，且 app.js 的 el 映射真的引用了这些元素；
  C. 生成完成后必须触发上传，且该请求带登录 token（否则已登录用户也会 401）；
  D. 不得退化回「只给本地路径 / 把双击打开当主卖点」。

运行：
    cd server && python tests/test_page_link_wiring.py
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_JS = REPO / "web" / "app.js"
INDEX_HTML = REPO / "web" / "index.html"
NODE_SRV = REPO / "desktop" / "vps-share" / "share_server.py"

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
    print("▶ 「生成网页 → 在线链接」接线守卫")
    app_js = APP_JS.read_text(encoding="utf-8")
    index_html = INDEX_HTML.read_text(encoding="utf-8")
    node_srv = NODE_SRV.read_text(encoding="utf-8")

    print("\n[A] 分享节点：HTML 分享必须渲染成页面原件")
    check("kind_of 认识 html/htm（HTML_EXT）",
          "HTML_EXT" in node_srv and 'return "html"' in node_srv)
    check("_page 对 html 走原件直出（套 H5 壳就只剩「下载」按钮）",
          re.search(r'kind_of\(meta\.get\("name"\)[^\n]*==\s*"html"', node_srv) is not None)
    check("原件直出用 text/html",
          "_raw_html" in node_srv and "text/html; charset=utf-8" in node_srv)
    check("带 CSP 兜底（挡借分享域名挂钓鱼页外发）",
          "Content-Security-Policy" in node_srv)

    print("\n[B] 前端：结果区必须有在线链接元素，且 el 映射引用它们")
    ids = ["pageQrWrap", "pageQrImg", "pageLinkInput", "pageCopyLinkBtn", "pageOpenLinkBtn"]
    for i in ids:
        check(f'index.html 有 id="{i}"', f'id="{i}"' in index_html)
    missing = [i for i in ids if f"$('{i}')" not in app_js]
    check("app.js 的 el 映射引用了全部链接元素", not missing, f"未引用：{missing}")

    print("\n[C] 生成完成后必须自动上传换链接，且带登录 token")
    check("有 pgPublishLink", "pgPublishLink" in app_js)
    check("生成结果里调用了它",
          re.search(r"function pgShowResult[\s\S]{0,2500}?pgPublishLink\(", app_js) is not None)
    idx = app_js.find("'/api/share/upload_path'")
    check("调用 /api/share/upload_path", idx > 0)
    if idx > 0:
        win = app_js[max(0, idx - 300): idx + 400]
        check("该请求带 authBearerHeaders()（否则已登录用户也 401）",
              "authBearerHeaders()" in win)

    print("\n[D] 不得退化回「只给本地路径」")
    check("本地路径已降级为次要行（.pg-local 样式在）", ".pg-local" in index_html)
    check("不再把「双击就能打开」当主卖点", "双击就能打开" not in index_html)
    check("链接成功后用分享节点出二维码",
          "pageQrImg.src = '/api/share/qr?text='" in app_js)

    print("")
    print("=========================================")
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
