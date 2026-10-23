"""server/tests/test_plan_flash_price_guard.py —— 后台秒杀配置守卫（2026-10-17 立，10-20 扩）

线上事故 A（2026-10-17）：VIP会员·1天档在后台把「价格¥」与「秒杀价¥」都填成 0.1，导致
  - 秒杀窗口内没有价差 → 卡片不划原价、没有「秒杀」观感，用户以为没生效；
  - 窗口结束后价格回原价（而原价就是秒杀价）→ 看起来「秒杀永远不结束」。

线上事故 B（2026-10-20）：活动结束后**没有落回普通·无活动模式**——
  - `mode` 字段永远停在 flash_sale（价格虽回原价，后台一直挂着旧秒杀配置）；
  - 更隐蔽：秒杀价被清空时 `flash_phase` 变 `none`（不是 `ended`），角标压根不收起；
  - 已切回普通模式的档位留着 `flash_price/flash_start/flash_end` + 「限时秒杀」角标。

本守卫钉住两条：
  [A] 静态接线：savePlans 提交前有「归一化块 + 校验块」、判定条件、拦截文案、释放闸门；
  [B] node 实跑：把两块抽出来喂真实数据，验证
      · 低于原价放行 / 等于或高于原价拦截 / 普通·限量模式不拦
      · 秒杀价为空、缺时间、开始≥结束 → 拦（旧版对「秒杀价为空」直接放行）
      · 秒杀窗口已过 → **自动切回普通模式**（mode/flash 三字段/活动角标全清）
      · 非秒杀模式残留秒杀字段 → 自动清零（不误伤正常档）
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
    print("\n[A] savePlans 提交前：归一化 + 校验 接线")
    src = APP_JS.read_text(encoding="utf-8")
    check("存在归一化数组 _flashFixed", "const _flashFixed = [];" in src)
    check("存在校验数组 _flashBad", "const _flashBad = [];" in src)
    i_norm = src.find("const _flashFixed = [];")
    i_bad = src.find("const _flashBad = [];", max(i_norm, 0))
    # 只认 savePlans 里那一次提交：app.js 别处也有 /api/admin/config/plans 的调用（读配置）
    i_submit = src.find("await adminRequest('/api/admin/config/plans'", max(i_bad, 0))
    check("归一化在提交之前、且在校验之前",
          0 < i_norm < i_bad < i_submit, f"norm@{i_norm} bad@{i_bad} submit@{i_submit}")

    check("绝不对「参数没填完」的活动做归一化（避免管理员输入被静默清空）",
          "if (fp0 > 0 && fs0 && fe0 && fe0 <= _nowSecSave) {" in src)
    check("已结束的活动 → mode 切 normal + 清角标",
          "it.mode = 'normal';" in src
          and "_flashFixed.push(catName + '档 ' + k + ' 秒杀已结束，已切回普通模式')" in src)
    check("普通/限量/活动模式残留秒杀字段 → 清零",
          "_flashFixed.push(catName + '档 ' + k + ' 已清掉残留的秒杀参数')" in src)
    check("切回普通时顺手清掉活动角标（秒杀/限时/抢购/特价）",
          "if (it.badge && /秒杀|限时|抢购|特价/.test(String(it.badge))) it.badge = '';" in src)
    # 后台必须让管理员「看得见」活动已结束，否则他只看到秒杀模式还勾着，会以为没回落
    check("后台档位行给出「秒杀已结束」可见角标",
          "const flashOverChip = _flashOver" in src
          and "秒杀已结束" in src
          and "${statusChip}${flashOverChip}" in src)

    check("校验只认秒杀模式档位（普通/限量/活动不误伤）",
          "if (!it || it.mode !== 'flash_sale') return;" in src)
    check("秒杀价为空也要拦（旧版在这里直接 return，等于放行）",
          "选了秒杀模式但秒杀价为空" in src and "if (!(fp > 0)) {" in src)
    check("判定「秒杀价不低于原价」", "if (fp >= base) {" in src)
    check("拦截文案同时点名「秒杀价」与「原价」",
          "秒杀价 ¥' + it.flash_price + ' 不低于原价 ¥' + base" in src)
    check("缺开始/结束时间也被拦", "缺少秒杀开始/结束时间" in src)
    check("结束时间已过也被拦（兜底：归一化漏网时仍不许保存）", "秒杀结束时间已过" in src)
    check("开始≥结束被拦（零长度窗口）", "秒杀结束时间必须晚于开始时间" in src)
    # 拦截分支必须自己释放闸门：不释放的话第一次被拦后 _plansSaving 永远为 true，
    # 之后点保存只会打印「重复点击已忽略」（2026-10-17 同文件已踩过一次）
    i_gate = src.find("if (_flashBad.length) {", max(i_bad, 0))
    i_ret = src.find("return;", i_gate)
    i_release = src.find("_plansSaving = false;", max(i_bad, 0))
    check("拦截分支内释放 _plansSaving", 0 < i_release < i_ret, f"release@{i_release} ret@{i_ret}")
    # 三张表都要查：只查下载表会漏掉 AI/积分包档
    check("三张表都参与归一化与校验",
          src.count("[['下载', dl], ['AI', ai], ['积分包', cp]]") >= 2)


def _extract_block(src: str) -> str:
    start = src.find("const _flashFixed = [];")
    end = src.find("if (_flashBad.length) {", start)
    if start < 0 or end < 0:
        return ""
    return src[start:end]


def test_behavior_node() -> None:
    print("\n[B] node 实跑：归一化 + 校验 真实判定")
    node = next((p for p in NODE_CANDIDATES if p and pathlib.Path(p).exists()), "")
    src = APP_JS.read_text(encoding="utf-8")
    block = _extract_block(src)
    if not node or not block:
        check("能抽取归一化+校验块并找到 node", False, "node 缺失或抽取失败")
        return
    # 固定 now 保证确定性：2026-10-16 18:00
    now_sec = 1792144800
    in_win = {"flash_start": 1792123200, "flash_end": 1792209600}
    past_win = {"flash_start": 1792000000, "flash_end": 1792100000}

    def _cleared(d: dict) -> bool:
        return (float(d.get("flash_price") or 0) == 0
                and float(d.get("flash_start") or 0) == 0
                and float(d.get("flash_end") or 0) == 0)

    cases = [
        ("秒杀价低于原价 → 放行",
         {"download_1day": {"mode": "flash_sale", "price_cny": 1.9, "flash_price": 0.5, **in_win}},
         [], lambda s: s["download_1day"]["mode"] == "flash_sale"),
        ("秒杀价等于原价 → 拦截",
         {"download_1day": {"mode": "flash_sale", "price_cny": 0.1, "flash_price": 0.1, **in_win}},
         ["不低于原价"], None),
        ("秒杀价高于原价 → 拦截",
         {"download_1day": {"mode": "flash_sale", "price_cny": 0.1, "flash_price": 0.9, **in_win}},
         ["不低于原价"], None),
        ("秒杀价为空（截图形态）→ 拦截",
         {"download_3day": {"mode": "flash_sale", "price_cny": 0.99, "flash_price": 0,
                            "flash_start": 1792141200, "flash_end": 1792141200}},
         ["秒杀价为空"], None),
        ("开始时间=结束时间 → 拦截",
         {"download_3day": {"mode": "flash_sale", "price_cny": 0.99, "flash_price": 0.5,
                            "flash_start": 1792209600, "flash_end": 1792209600}},
         ["晚于开始时间"], None),
        ("普通模式带 flash_price → 不拦且字段清零",
         {"download_7day": {"mode": "normal", "price_cny": 3.99, "flash_price": 0.1,
                            "flash_start": 1792000000, "flash_end": 1792100000}},
         [], lambda s: s["download_7day"]["mode"] == "normal" and _cleared(s["download_7day"])),
        ("限量模式带 flash_price → 不拦且字段清零",
         {"credits_5000": {"mode": "limited", "price_cny": 50.0, "flash_price": 10.0,
                           "stock": 5, "flash_start": 0, "flash_end": 0}},
         [], lambda s: s["credits_5000"]["mode"] == "limited" and _cleared(s["credits_5000"])),
        ("秒杀模式缺时间 → 拦截",
         {"download_1day": {"mode": "flash_sale", "price_cny": 1.9, "flash_price": 0.5,
                            "flash_start": 0, "flash_end": 0}},
         ["缺少秒杀开始/结束时间"], None),
        ("秒杀窗口已过 → 自动落回普通模式（不拦）",
         {"download_1day": {"mode": "flash_sale", "price_cny": 1.9, "flash_price": 0.5,
                            "badge": "限时秒杀", **past_win}},
         [], lambda s: (s["download_1day"]["mode"] == "normal"
                        and _cleared(s["download_1day"])
                        and s["download_1day"]["badge"] == "")),
        ("秒杀已结束但角标是自定义文案 → 也清空（避免残留误导）",
         {"download_7day": {"mode": "flash_sale", "price_cny": 2.99, "flash_price": 1.99,
                            "badge": "限时特价", **past_win}},
         [], lambda s: s["download_7day"]["badge"] == ""),
        ("原价为 0 → 拦截",
         {"download_1day": {"mode": "flash_sale", "price_cny": 0, "flash_price": 0.5, **in_win}},
         ["原价为 0"], None),
    ]
    for name, group, expect_hits, state_assert in cases:
        js = f"""
const Date_now = {now_sec * 1000};
Date.now = () => Date_now;
const dl = {group_json(group)};
const ai = {{}};
const cp = {{}};
{block}
console.log('RESULT:' + _flashBad.length);
console.log('DETAIL:' + _flashBad.join(' | '));
console.log('STATE:' + JSON.stringify(Object.assign({{}}, dl, ai, cp)));
"""
        proc = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
        out = (proc.stdout or "") + (proc.stderr or "")
        lines = [ln for ln in out.splitlines() if ln.startswith("RESULT:")]
        count = int(lines[0].split(":", 1)[1]) if lines else -1
        detail = next((ln[7:] for ln in out.splitlines() if ln.startswith("DETAIL:")), "")
        state_raw = next((ln[6:] for ln in out.splitlines() if ln.startswith("STATE:")), "{}")
        if not expect_hits:
            ok_state = True
            if state_assert is not None:
                try:
                    ok_state = bool(state_assert(json_loads(state_raw)))
                except Exception as e:  # noqa: BLE001
                    ok_state = False
                    detail += f" | STATE 解析失败: {e}"
            check(f"{name}（_flashBad 应为空，实际 {count} 条）", count == 0 and ok_state,
                  (detail or out)[:200])
        else:
            hit_ok = count > 0 and all(any(h in d for d in detail.split(" | ")) for h in expect_hits)
            check(f"{name}（实际 {count} 条）", hit_ok, (detail or out)[:200])


def json_loads(s: str):
    import json
    return json.loads(s or "{}")


def group_json(group: dict) -> str:
    import json
    return json.dumps(group, ensure_ascii=False)


def main() -> int:
    test_wiring()
    test_behavior_node()
    print("\n" + "=" * 64)
    if FAILS:
        print(f"❌ 秒杀配置守卫失败 {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("✅ 秒杀配置守卫（价差 + 结束回落 + 残渣清零）全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
