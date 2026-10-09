#!/usr/bin/env python3
"""后台「每日对账」独立成 tab + 日期精确到分 的守卫（纯离线，只读源码）。

背景（2026-10-09 用户要求，附截图）：
    ① 「每日对账 · 入账 vs 充值发货」原先压在**运维监控**页最底部 —— 访客/错误事件/
       异常告警三块占了两屏，钱账要滚到底才看得到。用户要求**独立成同级 tab**。
    ② 对账明细的日期只到「天」（如「2026-10-09 给 xxx 自动发货…」），一条差异要查明
       是哪一分钟发的货、和发货日志/授权中心流水根本对不上。用户要求**精确到分**。

本守卫钉住（防「挪回去了 / 挪一半 / 精确到分改过头把日切也改了」）：
  A. 后台 tab 栏里真的多了一个顶层「每日对账」tab，且排在「运维监控」之后（同级）；
  B. 对账区块必须**整块**搬出监控页（监控页体内不得再残留对账容器），且容器 id 不能改名
     （改名会让 app.js 静默失灵：页面上直接空白，控制台也不报错）；
  C. 前端接线齐全：切换器路由 / 运维监控不再连带拉对账 / 30s 只在停留该页时刷 / 独立刷新按钮；
  D. 「日期精确到分」落在**授权中心**（license_server.py）：4 处对账明细全用 `_bj_dt`
     （YYYY-MM-DD HH:MM，北京时间），且**账期汇总键与告警去重键仍必须用 `_bj_day`** ——
     这两处一旦跟着「精确到分」，同一天的差异会被拆成多条告警、汇总表也会裂成多行。

运行：
    cd server && python tests/test_admin_recon_tab.py
"""
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INDEX_HTML = REPO / "web" / "index.html"
APP_JS = REPO / "web" / "app.js"
LICENSE_SERVER = REPO / "deploy" / "license_server.py"

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


def _grab_def(src: str, name: str) -> str:
    """抓出一个函数的源码块：`def name(...)` 起，到它第一处 `return` 行止。

    只用于 `_bj_day` / `_bj_dt` 这种三行小函数（def + docstring + return）。
    """
    i = src.index(f"def {name}(")
    j = src.index("\n    return ", i)
    j = src.index("\n", j + 1)
    return src[i:j]


def main():
    print("=" * 60)
    print("后台「每日对账」独立 tab + 日期精确到分 守卫")
    print("=" * 60)

    html = INDEX_HTML.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    ls = LICENSE_SERVER.read_text(encoding="utf-8")

    # ── A. tab 栏 ─────────────────────────────────────────────────────────
    print("\nA. 后台 tab 栏（独立成同级 tab）")
    nav_i = html.index('class="admin-tabs"')
    nav = html[nav_i: html.index("</nav>", nav_i)]
    check("tab 栏里有顶层「每日对账」",
          'data-admin-view="recon"' in nav and "每日对账" in nav)
    check("与「运维监控」同级且排在其后",
          nav.index('data-admin-view="monitor"') < nav.index('data-admin-view="recon"'),
          "顺序不对（用户要的是放在运维监控右侧）")

    # ── B. 区块整块搬出监控页 ─────────────────────────────────────────────
    print("\nB. 对账区块已搬出运维监控页")
    mon_i = html.index('id="adminViewMonitor"')
    mon_close = html.index("\n        </section>", mon_i)  # 监控页闭合（同级缩进）
    mon_body = html[mon_i:mon_close]
    check("监控页体内不再残留对账容器（整块搬走，不是复制一份）",
          "adminMonitorRecon" not in mon_body and "adminMonitorMismatches" not in mon_body)

    rec_i = html.index('id="adminViewRecon"')
    check("对账页在监控页之后（同级 admin-view）", rec_i > mon_close)
    rec_body = html[rec_i: html.index("\n        </section>", rec_i)]
    check("对账容器 id 未被改名（app.js 按 adminMonitorRecon 取）",
          'id="adminMonitorRecon"' in rec_body and 'id="adminMonitorMismatches"' in rec_body,
          "改名 ⇒ 页面静默空白")
    check("独立页自带刷新入口 + 核对时刻位",
          'id="adminReconRefresh"' in rec_body and 'id="adminReconChecked"' in rec_body)

    # 反向一致性：app.js 是**按 id 字符串**取容器的，只查 HTML 查不出「前端取错名字」
    # （2026-10-09 变异测试发现：把 app.js 里的 adminMonitorRecon 改名，守卫当时仍绿）
    refs = sorted(set(re.findall(r"\$\('(adminMonitor(?:Recon|Mismatches))'\)", js)))
    check("前后端对账容器 id 一致（app.js 取的 id 必须真的在页面上）",
          refs == ["adminMonitorMismatches", "adminMonitorRecon"]
          and all(f'id="{i}"' in rec_body for i in refs),
          f"app.js 实际取：{refs}")

    # ── C. 前端接线 ───────────────────────────────────────────────────────
    print("\nC. 前端接线（切换 / 刷新 / 自动刷新）")
    check("切换器把 recon 路由到对账加载",
          re.search(r"name === 'recon'\)\s*loadMonitorRecon\(\)", js) is not None)

    lm_i = js.index("const loadMonitor = () =>")
    lm_body = js[lm_i: js.index("\n    };", lm_i)]
    check("运维监控页不再连带拉对账（已解耦，避免白刷接口）",
          "loadMonitorRecon" not in lm_body, "仍连带拉")

    check("30s 自动刷新仅在停留在对账页时才拉",
          re.search(r"_adminViewReconEl[^\n]*!=\s*\n?[^\n]*hidden\)\s*loadMonitorRecon\(\)", js)
          is not None or re.search(r"_adminViewReconEl[^\n]*hidden[^\n]*loadMonitorRecon\(\)", js) is not None)
    check("独立「刷新」按钮已接线",
          re.search(r"\$\('adminReconRefresh'\)[^\n]*addEventListener\('click',\s*loadMonitorRecon\)", js)
          is not None)

    mm_i = js.index("const _monMinute =")
    mm_body = js[mm_i: js.index("\n    };", mm_i)]
    check("_monMinute 按北京时间精确到分",
          "Asia/Shanghai" in mm_body and "'minute'" in mm_body and "hour12: false" in mm_body,
          "缺时区或分位")
    check("核对时刻写进 adminReconChecked（空结果时清空）",
          js.count("adminReconChecked") >= 2)

    # ── D. 授权中心：日期精确到分 ─────────────────────────────────────────
    print("\nD. 授权中心：4 处对账明细精确到分、日切不受影响")
    dt_src = _grab_def(ls, "_bj_dt")
    check("_bj_dt 存在且格式为 YYYY-MM-DD HH:MM",
          "%Y-%m-%d %H:%M" in dt_src, dt_src.replace("\n", " ")[:80])

    ns = {"time": time}
    exec(_grab_def(ls, "_bj_day"), ns)  # noqa: S102 —— 只跑我们自己仓库里的两个纯函数
    exec(dt_src, ns)                    # noqa: S102
    check("_bj_day 语义未变（UTC+8 日切）", ns["_bj_day"](0) == "1970-01-01", ns["_bj_day"](0))
    check("_bj_dt 语义正确（UTC+8、到分、无秒）",
          ns["_bj_dt"](0) == "1970-01-01 08:00" and len(ns["_bj_dt"](1759999999)) == 16,
          ns["_bj_dt"](0))

    fi = ls.index("def recon_impl(")
    body = ls[fi: ls.index("\ndef ", fi + 1)]
    dets = [body[m.start(): m.start() + 320] for m in re.finditer(r'"detail":', body)]
    check("对账明细共 4 处（新增/漏改都能看出来）", len(dets) == 4, f"实际 {len(dets)} 处")
    # grant_no_pay（2026-10-09 归并改版）：明细改走 `{when}` —— 单笔 = _bj_dt（到分），
    # 多笔 = 到秒区间（分钟级无从区分同分钟的多笔）。所以判据不再是「4 处都字面含 _bj_dt」，
    # 而是「4 处都**不得**退回只到天」+ 另外 3 处直接到分 + when 的定义必须到分或更细。
    check("4 处明细无一退回「只到天」（_bj_day）",
          not any("_bj_day" in s for s in dets),
          str([i for i, s in enumerate(dets) if "_bj_day" in s]))
    check("其余 3 处明细直接带分钟（_bj_dt）",
          sum(1 for s in dets if "_bj_dt" in s) == 3,
          f"带 _bj_dt 的处数：{sum(1 for s in dets if '_bj_dt' in s)}")
    check("grant_no_pay 明细走 when，且 when = 单笔到分 / 多笔到秒",
          bool(re.search(r'when = _bj_dt\(it\["first"\]\)', body))
          and "%H:%M:%S" in body and "{when}" in body,
          "when 定义丢失或退回粗粒度")
    # ⚠️ 逐处校验而不是「数够 2 处就行」—— 2026-10-09 变异测试发现：只改其中一处
    # （把 _row(_bj_day(...)) 换成 _bj_dt）时，计数阈值式判据会漏判。
    row_calls = [body[m.end(): m.end() + 80] for m in re.finditer(r"_row\(", body)]
    row_calls = [c for c in row_calls if not c.startswith("d: str")]  # 跳过函数定义本身
    check("账期汇总每一处 entry 都按「天」切（_row(_bj_day(...))）",
          len(row_calls) >= 4 and all("_bj_day" in c for c in row_calls),
          f"未按天切：{[c[:36] for c in row_calls if '_bj_day' not in c]}")
    check("告警去重键仍按「天」切（改了会一天多条告警）",
          bool(re.search(r"key = f\".*?_bj_day\(m\['at'\]\)", body)))

    # ── E. 查账区间（2026-10-09 用户要求：1 天/3 天/7 天/1 个月/半年/1 年）────
    print("\nE. 查账区间：按钮 / 接线 / 三层上限同值")
    core = (REPO / "server" / "routers" / "core.py").read_text(encoding="utf-8")
    css = (REPO / "web" / "styles.css").read_text(encoding="utf-8")
    board = (REPO / "web" / "ops" / "board.html").read_text(encoding="utf-8")

    days_btns = re.findall(r'data-recon-days="(\d+)"', rec_body)
    check("对账页有 6 个区间按钮（1/3/7/30/180/365）",
          days_btns == ["1", "3", "7", "30", "180", "365"], f"实际 {days_btns}")
    check("默认高亮近 7 天（与授权中心 10 分钟自扫窗口同口径）",
          re.search(r'admin-recon-range is-active" data-recon-days="7"', rec_body) is not None)
    check("区间条 + 生效区间回显位都在页面上",
          "adminReconRangeBar" in rec_body and "adminReconRangeTip" in rec_body)
    check("区间条不在运维监控页里（两 tab 的范围语义不同，别串）",
          "adminReconRangeBar" not in mon_body)
    # 逐行找**规则行**（选择器行以 `{` 结尾）而不是全文 contains：深色主题那两行是
    # 一行式规则（以 `}` 结尾），若只判 contains，删掉浅色规则也会假绿。
    css_lines = css.splitlines()
    check("区间按钮样式到位：基础规则 + 选中高亮规则（缺 ⇒ 按钮难看/看不出选中）",
          any(".admin-btn.admin-recon-range" in ln and ln.rstrip().endswith("{")
              for ln in css_lines)
          and any(".admin-btn.admin-recon-range.is-active" in ln and ln.rstrip().endswith("{")
                  for ln in css_lines)
          and ".admin-recon-rangebar" in css,
          "样式缺失")

    check("所选区间真当 days 传给后端（不再写死 7）",
          re.search(r"JSON\.stringify\(\{ days: _reconDays \}\)", js) is not None
          and "days: 7" not in js[js.index("'/api/app/license-recon'"):][:240])
    check("六个区间的中文标签齐全",
          all(k in js for k in ("1: '近 1 天'", "3: '近 3 天'", "7: '近 7 天'",
                                "30: '近 1 个月'", "180: '近半年'", "365: '近 1 年'")))
    check("入账卡片标题随区间变化（不再写死「近 7 天」）",
          "${_reconRangeLabel(_reconDays)}" in js)
    check("空账目文案随区间变化", "_reconRangeLabel(_reconDays) + '暂无账目</div>'" in js)
    check("用服务端回传的 days 反查实际生效区间，被截断时显式告警",
          "used !== _reconDays" in js and "区间上限被链路截断" in js)
    click_i = js.index("_reconDays = Number(b.dataset.reconDays)")
    check("区间按钮接线：改状态 + 切高亮 + 立即重拉",
          re.search(r"\.admin-recon-range\[data-recon-days\]'\)\.forEach", js) is not None
          and "loadMonitorRecon();" in js[click_i: click_i + 240])

    # 🔴 上限在**三层**里各有一份（本机 core.py → ECS worker core.py → 授权中心）：任一处
    #    没放开，大区间都会被上游先截断，且**静默**（不报错、只给少）。
    m_ls = re.search(r"^RECON_DAYS_MAX = (\d+)", ls, re.M)
    m_core = re.search(r"^_RECON_DAYS_MAX = (\d+)", core, re.M)
    check("授权中心 / 本机 core.py 都有区间上限常量", bool(m_ls and m_core))
    check("两层上限同值且 ≥365（够「近 1 年」）",
          bool(m_ls and m_core) and m_ls.group(1) == m_core.group(1)
          and int(m_ls.group(1)) >= 365,
          f"授权中心={m_ls and m_ls.group(1)} 本机={m_core and m_core.group(1)}")
    check("授权中心按上限常量钳制（残留 min(int(days), 60) 即漏改）",
          "min(int(days), RECON_DAYS_MAX)" in ls and "min(int(days), 60)" not in ls)
    check("本机转发按上限常量钳制（残留 min(days, 60) 即漏改）",
          "min(days, _RECON_DAYS_MAX)" in core and "min(days, 60)" not in core)
    check("大区间只出报告、不改告警状态（notify 门在位）",
          "notify: bool = True" in ls and "notify=d <= RECON_DAYS" in ls
          and re.search(r"if notify:", ls) is not None)
    check("看板页同源支持区间（两侧口径不能不一致）",
          re.findall(r'data-recon-days="(\d+)"', board) == ["1", "3", "7", "30", "180", "365"]
          and "days:reconDays" in board)

    print("")
    print("=========================================")
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
