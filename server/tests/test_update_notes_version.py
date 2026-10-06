"""「关于」页更新内容的版本对齐守卫（2026-10-07）。

## 用户实测反馈的 bug
当前版本 1.0.43，「关于」页却显示「v1.0.39 更新内容」—— 内容和版本对不上。

根因两层：
1. 前端判定「有没有更新」用的是 `latestVer !== cur`（**只要不相等**就算有更新）。
   而发布源可能**落后**于本机（本机抢先升级 / 源还没同步：实测线上 latest.json
   停在 1.0.39、本机已 1.0.43）⇒ 旧版本被当成新版本，于是渲染了旧版本的内容。
   服务端 `/api/system/latest` 的 `update_available` 用的是版本**大小**比较
   （`_parse_ver(latest) > _parse_ver(VERSION)`），前端兜底必须同语义。
2. `_cachedUpdateNotes()` 是死代码（定义了从不调用）⇒ 刚更新完回来看到的是源里
   那个旧版本的内容，而不是「本机这版改了什么」。

修法：引入 `_verNewer()` 逐段数字比较；渲染时**标题版本与内容必须同源**：
发布源更新 → 展示新版本条目；本机不比源旧 → 展示本机版本条目；找不到就隐藏，
**绝不拿别的版本的内容充数**。

运行：python3 test_update_notes_version.py
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_SERVER = _HERE.parent
_APP_JS = _SERVER.parent / "web" / "app.js"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  —— " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _render_changelog_src(src: str) -> str:
    """取 `_renderChangelog` 函数源码（括号配平）。"""
    i = src.index("function _renderChangelog(")
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    return ""


def _extract_fn(src: str, name: str) -> str:
    """按括号配平取出某个函数声明源码（不依赖缩进/换行风格）。"""
    i = src.index("function " + name + "(")
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("无法取出函数 " + name)


_NODE_HARNESS = """
// ── 最小 DOM/localStorage stub：只为跑 _renderChangelog 的判定分支 ──────────
let _aboutCurrentVer = '';
let _aboutLatest = null;
let _aboutUpdateAvail = false;
let _aboutChangelog = null;
let __ITEMS = [];
let __CACHED = null;
const __mk = () => ({ textContent: '', hidden: false });
const el = {
  profChangelog: __mk(), profChangelogTitle: __mk(), profChangelogDate: __mk(),
  profChangelogList: { replaceChildren: (...ns) => { __ITEMS = ns.map((n) => n.textContent); } },
};
const localStorage = {
  getItem: () => (__CACHED ? JSON.stringify(__CACHED) : null),
  setItem: () => {}, removeItem: () => {},
};
const document = { createElement: () => ({ textContent: '' }) };

%s
%s
%s
%s

const SCENARIOS = %s;
const out = [];
for (const sc of SCENARIOS) {
  _aboutCurrentVer = sc.cur;
  _aboutLatest = sc.latest;
  _aboutUpdateAvail = !!sc.avail;
  _aboutChangelog = sc.changelog || {};
  __CACHED = sc.cached || null;
  el.profChangelog.hidden = false;
  el.profChangelogTitle.textContent = '';
  el.profChangelogDate.textContent = '';
  __ITEMS = [];
  _renderChangelog();
  out.push({
    name: sc.name,
    hidden: !!el.profChangelog.hidden,
    title: el.profChangelogTitle.textContent || '',
    items: __ITEMS,
  });
}
console.log(JSON.stringify(out));
"""


def _render_scenarios(src: str, scenarios: list[dict]) -> list[dict]:
    """在 node 里真跑 _renderChangelog，返回每个场景的渲染结果。"""
    js = _NODE_HARNESS % (
        _extract_fn(src, "_verNewer"),
        _extract_fn(src, "_changelogItems"),
        _extract_fn(src, "_cachedUpdateNotes"),
        _extract_fn(src, "_renderChangelog"),
        json.dumps(scenarios, ensure_ascii=False),
    )
    p = pathlib.Path("/tmp/_vdl_render_probe.mjs")
    p.write_text(js, encoding="utf-8")
    out = subprocess.run(["node", str(p)], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError("node 渲染探针失败：" + out.stderr[:300])
    return json.loads(out.stdout.strip().splitlines()[-1])


_IIFE_MARK = "/* ==== 更新完成后主动告知"


def _extract_iife(src: str, marker: str) -> str:
    """按括号配平取出一个自执行 IIFE（从 marker 之后的 `(function () {` 起）。"""
    i = src.index(marker)
    j = src.index("(function () {", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "(":
            depth += 1
        elif src[k] == ")":
            depth -= 1
            if depth == 0:
                # 配平停在 `})`，必须补上 `()` 才会真正自执行（写成 `});` 只是个表达式，
                # 探针会「取到了代码却什么都没跑」，表现为所有场景都不弹）
                return src[j:k + 1] + "();"
    raise AssertionError("无法取出 IIFE：" + marker)


_POPUP_HARNESS = """
const SC = %s;
const __LS = SC.ls || {};
const __APPENDED = [];
const localStorage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(__LS, k) ? __LS[k] : null),
  setItem: (k, v) => { __LS[k] = String(v); },
  removeItem: (k) => { delete __LS[k]; },
};
const window = {
  VDL: {
    request: async (p) => (String(p).indexOf('changelog') >= 0
      ? (SC.changelog || {}) : (SC.info || {})),
  },
};
const document = {
  getElementById: () => null,
  createElement: () => ({
    id: '', style: { cssText: '' }, innerHTML: '',
    setAttribute() {}, addEventListener() {}, remove() {},
  }),
  addEventListener() {},
  body: { appendChild: (n) => { __APPENDED.push(n); } },
};

%s

setTimeout(() => {
  console.log(JSON.stringify({ appended: __APPENDED.length, ls: __LS }));
}, 20);
"""


def _popup_scenarios(src: str, scenarios: list[dict]) -> list[dict]:
    """在 node 里真跑「更新完成后弹层」IIFE，返回每个场景弹没弹。"""
    out = []
    for sc in scenarios:
        js = _POPUP_HARNESS % (
            json.dumps(sc, ensure_ascii=False),
            _extract_iife(src, _IIFE_MARK),
        )
        p = pathlib.Path("/tmp/_vdl_popup_probe.mjs")
        p.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", str(p)], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise AssertionError("node 弹层探针失败：" + r.stderr[:300])
        out.append(json.loads(r.stdout.strip().splitlines()[-1]))
    return out


def _ver_newer_cases() -> list[tuple[object, ...]]:
    """把 `_verNewer` 抽出来用 node 跑真实比较用例（不 mock）。"""
    src = _APP_JS.read_text(encoding="utf-8")
    i = src.index("function _verNewer(")
    j = src.index("{", i)
    depth = 0
    end = j
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    fn = src[i:end]
    cases = [
        # (a, b, 期望 a>b)  —— 覆盖用户报的那一组
        ("1.0.43", "1.0.39", True),
        ("1.0.39", "1.0.43", False),
        ("1.0.43", "1.0.43", False),
        ("v1.0.43", "1.0.39", True),
        ("1.0.9", "1.0.10", False),
        ("1.0.10", "1.0.9", True),
        ("2.0", "1.99", True),
        ("1.0", "1", False),
        ("", "1.0.43", False),
        ("1.0.43", "", False),
    ]
    js = fn + "\n" + "\n".join(
        f'console.log(_verNewer({json.dumps(a)}, {json.dumps(b)}) ? 1 : 0);' for a, b, _ in cases)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        return [("node 执行失败", out.stderr[:200])]
    got = [ln.strip() == "1" for ln in out.stdout.splitlines() if ln.strip() in ("0", "1")]
    bad = [(a, b, exp, g) for (a, b, exp), g in zip(cases, got) if exp != g]
    return bad


def main() -> None:
    src = _APP_JS.read_text(encoding="utf-8")
    body = _render_changelog_src(src)

    print("[A] 判定语义：必须比大小，不能比「是否相等」")
    check("存在 _verNewer 版本比较函数", "function _verNewer(" in src)
    check("_renderChangelog 不再用 `!==` 当「有更新」判据",
          "latestVer !== cur" not in body and "!== cur" not in body,
          "旧形态：源比本机旧时会把旧版本当新版本（用户实测踩到）")
    check("判定走 _verNewer", "_verNewer(latestVer, cur)" in body)

    print("\n[B] 标题与内容必须同源（不得混版本）")
    check("有更新分支用 latest 的条目", "_changelogItems(_aboutLatest" in body)
    check("无更新分支回退「本机版本」的内置条目",
          "pickBuiltin(cur)" in body,
          "否则本机抢先升级时没有内容可显示，会退回显示源里的旧版本")
    check("刚更新完缓存的条目被真正使用（不再是死代码）",
          "_cachedUpdateNotes()" in body and "function _cachedUpdateNotes(" in src)
    check("找不到条目也不整块隐藏（退回兜底文案）",
          "本版本暂无更新说明" in body,
          "老实现 `if (!items.length) { box.hidden = true; }` 会让「已是最新」的用户什么都看不到")

    print("\n[C] _verNewer 行为（node 实跑）")
    bad = _ver_newer_cases()
    check(f"10 组比较用例全对", not bad, str(bad))

    print("\n[D] 渲染行为（node 实跑 _renderChangelog，含用户截图那一组）")
    builtin = {"entries": [
        {"version": "1.0.43", "date": "2026-10-05", "items": ["新版条目-43"]},
        {"version": "1.0.39", "date": "2026-10-05", "items": ["旧版条目-39"]},
    ]}
    scenes = [
        # ① 用户举例：本机 1.0.39、最新 1.0.43 → 必须展示 1.0.43 的内容
        {"name": "源比本机新", "cur": "1.0.39", "avail": True,
         "latest": {"version": "1.0.43", "notes_list": ["新版本条目"], "published_at": "2026-10-06"},
         "changelog": builtin},
        # ② 用户截图：本机 1.0.43、发布源停在 1.0.39 → 必须展示 1.0.43 的内容
        {"name": "源落后于本机", "cur": "1.0.43", "avail": False,
         "latest": {"version": "1.0.39", "notes_list": ["旧版条目-39"], "published_at": "2026-10-05"},
         "changelog": builtin},
        # ③ 刚更新完：本机 == 源，但缓存了本次更新条目 → 用缓存
        {"name": "刚更新完用缓存", "cur": "1.0.43", "avail": False,
         "latest": {"version": "1.0.43", "notes_list": [], "published_at": ""},
         "changelog": builtin,
         "cached": {"version": "1.0.43", "items": ["本次更新条目"]}},
        # ④ 已是最新（本机 == 源）、且没更新缓存 → 必须展示「当前版本」的内置条目
        #    （2026-10-08 用户核心反馈：「当前是最新版本就不显示更新内容」）
        {"name": "已是最新显示当前版本", "cur": "1.0.43", "avail": False,
         "latest": {"version": "1.0.43", "notes_list": [], "published_at": ""},
         "changelog": builtin},
        # ⑤ 找不到任何同版本条目 → 仍展示区块 + 兜底文案（绝不整块隐藏）
        {"name": "无条目给兜底不隐藏", "cur": "1.0.99", "avail": False,
         "latest": {"version": "1.0.39", "notes_list": ["旧版条目-39"]},
         "changelog": builtin},
    ]
    got = {r["name"]: r for r in _render_scenarios(src, scenes)}
    r1 = got.get("源比本机新", {})
    check("① 源比本机新 → 标题 v1.0.43 + 新版本条目",
          (not r1.get("hidden")) and r1.get("title") == "v1.0.43 更新内容"
          and r1.get("items") == ["新版本条目"], str(r1))
    r2 = got.get("源落后于本机", {})
    check("② 源落后于本机 → 标题 v1.0.43 且内容不是 1.0.39 的（用户截图场景）",
          (not r2.get("hidden")) and r2.get("title") == "v1.0.43 更新内容"
          and r2.get("items") == ["新版条目-43"], str(r2))
    check("② 内容里绝不出现旧版本条目",
          "旧版条目-39" not in (r2.get("items") or []), str(r2.get("items")))
    r3 = got.get("刚更新完用缓存", {})
    check("③ 刚更新完 → 用更新时缓存的条目，标题＝当前版本",
          r3.get("items") == ["本次更新条目"]
          and r3.get("title") == "v1.0.43 更新内容", str(r3))
    r4 = got.get("已是最新显示当前版本", {})
    check("④ 已是最新 → 仍展示「当前版本」的内置条目（不再什么都不显示）",
          (not r4.get("hidden")) and r4.get("title") == "v1.0.43 更新内容"
          and r4.get("items") == ["新版条目-43"], str(r4))
    r5 = got.get("无条目给兜底不隐藏", {})
    check("⑤ 无同版本条目 → 仍展示区块 + 兜底文案（绝不整块隐藏）",
          (not r5.get("hidden")) and r5.get("title") == "v1.0.99 更新内容"
          and r5.get("items") == ["本版本暂无更新说明"], str(r5))

    # ── [E] 更新完成后必须主动告知（2026-10-07 用户实测：「更新完了不显示」）──
    print("\n[E] 更新完成后主动弹一次「本次更新已完成」（node 实跑弹层 IIFE）")
    notes = {"version": "1.0.43", "items": ["本次条目A", "本次条目B"]}
    nj = json.dumps(notes, ensure_ascii=False)
    pscenes = [
        {"name": "刚更新完", "ls": {"vdl_update_notes": nj}, "info": {"version": "1.0.43"}},
        {"name": "已展示过", "ls": {"vdl_update_notes": nj, "vdl_update_notes_shown": "1.0.43"},
         "info": {"version": "1.0.43"}},
        {"name": "版本没对上", "ls": {"vdl_update_notes": nj}, "info": {"version": "1.0.39"}},
        {"name": "无缓存", "ls": {}, "info": {"version": "1.0.43"}},
        {"name": "缓存条目为空", "ls": {"vdl_update_notes": json.dumps({"version": "1.0.43", "items": []})},
         "info": {"version": "1.0.43"}},
        # 缓存丢了（更新助手清理 / 无痕存储）也不能让用户「更新完什么都看不到」：
        # 本机版本 > 上次运行版本 → 判定为刚升级，条目退回内置日志
        {"name": "刚升级但缓存丢了", "ls": {"vdl_last_run_version": "1.0.39"},
         "info": {"version": "1.0.43"},
         "changelog": {"entries": [{"version": "1.0.43", "items": ["内置条目A", "内置条目B"]}]}},
        {"name": "同为最新再启动", "ls": {"vdl_last_run_version": "1.0.43"},
         "info": {"version": "1.0.43"}},
        {"name": "刚升级但内置日志无该版本", "ls": {"vdl_last_run_version": "1.0.39"},
         "info": {"version": "1.0.43"}, "changelog": {"entries": []}},
    ]
    pres = {sc["name"]: r for sc, r in zip(pscenes, _popup_scenarios(src, pscenes))}
    check("① 刚更新完（缓存版本 == 本机版本）→ 弹出卡片",
          pres["刚更新完"]["appended"] == 1, str(pres["刚更新完"]))
    check("② 该版本已展示过 → 不再打扰",
          pres["已展示过"]["appended"] == 0, str(pres["已展示过"]))
    check("③ 本机版本 != 缓存版本（更新其实没装上）→ 不弹",
          pres["版本没对上"]["appended"] == 0, str(pres["版本没对上"]))
    check("④ 没有更新缓存 → 不弹",
          pres["无缓存"]["appended"] == 0, str(pres["无缓存"]))
    check("⑤ 缓存条目为空 → 不弹（空卡片没有意义）",
          pres["缓存条目为空"]["appended"] == 0, str(pres["缓存条目为空"]))
    check("⑥ 刚升级但更新缓存丢了 → 仍要弹（退回内置日志条目），不能让用户什么也看不到",
          pres["刚升级但缓存丢了"]["appended"] == 1, str(pres["刚升级但缓存丢了"]))
    check("⑦ 上次运行就是本版本 → 不是刚升级，不弹",
          pres["同为最新再启动"]["appended"] == 0, str(pres["同为最新再启动"]))
    check("⑧ 刚升级但内置日志也没有该版本条目 → 不弹（空卡片没有意义）",
          pres["刚升级但内置日志无该版本"]["appended"] == 0,
          str(pres["刚升级但内置日志无该版本"]))

    iife = _extract_iife(src, _IIFE_MARK)
    check("⑨ 弹层有 3 个关闭入口（× / 知道了 / 点遮罩空白处）+ Esc",
          all(s in iife for s in ("data-close", "data-ok", "[data-card]", "Escape")),
          "缺少关闭入口")
    check("⑩ 关闭时写入已读标记（同一版本不再弹）",
          "KEY_SHOWN" in iife and "wr(KEY_SHOWN, cur)" in iife, "没有已读标记")
    check("⑪ 每次启动记录本次运行版本（升级判定依赖它）",
          "KEY_LAST" in iife and "wr(KEY_LAST, cur)" in iife, "没有记录上次运行版本")

    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 更新内容与版本号同源（不再拿旧版本内容充数）")


if __name__ == "__main__":
    main()
