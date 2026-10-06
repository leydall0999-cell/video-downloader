"""后台「免费额度」分栏守卫（2026-10-06）。

钉住三件事：
  ① `effective_daily_limits()` 覆盖生效且未覆盖的键保留默认（写死常量 → 可配）
  ② `free_quota` 在 `_SAVE_TABLE_KEYS` 白名单里（否则整条替换，改一处抹一处）
  ③ `_has_cost_gate` 的登记与真实代码一致（不误报/不漏报财务漏洞）
     + 后台 4 个分栏都在（下载会员/AI会员/积分包/免费额度）

运行：python3 test_free_quota_admin.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SERVER = _HERE.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

# 🔴 必须在 import membership 之前设：数据目录隔离，绝不写真实家目录的 plans.json
_TMP = tempfile.mkdtemp(prefix="vdl_fq_")
os.environ["VDL_DATA_DIR"] = _TMP
os.environ.setdefault("VDL_PLANS_CLOUD", "0")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

fails = []


def check(cond, label, extra=""):
    if cond:
        print(f"  OK   {label}" + (f"  [{extra}]" if extra else ""))
    else:
        print(f"  FAIL {label}" + (f"  [{extra}]" if extra else ""))
        fails.append(label)


def test_effective_limits_default_unchanged():
    """没配覆盖时，生效值必须等于代码常量（默认值永不丢失）。"""
    import membership as M
    mem, free = M.effective_daily_limits()
    check(free == M.FREE_DAILY_LIMITS, "无覆盖时免费档 == 代码常量")
    check(mem == M.DAILY_QUOTA_LIMITS, "无覆盖时会员档 == 代码常量")
    # 关键：不能因为加了覆盖机制就把「字幕 2 次/日」这种既有配额弄丢
    for k in ("download", "matting", "cloud", "app_compute", "subtitle"):
        check(k in free, f"免费档保留 {k}")


def test_override_applies_and_merges():
    """覆盖生效，且未覆盖的键保留默认（字段级合并，不是整条替换）。"""
    import membership as M
    M.save_plan_overrides({"free_quota": {
        "daily_free_limits": {"download": 99, "matting": 1},
        "daily_member_limits": {"download": 2000},
    }})
    M._PLAN_OVERRIDE_CACHE = None
    mem, free = M.effective_daily_limits()
    check(free.get("download") == 99, "免费档 download 被覆盖 99", str(free.get("download")))
    check(free.get("matting") == 1, "免费档 matting 被覆盖 1")
    check(mem.get("download") == 2000, "会员档 download 被覆盖 2000")
    check(free.get("subtitle") == 2, "未覆盖的 subtitle 保留默认 2（字段级合并）")
    check(free.get("cloud") == 3, "未覆盖的 cloud 保留默认 3")


def test_free_quota_in_save_table_keys():
    """`free_quota` 必须在字段级合并白名单里。

    不在白名单 → 走「整条替换」→ 后台只改免费次数会把会员次数一起抹掉
    （与 2026-10-03「改销量清空套餐」是同一类事故）。
    """
    import membership as M
    check("free_quota" in M._SAVE_TABLE_KEYS, "free_quota 在 _SAVE_TABLE_KEYS 里")
    check("free_trial" in M._SAVE_TABLE_KEYS, "free_trial 仍在白名单（没被误删）")


def test_dirty_value_does_not_break_limits():
    """脏值（字符串/None/非数字）必须被忽略而不是让整张配额表炸掉。"""
    import membership as M
    M.save_plan_overrides({"free_quota": {"daily_free_limits": {
        "download": "abc", "matting": None, "cloud": 3.7, "": 5, "app_compute": 7,
    }}})
    M._PLAN_OVERRIDE_CACHE = None
    mem, free = M.effective_daily_limits()
    check(free.get("download") == 10, "脏值 'abc' 被忽略、回退默认 10")
    check(free.get("matting") == 8, "None 被忽略、回退默认 8")
    check(free.get("cloud") == 3, "3.7 被收敛为 3")
    check(free.get("app_compute") == 7, "合法值 7 正常生效")
    check("" not in free, "空 key 被丢弃")


def test_quota_state_uses_effective_limits():
    """quota_state 必须走生效值，否则后台改了配额实际拦不住。

    ⚠️ `save_plan_overrides` 对 `free_quota` 是**字段级合并**（白名单保证），
    所以这里传空的 `daily_member_limits: {}` 不会抹掉前一个用例设的
    `download: 2000` —— 那正是白名单要保护的行为。本用例只断言「免费档生效」。
    """
    import membership as M
    M.save_plan_overrides({"free_quota": {"daily_free_limits": {"download": 1}}})
    M._PLAN_OVERRIDE_CACHE = None
    mem, free = M.effective_daily_limits()
    check(free.get("download") == 1, "免费档 download 覆盖为 1", str(free.get("download")))
    store = M.MembershipStore(path=Path(_TMP) / "m.json", now_fn=lambda: 1000.0)
    st = store.quota_state("download")
    check(st["limit"] == 1, "quota_state 返回覆盖后的 limit=1", str(st["limit"]))
    check(st["free_limit"] == 1, "free_limit 也是覆盖值 1")
    # 会员档此刻是前面用例设的 2000（字段级合并保留），断言生效即可
    check(st["member_limit"] == mem.get("download"),
          "member_limit 反映生效值", f"{st['member_limit']} vs {mem.get('download')}")


def test_white_list_preserves_sibling_fields():
    """白名单的核心价值：只提交一个子表，不得抹掉另一个子表的既有值。"""
    import membership as M
    M.save_plan_overrides({"free_quota": {
        "daily_free_limits": {"matting": 3},
        "daily_member_limits": {"matting": 777},
    }})
    M._PLAN_OVERRIDE_CACHE = None
    # 再只提交 free_limits（不带 member_limits）
    M.save_plan_overrides({"free_quota": {"daily_free_limits": {"download": 2}}})
    M._PLAN_OVERRIDE_CACHE = None
    mem, free = M.effective_daily_limits()
    check(free.get("matting") == 3, "free_limits.matting 保留 3")
    check(free.get("download") == 2, "free_limits.download 更新为 2")
    check(mem.get("matting") == 777, "member_limits 未受影响（仍是 777）")


def test_has_cost_gate_matches_reality():
    """登记的「有扣费拦截点」必须与真实代码一致。

    误报成「未接入」会让管理员去改本来正确的代码；漏报则让财务漏洞继续存在。
    """
    sys.path.insert(0, str(_SERVER / "routers"))
    import admin as A
    # 已接入的（实测业务代码里真有扣费调用点）
    for op in ("matting_cloud", "matting_vision", "local_matting_ai",
               "commentary_llm", "commentary_local_mlx", "subtitle_asr",
               "subtitle_translate", "dewatermark_ai",
               # 2026-10-06 补上的两个真实拦截点
               "commentary_vision", "voice_clone"):
        check(A._has_cost_gate(op) is True, f"{op} 判为已接入扣费")
    # ⚠️ 画质增强是云端抠图的内部步骤，外层已收 50 积分，用户定档**不单独收**
    # （单独再收会对同一次操作重复收费）⇒ 故意不登记
    check(A._has_cost_gate("matting_cloud_enhance") is False,
          "matting_cloud_enhance 判为不单独收费（已含在云端抠图 50 里）")


def test_cloud_quota_limits_readable():
    """云端免费额度上限从 quota.py 常量读（单一真源，别在 admin 里写死 3/1）。"""
    import admin as A
    life, daily = A._cloud_quota_limits()
    # 直接读源码里的常量定义，避免 import 时把 servers 下的同名模块弄进 sys.modules
    import re
    qsrc = (_SERVER / "quota.py").read_text(encoding="utf-8")
    m_life = re.search(r"^LIFETIME_CLOUD_EVENTS\s*=\s*(\d+)", qsrc, re.M)
    m_daily = re.search(r"^DAILY_AUTO_RUNS\s*=\s*(\d+)", qsrc, re.M)
    check(bool(m_life) and life == int(m_life.group(1)),
          "终身次数与 quota.py 常量一致", f"admin={life} quota={m_life.group(1) if m_life else '?'}")
    check(bool(m_daily) and daily == int(m_daily.group(1)),
          "每日 auto 与 quota.py 常量一致", f"admin={daily} quota={m_daily.group(1) if m_daily else '?'}")


def test_commentary_vision_charged_at_every_entry():
    """🔴 防漏扣：`commentary_vision` 必须在**全部 4 个解说入口**都传 vision。

    解说有 4 个入口（本地拖拽 / script-only / 两个上传），其中 2 个走
    `precheck_or_raise`、2 个走 `assert_upload_allowed`（转发到前者）。
    任一入口漏传 `vision` ⇒ 用户在该入口勾「画面理解」就白嫖 50 积分。
    本用例数出现次数，个数不对就红。
    """
    src = (_SERVER / "routers" / "commentary.py").read_text(encoding="utf-8")
    n_pre = src.count("precheck_or_raise(None,")
    n_up = src.count("assert_upload_allowed(None, _dur")
    check(n_pre == 2, f"2 处 precheck_or_raise 入口", str(n_pre))
    check(n_up == 2, f"2 处 assert_upload_allowed 入口", str(n_up))
    # 每一处都必须带 vision=
    check(src.count("vision=bool(payload.vision)") == n_pre,
          "所有 precheck 入口都传了 vision=payload.vision")
    check(src.count("vision=bool(vision)") == n_up,
          "所有上传入口都传了 vision=vision")
    # 预检侧必须有条件扣费（不能只在某个分支扣）
    q = (_SERVER / "routers" / "quota.py").read_text(encoding="utf-8")
    check("_charge_optional(request, \"commentary_vision\"" in q,
          "precheck_or_raise 里按 vision 开关扣 commentary_vision")
    check("if vision:" in q, "扣费有 vision 条件判断（不开就不扣）")


def test_voice_clone_gate_added():
    """`/api/voice-studio/tts` 必须有积分门禁（此前完全无鉴权无计费）。"""
    src = (_SERVER / "routers" / "voice_studio.py").read_text(encoding="utf-8")
    check("_credit_gate(\"voice_clone\"" in src, "tts 端点有 voice_clone 门禁")
    check("status_code=402" in src, "不足时抛 402（前端统一弹会员中心）")
    # 门禁必须在「服务未就绪」之后：没启用不该收钱
    i_ready = src.index("if not _vsc.is_ready()")
    i_gate = src.index('_credit_gate("voice_clone"')
    check(i_gate > i_ready, "门禁在 is_ready 之后（没启用不收钱）")


def test_admin_html_has_four_tabs():
    """后台「套餐与积分成本」必须有 4 个分栏：下载会员/AI会员/积分包/免费额度。"""
    html = (_SERVER.parent / "web" / "index.html").read_text(encoding="utf-8")
    for cat, label in (("dl", "下载会员"), ("ai", "AI 会员"),
                       ("cp", "积分包"), ("fq", "免费额度")):
        check(f'data-cat="{cat}"' in html, f"分栏 {label}（data-cat={cat}）存在")


def test_frontend_collects_free_quota():
    """前端必须真的收集并提交 free_quota，否则页面能看不能存。"""
    js = (_SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")
    check("freeQuotaBlock" in js, "定义了 freeQuotaBlock 渲染函数")
    check("daily_free_limits" in js, "提交 daily_free_limits")
    check("daily_member_limits" in js, "提交 daily_member_limits")
    check("payload.free_quota" in js, "把 free_quota 挂进保存 payload")
    # 未接入扣费的功能必须有醒目角标，否则管理员以为配了价就在收钱
    check("未接入扣费" in js, "对未接入扣费的功能打了角标")


def test_no_hardcoded_pipeline_path():
    """🔴 源码里绝不能硬编码某台机器的绝对路径（部署到 ECS 会失效）。"""
    src = (_SERVER / "routers" / "admin.py").read_text(encoding="utf-8")
    check("/Users/suixindelang" not in src, "admin.py 无硬编码家目录路径")
    check("VDL_PIPELINE_DIR" in src, "管线路径走环境变量")


def main():
    tests = [
        test_effective_limits_default_unchanged,
        test_override_applies_and_merges,
        test_free_quota_in_save_table_keys,
        test_dirty_value_does_not_break_limits,
        test_quota_state_uses_effective_limits,
        test_white_list_preserves_sibling_fields,
        test_has_cost_gate_matches_reality,
        test_commentary_vision_charged_at_every_entry,
        test_voice_clone_gate_added,
        test_cloud_quota_limits_readable,
        test_admin_html_has_four_tabs,
        test_frontend_collects_free_quota,
        test_no_hardcoded_pipeline_path,
    ]
    for t in tests:
        t()
    print()
    if fails:
        print(f"FAILED {len(fails)}:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print(f"✅ 免费额度分栏守卫全过（{len(tests)} 项）")


if __name__ == "__main__":
    main()
