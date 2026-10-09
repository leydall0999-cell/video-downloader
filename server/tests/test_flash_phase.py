# -*- coding: utf-8 -*-
"""守卫：秒杀窗口过期后必须自动收起秒杀角标（2026-10-03 用户需求）。

背景：后台给「VIP会员·1 天」配了秒杀窗口且角标文案填了「限时秒杀」。
窗口过了之后价格早已回原价，但卡片上「限时秒杀」角标和「秒杀至 XX:XX」
照旧挂着 —— 用户会以为自己正在享受秒杀价。

本守卫钉住三件事：
  [A] 后端 plan_sales_state 产出 flash_phase 三态（upcoming/active/ended），
      非秒杀模式（含「只填了秒杀时间但 mode 不是 flash_sale」）一律 none；
  [B] 前端两处卡片渲染（个人中心 pfRenderPlans / 会员页 memPlanCard）都按
      三态决定「秒杀价划线 / 秒杀角标 / 秒杀至…」是否出现，ended 必须全部收起；
  [C] pfFlashPhase 真实行为（交给 node 跑）：老后端没 flash_phase 字段时能
      按 mode/flash_start/flash_end 自行推导，保证只热更前端也生效；
  [D] 停留在页面上跨过临界点会自动重渲染（pfWatchFlashBoundary 已接线）。

所有用例离线进行（VDL_PLANS_CLOUD=0），绝不写真实家目录、不连网络。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent.parent
_tmp = tempfile.mkdtemp(prefix="vdl_flash_guard_")
os.environ["VDL_DATA_DIR"] = _tmp
os.environ["VDL_CLOUD_LINK"] = "0"
os.environ["VDL_PLANS_CLOUD"] = "0"

sys.path.insert(0, str(HERE.parent))
import membership as M  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, note: str = "") -> None:
    print(("  \u2705 " if cond else "  \u274c ") + name + (("  — " + note) if (note and not cond) else ""))
    if not cond:
        FAILS.append(name)


NOW = 1_800_000_000


def _plan(**kw) -> dict:
    base = {"price_cny": 0.99, "days": 1, "label": "VIP会员·1 天"}
    base.update(kw)
    return base


def test_backend_phase() -> None:
    print("\n[A] 后端 plan_sales_state.flash_phase 三态")
    st = M.plan_sales_state(_plan(mode="flash_sale", flash_price=0.5,
                                  flash_start=NOW - 60, flash_end=NOW + 3600), now=NOW)
    check("秒杀进行中 → flash_phase=active", st.get("flash_phase") == "active", str(st.get("flash_phase")))
    check("进行中 is_flash=True 且 price 用秒杀价", st.get("is_flash") is True and st.get("price") == 0.5)

    st2 = M.plan_sales_state(_plan(mode="flash_sale", flash_price=0.5,
                                   flash_start=NOW + 3600, flash_end=NOW + 7200), now=NOW)
    check("秒杀未开始 → flash_phase=upcoming", st2.get("flash_phase") == "upcoming", str(st2.get("flash_phase")))
    check("未开始不算秒杀中（不得提前给秒杀价）", st2.get("is_flash") is False and st2.get("price") == 0.99)

    st3 = M.plan_sales_state(_plan(mode="flash_sale", flash_price=0.5,
                                   flash_start=NOW - 7200, flash_end=NOW - 60), now=NOW)
    check("秒杀已结束 → flash_phase=ended", st3.get("flash_phase") == "ended", str(st3.get("flash_phase")))
    check("已结束价格回原价", st3.get("is_flash") is False and st3.get("price") == 0.99)
    check("已结束仍可原价购买（秒杀只改价格不改能不能买）", st3.get("buyable") is True)

    st4 = M.plan_sales_state(_plan(flash_price=0.5, flash_start=NOW - 60, flash_end=NOW + 3600), now=NOW)
    check("mode 不是 flash_sale 但填了秒杀时间 → none", st4.get("flash_phase") == "none", str(st4.get("flash_phase")))

    st5 = M.plan_sales_state(_plan(mode="flash_sale", flash_start=NOW - 60, flash_end=NOW + 3600), now=NOW)
    check("秒杀模式但没填秒杀价 → none（缺必要条件不算秒杀）", st5.get("flash_phase") == "none", str(st5.get("flash_phase")))

    st6 = M.plan_sales_state(_plan(mode="flash_sale", flash_price=0.5, flash_end=NOW + 3600), now=NOW)
    check("只填秒杀结束不填开始 → none", st6.get("flash_phase") == "none", str(st6.get("flash_phase")))
    check("flash_phase 字段一定存在（前端据此渲染）",
          all(("flash_phase" in s) for s in (st, st2, st3, st4, st5, st6)))


def _read_app_js() -> str:
    return (ROOT / "web" / "app.js").read_text(encoding="utf-8")


def test_frontend_wiring() -> None:
    print("\n[B] 前端两处卡片渲染都按三态收放秒杀元素")
    src = _read_app_js()
    check("定义 pfFlashPhase", "const pfFlashPhase" in src)
    check("定义 pfWatchFlashBoundary（跨临界点自动重渲染）", "const pfWatchFlashBoundary" in src)

    # 两处卡片渲染函数：个人中心 / 会员页
    check("两处渲染都取了 phase", src.count("const phase = pfFlashPhase(st)") == 2,
          f"实际 {src.count('const phase = pfFlashPhase(st)')} 处")

    # ended 一律收起伏秒杀角标
    n_badge = len(re.findall(r"const mkBadge = \(st\.badge && phase !== 'ended'\)", src))
    check("两处都改成「phase !== 'ended' 才显示角标」", n_badge == 2, f"实际 {n_badge} 处")
    check("不再有「有 badge 就无脑显示」的旧写法",
          "const mkBadge = st.badge ?" not in src)

    # 划线原价 / 秒杀价高亮只在进行中
    n_flash = len(re.findall(r"const flash = phase === 'active'", src))
    check("划线原价仅在 phase==='active' 出现", n_flash == 2, f"实际 {n_flash} 处")
    n_hl = len(re.findall(r"phase === 'active' \? ' is-flash' : ''", src))
    check("秒杀价红色高亮仅在 active 出现", n_hl == 2, f"实际 {n_hl} 处")

    # 「秒杀至…」只在 active；upcoming 给「X 开抢」
    hits = [i for i in range(len(src)) if src.startswith("秒杀至", i)]
    guarded = [i for i in hits if "phase === 'active'" in src[max(0, i - 120):i]]
    check("「秒杀至…」共两处且都包在 phase==='active' 判据里",
          len(hits) == 2 and len(guarded) == 2, f"共 {len(hits)} 处、受保护 {len(guarded)} 处")
    check("不再有旧判据 mode==='flash_sale' 就显示秒杀至",
          "if (st.mode === 'flash_sale' && st.flash_end)" not in src)
    n_open = len(re.findall(r"bits\.push\(`\$\{[^`]*\} 开抢`\)", src))
    check("upcoming 给出「X 开抢」（两处）", n_open == 2, f"实际 {n_open} 处")

    # 兜底推导：老后端没 flash_phase 时靠 mode/flash_start/flash_end
    check("兜底推导要求 mode==='flash_sale'", "s.mode !== 'flash_sale'" in src)
    check("兜底推导要求秒杀价 > 0", "Number(s.flash_price) > 0" in src)


def test_node_behavior() -> None:
    print("\n[C] pfFlashPhase 真实行为（node 实跑，模拟老后端不下发 flash_phase）")
    src = _read_app_js()
    m = re.search(r"  const pfFlashPhase = \(st\) => \{.*?\n  \};\n", src, re.S)
    if not m:
        check("抽取 pfFlashPhase 成功", False)
        return
    body = m.group(0)
    node = os.environ.get("VDL_NODE_BIN") or "node"
    harness = (
        "const Date0 = Date.now;\n"
        "const NOW = 1800000000000;\n"
        "Date.now = () => NOW;\n"
        + body
        + """
const fs = 1800000000, fe = 1800003600;
const cases = {
  active:   { mode: 'flash_sale', flash_price: 0.5, flash_start: fs - 60, flash_end: fe },
  upcoming: { mode: 'flash_sale', flash_price: 0.5, flash_start: fs + 3600, flash_end: fs + 7200 },
  ended:    { mode: 'flash_sale', flash_price: 0.5, flash_start: fs - 7200, flash_end: fs - 60 },
  wrongMode:{ mode: 'normal',     flash_price: 0.5, flash_start: fs - 60, flash_end: fe },
  noPrice:  { mode: 'flash_sale', flash_price: 0,   flash_start: fs - 60, flash_end: fe },
  given:    { mode: 'normal', flash_phase: 'ended' },
  empty:    {},
};
const out = {};
for (const [k, v] of Object.entries(cases)) out[k] = pfFlashPhase(v);
console.log(JSON.stringify(out));
"""
    )
    try:
        r = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=25)
    except FileNotFoundError:
        check("node 可用", False, "未找到 node")
        return
    if r.returncode != 0:
        check("node 跑通 pfFlashPhase", False, r.stderr.strip()[:200])
        return
    got = json.loads(r.stdout.strip())
    check("进行中 → active", got["active"] == "active", str(got))
    check("未开始 → upcoming", got["upcoming"] == "upcoming", str(got))
    check("已结束 → ended（老后端也能判断出来）", got["ended"] == "ended", str(got))
    check("非秒杀模式 → none", got["wrongMode"] == "none", str(got))
    check("没填秒杀价 → none", got["noPrice"] == "none", str(got))
    check("后端给了 flash_phase 就优先信后端", got["given"] == "ended", str(got))
    check("空 state 不炸（返回 none）", got["empty"] == "none", str(got))


def test_boundary_watch_wired() -> None:
    print("\n[D] 跨过临界点自动重渲染")
    src = _read_app_js()
    check("个人中心渲染末尾挂了监听", "pfWatchFlashBoundary(plans)" in src)
    check("会员页渲染挂了监听且三轨合一", "pfWatchFlashBoundary({" in src)
    check("会员页合并了 credit_packs（无 .plans 嵌套）", "_memPlans.credit_packs" in src)
    check("监听会清掉上一个定时器（不叠加）", "clearTimeout(_pfFlashTimer)" in src)
    check("到点后同时刷新会员页与个人中心",
          "pfRenderPlans();" in src.split("const pfWatchFlashBoundary")[1][:900]
          and "memRender();" in src.split("const pfWatchFlashBoundary")[1][:900])


if __name__ == "__main__":
    test_backend_phase()
    test_frontend_wiring()
    test_node_behavior()
    test_boundary_watch_wired()
    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("秒杀窗口守卫全部通过")
