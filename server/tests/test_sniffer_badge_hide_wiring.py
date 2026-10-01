#!/usr/bin/env python3
"""嗅探面板「入口徽标按状态隐藏」接线守卫（2026-10-02，纯源码级离线）。

背景（用户反馈）：扩展装了之后，顶栏的「🔍 浏览器嗅探」徽标 + 右侧抽屉面板成了
纯诊断信息，用户问「是不是不需要了，可以隐藏」。设计取舍：

  * 扩展**在线且无待办**（全自动模式）→ 收起入口徽标（不再占顶栏）；
  * 扩展**不在线**（没装 / 浏览器没开）→ 徽标自动回来，因为那是「CDP 兜底嗅探」
    与「首次下载扩展」的**唯一入口**，不能一并藏掉；
  * 扩展在线**但还有待办**（浏览器里加载的不是受管目录那个 / 版本落后 / 磁盘已更新
    待重载，即 update-status 的 needs_setup）→ **也必须露出来**。否则扩展一连上就把
    入口收走，而「迁移到受管目录」「更新扩展」的引导**全在那个面板里**，用户反而无路
    可走（2026-10-02 自检发现的自相矛盾）。

本守卫锁住这条契约，尤其防止后人「顺手改成无条件隐藏」（会让无扩展用户无路可走），
防止「为了藏徽标把面板 DOM 一起删了」（会让 sniffQuality 取不到 qualitySel 而炸、
并丢掉扩展状态/清晰度下拉），也防止漏掉 needs_setup 这一项（入口把引导自己藏死）。

运行：python3 test_sniffer_badge_hide_wiring.py   （仓库根或本目录均可）
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_SRC = os.path.join(_REPO, "web", "js", "desktop-app.js")

_fail = []
_total = [0]


def check(name, cond, detail=""):
    _total[0] += 1
    if cond:
        print("  ✅ " + name)
    else:
        print("  ❌ " + name + (("  → " + str(detail)) if detail else ""))
        _fail.append(name)


def _src():
    with open(_SRC, "r", encoding="utf-8") as f:
        return f.read()


def _section(src, start_marker, end_marker):
    """取 start_marker 到其后第一个 end_marker 之间的片段（找不到返回整段）。"""
    i = src.find(start_marker)
    if i < 0:
        return ""
    j = src.find(end_marker, i + len(start_marker))
    return src[i:j] if j > 0 else src[i:]


def main():
    print("=" * 68)
    print("嗅探面板「入口徽标按状态隐藏」接线守卫")
    print("=" * 68)
    src = _src()

    print("\n① 状态与判定函数")
    check("定义了状态变量 snifferExtOnline",
          "let snifferExtOnline = false;" in src)
    check("定义了状态变量 snifferNeedsAttention",
          "let snifferNeedsAttention = false;" in src)
    check("applyBadgeVisibility 同时看「在线」与「有无待办」两个条件",
          "badgeBtn.hidden = !!snifferExtOnline && !snifferNeedsAttention;" in src)
    check("旧的「只看在线」判定已移除",
          "badgeBtn.hidden = !!snifferExtOnline; };" not in src)

    print("\n② renderStatus 用扩展在线状态驱动徽标")
    # 取 renderStatus 定义段
    seg = _section(src, "const renderStatus = (st) => {", "\n    const KIND_LABEL")
    check("renderStatus 拿到了 extOnline 判定",
          "const extOnline = !!(st && st.ext_online) && !cdpBusy;" in seg)
    check("renderStatus 把 extOnline 同步进 snifferExtOnline",
          "snifferExtOnline = extOnline;" in seg)
    check("renderStatus 调用了 applyBadgeVisibility()",
          "applyBadgeVisibility();" in seg)
    # 顺序：先赋值再调用，否则会用上一帧的旧值
    i_set = seg.find("snifferExtOnline = extOnline;")
    i_call = seg.find("applyBadgeVisibility();")
    check("先赋值后调用（避免用上一帧旧值）",
          0 <= i_set < i_call, f"set@{i_set} call@{i_call}")

    print("\n②b 面板关闭时也必须能拿到扩展状态（否则冷启动徽标隐藏不生效）")
    # renderStatus 原本只在「打开面板」时被调用；若没有常驻轮询，冷启动时 snifferExtOnline
    # 永远是 false → 徽标一直挂着。故必须有独立的状态轮询。
    check("定义了常驻状态轮询 refreshStatusOnly",
          "const refreshStatusOnly = async () =>" in src)
    check("常驻轮询只拉轻量 /api/sniffer/status",
          "renderStatus(await request('/api/sniffer/status'))" in src)
    # 用行首正则锚定「独立语句」，避免把 `// refreshStatusOnly();` 这种注释掉也算通过
    check("冷启动立即判定一次（不等第一次定时器）",
          bool(re.search(r"(?m)^\s*refreshStatusOnly\(\);", src)))
    check("用 setInterval 周期跑状态轮询",
          bool(re.search(r"(?m)^\s*setInterval\(refreshStatusOnly, POLL_STATUS_MS\);", src)))
    # 关着面板时绝不能拉被限流的 /items
    seg_poll = _section(src, "const refreshStatusOnly = async () =>", "// 桌面壳就绪后")
    check("状态轮询里不请求被限流的 /api/sniffer/items",
          "sniffer/items" not in seg_poll,
          "关面板时不该拉 /items（限流端点）")

    print("\n②c needs_setup 必须参与徽标显隐（否则入口把迁移/更新引导自己藏死）")
    check("refreshAutoStatus 把 needs_setup 写进 snifferNeedsAttention",
          "snifferNeedsAttention = !!(autoSt && autoSt.needs_setup);" in src)
    check("refreshAutoStatus 之后重新判定一次徽标",
          bool(re.search(r"renderAutoRow\(\);\s*\n\s*applyBadgeVisibility\(\);", src)))
    check("常驻状态轮询顺带刷新自动更新状态（冷启动即正确）",
          "try { await refreshAutoStatus(); } catch (e) { /* 静默 */ }" in src)

    print("\n③ wire() 不再无条件把徽标显示出来")
    check("wire 走 applyBadgeVisibility（不硬编码 hidden=false）",
          "const wire = () => { applyBadgeVisibility(); };" in src)
    check("旧的「就绪即强制显示」写法已移除",
          "const wire = () => { badgeBtn.hidden = false; };" not in src)

    print("\n④ 面板本体/自动链路必须保留（藏徽标≠拆功能）")
    check("入口徽标仍存在（只是按状态 hidden）",
          "badgeBtn.className = 'badge';" in src and "badgeBtn.id = 'sniffBadge';" in src)
    check("扩展自动回流轮询仍是常驻 setInterval（不受面板开关影响）",
          "pickedTimer = setInterval(pollPicked, POLL_PICKED_MS);" in src)
    check("面板清晰度下拉仍在（否则 sniffQuality 取不到 qualitySel 会炸）",
          "const qualitySel = panel.querySelector('#sniffQuality');" in src)

    print("\n⑤ 设计意图留痕（防后人改成无条件隐藏）")
    check("源码注释说明了「扩展不在线时徽标回来 / 是唯一入口」",
          ("扩展不在线" in src and "唯一入口" in src),
          "注释被删=后人容易误改成无条件隐藏")

    print("\n" + "-" * 68)
    ok = _total[0] - len(_fail)
    if _fail:
        print(f"通过: {ok}  失败: {len(_fail)}")
        for n in _fail:
            print("  ❌ " + n)
        return 1
    print(f"通过: {ok}  失败: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
