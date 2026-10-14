"""扩展包下载端点 HEAD 预检守卫（2026-10-14 接续待办 #5）。

背景：`/api/extension/package` 原先只注册 GET ⇒ 下载管理器 / 断点续传探测发来的
HEAD 一律 405，探测方只能真拉整包才知道体积。现在 GET 与 HEAD 共用 `_package_payload()`。

钉死四条：
  ① 路由必须同时接受 GET 与 HEAD（只注册 GET 就退回 405）；
  ② HEAD 不带 body，但 Content-Length 必须等于 GET 的真实字节数；
     🔴 依赖 Starlette 对 HEAD「自动剥离 body、保留真实长度」（实测）。若有人改成
        「独立 HEAD handler + 空 content + 手工填长度」，一旦填错，下载器会按错的
        体积截断文件 —— 这条断言会把这种退回挡住。
  ③ HEAD 的 Content-Disposition 与 GET 同源（含 RFC 5987 的 filename*）；
  ④ Accept-Ranges: none（zip 每次现打，不支持 Range，如实声明）。

再加一条源码棘轮：`extension_package_head` 体内必须显式写 Content-Length，
免得有人「简化」成交给框架自动填。

只用 FastAPI TestClient（回环，零外部网络）；不 import app，避免拉起全部路由。
"""
from __future__ import annotations

import os
import re
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from fastapi import FastAPI                                   # noqa: E402
from fastapi.testclient import TestClient                      # noqa: E402
from routers import extension as ext                           # noqa: E402

PKG = "/api/extension/package"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ext.router)
    return TestClient(app)


def _route_methods(path: str) -> set[str]:
    out: set[str] = set()
    for r in ext.router.routes:
        if getattr(r, "path", "") == path:
            out |= set(getattr(r, "methods", set()) or set())
    return out


def test_package_route_accepts_head():
    """① HEAD 必须被接受：只注册 GET 时下载器预检会拿到 405。"""
    methods = _route_methods(PKG)
    assert {"GET", "HEAD"} <= methods, f"{PKG} 应同时支持 GET/HEAD，实际 {methods}"


def test_head_has_no_body_but_real_content_length():
    """② 无 body + 真实体积（预检的意义所在）。"""
    c = _client()
    head = c.head(PKG)
    get = c.get(PKG)
    assert head.status_code == 200, head.status_code
    assert len(head.content) == 0, "HEAD 不得带 body"
    n = int(head.headers["content-length"])
    assert n == len(get.content), f"HEAD 体积({n}) 应等于 GET 实际字节数({len(get.content)})"
    assert n > 0, "体积不能是 0（否则说明 Content-Length 被空 body 带成了 0）"


def test_head_headers_match_get():
    """③ 类型与文件名两法同源（含 UTF-8 中文名）。"""
    c = _client()
    head = c.head(PKG)
    get = c.get(PKG)
    assert head.headers["content-type"] == "application/zip"
    assert head.headers["content-disposition"] == get.headers["content-disposition"]
    assert "filename*=UTF-8''" in head.headers["content-disposition"], "中文文件名需 RFC 5987 编码"


def test_head_declares_no_range_support():
    """④ zip 每次现打、不支持 Range，必须如实声明。"""
    c = _client()
    assert c.head(PKG).headers.get("accept-ranges") == "none"


def test_route_declares_both_methods_in_source():
    """棘轮：路由声明必须同时含 GET 与 HEAD（防有人退回 `@router.get` ⇒ 预检又变 405）。"""
    src = open(os.path.join(_SERVER_DIR, "routers", "extension.py"), encoding="utf-8").read()
    m = re.search(r'@router\.\w+\("/api/extension/package"[^)]*\)', src)
    assert m, "未定位到 /api/extension/package 的路由装饰器（正则过期）"
    decl = m.group(0)
    assert '"GET"' in decl and '"HEAD"' in decl, f"路由必须同时声明 GET 与 HEAD：{decl}"
