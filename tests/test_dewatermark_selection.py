"""去水印选区事件绑定守门测试。

背景（2026-08-26 用户反馈「图片去水印选择不了」）：
- .dw-canvas / .dw-svg 都是 position:absolute + pointer-events:none，叠在 img 上
  仅作展示蒙层，不接收鼠标事件；事件需穿透到下面的 img 才能触发框选。
- 历史：3fb9bd03 (08-17) 把 .dw-canvas 设 pointer-events:none，当时 mousedown
  绑在 img 上，靠穿透到 img 收事件，正常。
- 后来 dwBindView 把 mousedown 误绑到了 canvas（app.js 原 cv.addEventListener('mousedown'）），
  而 canvas 是 none → 收不到点击 → 选不了。
  注：3933e0de/e0af5269 里的「canvas 改 auto + z-index:2」修复未合进 app-dev 线，
  所以 app-dev 上 canvas 一直是 none。

正确修复：mousedown 必须绑在 img（穿透生效），canvas/svg 保持 none 作蒙层。
"""
import re
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_JS = os.path.join(ROOT, "web", "app.js")
CSS = os.path.join(ROOT, "web", "styles.css")


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def test_selection_mousedown_bound_to_img_not_canvas():
    """选区 mousedown 必须绑在 img 上，绝不能绑在 canvas（canvas 是 pointer-events:none，收不到）。"""
    js = _read(APP_JS)
    # 必须存在 img.addEventListener('mousedown'
    assert "img.addEventListener('mousedown'" in js, \
        "dwBindView 内选区 mousedown 必须绑在 img 上（canvas 是 none，绑 canvas 会完全选不了）"
    # 绝不能存在 cv.addEventListener('mousedown'（回滚到 canvas 的写法）
    assert "cv.addEventListener('mousedown'" not in js, \
        "选区 mousedown 绝不能绑在 canvas：.dw-canvas 是 pointer-events:none，收不到点击"


def test_canvas_and_svg_are_pointer_events_none():
    """canvas / svg 必须 pointer-events:none，让框选事件穿透到下面的 img。"""
    css = _read(CSS)
    # .dw-canvas 块
    m_c = re.search(r"\.dw-canvas\s*\{([^}]*)\}", css)
    assert m_c, ".dw-canvas 规则必须存在"
    assert re.search(r"pointer-events\s*:\s*none", m_c.group(1)), \
        ".dw-canvas 必须 pointer-events:none（框选事件穿透到 img）"
    # .dw-svg 块
    m_s = re.search(r"\.dw-svg\s*\{([^}]*)\}", css)
    assert m_s, ".dw-svg 规则必须存在"
    assert re.search(r"pointer-events\s*:\s*none", m_s.group(1)), \
        ".dw-svg 必须 pointer-events:none（纯展示蒙层，不拦截事件）"


def test_dwBindView_attaches_mousedown_to_img_element():
    """确认 dwBindView 里 mousedown 的绑定目标是 img（而非 cv 参数）。"""
    js = _read(APP_JS)
    # 提取 dwBindView 函数体范围（从 'const dwBindView' 到下一个顶层 'const ' 或 'dwBindView(' 调用）
    start = js.find("const dwBindView =")
    assert start != -1, "必须存在 dwBindView 函数"
    end = js.find("dwBindView(el.dwImgPreview", start)
    body = js[start:end]
    # 在 dwBindView 体内应出现 img.addEventListener('mousedown'
    assert "img.addEventListener('mousedown'" in body, \
        "dwBindView 函数体内必须把 mousedown 绑到 img 参数"
    assert "cv.addEventListener('mousedown'" not in body, \
        "dwBindView 函数体内绝不能把 mousedown 绑到 cv(canvas)"
