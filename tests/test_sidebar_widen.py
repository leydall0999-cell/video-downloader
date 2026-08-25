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


def test_wrap_max_width_800_centered():
    """用户 2026-08-26 01:24 继续反馈「还是有点挤」→ wrap 从 880 再收到 800px 居中。

    主区净宽（视口 - 侧栏 200）：
      1280 视口 → 主区 1080，panel 800 居中 → 两侧各 140px 留白（之前 880 只有 100）
      1440 视口 → 主区 1240，panel 800 居中 → 两侧各 220px 留白
    """
    css = _read(CSS)
    m = re.search(r"\.wrap\s*\{\s*width:\s*min\(\s*(\d+)px", css)
    assert m, ".wrap 必须定义 max-width"
    v = int(m.group(1))
    assert v == 800, f".wrap max-width 应=800px（用户二次纠正后定值），当前 {v}px"
    # 显式禁止 ≥ 850 的 wrap 残留（防止再拉大贴右变挤）
    for hit in re.finditer(r"\.wrap[^}]*width:\s*min\(\s*(\d+)px", css):
        assert int(hit.group(1)) < 850, \
            f".wrap max-width 任何一处 ≥ 850px 都会显得挤（实际 {hit.group(1)}px）"
    # 显式禁止 880 / 1040 旧值
    assert "min(880px" not in css, ".wrap 不能再用 880px（用户已嫌挤）"
    assert "min(1040px" not in css, ".wrap 不能再用 1040px（之前贴右被截）"


def test_wrap_max_width_800_mobile_fallback():
    """小屏断点：@media ≤640px 兜底按 100%-1.5rem 自适应，max-width 也保持 800。"""
    css = _read(CSS)
    assert re.search(
        r"@media[^{]*max-width:\s*640px[^{]*\{[^}]*\.wrap[^}]*width:\s*min\(\s*800px\s*,\s*100%\s*-\s*1\.5rem\s*\)",
        css,
    ), "@media ≤640px .wrap 兜底应为 800px（min() 自动收缩到视口内）"


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


def test_panel_padding_spacious():
    """用户 2026-08-26 01:24 二轮反馈「还是有点挤」→ panel padding 从 1.25rem 加到 ≥1.5rem。

    主体 .panel padding 必须 ≥ 1.5rem（让内容离边框有明显距离），margin-bottom ≥ 1.2rem
    （让相邻 panel 之间的呼吸更明显）。
    """
    css = _read(CSS)
    m = re.search(r"\.panel\s*\{[^}]*padding:\s*([^;]+);[^}]*margin-bottom:\s*([^;]+);", css, re.S)
    assert m, "未找到 .panel 的 padding + margin-bottom 声明"
    pad_str, mb_str = m.group(1).strip(), m.group(2).strip()
    # padding 至少 2 个值；取横向最大值
    pad_nums = re.findall(r"[\d.]+rem", pad_str)
    assert len(pad_nums) >= 2, f".panel padding 至少 2 个值，实际 {pad_str!r}"
    horizontal = max(_rem_to_float(pad_nums[1]), _rem_to_float(pad_nums[3])) if len(pad_nums) == 4 else _rem_to_float(pad_nums[1])
    assert horizontal >= 1.5, f".panel 横向 padding 必须 ≥ 1.5rem（防再改小变挤），当前 {horizontal}rem"
    # margin-bottom 必须 ≥ 1.2rem
    mb_val = _rem_to_float(re.findall(r"[\d.]+rem", mb_str)[0])
    assert mb_val >= 1.2, f".panel margin-bottom 必须 ≥ 1.2rem（防再改小变挤），当前 {mb_val}rem"


def test_hero_padding_top_spacious():
    """hero 标题与下方主面板间距：上 padding ≥ 2.8rem、下 padding ≥ 2rem。"""
    css = _read(CSS)
    m = re.search(r"\.hero\s*\{\s*text-align:\s*center;\s*padding:\s*([^;]+);", css)
    assert m, "未找到 .hero 的 padding 声明"
    parts = m.group(1).split()
    assert len(parts) == 3, f".hero padding 必须是 3 个值（top h bottom），实际 {m.group(1)!r}"
    top = float(parts[0].replace("rem", "")) if "rem" in parts[0] else 0
    bot = float(parts[2].replace("rem", "")) if "rem" in parts[2] else 0
    assert top >= 2.8, f".hero 顶部 padding 必须 ≥ 2.8rem，当前 {parts[0]}"
    assert bot >= 2.0, f".hero 底部 padding 必须 ≥ 2rem（让标题与 panel 距离更开），当前 {parts[2]}"


def test_no_old_240_sidebar_width():
    css = _read(CSS)
    leaks = re.findall(r"\bsidebar\b[^{}]*\{\s*[^}]*width:\s*240px", css, re.S)
    assert not leaks, f"还存在 .sidebar 240px 定义：{leaks}"


def test_no_old_240_main_margin_left():
    css = _read(CSS)
    assert "margin-left: 240px" not in css, "main#main margin-left 还残留 240px"


def test_main_and_wrap_are_separate_elements():
    """用户 2026-08-26 01:34 反馈「往右挪一点,挤到一起了」根因：

    旧版 <main id="main" class="wrap"> 让 main 和 .wrap 是同一个元素，
    CSS 上 main#main { margin-left: 200px } 和 .wrap { margin-inline: auto }
    在同一元素打架——main#main 特异性更高，margin-left 赢 200px，
    margin-right 还是 auto，宽度 800px 被死死顶在 200px 处、右边留 280px 大空，
    视觉上"挤到左边"。

    修复：拆成 <main id="main"><div class="wrap">…</div></main>，让 main 专职给
    固定侧栏让位（margin-left:200），div.wrap 专职在 main 内居中
    （margin-inline:auto + width:800）。两者职责分离，居中才能真正生效。

    此测试防回滚：main 不能带 wrap 类；wrap 必须是 main 的后代元素。
    """
    html = _read(HTML)
    # 1) main 元素不能再带 class="wrap"
    m = re.search(r"<main\b[^>]*>", html)
    assert m, "未找到 <main> 元素"
    main_tag = m.group(0)
    assert 'class="wrap"' not in main_tag, \
        f"main 不能带 class='wrap'（会和 main#main margin-left 打架把内容挤到左边）: {main_tag}"
    # 2) main 内部必须有 <div class="wrap">（真正的居中容器）
    assert re.search(r'<main\b[^>]*>\s*<div\s+class="wrap">', html), \
        "main 内部必须包一个 <div class=\"wrap\"> 居中容器（main/wrap 必须拆开）"
    # 3) 反向断言：HTML 里 class="wrap" 只能出现在 <div> 上，不能在 <main> 上
    for hit in re.finditer(r'class="wrap"', html):
        # 找到 class="wrap" 所在的标签起始位置，向前回溯最近的 '<'
        idx = hit.start()
        tag_start = html.rfind("<", 0, idx)
        tag = html[tag_start:idx]
        assert tag.lstrip("<").startswith("div"), \
            f'class="wrap" 必须在 <div> 上，不能在 {tag!r}（会和 main margin-left 冲突）'
