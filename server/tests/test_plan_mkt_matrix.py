"""server/tests/test_plan_mkt_matrix.py —— 营销面板「模式 × 分组」联动守卫（2026-10-03）

背景：用户连续两次反馈「选了限量模式，限量参数框却不见了 / 显示成定时开售的框」。
根因是联动的三处命名不同名（模式 value=limited、分组 data-mkt=stock），
纯字符串比较永远不匹配。

这类「UI 元素凭空消失」的 bug 单测抓不到（本项目已踩 3 次），所以本守卫**直接解析
web/app.js 里的三处真源**做交叉核对：
  1) PLAN_MODES 的四个 value；
  2) _MKT_BY_MODE 映射表；
  3) 模板里的 data-mkt 标识集合；
并模拟 4 个模式 × 3 个分组的显隐矩阵，任何一处新增模式没加映射 / 分组写成孤儿就红。
"""
from __future__ import annotations

import pathlib
import re
import sys

FAILS: list[str] = []

APP_JS = pathlib.Path(__file__).resolve().parents[2] / "web" / "app.js"
GROUPS = ("flash", "stock", "event")
PLAN_MODE_VALUES = ("normal", "flash_sale", "limited", "event")


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + ((" — " + extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _parse_modes(src: str) -> list[str]:
    """从 PLAN_MODES 数组里取出全部 value（['normal', '普通（无活动）'] 这种形式）。"""
    i = src.find("const PLAN_MODES")
    if i < 0:
        return []
    j = src.find("];", i)
    block = src[i:j]
    return re.findall(r"\['([a-z_]+)'", block)


def _parse_map(src: str) -> dict[str, str]:
    """从 _MKT_BY_MODE 映射表里取出 {模式值: 分组标识}。"""
    i = src.find("_MKT_BY_MODE")
    if i < 0:
        return {}
    j = src.find("}", i)
    block = src[i:j]
    return dict(re.findall(r"([a-z_]+)\s*:\s*'([a-z]+)'", block))


def _parse_group_ids(src: str) -> set[str]:
    """模板里用到的全部 data-mkt 标识。"""
    return set(re.findall(r'data-mkt="([a-z]+)"', src))


def test_mode_list() -> None:
    print("\n[A] 模式清单")
    src = APP_JS.read_text(encoding="utf-8")
    modes = _parse_modes(src)
    check("能解析出 4 个售卖模式", len(modes) == 4, f"实际 {modes}")
    for m in ("normal", "flash_sale", "limited", "event"):
        check(f"模式含 {m}", m in modes, f"实际 {modes}")
    # 模式值必须与后端 PLAN_MODES 一致（后端会校验非法值回落 normal）
    # 注意后端是**元组** PLAN_MODES = ("normal", "flash_sale", "limited", "event")
    import membership as M                                       # noqa: E402
    back = sorted(str(x) for x in M.PLAN_MODES)
    check("与后端 membership.PLAN_MODES 完全一致",
          sorted(modes) == back, f"前端 {sorted(modes)} vs 后端 {back}")


def test_map_covers_all_modes() -> None:
    print("\n[B] 映射表覆盖所有非普通模式")
    src = APP_JS.read_text(encoding="utf-8")
    modes = _parse_modes(src)
    mp = _parse_map(src)
    check("能解析出 _MKT_BY_MODE", bool(mp), "未找到映射表")
    for m in modes:
        if m == "normal":
            continue
        check(f"模式 {m} 有分组映射", m in mp, f"映射表 = {mp}")
        if m in mp:
            check(f"模式 {m} 映射到的分组已定义", mp[m] in GROUPS,
                  f"{m} → {mp[m]}，已定义分组 {list(GROUPS)}")


def test_no_orphan_groups() -> None:
    print("\n[C] 没有永远不显示的孤儿分组")
    src = APP_JS.read_text(encoding="utf-8")
    mp = _parse_map(src)
    ids = _parse_group_ids(src)
    check("模板里定义了 3 个分组", len(ids) == 3, f"实际 {sorted(ids)}")
    targets = set(mp.values())
    orphan = sorted(ids - targets)
    check("每个分组都至少有一个模式能命中（无孤儿）", not orphan, f"孤儿 = {orphan}")
    missing = sorted(targets - ids)
    check("映射表指向的分组都在模板里存在", not missing, f"缺失 = {missing}")


def test_no_group_ever_hidden() -> None:
    """🔴 三组参数必须「常显」，任何模式下都不许 hidden。

    2026-10-03 用户反馈：按模式隐藏分组后，选了模式却找不到对应输入框
    （截图里选「限量」却只看到「补充说明」），比平铺一起更难用。
    现在改为常显 + 「当前模式生效」角标高亮，所以模板里不允许出现 hidden。
    """
    print("\n[D] 三组参数常显（任何模式都不隐藏）")
    src = APP_JS.read_text(encoding="utf-8")
    # 模板里的 data-mkt 标签一律不带 hidden 属性
    with_hidden = re.findall(r'data-mkt="([a-z]+)"[^>]*\shidden', src)
    check("模板里没有「data-mkt + hidden」的组合", not with_hidden,
          f"发现带 hidden 的分组：{with_hidden}")
    n = len(re.findall(r'data-mkt="([a-z]+)"', src))
    check("模板里共 3 个分组（每组 2 处：现有档位 + 新增档位）", n == 6, f"实际 {n} 处")
    # 三组都必须有「当前模式生效」角标
    tags = re.findall(r'class="plan-mkt-tag" data-tag="([a-z_]+)"', src)
    check("三组都带「当前模式生效」角标", sorted(set(tags)) == sorted(PLAN_MODE_VALUES[1:]),
          f"实际 {sorted(set(tags))}")


def test_matrix() -> None:
    print("\n[E] 4 模式 × 3 分组：高亮标记正确（不是隐藏）")
    src = APP_JS.read_text(encoding="utf-8")
    modes = _parse_modes(src)
    mp = _parse_map(src)
    expect = {
        "normal": [],
        "flash_sale": ["flash"],
        "limited": ["stock"],
        "event": ["event"],
    }
    for m in modes:
        want = mp.get(m) or ""
        # 高亮的组 = data-mkt === want 的那一个；普通模式不高亮任何组
        on = [g for g in GROUPS if g == want]
        check(f"模式 {m} → 高亮 {expect.get(m)}", on == expect.get(m), f"实际 {on}")
    check("普通模式不高亮任何组（角标全隐藏，但参数框都还在）",
          expect["normal"] == [] and not re.search(r'data-mkt="[a-z]+"[^>]*\shidden', src),
          "普通模式应一个都不高亮")


def main() -> int:
    print("=" * 60)
    print("营销面板 模式×分组 联动守卫")
    print("=" * 60)
    test_mode_list()
    test_map_covers_all_modes()
    test_no_orphan_groups()
    test_no_group_ever_hidden()
    test_matrix()
    print("\n" + "=" * 60)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 营销面板 模式×分组 联动守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    sys.exit(main())
