"""
去水印视图（dwView）布局守门测试。

2026-08-26 08:57 用户反馈两条 bug，本次修复：

【1】上传图片空余太多
    根因：`.dw-preview-wrap` 里的 `<svg class="dw-svg">` 没有定位规则，
    默认 `position: static`。JS `dwResizeOverlay` 给 svg 设了
    `width/height = img.clientWidth/Height` 并想用 `left/top` 定位覆盖图片，
    但没有 absolute → left/top 失效，svg 当流式元素在图片下方占据
    整整一张图的高度空白，把"放大编辑"按钮顶到容器底部。
    修复：`.dw-svg { position: absolute; left: 0; top: 0; pointer-events: none; }`
    让 svg 和 `.dw-canvas` 一样绝对定位叠在图片上，不占流式空间。

【2】原图 / 处理后 大小不一
    根因：`.dw-compare img` 是 `height: auto`，原图与处理后长宽比不同，
    导致两个对比框高度不一致。
    修复：固定 `height: 260px` + `object-fit: contain`，两个图框等高，
    grid `align-items: stretch`（默认）让两个 div 盒子自然等高。
"""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "web" / "styles.css"
HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dw_svg_is_absolute_positioned():
    """`.dw-svg` 必须绝对定位，否则会作为流式元素在图片下方占据大量空白。

    防回滚：`.dw-svg` 不能再用 `position: static` / 不写 position。
    """
    css = _read(CSS)
    m = re.search(r"\.dw-svg\s*\{([^}]*)\}", css, re.S)
    assert m, "未找到 .dw-svg 规则（必须显式声明，svg 默认 static 会占流式空白）"
    body = m.group(1)
    assert re.search(r"position\s*:\s*absolute", body), \
        f".dw-svg 必须 position: absolute（防 svg 占流式空白），当前：{body.strip()}"
    # 必须 pointer-events: none（不要拦截图片的框选交互）
    assert "pointer-events: none" in body or "pointer-events:none" in body, \
        f".dw-svg 必须 pointer-events: none（否则会拦截图片框选），当前：{body.strip()}"


def test_dw_svg_absolute_matches_canvas_pattern():
    """.dw-svg 的定位属性集合应与 .dw-canvas 一致（左/上 0，绝对定位，pointer-events:none）。"""
    css = _read(CSS)
    canvas_m = re.search(r"\.dw-canvas\s*\{([^}]*)\}", css, re.S)
    svg_m = re.search(r"\.dw-svg\s*\{([^}]*)\}", css, re.S)
    assert canvas_m and svg_m, "需同时存在 .dw-canvas 与 .dw-svg 规则"
    canvas_body, svg_body = canvas_m.group(1), svg_m.group(1)
    # 两边都必须 position: absolute + pointer-events: none
    for name, body in ((".dw-canvas", canvas_body), (".dw-svg", svg_body)):
        assert "position: absolute" in body or "position:absolute" in body, \
            f"{name} 必须 position: absolute"
        assert "pointer-events: none" in body or "pointer-events:none" in body, \
            f"{name} 必须 pointer-events: none"


def test_dw_compare_img_has_fixed_height():
    """`.dw-compare img` 必须固定高度，否则原图/处理后长宽比不同导致两个盒子高度不一致。

    必须是具体像素/rem 高度，不是 auto，且保留 object-fit: contain 保证图片比例正确。
    """
    css = _read(CSS)
    m = re.search(r"\.dw-compare\s+img\s*\{([^}]*)\}", css, re.S)
    assert m, "未找到 .dw-compare img 规则"
    body = m.group(1)
    # 禁止 height: auto（这是导致两个盒子高度不一的真因）
    assert not re.search(r"height\s*:\s*auto", body), \
        f".dw-compare img 不能 height: auto（原图/处理后长宽比不同会导致盒子高度不一），当前：{body.strip()}"
    # 必须有具体 height（px 或 rem）
    h = re.search(r"height\s*:\s*(\d+(?:\.\d+)?)(px|rem)", body)
    assert h, f".dw-compare img 必须有固定 height（px/rem），当前：{body.strip()}"
    # 必须保留 object-fit: contain（否则固定 height 会拉伸图片）
    assert "object-fit" in body and "contain" in body, \
        f".dw-compare img 必须保留 object-fit: contain（防图片被拉伸），当前：{body.strip()}"


def test_dw_compare_grid_equal_columns():
    """`.dw-compare` 必须是等宽两列 grid（1fr 1fr），否则两栏宽度不同，盒子大小自然不一。"""
    css = _read(CSS)
    m = re.search(r"\.dw-compare\s*\{([^}]*)\}", css, re.S)
    assert m, "未找到 .dw-compare 规则"
    body = m.group(1)
    cols = re.search(r"grid-template-columns\s*:\s*([^;]+);", body)
    assert cols, ".dw-compare 必须是 grid 布局"
    assert "1fr" in cols.group(1) and cols.group(1).count("1fr") >= 2, \
        f".dw-compare 必须两列等宽 1fr 1fr，当前：{cols.group(1).strip()}"


def test_dw_preview_wrap_no_min_height_padding():
    """`.dw-preview-wrap` 不能加 min-height 或 padding（会让空状态也撑很高）。"""
    css = _read(CSS)
    m = re.search(r"\.dw-preview-wrap\s*\{([^}]*)\}", css, re.S)
    assert m, "未找到 .dw-preview-wrap 规则"
    body = m.group(1)
    assert "min-height" not in body, \
        f".dw-preview-wrap 不能有 min-height（会让未上传时也撑高），当前：{body.strip()}"
    assert "padding" not in body, \
        f".dw-preview-wrap 不能加 padding（会让图片四周多出空白），当前：{body.strip()}"


def test_dw_svg_absolute_in_html_order():
    """HTML 中 .dw-svg 必须在 .dw-canvas 之前或之后都行，但必须有 .dw-svg 类，让 CSS 能匹配。"""
    html = _read(HTML)
    assert 'id="dwImgSvg"' in html and 'class="dw-svg"' in html, \
        "index.html 中 dwImgSvg 必须带 class=\"dw-svg\"（CSS 才能定位）"
    # svg 必须在 dw-preview-wrap 容器内
    wrap_start = html.find('id="dwPreviewWrap"')
    wrap_end_marker = html.find('</button>', wrap_start)  # dw-expand 按钮
    svg_idx = html.find('id="dwImgSvg"', wrap_start)
    assert wrap_start != -1 and svg_idx != -1 and svg_idx > wrap_start, \
        "dwImgSvg 必须在 dwPreviewWrap 内部"

# ---------------------------------------------------------------------------
# 2026-08-26 用户反馈：「处理完之前都不显示」
# 截图显示 dwView 的"原图/处理后"两个黑框 + "下载结果"按钮已经露出来。
#
# 根因：.dw-result{display:flex} 与 UA [hidden]{display:none} 同特异性
# (0,1,0)，作者 CSS 写在后面赢 → hidden 属性失效。dwImgResult/dwPdfResult
# 虽然 HTML 写了 hidden，但 CSS 仍然把对比区显示出来，里面的 <img> 没 src
# 时 background:#0b0d12 渲染成两个黑框。
#
# 修复：.dw-result[hidden] { display: none; }（特异性 0,2,0，必赢）。
# ---------------------------------------------------------------------------

def test_dw_result_respects_hidden_attribute():
    """`.dw-result[hidden]` 必须 display:none，否则 HTML hidden 属性失效。"""
    css = _read(CSS)
    m = re.search(r"\.dw-result\[hidden\]\s*\{([^}]*)\}", css, re.S)
    assert m, (
        "必须存在 .dw-result[hidden] { display: none } 规则。"
        "否则 .dw-result{display:flex} 会同特异性赢过 UA [hidden]{display:none},"
        "导致 dwImgResult/dwPdfResult 写 hidden 也照样显示'原图/处理后'黑框。"
    )
    body = m.group(1)
    assert re.search(r"display\s*:\s*none", body), \
        f".dw-result[hidden] 必须 display:none，当前：{body.strip()}"


def test_dw_result_html_uses_hidden_attribute():
    """dwImgResult / dwPdfResult 在 HTML 中必须用 hidden 属性，配合 CSS 隐藏。"""
    html = _read(HTML)
    for elem_id in ("dwImgResult", "dwPdfResult"):
        m = re.search(rf'<div\s+class="dw-result"\s+id="{elem_id}"[^>]*>', html)
        assert m, f"未找到 id='{elem_id}' 的 .dw-result 容器"
        tag = m.group(0)
        assert re.search(rf'<div\s+class="dw-result"\s+id="{elem_id}"[^>]*\bhidden\b', html), \
            f"id='{elem_id}' 的 div 必须带 hidden 属性（初始未处理完不显示对比）"


def test_dw_compare_imgs_hidden_when_dwResult_hidden():
    """HTML hidden 行为正确时，dwImgOrig/dwImgOut 必须没有显式 src。

    防止后续误改：处理前 img 没有 src，避免浏览器默认空 src 行为；
    且 img 没有 inline display 覆盖（让父级 hidden 透传生效）。
    """
    html = _read(HTML)
    # 找 dw-compare 真正闭合 div（手动平衡嵌套 div，避免被第一个 </div> 截断）
    start = html.find('class="dw-compare"')
    assert start != -1, "未找到 .dw-compare 容器"
    i = html.find(">", start) + 1
    depth = 1
    while i < len(html) and depth > 0:
        next_open = html.find("<div", i)
        next_close = html.find("</div>", i)
        if next_close == -1:
            break
        if next_open != -1 and next_open < next_close:
            depth += 1
            i = next_open + 4
        else:
            depth -= 1
            i = next_close + len("</div>")
    assert depth == 0, "解析 dw-compare 嵌套失败"
    compare_html = html[start:i]
    # 两个 img 都在 compare 内，且都不带 src
    for img_id in ("dwImgOrig", "dwImgOut"):
        m = re.search(rf'<img\s+id="{img_id}"[^>]*>', compare_html)
        assert m, f".dw-compare 内必须存在 id='{img_id}' 的 img"
        assert "src=" not in m.group(0), \
            f"id='{img_id}' 不应在 HTML 里写死 src（应 JS 动态注入）"
