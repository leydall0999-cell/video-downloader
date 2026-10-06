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


def test_cloud_quota_limits_are_configurable():
    """🔴 云端额度（终身 / 每日 auto）必须**后台可改**（用户 2026-10-06 问
    「后台能不能改」—— 此前只读 quota.py 模块常量，看得到数字却改不了）。

    覆盖值存 `plans.json` 的 `free_quota.{cloud_lifetime, daily_auto}`，
    `quota.cloud_quota_limits()` 是唯一读取口；未配置时落回代码常量。
    """
    import membership as M
    import quota as Q

    # 1) 无覆盖时 == 代码常量
    d = Path(_TMP) / "cq_limits"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plans.json").write_text("{}", encoding="utf-8")
    os.environ["VDL_DATA_DIR"] = str(d)
    import importlib
    importlib.reload(Q)
    check(Q.cloud_quota_limits() == (Q.LIFETIME_CLOUD_EVENTS, Q.DAILY_AUTO_RUNS),
          "无覆盖时云端额度 == 代码常量", str(Q.cloud_quota_limits()))

    # 2) 覆盖生效
    (d / "plans.json").write_text(
        '{"free_quota": {"cloud_lifetime": 10, "daily_auto": 5}}', encoding="utf-8")
    importlib.reload(Q)
    check(Q.cloud_quota_limits() == (10, 5),
          "后台改 10/5 后生效", str(Q.cloud_quota_limits()))

    # 3) 真实影响剩余额度（不只是 getter 返回值变了）
    q = Q.QuotaManager(base_dir=str(d / "state"), is_member_fn=lambda: False,
                       token_fn=lambda: "")
    check(q.lifetime_cloud_remaining() == 10,
          "QuotaManager 终身剩余跟着变（真生效，非仅展示）",
          str(q.lifetime_cloud_remaining()))
    check(q.daily_auto_remaining() == 5, "每日 auto 剩余跟着变",
          str(q.daily_auto_remaining()))

    # 4) 脏值/负数忽略并回退默认（不让后台填错炸掉链路）
    (d / "plans.json").write_text(
        '{"free_quota": {"cloud_lifetime": "abc", "daily_auto": -1}}', encoding="utf-8")
    importlib.reload(Q)
    check(Q.cloud_quota_limits() == (Q.LIFETIME_CLOUD_EVENTS, Q.DAILY_AUTO_RUNS),
          "脏值/负数回退默认（不炸表）", str(Q.cloud_quota_limits()))

    # 5) admin 接口读到的也是生效值（后台页面显示与实际一致）
    (d / "plans.json").write_text(
        '{"free_quota": {"cloud_lifetime": 7, "daily_auto": 2}}', encoding="utf-8")
    importlib.reload(Q)
    # 🔴 membership 的 plans.json 路径由 auth_store._base_dir() 决定，且
    # `load_plan_overrides` 自带 mtime 缓存 —— 必须把它也指向同一目录并清缓存，
    # 否则 admin 侧仍读旧值（实测踩过：只改 os.environ 不够）。
    old_data_dir = os.environ.get("VDL_DATA_DIR")
    os.environ["VDL_DATA_DIR"] = str(d)
    try:
        import auth_store
        importlib.reload(auth_store)
        M._PLAN_OVERRIDE_CACHE = None
        check(str(M.plan_override_path()) == str(d / "plans.json"),
              "membership 与 quota 读同一个 plans.json",
              f"{M.plan_override_path()} vs {d / 'plans.json'}")
        sys.path.insert(0, str(_SERVER / "routers"))
        import admin as A
        life, daily = A._cloud_quota_limits()
        check((life, daily) == (7, 2), "admin._cloud_quota_limits 读到覆盖值",
              f"{life},{daily}")
    finally:
        if old_data_dir is None:
            os.environ.pop("VDL_DATA_DIR", None)
        else:
            os.environ["VDL_DATA_DIR"] = old_data_dir
        M._PLAN_OVERRIDE_CACHE = None


def test_frontend_can_edit_cloud_quota():
    """前端必须有云端额度的输入框并提交（否则又是「能看不能改」）。"""
    js = (_SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")
    check('class="admin-input admin-input-sm fq-cloud"' in js, "云端额度有输入框")
    check('data-key="cloud_lifetime"' in js, "终身次数可填")
    check('data-key="daily_auto"' in js, "每日 auto 次数可填")
    check("payload[el.dataset.key] = v" in js, "输入值被收集进 payload")
    check("fq.cloud_lifetime = payload.cloud_lifetime" in js, "终身次数随保存提交")
    check("fq.daily_auto = payload.daily_auto" in js, "每日 auto 随保存提交")


def test_unknown_resource_keys_are_ignored():
    """🔴 覆盖层里**不存在的 resource 键**必须被忽略（2026-10-06 修的真实 bug）。

    背景：前端保存时误把展示标识 `FEATURE_USAGE_DEFS[].key`（video_parse /
    local_matting / subtitle_extract）当成 `resource` 提交，plans.json 里躺着
    三条**永不生效**的垃圾项。后台界面还照样显示它们，但改次数对 download /
    matting / subtitle 毫无作用。根因在前端已修（data-key 改用 resource），
    这里钉住后端兜底：历史脏数据也不得污染生效的配额表。
    """
    import membership as M
    M.save_plan_overrides({"free_quota": {
        "daily_free_limits": {"video_parse": 3, "local_matting": 3,
                              "subtitle_extract": 3, "download": 6},
        "daily_member_limits": {"video_parse": 1000, "download": 800},
    }})
    M._PLAN_OVERRIDE_CACHE = None
    mem, free = M.effective_daily_limits()
    for junk in ("video_parse", "local_matting", "subtitle_extract"):
        check(f"垃圾键 {junk} 未进生效配额", junk not in free and junk not in mem)
    check(free.get("download") == 6, "合法键 download 正常生效（6）", str(free.get("download")))
    check(mem.get("download") == 800, "合法键 member download 生效（800）", str(mem.get("download")))
    # 配额表里有的键一个都不能少
    for k in M.FREE_DAILY_LIMITS:
        check(f"配额表键 {k} 仍在生效表里", k in free)


def test_frontend_daily_inputs_use_resource_key():
    """🔴 前端每日配额的输入框必须用 `resource` 而非 `key`（否则改了不生效）。

    这是真实事故：`f.key`（展示标识 video_parse）与 `f.resource`（配额表键
    download）是两个不同字段，写错时界面照常保存、但改的是无效键。
    """
    js = (_SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")
    seg = js[js.index("① 纯免费功能"):js.index("① 纯免费功能") + 1600]
    check('data-key="${esc(f.resource)}"' in seg,
          "每日配额输入框用 f.resource 作 data-key")
    # 确认没有残留 f.key 作为提交键
    bad = [ln.strip() for ln in seg.splitlines()
           if ('fq-free' in ln or 'fq-member' in ln) and 'f.key' in ln]
    check(not bad, "没有 f.key 混进提交键", "; ".join(bad[:1]))


def test_effective_limits_default_unchanged():
    """没配覆盖时，生效值必须等于代码常量（默认值永不丢失）。"""
    import membership as M
    mem, free = M.effective_daily_limits()
    check(free == M.FREE_DAILY_LIMITS, "无覆盖时免费档 == 代码常量")
    check(mem == M.DAILY_QUOTA_LIMITS, "无覆盖时会员档 == 代码常量")
    # 关键：不能因为加了覆盖机制就把「字幕 2 次/日」这种既有配额弄丢
    for k in ("download", "matting", "cloud", "app_compute", "subtitle"):
        check(k in free, f"免费档保留 {k}")


def test_free_quota_panel_covers_every_quota_key():
    """🔴 防漏项：配额表里的**每一个**键都必须出现在后台「免费额度」栏。

    实测踩过：配额表有 `cloud`（云端算力，两端都真实生效、免费 3/日），
    但 `FEATURE_USAGE_DEFS` 漏登记 ⇒ 后台看不到也改不了。现由
    `admin._daily_feature_rows()` 以配额表为真源兜底拼装，故本用例对
    「键集合一致」做硬断言 —— 以后给配额表加键却忘了登记展示名，后台仍会显示。
    """
    import membership as M
    sys.path.insert(0, str(_SERVER / "routers"))
    import admin as A
    rows = A._daily_feature_rows()
    got = {r["resource"] for r in rows}
    want = set(M.FREE_DAILY_LIMITS) | set(M.DAILY_QUOTA_LIMITS)
    missing = want - got
    check(not missing, "配额表所有键都在后台页面出现", f"缺: {sorted(missing)}")
    check("cloud" in got, "云端算力（cloud）已列出（此前漏项）")
    # 展示值必须是**生效值**（叠加后台覆盖），不能是写死的默认
    by_res = {r["resource"]: r for r in rows}
    check(by_res.get("cloud", {}).get("free_limit") == M.FREE_DAILY_LIMITS.get("cloud"),
          "cloud 免费次数取自配额表（当前 3/日）",
          str(by_res.get("cloud", {}).get("free_limit")))
    check(by_res.get("cloud", {}).get("member_limit") == M.DAILY_QUOTA_LIMITS.get("cloud"),
          "cloud 会员次数取自配额表（当前 200/日）",
          str(by_res.get("cloud", {}).get("member_limit")))


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
    """admin 的云端额度必须与 `quota.cloud_quota_limits()` 一致（同一真源）。

    🔴 2026-10-06 改口径：此前断言是「等于 quota.py 的**模块常量**」，
    那只对「没配覆盖」时成立。现在后台可改了（`free_quota.cloud_lifetime` /
    `daily_auto`），两边都读 `cloud_quota_limits()`，故改为断言**两者一致**——
    无论是否配置过。这才是「单一真源」的正确表述。
    """
    import quota as Q
    import admin as A
    import importlib
    importlib.reload(Q)
    A_life, A_daily = A._cloud_quota_limits()
    q_life, q_daily = Q.cloud_quota_limits()
    check((A_life, A_daily) == (q_life, q_daily),
          "admin 与 quota 读同一真源（改一处两端同步）",
          f"admin={A_life},{A_daily} quota={q_life},{q_daily}")


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


def test_free_quota_shares_block_with_ai_cost():
    """🔴 免费额度必须与「AI 积分成本」在**同一区块、左右两栏**（用户定档
    「左右排版，不要上下排版，参考会员那个」）。

    理由：那 11 项消耗积分的功能与成本表是**同一批 op**，拆开放要来回对照；
    「首次体验」策略本来也在 AI 成本区里。形态对齐「下载会员/AI会员/积分包」
    那套 admin-seg 切换器。
    """
    html = (_SERVER.parent / "web" / "index.html").read_text(encoding="utf-8")
    js = (_SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")

    # 同一 section 内左右两栏：两个面板 + 一个切换器
    check('id="adminAiCostSec"' in html, "AI 计费区块存在")
    check('id="adminAiCostSeg"' in html, "有左右分栏切换器 adminAiCostSeg")
    check('data-acat="cost"' in html, "左栏 = AI 积分成本")
    check('data-acat="fq"' in html, "右栏 = 免费额度")
    check('data-acat-panel="cost"' in html, "左栏面板")
    check('data-acat-panel="fq"' in html, "右栏面板")
    # 免费额度不再是一个独立 section（那才是「上下排版」）
    check('id="adminFreeQuotaSec"' not in html, "免费额度不再是独立 section（已左右并入）")
    # 切换逻辑存在，且两栏互斥
    check("applyAiCostSeg" in js, "定义了 applyAiCostSeg 切换函数")
    check("p.hidden = p.dataset.acatPanel !== cur" in js, "两栏互斥显隐")
    # 保存按钮跟着当前栏走，避免在右栏误点「保存改动」改了价格
    check("bCost.hidden = (cur !== 'cost')" in js and "bFq.hidden = (cur !== 'fq')" in js,
          "保存按钮随当前栏切换显隐")
    # 容器与按钮 id 仍存在
    check('id="adminFreeQuotaBox"' in html, "免费额度容器")
    check('id="adminFqSave"' in html, "免费额度保存按钮")
    # 套餐分栏里不该再有 fq
    check('data-cat="fq"' not in html, "套餐分栏无 fq（不与套餐分开放）")
    for cat, label in (("dl", "下载会员"), ("ai", "AI 会员"), ("cp", "积分包")):
        check(f'data-cat="{cat}"' in html, f"套餐分栏保留 {label}")


def test_free_quota_loading_wired():
    """进入「系统配置」页必须会加载免费额度（否则新区块空白）。"""
    js = (_SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")
    check("else if (name === 'config') { loadConfig(); loadAiCosts(); }" in js,
          "config 页会调 loadAiCosts（免费额度才会渲染）")
    check("renderFreeQuota(r.free_quota)" in js, "loadAiCosts 里渲染免费额度")
    check("adminFqSave" in js and "saveFreeQuota" in js, "独立保存逻辑已接上")
    check("freeQuotaBlock" not in js, "旧的 freeQuotaBlock 已移除（无重复渲染）")


def test_frontend_collects_free_quota():
    """前端必须真的收集并提交两类配置，否则页面能看不能存。"""
    js = (_SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")
    check("renderFreeQuota" in js, "定义了 renderFreeQuota 渲染函数")
    check("daily_free_limits" in js, "提交 daily_free_limits（纯免费功能每日次数）")
    check("daily_member_limits" in js, "提交 daily_member_limits（会员每日次数）")
    # 试用水位走 free_trial.exclude（与「首次体验」同一份配置，不另开一套）
    check("free_trial: { exclude:" in js, "试用水位写入 free_trial.exclude")
    # 两类各走自己的接口：free_quota→套餐接口，free_trial→积分成本接口
    check("/api/admin/config/plans" in js, "free_quota 走套餐接口")
    check("/api/admin/ai/credit-costs" in js, "free_trial 走积分成本接口")
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
        test_unknown_resource_keys_are_ignored,
        test_frontend_daily_inputs_use_resource_key,
        test_cloud_quota_limits_are_configurable,
        test_frontend_can_edit_cloud_quota,
        test_free_quota_panel_covers_every_quota_key,
        test_override_applies_and_merges,
        test_free_quota_in_save_table_keys,
        test_dirty_value_does_not_break_limits,
        test_quota_state_uses_effective_limits,
        test_white_list_preserves_sibling_fields,
        test_has_cost_gate_matches_reality,
        test_commentary_vision_charged_at_every_entry,
        test_voice_clone_gate_added,
        test_cloud_quota_limits_readable,
        test_free_quota_shares_block_with_ai_cost,
        test_free_quota_loading_wired,
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
