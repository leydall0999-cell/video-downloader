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
    check("找不到条目时整块隐藏", "box.hidden = true" in body)

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
        # ④ 找不到任何条目 → 整块隐藏（绝不拿别的版本充数）
        {"name": "无条目应隐藏", "cur": "1.0.99", "avail": False,
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
    check("③ 刚更新完 → 用更新时缓存的条目",
          r3.get("items") == ["本次更新条目"] and r3.get("title") == "v1.0.43 更新内容", str(r3))
    r4 = got.get("无条目应隐藏", {})
    check("④ 无同版本条目 → 整块隐藏", r4.get("hidden") is True, str(r4))

    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ 更新内容与版本号同源（不再拿旧版本内容充数）")


if __name__ == "__main__":
    main()
