"""server/tests/test_plan_flash_price_guard.py —— 后台「秒杀价必须低于原价」守卫（2026-10-17）

线上事故：VIP会员·1天档在后台把「价格¥」与「秒杀价¥」都填成 0.1，导致
  - 秒杀窗口内没有价差 → 卡片不划原价、没有「秒杀」观感，用户以为没生效；
  - 窗口结束后价格回原价（而原价就是秒杀价）→ 看起来「秒杀永远不结束」。

前台倒计时/角标本身是对的（见 test_member_countdown.py），根因是**配置**，
所以在保存前拦一道，并用本守卫钉住：

  [A] 静态接线：savePlans 提交前有 _flashBad 校验、判定条件、拦截文案、释放闸门；
  [B] node 实跑：把校验块抽出来喂真实数据，验证「低于原价放行 / 等于或高于原价
      拦截 / 普通模式不拦 / 缺时间拦 / 结束时间已过拦」。
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

FAILS: list[str] = []

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP_JS = ROOT / "web" / "app.js"

NODE_CANDIDATES = (
    "/Users/suixindelang/.workbuddy/binaries/node/versions/22.22.2-3/bin/node",
    shutil.which("node") or "",
)


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + ((" — " + extra) if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def test_wiring() -> None:
    print("\n[A] savePlans 提交前校验接线")
    src = APP_JS.read_text(encoding="utf-8")
    check("存在 _flashBad 校验数组", "const _flashBad = [];" in src)
    check("只查秒杀模式档位（普通/限量/活动不误伤）",
          "it.mode !== 'flash_sale' || !(Number(it.flash_price) > 0)" in src)
    check("判定「秒杀价不低于原价」", "Number(it.flash_price) >= base" in src)
    check("拦截文案同时点名「秒杀价」与「原价」",
          "秒杀价 ¥' + it.flash_price + ' 不低于原价 ¥' + base" in src)
    check("缺开始/结束时间也被拦", "缺少秒杀开始/结束时间" in src)
    check("结束时间已过也被拦", "秒杀结束时间已过" in src)
    i_bad = src.find("const _flashBad = [];")
    # 只认 savePlans 里那一次提交：app.js 别处也有 /api/admin/config/plans 的调用（读配置）
    i_submit = src.find("await adminRequest('/api/admin/config/plans'", i_bad)
    check("校验发生在提交之前", 0 < i_bad < i_submit, f"bad@{i_bad} submit@{i_submit}")
    # 拦截分支必须自己释放闸门：不释放的话第一次被拦后 _plansSaving 永远为 true，
    # 之后点保存只会打印「重复点击已忽略」（2026-10-17 同文件已踩过一次）
    i_gate = src.find("if (_flashBad.length) {", i_bad)
    i_ret = src.find("return;", i_gate)
    i_release = src.find("_plansSaving = false;", i_bad)
    check("拦截分支内释放 _plansSaving", 0 < i_release < i_ret, f"release@{i_release} ret@{i_ret}")
    # 三张表都要查：只查下载表会漏掉 AI/积分包档
    check("三张表都参与校验", "[['下载', dl], ['AI', ai], ['积分包', cp]]" in src)


def _extract_block(src: str) -> str:
    start = src.find("const _flashBad = [];")
    end = src.find("if (_flashBad.length) {", start)
    if start < 0 or end < 0:
        return ""
    return src[start:end]


def test_behavior_node() -> None:
    print("\n[B] node 实跑：校验块真实判定")
    node = next((p for p in NODE_CANDIDATES if p and pathlib.Path(p).exists()), "")
    src = APP_JS.read_text(encoding="utf-8")
    block = _extract_block(src)
    if not node or not block:
        check("能抽取校验块并找到 node", False, "node 缺失或抽取失败")
        return
    # 用固定 now 保证确定性：flash_end 已过 → 2026-10-17 12:00（秒杀已结束）
    now_sec = 1792144800  # 2026-10-16 18:00，落在秒杀窗口内
    cases = [
        ("秒杀价低于原价 → 放行", {"download_1day": {"mode": "flash_sale", "price_cny": 1.9, "flash_price": 0.5, "flash_start": 1792123200, "flash_end": 1792209600}}, [], ),
        ("秒杀价等于原价 → 拦截", {"download_1day": {"mode": "flash_sale", "price_cny": 0.1, "flash_price": 0.1, "flash_start": 1792123200, "flash_end": 1792209600}}, ["不低于原价"]),
        ("秒杀价高于原价 → 拦截", {"download_1day": {"mode": "flash_sale", "price_cny": 0.1, "flash_price": 0.9, "flash_start": 1792123200, "flash_end": 1792209600}}, ["不低于原价"]),
        ("普通模式带 flash_price → 不拦", {"download_7day": {"mode": "normal", "price_cny": 3.99, "flash_price": 0.1, "flash_start": 0, "flash_end": 0}}, []),
        ("限量模式带 flash_price → 不拦", {"credits_5000": {"mode": "limited", "price_cny": 50.0, "flash_price": 10.0, "stock": 5, "flash_start": 0, "flash_end": 0}}, []),
        ("秒杀模式缺时间 → 拦", {"download_1day": {"mode": "flash_sale", "price_cny": 1.9, "flash_price": 0.5, "flash_start": 0, "flash_end": 0}}, ["缺少秒杀开始/结束时间"]),
        ("秒杀结束时间已过 → 拦", {"download_1day": {"mode": "flash_sale", "price_cny": 1.9, "flash_price": 0.5, "flash_start": 1792000000, "flash_end": 1792100000}}, ["结束时间已过"]),
        ("原价为 0 → 拦", {"download_1day": {"mode": "flash_sale", "price_cny": 0, "flash_price": 0.5, "flash_start": 1792123200, "flash_end": 1792209600}}, ["原价为 0"]),
    ]
    for idx, (name, group, expect_hits) in enumerate(cases):
        js = f"""
const Date_now = {now_sec * 1000};
Date.now = () => Date_now;
const dl = {group_json(group)};
const ai = {{}};
const cp = {{}};
{block}
console.log('RESULT:' + _flashBad.length);
console.log(_flashBad.join(' | '));
"""
        proc = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
        out = (proc.stdout or "") + (proc.stderr or "")
        lines = [ln for ln in out.splitlines() if ln.startswith("RESULT:")]
        count = int(lines[0].split(":", 1)[1]) if lines else -1
        detail = "\n".join(ln for ln in out.splitlines() if not ln.startswith("RESULT:"))
        if not expect_hits:
            check(f"{name}（_flashBad 应为空，实际 {count} 条）", count == 0, detail[:200])
        else:
            hit_ok = count > 0 and all(any(h in d for d in detail.split(" | ")) for h in expect_hits)
            check(f"{name}（实际 {count} 条）", hit_ok, detail[:200])


def group_json(group: dict) -> str:
    import json
    return json.dumps(group, ensure_ascii=False)


def main() -> int:
    test_wiring()
    test_behavior_node()
    print("\n" + "=" * 64)
    if FAILS:
        print(f"❌ 秒杀价守卫失败 {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("✅ 秒杀价必须低于原价守卫全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
