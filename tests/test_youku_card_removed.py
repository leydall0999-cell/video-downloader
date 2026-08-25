"""tests/test_youku_card_removed.py — 「优酷部分不要」守门测试。

背景：
  2026-08-26 用户明确：「优酷部分不要」（即不希望桌面 app 里再有"零门槛本机浏览器引擎 + 自动拦截 ckey"那一坨魔法能力）。
  保留范围：公共 cookie 池（你酷登录态仍可通过 /api/cookie/contribute 入池，yt-dlp YoukuIE / UPS 通道继续工作），
            平台定义里 youku（粘贴你酷链接照样能识别 + 走 _youku_info 公共 UPS 通道）。
  删除范围：HTML 卡片、desktop-app.js initYoukuLocal IIFE、后端 /api/youku/* 5 路由 + bookmarklet、
            _youku_profile_dir 工具函数、youku_browser.py / youku_local.py 模块文件、
            downloader.py 中本机引擎优先注入块。

本测试覆盖：
  ① HTML 卡片所有 ID 不存在（youkuLocalCard / youkuLocalStatus / youkuLoginBtn / youkuOpenPage）
  ② HTML 不再出现"优酷零门槛解析"中文文案
  ③ desktop-app.js 不再出现 initYoukuLocal / /api/youku/ 调用
  ④ 后端 /api/youku/* 路由全部下架（bookmarklet / login / engine-status / local-ckey get/post）
  ⑤ _youku_profile_dir 函数已删除
  ⑥ _YOUKU_BOOKMARKLET 常量已删除
  ⑦ server/youku_browser.py 与 server/youku_local.py 模块文件已删
  ⑧ downloader.py 不再 import youku_browser / youku_local
  ⑨ FastAPI app 真路由表里确实没有这些路径（防遗漏 inline route / 装饰器失误）
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "server"
WEB = REPO / "web"
sys.path.insert(0, str(SERVER))

YOUKU_REMOVE_IDS = ("youkuLocalCard", "youkuLocalStatus", "youkuLoginBtn", "youkuOpenPage")
YOUKU_REMOVE_API_PREFIXES = ("/api/youku/",)
YOUKU_REMOVE_API_EXACT = {
    "/api/youku/bookmarklet",
    "/api/youku/login",
    "/api/youku/engine-status",
    "/api/youku/local-ckey",
}


# ─────────────────────────────────────────────────────────────────────────────
# 共享 fixture：懒加载 app + 读源码
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def srv():
    """延迟 import server.app（依赖较重，模块级缓存一次即可）。"""
    return importlib.import_module("app")


@pytest.fixture(scope="module")
def index_html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def desktop_js() -> str:
    return (WEB / "js" / "desktop-app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_py() -> str:
    return (SERVER / "app.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def downloader_py() -> str:
    return (SERVER / "downloader.py").read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# ① HTML 卡片
# ─────────────────────────────────────────────────────────────────────────────

class TestYoukuHtmlCardRemoved:
    """HTML 不应再包含你库「零门槛解析」卡片的 ID/按钮/中文文案。"""

    def test_html_no_youku_card_ids(self, index_html: str):
        """卡片所有 ID 必须清除。"""
        for cid in YOUKU_REMOVE_IDS:
            assert f'id="{cid}"' not in index_html, (
                f"index.html 仍包含 {cid}，卡片应被整段删除"
            )

    def test_html_no_youku_card_class(self, index_html: str):
        """卡片容器 class 也应清除（防止 CSS 死代码）。"""
        assert "youku-local-card" not in index_html, (
            "index.html 仍含 youku-local-card 容器"
        )
        for cls in ("youku-local-head", "youku-local-title",
                    "youku-local-status", "youku-local-tip", "youku-local-actions"):
            assert cls not in index_html, f"index.html 残留 {cls}"

    def test_html_no_marketing_text(self, index_html: str):
        """「优酷零门槛解析」文案不应再出现（卡片标题 + 说明）。"""
        assert "优酷零门槛解析" not in index_html, (
            "index.html 仍保留「优酷零门槛解析」卡片标题/文案"
        )

    def test_html_no_login_open_buttons(self, index_html: str):
        """「登录优酷」/「打开优酷」按钮 ID/文案必须清除。"""
        for txt in ("登录优酷", "打开优酷"):
            # 兼容前后可能空格/标点
            assert txt not in index_html, f"index.html 仍含按钮文案「{txt}」"


# ─────────────────────────────────────────────────────────────────────────────
# ② desktop-app.js
# ─────────────────────────────────────────────────────────────────────────────

class TestDesktopJsYoukuRemoved:
    """desktop-app.js 不再调用 /api/youku/* 也不再注册 initYoukuLocal IIFE。"""

    def test_no_init_youku_iife(self, desktop_js: str):
        """整段 IIFE `initYoukuLocal` 删除。"""
        assert "initYoukuLocal" not in desktop_js, (
            "desktop-app.js 仍含 initYoukuLocal IIFE"
        )

    def test_no_youku_api_calls(self, desktop_js: str):
        """/api/youku/ 系列调用不应再出现在桌面脚本里。"""
        for path in ("/api/youku/engine-status", "/api/youku/login",
                     "/api/youku/local-ckey", "/api/youku/bookmarklet"):
            assert path not in desktop_js, (
                f"desktop-app.js 仍调用 {path}"
            )

    def test_no_youku_dom_ids(self, desktop_js: str):
        """卡片相关 DOM ID 不应再被引用。"""
        for cid in YOUKU_REMOVE_IDS:
            assert cid not in desktop_js, f"desktop-app.js 仍引用 {cid}"

    def test_no_yk_home_constant(self, desktop_js: str):
        """「打开优酷」按钮的 YK_HOME 常量也不应残留。"""
        assert "YK_HOME" not in desktop_js, "桌面脚本仍含 YK_HOME 常量"


# ─────────────────────────────────────────────────────────────────────────────
# ③ 后端 app.py 路由
# ─────────────────────────────────────────────────────────────────────────────

class TestYoukuApiRoutesRemoved:
    """app.py 不再注册 /api/youku/* 路由（既看源码字符串也查实际路由表）。"""

    def test_no_route_decorators(self, app_py: str):
        """源码里不应再有 /api/youku/* 的装饰器定义。"""
        for path in YOUKU_REMOVE_API_EXACT:
            # 匹配 @app.<method>("<path>") 装饰器
            pattern = rf'@app\.[a-z_]+\(\s*"{re.escape(path)}"\s*\)'
            assert not re.search(pattern, app_py), (
                f"app.py 仍注册路由 {path}"
            )

    def test_no_routes_table(self, srv):
        """实际 FastAPI 路由表也不应包含这些路径。"""
        from app import app as fastapi_app
        registered_paths = {
            r.path for r in fastapi_app.routes if isinstance(r, APIRoute)
        }
        for path in YOUKU_REMOVE_API_EXACT:
            assert path not in registered_paths, (
                f"FastAPI 路由表仍含 {path}（实际生效）"
            )

    def test_no_youku_profile_dir_helper(self, app_py: str):
        """_youku_profile_dir() 工具函数应被删除。"""
        assert "def _youku_profile_dir" not in app_py, (
            "app.py 仍含 _youku_profile_dir 工具函数"
        )

    def test_no_youku_bookmarklet_constant(self, app_py: str):
        """_YOUKU_BOOKMARKLET 常量应被删除。"""
        assert "_YOUKU_BOOKMARKLET" not in app_py, (
            "app.py 仍含 _YOUKU_BOOKMARKLET 常量"
        )


# ─────────────────────────────────────────────────────────────────────────────
# ④ 后端模块文件
# ─────────────────────────────────────────────────────────────────────────────

class TestYoukuBrowserAndLocalRemoved:
    """youku_browser.py / youku_local.py 模块文件应被删除。"""

    def test_youku_browser_module_deleted(self):
        assert not (SERVER / "youku_browser.py").exists(), (
            "server/youku_browser.py 仍存在（应删除）"
        )

    def test_youku_local_module_deleted(self):
        assert not (SERVER / "youku_local.py").exists(), (
            "server/youku_local.py 仍存在（应删除）"
        )

    def test_no_module_import_in_app_py(self, app_py: str):
        """app.py 不应再 import 这两个模块。"""
        assert "youku_browser" not in app_py, "app.py 仍 import youku_browser"
        assert "youku_local" not in app_py, "app.py 仍 import youku_local"


# ─────────────────────────────────────────────────────────────────────────────
# ⑤ downloader.py 中本机引擎块清除
# ─────────────────────────────────────────────────────────────────────────────

class TestDownloaderYoukuLocalRemoved:
    """downloader.py 中不应再注入本机你库引擎/本机 ckey 块。"""

    def test_no_youku_browser_import(self, downloader_py: str):
        """不再 try-from youku_browser。"""
        assert "from youku_browser" not in downloader_py, (
            "downloader.py 仍 import youku_browser"
        )

    def test_no_youku_local_import(self, downloader_py: str):
        """不再 try-from youku_local。"""
        assert "from youku_local" not in downloader_py, (
            "downloader.py 仍 import youku_local"
        )

    def test_kept_cookie_pool_import(self, downloader_py: str):
        """但仍保留公共池导入（优酷共享池登录态 + ckey 来自用户 Copy as cURL 贡献路径）。"""
        assert "from cookie_pool import get_cookie as _pool_get" in downloader_py, (
            "downloader.py 失去 cookie_pool 公共池注入（优酷解析会断）"
        )

    def test_kept_youku_info_dispatch(self, downloader_py: str):
        """仍走 _youku_info 公共 UPS 通道（粘贴优酷链接的主路径）。"""
        # 仅截取你库分支这段
        m = re.search(
            r'if \(_host_of\(url\)\.endswith\("youku\.com"\)\):(.*?)(?=\n    # |\Z)',
            downloader_py,
            re.DOTALL,
        )
        assert m, "downloader.py 缺你库解析分支"
        block = m.group(1)
        assert "_youku_info(" in block, "你库分支未走 _youku_info"
        assert "resolve_via_browser" not in block, "你库分支仍残留浏览器引擎调用"
        assert "get_local_ckey" not in block, "你库分支仍残留本机 ckey 注入"


# ─────────────────────────────────────────────────────────────────────────────
# ⑥ 平台定义仍保留（证明你酷作为识别/下载目标的能力未消失）
# ─────────────────────────────────────────────────────────────────────────────

class TestYoukuPlatformStillParsed:
    """你酷平台项仍保留在 platforms.py（粘贴链接能被识别 + 走下载主路径）。"""

    def test_platforms_have_youku(self):
        platforms_src = (SERVER / "platforms.py").read_text(encoding="utf-8")
        assert re.search(r'Platform\(\s*"youku"\s*,\s*"优酷"', platforms_src), (
            "platforms.py 失去 youku 平台项（粘贴优酷链接将无法识别）"
        )

    def test_cookie_pool_whitelist_retains_youku(self):
        cookie_pool_src = (SERVER / "cookie_pool.py").read_text(encoding="utf-8")
        # 公共池白名单仍含 youku.com（共享登录态入池）—— 含在 _BASE_DOMAINS
        assert '"youku.com"' in cookie_pool_src, (
            "cookie_pool.py 失去 youku.com 白名单（公共池无法上报你酷登录态）"
        )
        assert '"youku.com":' in cookie_pool_src, (
            "cookie_pool.py 失去 youku.com 结构字段白名单"
        )
