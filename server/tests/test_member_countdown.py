"""server/tests/test_member_countdown.py —— 会员卡秒杀/活动倒计时守卫（2026-10-03）

用户需求：「秒杀卡片上做个倒计时」。

倒计时属于「只在特定状态下才出现」的 UI，一旦后端不下发对应字段、
或前端类名写错、或定时器没启动，表现就是**静默什么都不显示**——
本项目这类「UI 元素凭空消失」的 bug 已踩 4 次，故本守卫做四层核对：

  [A] 后端 state 契约里有算倒计时需要的字段（mode / flash_price / flash_start /
      flash_end / start_at / end_at / is_flash / buyable）；
  [B] web/app.js 的接线：模板写 data-until、查询用同一选择器、单例定时器、
      渲染后启动、弹窗关闭停止；
  [C] _fmtCountdown 的真实行为（抽出来交给 node 跑，不是纸面断言）；
  [D] 回归钉：_memberMsg 函数体完整（防「整块被替换成签名行」这类事故，
      2026-10-03 真实发生过一次）。
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

FAILS: list[str] = []

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_JS = ROOT / "web" / "app.js"
CSS = ROOT / "web" / "styles.css"

NODE_CANDIDATES = (
    "/Users/suixindelang/.workbuddy/binaries/node/versions/22.22.2-3/bin/node",
    shutil.which("node") or "",
)


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + ((" — " + extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def test_state_contract() -> None:
    print("\n[A] 后端 state 契约（倒计时数据来源）")
    import membership as M                                       # noqa: E402
    st = M.plan_sales_state({
        "mode": "flash_sale", "price_cny": 9.9, "flash_price": 4.9,
        "flash_start": 1_700_000_000, "flash_end": 1_700_003_600,
        "start_at": 0, "end_at": 0,
    })
    for k in ("mode", "flash_price", "flash_start", "flash_end", "start_at",
              "end_at", "is_flash", "buyable", "price", "original_price"):
        check(f"state 含 {k}", k in st, f"实际字段 {sorted(st)}")
    # 活动模式也要能算「距开售 / 距结束」
    st2 = M.plan_sales_state({"mode": "event", "price_cny": 9.9,
                              "start_at": 1_700_000_000, "end_at": 1_700_003_600})
    check("活动模式 buyable=False 且有 start_at（可算距开售）",
          st2["buyable"] is False and st2["start_at"] > 0, f"state = {st2}")


def test_frontend_wiring() -> None:
    print("\n[B] 前端接线（模板 / 选择器 / 定时器生命周期）")
    src = APP_JS.read_text(encoding="utf-8")
    check("_fmtCountdown 已定义", "function _fmtCountdown" in src)
    check("模板下发 data-until", 'class="member-plan-count' in src and 'data-until="${cdUntil}"' in src)
    check("查询选择器与模板类名一致（member-plan-count[data-until]）",
          "querySelectorAll('.member-plan-count[data-until]')" in src)
    check("数字节点类名一致（member-count-val）",
          "'.member-count-val'" in src and 'class="member-count-val"' in src)
    check("单例定时器（避免重复 setInterval 越跑越多）",
          "if (!_memberCdTimer) _memberCdTimer = setInterval(" in src)
    check("渲染会员卡后启动倒计时",
          "_startMemberCountdowns();" in src)
    check("弹窗 close → 停表",
          "addEventListener('close', _stopMemberCountdowns)" in src)
    check("到点后刷新一次卡片（价格/角标跟着回落）",
          "renderMemberPlans();" in src and "_memberCdRefreshedAt" in src)
    check("倒计时插在价格行之后",
          src.find("${price}${unit}</div>\n        ${countdown}") > 0)
    css = CSS.read_text(encoding="utf-8")
    for cls in (".member-plan-count", ".member-count-val", ".member-plan-count.is-done"):
        check(f"样式含 {cls}", cls in css)


def _extract_fmt_countdown(src: str) -> str:
    i = src.find("function _fmtCountdown")
    if i < 0:
        return ""
    j = src.find("\n  }", i)
    return src[i:j + 4]


def test_countdown_format_behavior() -> None:
    print("\n[C] _fmtCountdown 真实行为（交给 node 跑）")
    node = next((p for p in NODE_CANDIDATES if p and pathlib.Path(p).exists()), "")
    if not node:
        check("找到 node 可执行文件", False, "未找到 node，跳过行为断言")
        return
    src = APP_JS.read_text(encoding="utf-8")
    fn = _extract_fmt_countdown(src)
    if not fn:
        check("能抽取 _fmtCountdown 源码", False)
        return
    cases = [(59, "00:00:59"), (61, "00:01:01"), (3661, "01:01:01"),
             (90061, "1天 01:01:01"), (0, "00:00:00"), (-5, "00:00:00")]
    args = ",".join(str(c[0]) for c in cases)
    code = fn + "\nconsole.log(JSON.stringify([" + args + "].map(_fmtCountdown)));"
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        check("node 执行成功", False, out.stderr.strip()[:200])
        return
    got = json.loads(out.stdout.strip())
    for (sec, want), g in zip(cases, got):
        check(f"{sec} 秒 → {want}", g == want, f"实际 {g}")


def _extract_func(src: str, name: str) -> str:
    i = src.find("function " + name)
    if i < 0:
        return ""
    j = src.find("\n  }", i)
    return src[i:j + 4]


def test_render_real_output() -> None:
    """把 _memberCard 抽出来在 node 里真跑，直接断言产出的 HTML。

    静态 grep 断言只能证明「字符串在文件里」，证不了「渲染出来真有这个节点」——
    本项目 2026-10-03 就发生过整块模板被误删、grep 却以为没事的事故。
    """
    print("\n[E] _memberCard 真实渲染（node 实跑）")
    node = next((p for p in NODE_CANDIDATES if p and pathlib.Path(p).exists()), "")
    if not node:
        check("找到 node 可执行文件", False, "未找到 node，跳过渲染断言")
        return
    src = APP_JS.read_text(encoding="utf-8")
    card = _extract_func(src, "_memberCard")
    if not card:
        check("能抽取 _memberCard 源码", False)
        return
    now = 1_700_000_000
    harness = """
const esc = (s) => String(s == null ? '' : s);
const escHtml = esc;
function _memberFmtDate(ts, withTime) { return ts ? 'T' + ts : '--'; }
// 必须用真实当前时间：_memberCard 内部用 Date.now() 判「未开始/进行中」
const NOW = Math.floor(Date.now() / 1000);
""" + card + """
const base = { price_cny: 9.9 };
const flash = Object.assign({}, base, {
  state: { mode: 'flash_sale', on_sale: true, buyable: true, reason: '', price: 4.9,
           original_price: 9.9, is_flash: true, flash_price: 4.9,
           flash_start: NOW - 60, flash_end: NOW + 3661,
           start_at: 0, end_at: 0, stock: 0, sold: 0, remaining: null, badge: '限时秒杀', desc: '' },
});
const soon = Object.assign({}, base, {
  state: { mode: 'flash_sale', on_sale: true, buyable: true, reason: '', price: 9.9,
           original_price: 9.9, is_flash: false, flash_price: 4.9,
           flash_start: NOW + 3661, flash_end: NOW + 7200,
           start_at: 0, end_at: 0, stock: 0, sold: 0, remaining: null, badge: '', desc: '' },
});
const ev = Object.assign({}, base, {
  state: { mode: 'event', on_sale: true, buyable: false, reason: '活动未开始', price: 9.9,
           original_price: 9.9, is_flash: false, flash_price: 0, flash_start: 0, flash_end: 0,
           start_at: NOW + 120, end_at: NOW + 7200, stock: 0, sold: 0, remaining: null,
           badge: '', desc: '' },
});
const none = Object.assign({}, base, {
  state: { mode: 'normal', on_sale: true, buyable: true, reason: '', price: 9.9,
           original_price: 9.9, is_flash: false, flash_price: 0, flash_start: 0, flash_end: 0,
           start_at: 0, end_at: 0, stock: 0, sold: 0, remaining: null, badge: '', desc: '' },
});
console.log(JSON.stringify({
  now: NOW,
  flash: _memberCard(flash, 'download_1day', { unit: ' / 1天' }),
  soon: _memberCard(soon, 'download_1day', { unit: ' / 1天' }),
  ev: _memberCard(ev, 'download_1day', { unit: ' / 1天' }),
  none: _memberCard(none, 'download_1day', { unit: ' / 1天' }),
}));
"""
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=25)
    if out.returncode != 0:
        check("node 渲染 _memberCard 成功", False, out.stderr.strip()[:300])
        return
    r = json.loads(out.stdout.strip())
    now = r["now"]

    h = r["flash"]
    check("秒杀进行中：产出倒计时节点", 'class="member-plan-count is-flash"' in h)
    check("秒杀进行中：data-until = flash_end", f'data-until="{now + 3661}"' in h)
    check("秒杀进行中：文案「距秒杀结束」", "距秒杀结束" in h)
    check("秒杀进行中：有占位符待秒级刷新", "--:--:--" in h)
    check("秒杀进行中：显示秒杀价 + 原价划线", "is-flash" in h and "member-price-orig" in h)

    h2 = r["soon"]
    check("秒杀未开始：文案「距开抢」", "距开抢" in h2 and f'data-until="{now + 3661}"' in h2)
    check("秒杀未开始：有「即将开抢」角标", "即将开抢" in h2)

    h3 = r["ev"]
    check("活动未开始：文案「距开售」", "距开售" in h3 and f'data-until="{now + 120}"' in h3)
    check("活动未开始：仍显示「活动未开始」置灰角标", "活动未开始" in h3)

    h4 = r["none"]
    check("普通档：没有倒计时节点", "member-plan-count" not in h4)


def test_member_msg_intact() -> None:
    print("\n[D] 回归钉：_memberMsg 函数体完整")
    src = APP_JS.read_text(encoding="utf-8")
    i = src.find("function _memberMsg")
    j = src.find("\n  }", i)
    body = src[i:j] if i >= 0 else ""
    for need in ("el.memberActMsg.textContent", "el.memberActMsg.hidden",
                 "el.memberActMsg.style.color"):
        check(f"_memberMsg 保留 {need}", need in body, "函数体被截断过？")

    # 防「整块替换」：这两个函数也必须各自有闭合体
    for fname in ("function _memberCard", "function _startMemberCountdowns",
                  "function _stopMemberCountdowns", "_tickMemberCountdowns"):
        k = src.find(fname)
        check(f"{fname} 有函数体", k > 0 and re.search(r"\{\s*\S", src[k:k + 200]) is not None)


def main() -> int:
    print("=" * 60)
    print("会员卡秒杀/活动倒计时守卫")
    print("=" * 60)
    test_state_contract()
    test_frontend_wiring()
    test_countdown_format_behavior()
    test_render_real_output()
    test_member_msg_intact()
    print("\n" + "=" * 60)
    if FAILS:
        print("❌ 失败 %d 项：" % len(FAILS))
        for f in FAILS:
            print("   - " + f)
        return 1
    print("✅ 会员卡倒计时守卫全部通过")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    sys.exit(main())
