#!/usr/bin/env python3
"""嗅探面板「入口徽标常显」接线守卫（2026-10-23 改契约，纯源码级离线）。

历史：2026-10-02 用户说「这个是不是不需要了，可以隐藏」，于是做成
「扩展在线且无待办（全自动模式）→ 收起徽标；离线或 needs_setup → 露出来」。

2026-10-23 用户反馈「嗅探是隐藏了吗」——自动收起让入口凭空消失，用户找不到点。
现契约改为 **徽标一律常显**：🔍 浏览器嗅探 固定在顶栏，点开即面板。
面板内部仍按扩展状态切换「CDP 兜底 / 下载扩展」等按钮，与徽标显隐无关。

本守卫锁住新契约，防止后人「顺手」把显隐条件加回去（历史 bug 会复发）：
  * applyBadgeVisibility 里必须是恒 false（hidden=false），不得读 snifferExtOnline；
  * 因此 snifferExtOnline / snifferNeedsAttention 仍照旧计算（面板状态 + 回退用），
    不允许因为不参与显隐就删掉；
  * 面板 DOM、扩展自动回流轮询、清晰度下拉必须保留（常显≠拆功能）；
  * 源码注释里要留「常显」与回退写法，别让后人再猜一遍。

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


def _code_lines(src):
    """去掉整行注释与行尾注释后的代码行（避免「注释里写着旧写法」也被判通过）。"""
    out = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("//"):
            continue
        out.append(line.split("//")[0])
    return out


def _section(src, start_marker, end_marker):
    """取 start_marker 到其后第一个 end_marker 之间的片段（找不到返回空串）。"""
    i = src.find(start_marker)
    if i < 0:
        return ""
    j = src.find(end_marker, i + len(start_marker))
    return src[i:j] if j > 0 else src[i:]


def main():
    print("=" * 68)
    print("嗅探面板「入口徽标常显」接线守卫")
    print("=" * 68)
    src = _src()
    code = "\n".join(_code_lines(src))

    print("\n① 徽标恒显：applyBadgeVisibility 不得读扩展状态")
    seg = _section(src, "const applyBadgeVisibility = () => {", "\n    };")
    seg_code = "\n".join(_code_lines(seg))
    check("定义了 applyBadgeVisibility", bool(seg_code.strip()))
    check("函数体里 hidden 恒为 false",
          "badgeBtn.hidden = false;" in seg_code, seg_code.strip()[:120])
    check("函数体不再引用 snifferExtOnline / snifferNeedsAttention",
          ("snifferExtOnline" not in seg_code) and ("snifferNeedsAttention" not in seg_code),
          "显隐条件被加回＝「嗅探是隐藏了吗」会复发")

    print("\n①b 全文件代码里没有任何「按状态隐藏」的写法")
    bad = [l.strip() for l in _code_lines(src) if re.search(r"badgeBtn\.hidden\s*=\s*!!", l)]
    check("没有 badgeBtn.hidden = !!<状态> 形式的赋值", not bad, bad[:3])
    # 徽标创建处也必须常显
    check("创建处即 hidden=false（不等 ready 事件）",
          "badgeBtn.hidden = false;" in src.split("const applyBadgeVisibility")[0])

    print("\n①c 状态仍照旧计算（面板状态与回退要用，不许顺手删）")
    check("定义了状态变量 snifferExtOnline",
          "let snifferExtOnline = false;" in src)
    check("定义了状态变量 snifferNeedsAttention",
          "let snifferNeedsAttention = false;" in src)
    check("refreshAutoStatus 把 needs_setup 写进 snifferNeedsAttention",
          "snifferNeedsAttention = !!(autoSt && autoSt.needs_setup);" in src)
    check("needs_setup 变化后仍重新走一次 applyBadgeVisibility（保持单一出口）",
          bool(re.search(r"renderAutoRow\(\);\s*\n\s*applyBadgeVisibility\(\);", src)))

    print("\n② renderStatus 仍同步扩展在线状态")
    seg_rs = _section(src, "const renderStatus = (st) => {", "\n    const KIND_LABEL")
    check("renderStatus 拿到了 extOnline 判定",
          "const extOnline = !!(st && st.ext_online) && !cdpBusy;" in seg_rs)
    check("renderStatus 把 extOnline 同步进 snifferExtOnline",
          "snifferExtOnline = extOnline;" in seg_rs)
    check("renderStatus 调用了 applyBadgeVisibility()",
          "applyBadgeVisibility();" in seg_rs)

    print("\n②b 冷启动也要能拿到扩展状态（面板关着时同样成立）")
    check("定义了常驻状态轮询 refreshStatusOnly",
          "const refreshStatusOnly = async () =>" in src)
    check("常驻轮询只拉轻量 /api/sniffer/status",
          "renderStatus(await request('/api/sniffer/status'))" in src)
    check("冷启动立即判定一次（不等第一次定时器）",
          bool(re.search(r"(?m)^\s*refreshStatusOnly\(\);", src)))
    check("用 setInterval 周期跑状态轮询",
          bool(re.search(r"(?m)^\s*setInterval\(refreshStatusOnly, POLL_STATUS_MS\);", src)))
    seg_poll = _section(src, "const refreshStatusOnly = async () =>", "// 桌面壳就绪后")
    check("状态轮询里不请求被限流的 /api/sniffer/items",
          "sniffer/items" not in seg_poll,
          "关面板时不该拉 /items（限流端点）")
    check("常驻状态轮询顺带刷新自动更新状态（冷启动即正确）",
          "try { await refreshAutoStatus(); } catch (e) { /* 静默 */ }" in src)

    print("\n③ wire() 只走 applyBadgeVisibility（单一出口）")
    check("wire 走 applyBadgeVisibility",
          "const wire = () => { applyBadgeVisibility(); };" in src)

    print("\n④ 面板本体/自动链路必须保留（常显≠拆功能）")
    check("入口徽标仍存在",
          "badgeBtn.className = 'badge';" in src and "badgeBtn.id = 'sniffBadge';" in src)
    check("扩展自动回流轮询仍是常驻 setInterval（不受面板开关影响）",
          "pickedTimer = setInterval(pollPicked, POLL_PICKED_MS);" in src)
    check("面板清晰度下拉仍在（否则 sniffQuality 取不到 qualitySel 会炸）",
          "const qualitySel = panel.querySelector('#sniffQuality');" in src)

    print("\n⑤ 设计意图留痕（防后人再改成条件隐藏）")
    check("注释写明了「常显」",
          "常显" in src, "注释被删＝下一个人又要猜为什么不能隐藏")
    check("注释留了回退写法（历史行为可复原）",
          "snifferNeedsAttention;" in src and "回退" in src)

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
