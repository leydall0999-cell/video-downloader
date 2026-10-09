"""server/tests/test_plan_mkt_matrix.py —— 营销面板「模式 × 分组」联动守卫（2026-10-03）

背景：用户连续两次反馈「选了限量模式，限量参数框却不见了 / 显示成定时开售的框」。
根因是联动的三处命名不同名（模式 value=limited、分组 data-mkt=stock），
纯字符串比较永远不匹配。

这类「UI 元素凭空消失」的 bug 单测抓不到（本项目已踩 3 次），所以本守卫**直接解析
web/app.js 里的四处真源**做交叉核对：
  1) PLAN_MODES 的四个 value；
  2) _MKT_BY_MODE 映射表（2026-10-03 起为「模式 → 分组数组」，活动模式同时要
     「定时开售」+「数量」两组）；
  3) 模板里的 data-mkt 标识集合；
  4) _MKT_GROUPS_UNIVERSAL（2026-10-09 起，独立于模式联动的全模式可见组，
     例如「每人限购」任何模式都该显示，不放进 _MKT_BY_MODE 防止漏改）。
并模拟 4 个模式 × 4 个分组的显隐矩阵，任何一处新增模式没加映射 / 分组写成孤儿就红。

⚠️ 同一个 UI 有两处模板（现有档位渲染 planBlock / 新增档位时插入的节点），
两处都要有同样的分组，漏一处就会出现「新加的档位看不到数量框」。
"""
from __future__ import annotations

import pathlib
import re
import sys

FAILS: list[str] = []

APP_JS = pathlib.Path(__file__).resolve().parents[2] / "web" / "app.js"
GROUPS = ("flash", "stock", "event", "limit")
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


def _parse_map(src: str) -> dict[str, list[str]]:
    """从 _MKT_BY_MODE 映射表里取出 {模式值: [分组标识...]}。

    形如：const _MKT_BY_MODE = { flash_sale: ['flash'], event: ['event', 'stock'], ... };
    """
    i = src.find("_MKT_BY_MODE = {")
    if i < 0:
        return {}
    j = src.find("\n", i)
    block = src[i:j if j > 0 else len(src)]
    out: dict[str, list[str]] = {}
    for m, body in re.findall(r"([a-z_]+)\s*:\s*\[([^\]]*)\]", block):
        out[m] = re.findall(r"'([a-z]+)'", body)
    return out


def _parse_group_ids(src: str) -> set[str]:
    """模板里用到的全部 data-mkt 标识。"""
    return set(re.findall(r'data-mkt="([a-z]+)"', src))


def _parse_universal(src: str) -> list[str]:
    """_MKT_GROUPS_UNIVERSAL（独立于模式联动的全模式可见组，2026-10-09 起）。"""
    i = src.find("_MKT_GROUPS_UNIVERSAL = [")
    if i < 0:
        return []
    j = src.find("];", i)
    return re.findall(r"'([a-z]+)'", src[i:j])


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
    check("能解析出 _MKT_BY_MODE（数组形式）", bool(mp), f"未找到映射表 / 解析为空：{mp}")
    for m in modes:
        if m == "normal":
            continue
        check(f"模式 {m} 有分组映射", m in mp, f"映射表 = {mp}")
        if m in mp:
            check(f"模式 {m} 至少映射 1 个分组", len(mp[m]) >= 1, f"{m} → {mp[m]}")
            for g in mp[m]:
                check(f"模式 {m} 映射的分组 {g} 已定义", g in GROUPS,
                      f"{m} → {mp[m]}，已定义分组 {list(GROUPS)}")


def test_no_orphan_groups() -> None:
    print("\n[C] 没有永远不显示的孤儿分组")
    src = APP_JS.read_text(encoding="utf-8")
    mp = _parse_map(src)
    ids = _parse_group_ids(src)
    universal = set(_parse_universal(src))
    check("模板里定义了 4 个分组（flash/stock/event/limit）", len(ids) == 4,
          f"实际 {sorted(ids)}")
    targets: set[str] = set()
    for lst in mp.values():
        targets.update(lst)
    # 孤儿判定：模板里出现的分组，要么被某个模式映射到，要么显式列在 universal 组
    orphan = sorted(ids - targets - universal)
    check("每个分组要么被模式映射、要么是 universal 组（无孤儿）",
          not orphan, f"孤儿 = {orphan}（被模式映射到的 = {sorted(targets)}, universal = {sorted(universal)}）")
    missing = sorted(targets - ids)
    check("映射表指向的分组都在模板里存在", not missing, f"缺失 = {missing}")


def test_groups_linked_to_mode() -> None:
    """选什么模式只显示该模式的参数（用户 2026-10-03 明确要求，最直观）。

    🔴 这条断言是「防来回折腾」用的：今天在这个 UI 上改了三版
    （全平铺 → 按模式隐藏 → 全显+高亮 → 回到按模式隐藏），
    每次都是因为把「还没切模式所以看不到参数」误当成 bug。
    现在钉死：模板里分组不带 hidden（由 JS 联动控制），
    且 applyMktGroups 必须用映射表数组做 includes 判断。
    """
    print("\n[D] 分组与模式联动（选什么模式只显示该模式参数）")
    src = APP_JS.read_text(encoding="utf-8")
    n = len(re.findall(r'data-mkt="([a-z]+)"', src))
    check("模板里共 4 个分组 × 2 处（现有档位 + 新增档位）", n == 8, f"实际 {n} 处")
    # 模板里不应把分组写死 hidden（否则初始渲染就全隐藏了）
    with_hidden = re.findall(r'data-mkt="([a-z]+)"[^>]*\shidden', src)
    check("模板里没有写死 hidden（初始渲染交由 JS 联动）", not with_hidden,
          f"发现 {with_hidden}")
    # 两处模板都要有「数量」输入框（漏一处 → 新加的档位看不到数量）
    n_stock = len(re.findall(r'class="[^"]*plan-stock', src))
    check("两处模板都有 .plan-stock 输入框", n_stock == 2, f"实际 {n_stock} 处")
    n_stock_label = src.count(">总份数<input")
    check("两处模板都用了通用文案「总份数」", n_stock_label == 2, f"实际 {n_stock_label} 处")
    # JS 联动语句必须存在，且用映射表 + includes
    check("applyMktGroups 用映射表数组比较（不是直接比 data-mkt !== mode）",
          "_MKT_BY_MODE" in src and "!want.includes(g.dataset.mkt)" in src)
    # 2026-10-09 起 universal 组（limit）通过 .concat() 强制全模式显示，不放进 _MKT_BY_MODE
    check("universal 组通过 .concat() 强制全模式可见（不污染模式映射表）",
          ".concat(..._MKT_GROUPS_UNIVERSAL)" in src or ".concat(_MKT_GROUPS_UNIVERSAL)" in src)
    check("change 与 click 双挂（WKWebView 下 change 未必冒泡成 click）",
          "addEventListener('change'" in src and "t.closest('.plan-mode')" in src)
    check("渲染后立即标注一次（不必等用户动下拉）",
          "querySelectorAll('.admin-plan-item').forEach((it) => applyMktGroups(it))" in src)


def test_matrix() -> None:
    print("\n[E] 4 模式 × 4 分组：显示矩阵")
    src = APP_JS.read_text(encoding="utf-8")
    modes = _parse_modes(src)
    mp = _parse_map(src)
    universal = _parse_universal(src)
    # 普通模式只显示 universal 组；其他模式叠加模式映射的组
    expect = {
        "normal": list(universal),
        "flash_sale": sorted(set(["flash"]) | set(universal)),
        "limited": sorted(set(["stock"]) | set(universal)),
        "event": sorted(set(["event", "stock"]) | set(universal)),
    }
    for m in modes:
        want = sorted(set(mp.get(m) or []) | set(universal))
        check(f"模式 {m} → 显示 {expect.get(m)}", want == expect.get(m),
              f"实际 {want}")


def test_event_mode_has_qty() -> None:
    """用户 2026-10-03：活动（定时开售）模式「这个也加个数量」。"""
    print("\n[F] 活动模式必须能填数量")
    src = APP_JS.read_text(encoding="utf-8")
    mp = _parse_map(src)
    check("event 模式映射含 stock 分组", "stock" in (mp.get("event") or []),
          f"event → {mp.get('event')}")
    check("limited 模式映射仍含 stock 分组", "stock" in (mp.get("limited") or []),
          f"limited → {mp.get('limited')}")
    # 后端 plansales 状态对任意模式都算 stock/sold，这里交叉确认字段名一致
    import membership as M                                       # noqa: E402
    st = M.plan_sales_state({"mode": "event", "price_cny": 10, "stock": 5, "sold": 5})
    check("后端 event 模式 stock 售罄会置灰（buyable=False）", st["buyable"] is False,
          f"state = {st}")
    st2 = M.plan_sales_state({"mode": "event", "price_cny": 10, "stock": 5, "sold": 1})
    check("后端 event 模式 stock 未售罄可买且剩余 4", st2["buyable"] and st2["remaining"] == 4,
          f"state = {st2}")


def main() -> int:
    print("=" * 60)
    print("营销面板 模式×分组 联动守卫")
    print("=" * 60)
    test_mode_list()
    test_map_covers_all_modes()
    test_no_orphan_groups()
    test_groups_linked_to_mode()
    test_matrix()
    test_event_mode_has_qty()
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
