#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首页「热门功能快捷入口」接线守卫（纯离线，只读源码）。

背景（2026-09-30 用户要求「快捷入口继续完善」）：
    首页是用户打开 App 的第一屏，它的失效全是**静默**的——
      · 卡片 data-view 拼错 → 点了没反应，控制台不报错；
      · 侧栏加了新功能忘了加首页 → 新功能永远没人从首页发现
        （生成二维码 / 生成网页 上线后就一直没进首页）；
      · 分组容器改名/嵌套漏写 → 卡片整块消失或被吞进看不见的层。
    2026-09-30 把 12 张平铺卡片改成与侧栏同款的 5 组（下载 / 创作 / 转换 / 工具 / 媒体库），
    并补齐「生成二维码 / 生成网页 / 音乐转换 / 图片转换 / 视频音频桥接」五个入口 → 17 张。

本测试钉住：
  A. 首页每一张卡片的 data-view 必须是侧栏真实存在的视图名（防拼错 = 点了没反应），且不重复；
  B. 侧栏的每个主功能（非 home / 非个人中心 / 非隐藏）都必须在首页有入口（防"加了功能忘了首页"）；
  C. 首页分组名与侧栏分组名一致，且分组容器 / 样式齐备（防容器改名导致卡片不成组）；
  D. 首页点击仍是事件委托（closest('[data-view]')），新增卡片不需要再写一行 JS。

运行：
    cd server && python tests/test_home_entries.py
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_JS = REPO / "web" / "app.js"
INDEX_HTML = REPO / "web" / "index.html"
STYLES_CSS = REPO / "web" / "styles.css"

# 首页不该出现这些视图：home 是自己，profile* 是个人中心分组，torrent 是隐藏功能
_SKIP_PREFIX = ("profile",)
_SKIP_EXACT = {"home", "torrent"}

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


def sidebar_entries(html):
    """侧栏（桌面端）功能项：返回 [(view, hidden)]，排除个人中心 / 后台。"""
    out = []
    for m in re.finditer(r'<button\b[^>]*class="sidebar-item[^"]*"[^>]*>', html):
        tag = m.group(0)
        vm = re.search(r'data-view="([^"]+)"', tag)
        if not vm:
            continue
        out.append((vm.group(1), re.search(r'\bhidden\b', tag) is not None))
    return out


def main():
    print("▶ 首页「热门功能快捷入口」接线守卫")
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_js = APP_JS.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")

    # ---- 侧栏真源 ----
    side = sidebar_entries(html)
    check("侧栏能解析出功能项（解析锚点没被改坏）", len(side) >= 15, f"只解到 {len(side)} 项")
    side_views = [v for v, h in side]
    side_visible = {v for v, h in side if not h and v not in _SKIP_EXACT
                    and not v.startswith(_SKIP_PREFIX)}

    # ---- 首页卡片 ----
    start = html.find('class="home-groups" id="homeGrid"')
    check("首页有分组容器 .home-groups#homeGrid", start != -1)
    if start == -1:
        return 1
    end = html.find("\n    </div>", start)
    check("首页分组容器有正确闭合", end != -1)
    block = html[start:end]
    home_views = re.findall(r'data-view="([^"]+)"', block)

    print("\n[A] 卡片 data-view 必须是侧栏真实视图名（拼错=点了没反应）")
    bad = sorted({v for v in home_views if v not in side_views})
    check(f"全部 {len(home_views)} 张卡片的 data-view 都在侧栏存在", not bad, f"幽灵视图：{bad}")
    dup = sorted({v for v in home_views if home_views.count(v) > 1})
    check("首页没有重复入口", not dup, f"重复：{dup}")

    print("\n[B] 侧栏主功能必须在首页有入口（防加了功能忘了首页）")
    missing = sorted(side_visible - set(home_views))
    check(f"侧栏 {len(side_visible)} 个主功能已全部出现在首页", not missing, f"首页缺失：{missing}")

    print("\n[C] 分组结构与侧栏分组名一致")
    home_groups = re.findall(r'class="home-group-emoji"[^>]*>[^<]*</span>([^<]+)</h3>', block)
    side_groups = re.findall(r'class="sidebar-group-en"[^>]*>([^<]+)</span>', html)
    check("首页解析出 5 个分组", len(home_groups) == 5, f"实际 {home_groups}")
    for want in ("下载", "创作", "转换", "工具", "媒体库"):
        check(f"首页有「{want}」分组", want in home_groups)
    unknown = [g for g in home_groups if g not in side_groups]
    check("首页分组名都来自侧栏（没有自造分组名）", not unknown, f"未知：{unknown}")

    print("\n[D] 点击仍是事件委托（新增卡片不用再写 JS）")
    check("homeView 上委托 closest('[data-view]')",
          re.search(r"el\.homeView\.addEventListener\('click'[\s\S]{0,220}?closest\('\[data-view\]'\)",
                    app_js) is not None)

    print("\n[E] 分组样式齐备（否则卡片平铺成一片、看不出分组）")
    # 必须匹配到规则块本体（带 {）——只查子串会被 .home-group-title::before 之类的
    # 派生选择器蒙混过关（2026-09-30 变异测试实测：删掉规则本体仍绿）。
    for sel in (".home-groups {", ".home-group-title {", ".home-group .home-grid {"):
        check(f"styles.css 有规则块 {sel}", sel in css)

    print("")
    print("=========================================")
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
