#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异确认：反破解风控「风险评分」守卫（test_risk_eval.py）真的咬得住吗。

只改**临时副本**（把 deploy/license_server.py 复制到临时目录再改），真实源码零改动；
用 VDL_RISK_SERVER 把守卫指向副本。历史教训（2026-10-12）：变异必须只在副本上做，
否则进程被杀会把缺陷代码留在真实源码里。

运行：`.build_venv/bin/python server/tests/mutation_risk_eval.py`
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
SRC = REPO / "deploy" / "license_server.py"
GUARD = REPO / "server" / "tests" / "test_risk_eval.py"


def _pick_py() -> str:
    for cand in (REPO / ".build_venv" / "bin" / "python",
                 pathlib.Path("/Users/suixindelang/.workbuddy/binaries/python/versions/3.13.12/bin/python3")):
        if pathlib.Path(cand).exists():
            return str(cand)
    return sys.executable


PY = _pick_py()

MUTATIONS: list[tuple[str, str, str]] = [
    ("M1 抬高 T1 阈值（限流档永不触发）",
     "RISK_T1 = 30", "RISK_T1 = 300"),

    ("M2 摘掉 device_churn 信号（换机不再计分）",
     "RISK_SIGNALS = (_sig_device_churn, _sig_fp_reuse, _sig_hb_anomaly,\n"
     "                _sig_spend_burst, _sig_redeem_brute, _sig_integrity)",
     "RISK_SIGNALS = (_sig_fp_reuse, _sig_hb_anomaly,\n"
     "                _sig_spend_burst, _sig_redeem_brute, _sig_integrity)"),

    ("M3 去掉 100 分上限（满贯不再截断）",
     '    score = min(100, sum(h["w"] for h in merged))',
     '    score = sum(h["w"] for h in merged)'),

    ("M4 豁免失效（测试设备也会被自动响应）",
     '    if isinstance(user.get("risk"), dict) and user["risk"].get("exempt"):\n'
     '        return True',
     '    if False:\n        return True'),

    ("M5 档位边界 >= 改 >（=T3 不再进 review）",
     '    if score >= RISK_T3:\n        return "review"',
     '    if score > RISK_T3:\n        return "review"'),

    ("M6 抬高心跳阈值（自动化脚本特征打不着）",
     "RISK_HB_BURST_N = 10", "RISK_HB_BURST_N = 100"),

    ("M7 引入 ban 档（自动永久封禁复活）",
     '    if score >= RISK_T3:\n        return "review"',
     '    if score >= RISK_T3:\n        return "ban"'),

    ("M8 关掉指纹索引容量淘汰（无界增长）",
     "    if len(idx) > FP_INDEX_CAP:", "    if False:"),

    ("M9 关掉事件窗口裁剪（旧事件永久计分）",
     '        if now - float(e.get("at", 0)) > within:\n            continue',
     '        if False:\n            continue'),

    ("M10 关掉 risk_top 的 min_score floor（0 分账号混入队列）",
     '        if ev["score"] < floor:\n            continue',
     '        if False:\n            continue'),
]

# 每项期望命中的断言关键字（出现在守卫红字里才算真咬住）
EXPECT_RED = {
    "M1": "30 分（=T1）→ throttle",
    "M2": "device_churn 命中",
    "M3": "满贯被截到 100",
    "M4": "risk.exempt → 恒 0",
    "M5": "80 分（=T3）→ review",
    "M6": "hb_anomaly 命中",
    "M7": "档位集合不含 ban",
    "M8": "淘汰最久未出现",
    "M9": "窗口外不命中",
    "M10": "floor=1 排除 0 分账号",
}


def _run_guard(server_path: pathlib.Path) -> tuple[bool, str]:
    env = dict(os.environ)
    env["VDL_RISK_SERVER"] = str(server_path)
    p = subprocess.run([PY, str(GUARD)], capture_output=True, text=True,
                       env=env, timeout=600)
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    before = hashlib.sha256(SRC.read_bytes()).hexdigest()
    print(f"真实源码 sha256（开工前）：{before[:16]}")

    ok, out = _run_guard(SRC)
    if not ok:
        print("❌ 基线失败：未变异的守卫就不全绿，本次结果作废")
        print("\n".join([l for l in out.splitlines() if "❌" in l][:8]))
        return 1
    print("✅ 基线：未变异时守卫全绿\n")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_risk_mut_"))
    all_ok = True
    try:
        for name, old, new in MUTATIONS:
            src = SRC.read_text(encoding="utf-8")
            if old not in src:
                print(f"⚠️  {name}: 锚点未命中，跳过（请人工核对）")
                all_ok = False
                continue
            mut = tmp / "license_mut.py"
            mut.write_text(src.replace(old, new, 1), encoding="utf-8")
            passed, out = _run_guard(mut)
            if passed:
                print(f"❌ {name}: 改坏后守卫仍全绿 —— 断言没咬住！")
                all_ok = False
            else:
                reds = [l.strip() for l in out.splitlines() if l.strip().startswith("❌")]
                expect = EXPECT_RED.get(name.split()[0], "")
                hit = any(expect and expect in r for r in reds) if expect else None
                tag = "（命中期望断言）" if hit else "（红了但非期望项，请核对）"
                print(f"✅ {name}: 如期变红 {tag} {reds[0][:66] if reds else '非零退出'}")
                if expect and not hit:
                    all_ok = False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    after = hashlib.sha256(SRC.read_bytes()).hexdigest()
    print(f"\n真实源码 sha256（收工后）：{after[:16]}")
    same = before == after
    print("✅ 真实源码零改动" if same else "❌ 真实源码被改动（严重）")
    print("\n" + ("🎉 全部变异如期变红" if (all_ok and same) else "❌ 存在未咬住/被污染项"))
    return 0 if (all_ok and same) else 1


if __name__ == "__main__":
    sys.exit(main())
