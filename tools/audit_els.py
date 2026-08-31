"""tools/audit_els.py —— 「el 表 / HTML id 对齐」审计。

铁律：JS 写 `el.comHistory.hidden = ...` 必须先把 comHistory 注册到 `el` 表里，
否则运行时 `el.comHistory === undefined`，访问 .hidden 抛 TypeError，整段回调挂死。
这个 bug 已经在 VDL 视频解说模块踩过 5 次（comGrid / comHistory / comTtsProvider /
comVision / comCorrectTranscript / comBgmVolumeVal），每次都是「重构/拆区时 HTML
加了新 id 但忘了同步注册表」。本审计就是为了把这个错在落地前抓到。

用法：
  python tools/audit_els.py web/index.html web/app.js

退出码：
  0  = 通过（未注册的 el.X = 0）
  1  = 失败（打印全部未注册的 el.X）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 匹配 `el.identifier` 中 identifier 后的第一个「点/空格/括号/分号/=」之前的内容。
# 不捕获 `el.comGrid.length` 这种二次访问，只看根属性。
EL_USAGE_RE = re.compile(r'\bel\.([A-Za-z_][A-Za-z_0-9]*)')
# 匹配 el 表里「identifier: $('domId')」或 lambda「identifier: () => ...」两种注册
EL_REG_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z_0-9]*)\s*:\s*("
    r"\$\(\s*['\"][A-Za-z_][A-Za-z_0-9]*['\"]"
    r"|\(\s*\)\s*=>"
    r")",
    re.M,
)
# HTML id="X"
HTML_ID_RE = re.compile(r'''\bid\s*=\s*["']([A-Za-z_][A-Za-z_0-9]*)["']''')

# 内部状态变量（带 _ 前缀）不算 DOM 注册错配（脚本内约定俗成）
INTERNAL_PREFIXES = ('_',)


def is_internal(name: str) -> bool:
    return any(name.startswith(p) for p in INTERNAL_PREFIXES)


def collect_usage(js_text: str) -> set[str]:
    """所有 `el.X` 引用"""
    return {m.group(1) for m in EL_USAGE_RE.finditer(js_text)}


def collect_registrations(js_text: str) -> set[str]:
    """注册名集合（不关心对应的 dom id，单纯「是否注册过」）"""
    return {m.group(1) for m in EL_REG_RE.finditer(js_text)}


def collect_html_ids(html_text: str) -> set[str]:
    return {m.group(1) for m in HTML_ID_RE.finditer(html_text)}


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("用法: python tools/audit_els.py web/index.html web/app.js", file=sys.stderr)
        return 2
    html_path, js_path = Path(argv[1]), Path(argv[2])
    html = html_path.read_text(encoding='utf-8')
    js = js_path.read_text(encoding='utf-8')

    # 排除内部状态变量（_ 前缀约定）和合法别名：检查合法注册时通过 el.X 的引用追踪回原注册
    # 现在只依赖直接注册更可靠；如果遇到 `el.badge` 但注册是 `badge: $('engineBadge')` 这种 alias
    # 也算正常（直接命中 badge，不算错）。
    used = {u for u in collect_usage(js) if not is_internal(u)}
    regs = collect_registrations(js)
    ids = collect_html_ids(html)

    # 1) JS 使用了 el.X 但 X 没有注册 → 致命错（截图那种 TypeError）
    unused = sorted(used - regs)

    # 2) 注册了但 HTML 里没对应 id → 警告（不影响运行，但说明 JS 在等不存在的节点）
    # 当前审计只保留 name，不做 dom-id 对齐（上面 collect_registrations 改成 set）。
    orphan_regs = []

    # 3) HTML 有但完全没 JS 触达 → 仅信息（可能死代码，不算 bug）
    dead_ids = sorted(ids - regs)

    print(f"HTML ids: {len(ids)}, JS el.X usages: {len(used)}, "
          f"registrations: {len(regs)}")
    if unused:
        print(f"\n[FAIL] {len(unused)} 个 el.X 引用了但 el 表没注册（运行时会 TypeError）:")
        for k in unused:
            print(f"  - el.{k}")
    if orphan_regs:
        print(f"\n[WARN] {len(orphan_regs)} 个 el 注册对应 HTML id 不存在（多半拼写错）：")
        for k in orphan_regs:
            print(f"  - {k}")
    if dead_ids and '-v' in argv:
        print(f"\n[INFO] {len(dead_ids)} 个 HTML id 完全没 JS 触达（可能死代码）：")
        for k in dead_ids:
            print(f"  - {k}")

    return 1 if unused else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
