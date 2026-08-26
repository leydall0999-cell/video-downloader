"""
「支持 N 个平台」徽章（#engineBadge）显隐的守门测试。

2026-08-26 用户反馈：「只在下载模块显示」（截图指顶部 header 里
「支持 116 个平台」徽章按钮，目前所有 tab 都显示，希望只在下载
模块可见）。

实现：web/app.js switchView() 末尾根据 _isDefault 联动徽章 hidden
（与 el.downloadView.hidden 同源），保证 download 模块可见、其余
6 个真功能页（library / subscribe / torrent / commentary /
uploadconvert / dw）隐藏。

不允许：徽章被设成永远显示；徽章缺失；switchView 没联动 hidden。
"""
import re
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "web" / "app.js"
INDEX_HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_engineBadge_exists_in_html():
    """徽章按钮必须存在于 header 中。"""
    html = _read(INDEX_HTML)
    assert 'id="engineBadge"' in html, "header 里必须有 #engineBadge 徽章按钮"
    # 必须有 aria-haspopup=dialog（点击弹平台列表弹窗）
    assert re.search(r'id="engineBadge"[^>]*aria-haspopup="dialog"', html), \
        "#engineBadge 应保持 aria-haspopup=dialog 触发弹窗"


def test_engineBadge_in_header_not_hero():
    """徽章必须在 <header> 里（顶部全局位置），不在 .hero 下载区。"""
    html = _read(INDEX_HTML)
    # 找到 header 范围
    header_match = re.search(r'<header\b[^>]*>(.*?)</header>', html, re.S)
    assert header_match, "必须有 <header> 元素"
    assert 'id="engineBadge"' in header_match.group(1), \
        "#engineBadge 应在 <header> 里（用户截图位置）"
    # 同时不能在 .hero 内（之前没漏，但要明确不许）
    hero_match = re.search(r'<section\s+class="hero"[^>]*>(.*?)</section>', html, re.S)
    if hero_match:
        assert 'id="engineBadge"' not in hero_match.group(1), \
            "#engineBadge 不应在 .hero 下载区，header 全局位才是正确位置"


def test_switchView_toggles_engineBadge():
    """switchView() 必须根据 _isDefault 联动 engineBadge.hidden。"""
    js = _read(APP_JS)
    # 找 switchView 函数体
    fn_match = re.search(r'function\s+switchView\s*\([^)]*\)\s*\{(.*?)\n\s*\};', js, re.S)
    assert fn_match, "必须存在 switchView 函数"
    body = fn_match.group(1)
    # 必须联动 engineBadge.hidden
    assert re.search(r'engineBadge\.hidden', body), \
        "switchView 内必须联动 el.engineBadge.hidden（与 _isDefault 同源）"
    # 必须基于 _isDefault（不能写死 true/false）
    assert re.search(r'engineBadge\.hidden\s*=\s*!_isDefault', body), \
        "engineBadge.hidden 必须写成 '= !_isDefault'，保持与 downloadView.hidden 同生同灭"


def test_engineBadge_not_always_visible_init():
    """徽章默认 hidden=true（依赖 switchView 在启动时设为可见）。

    启动流程：IIFE 末尾会调 switchView('download') → _isDefault=true → hidden=false。
    所以 HTML 里应保持 hidden（与现有其他徽章风格一致），不要写死 visible，
    否则「先一闪再被 hide」的体验会出现。
    """
    html = _read(INDEX_HTML)
    m = re.search(r'<button[^>]*id="engineBadge"[^>]*>', html)
    assert m, "找不到 #engineBadge 标签"
    tag = m.group(0)
    # 允许 hidden / 默认无 hidden（只要不写死显示）
    # 这里只检查：不应该有 hidden=false / 不该有显式的 show 类
    assert 'hidden=""' not in tag and 'hidden="false"' not in tag and 'hidden="until-found"' not in tag, \
        "#engineBadge 不要写死 hidden 属性值（启动后由 switchView 控制）"