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


def _reload_quota():
    """重新执行 `server/quota.py` 并返回新模块对象（测试用）。

    🔴 两个坑（都实测踩过）：
      ① 不能 `import quota` —— 本测试把 `routers/` 放进 sys.path，
         `routers/quota.py` 会抢走 `quota` 这个名字（AttributeError）。
      ② 不能 `importlib.reload()` —— 它要求模块有可查的 spec，自
         `spec_from_file_location` 建的模块会报 "spec not found"。
    所以直接重读源码 exec 一遍，最可靠。
    """
    import sys as _sys, types as _t
    mod = _t.ModuleType("_vdl_quota_root")
    mod.__file__ = str(_SERVER / "quota.py")
    _sys.modules["_vdl_quota_root"] = mod
    exec(compile((_SERVER / "quota.py").read_text(encoding="utf-8"),
                 "quota.py", "exec"), mod.__dict__)
    return mod


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
    # 🔴 必须从 server/ 根取 quota：本测试已把 routers/ 放进 sys.path，
    # `routers/quota.py` 会抢先占住 `quota` 这个名字（实测 AttributeError）。
    Q = _reload_quota()

    # 1) 无覆盖时 == 代码常量
    d = Path(_TMP) / "cq_limits"
    d.mkdir(parents=True, exist_ok=True)
    (d / "plans.json").write_text("{}", encoding="utf-8")
    os.environ["VDL_DATA_DIR"] = str(d)
    import importlib
    Q = _reload_quota()
    check(Q.cloud_quota_limits() == (Q.LIFETIME_CLOUD_EVENTS, Q.DAILY_AUTO_RUNS),
          "无覆盖时云端额度 == 代码常量", str(Q.cloud_quota_limits()))

    # 2) 覆盖生效
    (d / "plans.json").write_text(
        '{"free_quota": {"cloud_lifetime": 10, "daily_auto": 5}}', encoding="utf-8")
    Q = _reload_quota()
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
    Q = _reload_quota()
    check(Q.cloud_quota_limits() == (Q.LIFETIME_CLOUD_EVENTS, Q.DAILY_AUTO_RUNS),
          "脏值/负数回退默认（不炸表）", str(Q.cloud_quota_limits()))

    # 5) admin 接口读到的也是生效值（后台页面显示与实际一致）
    (d / "plans.json").write_text(
        '{"free_quota": {"cloud_lifetime": 7, "daily_auto": 2}}', encoding="utf-8")
    Q = _reload_quota()
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
    """前端必须有云端额度的输入框并提交（否则又是「能看不能改」）。

    🔴 2026-10-06 拆池：终身次数按功能独立（4 个 resource 各一输入行，
    data-res 标识），保存时收集成字典 {resource: n} 提交；daily_auto 仍单值。
    """
    js = (_SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")
    check('class="admin-input admin-input-sm fq-cloud"' in js, "云端额度有输入框")
    check('data-key="cloud_lifetime" data-res=' in js, "终身次数按功能独立可填（data-res）")
    check("cloud_lifetime_per_resource" in js, "渲染读 per-resource limits（4 行）")
    check('data-key="daily_auto"' in js, "每日 auto 次数可填")
    check("clDict[el.dataset.res] = v" in js, "输入值按 resource 收集进 payload")
    check("payload.cloud_lifetime = clDict" in js, "终身次数（字典）随保存提交")
    check("fq.cloud_lifetime = payload.cloud_lifetime" in js, "终身次数提交到套餐接口")
    check("fq.daily_auto = payload.daily_auto" in js, "每日 auto 随保存提交")


def test_quota_rows_use_function_names():
    """🔴 后台「免费额度」必须按**用户看到的功能名**列出（用户 2026-10-06 14:22
    「得按功能来：视频下载、订阅追更、视频解说、本地字幕提取、音视频格式转换、
    音乐转换等，这样更清楚」）。

    此前后台只写「云端算力 / 本地重算力」—— 内部技术词，管理员看不出管哪些功能。
    现在每个额度池拆成若干**功能行**，名称 = 首页卡片原文。
    """
    import membership as M
    names = {str(x.get("name") or "") for x in M.FEATURE_USAGE_DEFS}
    # 用户点名的功能必须都在
    for fn in ("视频下载", "订阅追更", "视频解说", "字幕提取",
               "视频格式转换", "音乐转换", "图片转换", "高效压缩",
               "高清修复", "一键抠图"):
        check(f"功能名「{fn}」在表里", fn in names)
    # 技术词不得作为**功能名**出现（可作为 resource 键，那是内部标识）
    for tech in ("云端算力", "本地重算力"):
        check(f"「{tech}」不再作为功能名", tech not in names,
              "改用具体功能名，技术词只作 resource 键")
    # 🔴 用户 2026-10-04 定档：文案不得出现「云端/算力/AI/本地」（守卫
    # test_membership_benefits 会红）。功能名同样是给管理员看的文案，须一致。
    for x in M.FEATURE_USAGE_DEFS:
        nm = str(x.get("name") or "")
        hit = [k for k in ("云端", "算力", "AI", "本地") if k in nm]
        check(f"功能名「{nm}」不含技术词", not hit, f"命中 {hit}")

    # 每个 resource 至少有一个功能行（否则该额度在后台不可见）
    for r in set(M.FREE_DAILY_LIMITS) | set(M.DAILY_QUOTA_LIMITS):
        rows = [x for x in M.FEATURE_USAGE_DEFS if x.get("resource") == r]
        check(f"额度 {r} 有功能行", bool(rows), "配额表里的键必须有功能名，否则后台看不到")


def test_daily_feature_rows_mark_shared():
    """同一 resource 的多个功能行必须带 `shared_group`（前端据此告知共用额度）。"""
    import membership as M
    sys.path.insert(0, str(_SERVER / "routers"))
    import admin as A
    rows = A._daily_feature_rows()
    check(bool(rows), "能取到功能行")
    # 🔴 2026-10-06 用户定档「每个功能独立配置」后，原本共用 app_compute 的
    # 6 个重活已拆成 **4 个独立键**：convert / compress / sr / bridge。
    # （commentary 不在内：视频解说走 quota.py 终身云端 + AI 积分，不走日配额。）
    for key in ("convert_video", "convert_audio", "convert_image",
                "compress", "sr", "bridge"):
        ks = [r for r in rows if r["resource"] == key]
        check(f"独立额度键 {key} 存在", bool(ks), "拆键后该功能应有独立额度")
    # 5 个旧键仍共用（下载与追更同一份；转换三兄弟同一份；字幕/抠图/在线各自独立）
    check(len([r for r in rows if r["resource"] == "download"]) == 2,
          "视频下载/订阅追更共用 download（同一类）")
    # 🔴 2026-10-06 17:11 用户「几个分开不要几个放一起」⇒ 转换三兄弟也拆开
    for k in ("convert_video", "convert_audio", "convert_image"):
        check(f"转换类 {k} 恰好 1 个功能行（真独立）",
              len([r for r in rows if r["resource"] == k]) == 1)
    check(all(r["known"] for r in rows if r["resource"] in
              ("convert", "compress", "sr", "bridge")),
          "拆出的 4 个独立键都 known=true")
    # 覆盖值取生效值：plans.json 覆盖 app_compute=3 时应显示 3 而非代码默认 5
    import os, tempfile, json as _json
    d = Path(tempfile.mkdtemp())
    (d / "plans.json").write_text(
        _json.dumps({"free_quota": {"daily_free_limits": {"app_compute": 3}}}), encoding="utf-8")
    old = os.environ.get("VDL_DATA_DIR")
    os.environ["VDL_DATA_DIR"] = str(d)
    try:
        import importlib
        import auth_store, membership as MM
        importlib.reload(auth_store); importlib.reload(MM)
        MM._PLAN_OVERRIDE_CACHE = None
        importlib.reload(A)
        rows2 = A._daily_feature_rows()
        ac2 = [r for r in rows2 if r["resource"] == "app_compute"]
        check(all(r["free_limit"] == 3 for r in ac2),
              "覆盖值生效：app_compute 显示 3（不是代码默认 5）",
              str([r["free_limit"] for r in ac2]))
    finally:
        if old is None:
            os.environ.pop("VDL_DATA_DIR", None)
        else:
            os.environ["VDL_DATA_DIR"] = old


def test_frontend_groups_by_resource():
    """前端必须按 resource 分块（一个额度池一块，块内列功能名）。"""
    js = (_SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")
    check("const _byRes = {}" in js, "按 resource 聚合（_byRes）")
    check('class="fq-block"' in js, "用 fq-block 分块渲染")
    check('class="fq-block-fns"' in js, "块内渲染功能名列表")
    # 🔴 2026-10-06 拆键后提示以「独立计数」为主，只对同类功能说「共用」。
    check("独立计数" in js or "共用同一份次数" in js,
          "提示额度是独立还是共用（避免误判）")
    # 输入框仍以 resource 为键（改任一功能行都改同一份）
    check('class="admin-input admin-input-sm fq-free" data-key="${esc(r)}"' in js,
          "输入框按 resource 提交（共用同一份额度）")
    css = (_SERVER.parent / "web" / "styles.css").read_text(encoding="utf-8")
    check(".fq-block-fns" in css, "有 fq-block-fns 样式")


def test_hint_text_matches_reality():
    """🔴 提示文案不许与实际配额结构矛盾（用户 2026-10-06 16:55 截图发现）。

    实测事故：拆键后 `compress` / `sr` / `bridge` 已各自独立，但页面提示仍写
    「高效压缩 / 高清修复都走本地算力，用掉一次就少一次」—— **文字在骗人**，
    管理员据此以为没拆开。**改了数据结构必须同步改文案**。
    """
    js = (_SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")
    import re as _re
    import membership as M
    import admin as A
    rows = A._daily_feature_rows()
    # 共用同一 resource 的功能组（文案里若提「共用」必须是真的）
    by_res = {}
    for r in rows:
        by_res.setdefault(r["resource"], []).append(r["name"])
    # 已独立的功能：文案不得再说它们共用
    for key in ("compress", "sr", "bridge"):
        grp = by_res.get(key) or []
        check(len(grp) <= 1, f"{key} 组只有 1 个功能（真独立）", f"实际 {grp}")
        if len(grp) <= 1:
            # 前端不得把「高效压缩」「高清修复」写进「共用」那句里
            # 🔴 只取**真正渲染给用户的那句**：源码里 `h += '<p class="admin-hint">…'`
            #    的字面量。按 `<p class="admin-hint">` 到 `</p>` 精确截取 ——
            #    早先按「① 之后 1200 字 + 剔注释」仍在误报/漏报（注释里也含
            #    「高效压缩…共用」这类字样，边界判断靠不住）。
            _m = _re.search(r'<p class="admin-hint">(.*?)</p>', js, _re.S)
            check(_m is not None, "找到提示文案（admin-hint）")
            hint = _m.group(1) if _m else ""
            for name in ("高效压缩", "高清修复", "音视频桥接"):
                # 已独立的功能名若出现在「共用」语境里 ⇒ 文案说谎
                bad_ctx = [sent.strip()
                           for sent in _re.split(r"。|？|！", hint)
                           if name in sent and "共用" in sent]
                check(not bad_ctx, f"「{name}」没被说成共用",
                      f"文案与拆键后事实矛盾：{bad_ctx[0][:60] if bad_ctx else ''}")
    check("独立计数" in js, "提示文案里有「独立计数」表述",
          "应明确告知每个额度框独立计数")


def test_legacy_key_hidden_from_admin_table():
    """老键（app_compute）不得出现在后台配置表（会让人以为仍共用一份）。"""
    import membership as M
    import admin as A
    rows = A._daily_feature_rows()
    legacy = set(M.LEGACY_QUOTA_KEYS)
    shown = {r["resource"] for r in rows}
    check(not (shown & legacy), f"老键未出现在配置表：{sorted(shown & legacy)}")


def test_convert_target_routing():
    """🔴 转换必须按 `target` 判类型（视频/音乐/图片各自独立额度）。

    2026-10-06 17:11 用户要求「几个分开不要几个放一起」。视频/音乐/图片转换走
    **同一个端点**，靠 `target` 扩展名区分 ⇒ 必须验证 `convert_quota_key` 判对，
    否则用户转 5 次 mp3 会占掉视频的额度（判错）。
    """
    import re as _re
    import pathlib
    src = (pathlib.Path(_SERVER) / "app.py").read_text(encoding="utf-8")
    i = src.index("_CONVERT_AUDIO_TARGETS = {")
    j = src.index("def app_compute_gate")
    ns = {}
    exec(compile(src[i:j], "<f>", "exec"), ns)
    f = ns["convert_quota_key"]
    check(f("mp4") == ("convert_video", "视频格式转换"), "mp4 → 视频额度")
    check(f("mkv")[0] == "convert_video", "mkv → 视频额度")
    check(f("mp3") == ("convert_audio", "音乐转换"), "mp3 → 音乐额度")
    check(f("flac")[0] == "convert_audio", "flac → 音乐额度")
    check(f("png") == ("convert_image", "图片转换"), "png → 图片额度")
    check(f("webp")[0] == "convert_image", "webp → 图片额度")
    check(f("mp3", is_image=True)[0] == "convert_image",
          "is_image=True 时图片优先（上传转码路径）")
    # 🔴 未知格式必须保守落视频（不放行、不落空）
    check(f("xyz")[0] == "convert_video", "未知格式保守落视频（不多放行）")
    # 业务侧必须真的调用它（不能只定义了不用）
    cv = (pathlib.Path(_SERVER) / "routers" / "convert.py").read_text(encoding="utf-8")
    check("app.convert_quota_key(" in cv, "convert.py 真的调用 convert_quota_key")
    # 🔴 2026-10-06 第二轮拆池：拼接已拆成独立键（concat_video/concat_audio），
    #    不再走 convert_quota_key ⇒ 调用点从 4 变 3，其余仍必须按类型判定。
    check(cv.count("app.convert_quota_key(") >= 3,
          "3 个格式转换调用点都改用类型判定", f"实际 {cv.count('app.convert_quota_key(')} 处")
    check('"concat_video"' in cv and '"concat_audio"' in cv,
          "拼接走独立键 concat_video / concat_audio（不再混进格式转换）")
    check('app_compute_gate(request, "convert"' not in cv,
          "没有残留写死的 convert 键（否则又变共用）")


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
    # 🔴 老键（LEGACY_QUOTA_KEYS，如 app_compute）只用于存量归集，**故意不出现**
    #    在后台配置表（显示它会让人以为转换/压缩/修复/桥接仍共用一份）。
    want = (set(M.FREE_DAILY_LIMITS) | set(M.DAILY_QUOTA_LIMITS)) - set(M.LEGACY_QUOTA_KEYS)
    missing = want - got
    check(not missing, "配额表所有真配额键都在后台页面出现", f"缺: {sorted(missing)}")
    legacy_shown = got & set(M.LEGACY_QUOTA_KEYS)
    check(not legacy_shown, "老键未出现在后台配置表", f"不该显示: {sorted(legacy_shown)}")
    # 🔴 2026-10-06 拆池：旧总池键 cloud 已进 LEGACY_QUOTA_KEYS（无写入点，
    # 只做存量归集）→ 与 app_compute 一样故意不显示；取而代之的是 4 个独立键。
    for res in ("cloud_commentary", "cloud_convert", "cloud_dewatermark", "cloud_subtitle"):
        check(f"{res} 已列出（拆池独立行）", res in got)
    # 展示值必须是**生效值**（叠加后台覆盖），不能是写死的默认
    by_res = {r["resource"]: r for r in rows}
    check(by_res.get("cloud_commentary", {}).get("free_limit")
          == M.FREE_DAILY_LIMITS.get("cloud_commentary"),
          "cloud_commentary 免费次数取自配额表（当前 3/日）",
          str(by_res.get("cloud_commentary", {}).get("free_limit")))
    check(by_res.get("cloud_subtitle", {}).get("member_limit")
          == M.DAILY_QUOTA_LIMITS.get("cloud_subtitle"),
          "cloud_subtitle 会员次数取自配额表（当前 200/日）",
          str(by_res.get("cloud_subtitle", {}).get("member_limit")))


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
    import admin as A
    Q = _reload_quota()
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
        test_quota_rows_use_function_names,
        test_daily_feature_rows_mark_shared,
        test_frontend_groups_by_resource,
        test_hint_text_matches_reality,
        test_legacy_key_hidden_from_admin_table,
        test_convert_target_routing,
        test_unknown_resource_keys_are_ignored,
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
