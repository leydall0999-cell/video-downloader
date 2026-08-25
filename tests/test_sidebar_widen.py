"""
Sidebar 宽度与主内容最大宽度的守门测试。

2026-08-26 用户反馈：「太挤了,左右可以拓宽一些」。本次改动：
  - .sidebar            width 240px → 200px
  - main#main           margin-left 240px → 200px
  - .wrap               max-width 880px → 1040px  （同时含 @media ≤640px 兜底）
  - .sidebar-item-icon  28px → 24px
  - .sidebar-item       padding .55rem .75rem .55rem .8rem → .45rem .5rem .45rem .65rem
                        font-size  .92rem → .84rem
  - .sidebar-brand-name .95rem → .85rem
  - .sidebar-group-title font-size .72rem → .66rem
  - .sidebar padding 1.2rem .85rem .8rem → 1rem .55rem .65rem

不允许这些数值被悄摸摸改回。
"""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parents[1] / "web" / "styles.css"
HTML = Path(__file__).resolve().parents[1] / "web" / "index.html"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rem_to_float(s: str) -> float:
    """e.g. '.5rem' → 0.5  /  '1.05rem' → 1.05"""
    return float(s.rstrip("rem"))


def test_sidebar_width_200():
    css = _read(CSS)
    # 搜 width: 200px（不出现在其他选择器里）
    m = re.search(r"\.sidebar\s*\{[^}]*width:\s*200px", css, re.S)
    assert m, "侧栏宽度应固定为 200px"


def test_main_margin_left_200():
    css = _read(CSS)
    m = re.search(r"main\s*#\s*main\s*\{\s*margin-left:\s*200px", css)
    assert m, "main#main 应该给侧栏让出 200px"


def test_wrap_max_width_880_centered():
    """用户纠正：主面板 .wrap max-width 改回 880px 居中，让主面板在主区里有呼吸。

    上一版（1040）在 1280 视口下侧栏 200 + 主区 1080，wrap 1040 占满贴右
    被截（用户截图 2026-08-26 01:19）。880 居中后：
      1280 视口 → 200 侧栏 + 880 wrap（主区两侧各 100px 留白）
      1440 视口 → 200 侧栏 + 880 wrap（主区两侧各 180px 留白）
    """
    css = _read(CSS)
    m = re.search(r"\.wrap\s*\{\s*width:\s*min\(\s*(\d+)px", css)
    assert m, ".wrap 必须定义 max-width"
    v = int(m.group(1))
    assert v == 880, f".wrap max-width 应=880px（用户纠正后定值），当前 {v}px"
    # 显式禁止 ≥ 1000 的 wrap 残留（防止再拉大贴右）
    for hit in re.finditer(r"\.wrap[^}]*width:\s*min\(\s*(\d+)px", css):
        assert int(hit.group(1)) < 1000, \
            f".wrap max-width 任何一处 ≥ 1000px 都会贴右（实际 {hit.group(1)}px）"
    # 显式禁止 1040 旧值
    assert "min(1040px" not in css, ".wrap 不能再用 1040px（之前贴右被截）"


def test_wrap_max_width_880_mobile_fallback():
    """小屏断点：@media ≤640px 兜底按 100%-1.5rem 自适应，max-width 也保持 880。"""
    css = _read(CSS)
    assert re.search(
        r"@media[^{]*max-width:\s*640px[^{]*\{[^}]*\.wrap[^}]*width:\s*min\(\s*880px\s*,\s*100%\s*-\s*1\.5rem\s*\)",
        css,
    ), "@media ≤640px .wrap 兜底应为 880px（min() 自动收缩到视口内）"


def test_sidebar_item_icon_size_24():
    css = _read(CSS)
    m = re.search(r"\.sidebar-item-icon\s*\{[^}]*width:\s*24px[^}]*height:\s*24px", css, re.S)
    assert m, "侧栏 item 图标盒尺寸应为 24×24"


def test_sidebar_item_padding_compact():
    """侧栏 item 的 padding 是紧致模式（≤ .5rem 横向）。"""
    css = _read(CSS)
    block = re.search(r"\.sidebar-item\s*\{[^}]*padding:\s*([^;]+);", css, re.S)
    assert block, "未找到 .sidebar-item 的 padding 声明"
    pad = block.group(1).strip()
    # 收紧后形如 ".45rem .5rem .45rem .65rem"
    nums = re.findall(r"[\d.]+rem", pad)
    assert len(nums) == 4, f"padding 必须是 4 个值（top right bottom left），实际 {pad!r}"
    # 横向最大值 ≤ .65rem
    horizontal_max = max(_rem_to_float(nums[1]), _rem_to_float(nums[3]))
    assert horizontal_max <= 0.66, \
        f"侧栏 item 横向 padding 最大值应 ≤ .65rem（现状 {nums[1]} / {nums[3]}）"


def test_sidebar_padding_compact():
    """侧栏整体 padding 必须紧凑（≤ .7rem 横向、≤ 1.1rem 纵向）。"""
    css = _read(CSS)
    # 找最近的 .sidebar { ... padding: ...; ... }
    m = re.search(r"\.sidebar\s*\{[^}]*padding:\s*([^;]+);", css, re.S)
    assert m, "未找到 .sidebar 的 padding 声明"
    pad = m.group(1).strip()
    nums = re.findall(r"[\d.]+rem", pad)
    assert len(nums) == 3, f".sidebar padding 必须是 3 个值（top h bottom），实际 {pad!r}"
    assert _rem_to_float(nums[1]) <= 0.7, f"侧栏横向 padding 应 ≤ .7rem（现状 {pad!r}）"
    assert _rem_to_float(nums[0]) <= 1.1, f"侧栏顶部 padding 应 ≤ 1.1rem（现状 {pad!r}）"


def test_index_html_has_nine_tab_ids():
    """保守闸门：index.html 必须包含 9 个 sTab*（侧栏版侧栏 tab）。"""
    html = _read(HTML)
    for tab_id in ("sTabDownload", "sTabLibrary", "sTabCommentary",
                   "sTabUploadConvert", "sTabDw", "sTabSubscribe",
                   "sTabTorrent", "sTabBaidu", "sTabPcs"):
        assert f'id="{tab_id}"' in html, f"缺失侧栏 item #{tab_id}"


def test_no_old_240_sidebar_width():
    css = _read(CSS)
    leaks = re.findall(r"\bsidebar\b[^{}]*\{\s*[^}]*width:\s*240px", css, re.S)
    assert not leaks, f"还存在 .sidebar 240px 定义：{leaks}"


def test_no_old_240_main_margin_left():
    css = _read(CSS)
    assert "margin-left: 240px" not in css, "main#main margin-left 还残留 240px"
