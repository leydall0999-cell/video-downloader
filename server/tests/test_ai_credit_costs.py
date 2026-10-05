"""AI 积分成本表守卫（2026-10-05）。

用户定档：**云端 + 本机重算力都计入积分**，且**每次消耗多少积分要能在后台配**。

本守卫钉两件事（缺一不可，都是踩过的坑）：

[A] **每个计费项都要有真实调用点** —— 防止把「不存在的功能」写进成本表
    （V1 时 `FEATURE_USAGE_DEFS` 抄 DataTool 抄来 6 行从未实现的功能，
     用户在个人中心看到 9 个功能实际只有 1 个能用）。

[B] **每个真实模型调用都要有计费项** —— 2026-10-05 盘点的核心发现：
    全仓 24 个模型调用点，**只有 1 处**（`routers/matting.py`）真在扣积分。
    解说（最贵的功能，6 个 LLM/VLM 调用点）完全免费；AI 去水印 LaMa
    连日配额都没有。漏接计费不会报错、不会告警，只是**静默送钱**。

跑法：`../.build_venv/bin/python tests/test_ai_credit_costs.py`
（需带 venv：app.py 顶层 import requests）
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # server/
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  → " + extra) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def test_a_every_cost_has_real_call_site() -> None:
    print("\n[A] 每个计费项都有真实调用点（不许挂空壳功能）")
    import membership as M
    for op, row in M.AI_CREDIT_COSTS.items():
        where = str(row.get("where") or "")
        check(f"{op} 标注了调用点", bool(where), "where 为空 ⇒ 无法核对是否真存在")
        check(f"{op} 有中文功能名", bool(str(row.get("name") or "").strip()))
        check(f"{op} 有计价说明", bool(str(row.get("note") or "").strip()),
              "没有说明 ⇒ 日后没人知道这个价对应什么")
        # 计价语义：0 可以（显式免费），但负数一定是错的
        check(f"{op} 单价非负", int(row.get("cost", 0)) >= 0)


def test_b_every_model_call_has_cost_entry() -> None:
    print("\n[B] 每个真实模型调用都有计费项（漏接 = 静默送钱）")
    import membership as M
    ops = set(M.AI_CREDIT_COSTS)

    # B1 已知的高价值调用点 → 必须能映射到某个计费 op
    #     （格式：文件片段 → 期望覆盖它的 op 之一）
    # 🔴 判据落点说明：解说的扣费实现在 `routers/quota.py::precheck_or_raise`（统一收口），
    #   op 名也只出现在那里；`commentary.py` 侧只调 `precheck_or_raise(...)`。
    #   所以对 commentary.py 要断言的是「调了收口闸门」，而 op 名断言放在 quota.py。
    must_cover = [
        # 解说：整个链路 6 个 LLM/VLM 调用点，此前**完全无门禁**。
        # 断言「调了统一收口闸门」——这才是扣费是否生效的关键。
        ("server/routers/commentary.py", {"precheck_or_raise"}, False),
        # 收口点本身：必须出现全部解说相关 op（这里是**字符串**比较）
        ("server/routers/quota.py", {"commentary_llm", "commentary_local_mlx"}, True),
        # 抠图：显式云端 + 本地 + analyze 的 VLM
        ("server/routers/matting.py", {"matting_cloud", "local_matting_ai", "matting_vision"}, True),
        # 字幕：提取（本机 ASR）+ 翻译（云端 LLM）
        ("server/routers/subtitle.py", {"subtitle_asr"}, True),
        ("server/routers/subtitles.py", {"subtitle_translate"}, True),
        # 去水印 LaMa / 扩散模型
        ("server/routers/dewatermark.py", {"dewatermark_ai"}, True),
    ]
    for rel, expected, as_string in must_cover:
        f = REPO / rel
        check(f"{rel} 存在", f.is_file())
        if not f.is_file():
            continue
        src = f.read_text(encoding="utf-8")
        # 剥整行注释后再找，避免我写的说明文字成为证据
        code = "\n".join(l for l in src.split("\n") if not re.match(r"^\s*#", l))
        for op in expected:
            # as_string=True → op 必须是字符串字面量（`"matting_cloud"`）；
            # as_string=False → 只要代码里出现该标识符即可（函数是裸调用）。
            found = (re.search(rf"""["']{re.escape(op)}["']""", code) is not None
                     if as_string else (op in code))
            check(f"{rel} {'引用' if as_string else '调用'}了 {op}", found,
                  "该文件里找不到 ⇒ 这条计费路径可能没接上")

    # B2 关键：matting 的「自动升级」必须有记账回调（财务漏洞的修复点）

    # B2-0 解说扣费必须在收口闸门里**真的调用**（只定义函数不算接上）
    # 🔴 判据必须落在 precheck_or_raise 的**函数体**上，不能用「文件里出现过这个名字」——
    #   变异测试实测：把调用删掉后，`def charge_commentary_credits(` 的**定义处**
    #   仍在 2000 字符窗口内，宽正则照样匹配 ⇒ 守卫假绿了一次。
    quota_src = (REPO / "server" / "routers" / "quota.py").read_text(encoding="utf-8")
    check("quota.py 定义了 charge_commentary_credits",
          "def charge_commentary_credits" in quota_src)
    _m = re.search(r"def precheck_or_raise\(.*?(?=\ndef |\Z)", quota_src, re.S)
    check("找到 precheck_or_raise 函数体（用于精确定位）", _m is not None)
    _body = _m.group(0) if _m else ""
    check("precheck_or_raise **函数体内**真的调用了 charge_commentary_credits（解说扣费生效点）",
          "charge_commentary_credits(" in _body,
          "函数定义了但没被调用 ⇒ 解说仍然免费（变异 A 实测过这个坑）")
    check("扣费不足时抛 402 MEMBER_QUOTA",
          "MEMBER_QUOTA|" in quota_src and "status_code=402" in quota_src)

    mat = (REPO / "server" / "routers" / "matting.py").read_text(encoding="utf-8")
    mat_ai = (REPO / "server" / "matting_ai.py").read_text(encoding="utf-8")
    check("matting.py 造了云端升级记账钩子", "_make_cloud_charge_hook" in mat)
    check("钩子挂进 job（供后台线程回调）", "on_cloud_charge" in mat)
    # 🔴 同样要精确到「自动升级那一段」：变异 B 把 `_cb = (meta or {}).get("on_cloud_charge")`
    #   改成 `_cb = None`，字符串 `on_cloud_charge` 仍在文件里 ⇒ 宽检查假绿。
    _mi = re.search(r"_force_cloud\s*=\s*bool\(force_cloud\).*?(?=\n        _cloud_models)", mat_ai, re.S)
    check("找到 matting_ai 的自动升级判定段", _mi is not None)
    _mibody = _mi.group(0) if _mi else ""
    check("升级段内**真的**取出了回调并调用（不是 `_cb = None`）",
          'meta or {}).get("on_cloud_charge")' in _mibody and "_cb(" in _mibody,
          "本地预检失败自动升级到火山时不扣分 = 白嫖云端算力（变异 B 实测过）")

    # B3 去水印 auto 难例回落也要记账
    dw = (REPO / "server" / "routers" / "dewatermark.py").read_text(encoding="utf-8")
    check("dewatermark.py 造了回落记账钩子", "_make_dw_charge_hook" in dw)
    # 🔴 同样精确到回落分支内（变异 E 把 `_cb = job.get("on_ai_charge")` 改成 None，
    #   字符串 on_ai_charge 仍在文件里 ⇒ 宽检查会假绿）。
    _di = re.search(r"_auto_should_fallback_to_ai\(.*?(?=\n\s*try:)", dw, re.S)
    check("找到去水印回落判定段", _di is not None)
    _dibody = _di.group(0) if _di else ""
    check("回落分支内**真的**取出了回调并调用（不是 `_cb = None`）",
          'job.get("on_ai_charge")' in _dibody and "_cb(" in _dibody,
          "engine=auto 难例回落 LaMa 时不扣分（变异 E 实测过）")


def test_c_credit_cost_never_silent_for_unknown_op() -> None:
    print("\n[C] 未登记的 op 要告警，不能静默免费（财务漏洞的温床）")
    import membership as M
    check("ALLOWED_CREDIT_OPS 与表一致",
          set(M.ALLOWED_CREDIT_OPS) == set(M.AI_CREDIT_COSTS))
    # 未知 op 仍返回 0（不能炸），但必须进 _UNLOGGED_OPS（=有告警记录）
    before = len(M._UNLOGGED_OPS)
    v = M.credit_cost("definitely_not_registered")
    check("未知 op 返回 0（不阻断主流程）", v == 0)
    check("未知 op 被记录进告警集合", len(M._UNLOGGED_OPS) > before,
          "静默返回 0 ⇒ 新功能忘记配价时无人发现")
    # 已知 op 不应进告警集合
    n = len(M._UNLOGGED_OPS)
    M.credit_cost("matting_cloud")
    check("已登记 op 不产生告警", len(M._UNLOGGED_OPS) == n)


def test_d_override_layer_works() -> None:
    print("\n[D] 后台改价 → 覆盖层生效；恢复默认 → 落回代码值")
    import membership as M
    ov_path = Path(os.environ.get("VDL_HOME", str(Path.home() / ".video-downloader"))) / "plans.json"
    old = ov_path.read_text(encoding="utf-8") if ov_path.is_file() else None
    try:
        base = M.credit_cost("matting_cloud")
        M.save_plan_overrides({"credit_costs": {"matting_cloud": base + 7}})
        M._PLAN_OVERRIDE_CACHE = None
        check("覆盖层价高于默认", M.credit_cost("matting_cloud") == base + 7,
              f"实际 {M.credit_cost('matting_cloud')} ≠ {base + 7}")
        # 恢复默认：条目值为 None 才是删除（表内逐条合并语义）
        M.save_plan_overrides({"credit_costs": {"matting_cloud": None}})
        M._PLAN_OVERRIDE_CACHE = None
        check("None 删除后落回代码默认值", M.credit_cost("matting_cloud") == base,
              f"实际 {M.credit_cost('matting_cloud')} ≠ {base}")
    finally:
        if old is None:
            if ov_path.is_file():
                ov_path.unlink()
        else:
            ov_path.write_text(old, encoding="utf-8")
        M._PLAN_OVERRIDE_CACHE = None


def test_e_cost_table_for_admin() -> None:
    print("\n[E] 后台成本表返回完整且不含凭据")
    import json

    import membership as M
    tbl = M.credit_cost_table()
    check("表非空", len(tbl) >= 10, f"只有 {len(tbl)} 项")
    for r in tbl:
        for f in ("op", "name", "default", "effective", "overridden"):
            check(f"{r['op']} 含 {f}", f in r)
        check(f"{r['op']} 生效价非负", int(r["effective"]) >= 0)
    blob = json.dumps(tbl, ensure_ascii=False)
    # 🔴 判据必须避「token」一词 —— `real_cost` 字段里会写「3518+6474 token」这种
    #    计价依据（这恰恰是我们要的：改价前能看到真实用量），与「凭据泄漏」无关。
    #    真要防的是**凭据字段**（api_key / token 的值），不是所有含 token 的文本。
    for bad in ("api_key", "access_key", "secret_key", "Bearer", "sk-"):
        check(f"表里不含凭据字段 {bad}", bad not in blob)
    check("real_cost 计价依据已随表返回（改价时要看）",
          any("token" in (r.get("real_cost") or "") or "¥" in (r.get("real_cost") or "")
              for r in tbl),
          "缺 real_cost ⇒ 管理员改价时看不到平台真实成本")


if __name__ == "__main__":
    test_a_every_cost_has_real_call_site()
    test_b_every_model_call_has_cost_entry()
    test_c_credit_cost_never_silent_for_unknown_op()
    test_d_override_layer_works()
    test_e_cost_table_for_admin()
    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   -", f)
        raise SystemExit(1)
    print("🎉 AI 积分成本表守卫全部通过")
