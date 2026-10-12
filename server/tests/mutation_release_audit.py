#!/usr/bin/env python3
"""变异确认：发布包机密核查工具「改坏了会被拦下」吗。

只改**临时副本**（把工具复制到临时目录再改），真实源码零改动；用 VDL_AUDIT_TOOL
把守卫指向副本。历史教训（2026-10-12）：变异必须只在副本上做，否则进程被杀会
把缺陷代码留在真实源码里。

运行：`.build_venv/bin/python server/tests/mutation_release_audit.py`
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
PY = str(REPO / ".build_venv" / "bin" / "python")
TOOL = REPO / "server" / "tools" / "audit_release_secrets.py"
GUARD = REPO / "server" / "tests" / "test_release_audit.py"

MUTATIONS: list[tuple[str, str, str]] = [
    ("M1 关掉明文凭据检查（漏报密钥）",
     "        for label, pat in SECRET_PATTERNS:",
     "        for label, pat in []:"),

    ("M2 去掉 base64 边界（假 AKIA 误报复辟）",
     r'    ("AWS AccessKey（AKIA…）",' + "\n" + r'     re.compile(rb"(?<![" + _B64 + rb"])AKIA[0-9A-Z]{16}(?![" + _B64 + rb"])")),',
     r'    ("AWS AccessKey（AKIA…）", re.compile(rb"AKIA[0-9A-Z]{16}")),'),

    ("M3 .pem 只看后缀（把公开 CA 包当私钥）",
     'KEY_SUFFIXES = {".key", ".p12", ".pfx", ".jks", ".keystore"}',
     'KEY_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}'),

    ("M4 我方区垃圾降级为第三方（不再阻断发版）",
     'hits["F1-我方区垃圾" if in_our_zone else "F2-第三方区冗余"].append(rel)',
     'hits["F2-第三方区冗余"].append(rel)'),

    ("M5 恢复大文件跳过（主可执行不再被扫）",
     "        try:\n            blob = _read(rel, zf, root, SCAN_LIMIT_BYTES, open_name)",
     "        if size and size > 8 * 1024 * 1024 and not is_text:\n            continue\n"
     "        try:\n            blob = _read(rel, zf, root, SCAN_LIMIT_BYTES, open_name)"),

    ("M6 私有网段 IP 对二进制也判（版本号误报复辟）",
     '    ("私有网段 IP（仅文本文件）", re.compile(rb"\\b10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\b"), True),',
     '    ("私有网段 IP（仅文本文件）", re.compile(rb"\\b10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}\\b"), False),'),
    ("M7 zip 路径不归一化（ditto 前缀让判定失效、虚报凭据）",
     "                yield _rebase(shown), z, info.file_size, raw, shown",
     "                yield shown, z, info.file_size, raw, shown"),

    ("M8 不跳过 AppleDouble 伴生文件（噪音混进我方区垃圾）",
     '        if base.startswith("._") or "/__MACOSX/" in shown or shown.startswith("__MACOSX/"):',
     '        if False:'),
]
EXPECT_RED = {
    "M1": "抓到明文凭据",
    "M2": "base64 中的 AKIA 不再误报",
    "M3": "公开 CA 包被识别为公开证书",
    "M4": "抓到我方区垃圾",
    "M5": "20MB 主可执行里的凭据被抓到",
    "M6": "二进制版本号 10.11.12.13 不再误报",
    "M7": "第三方库仍被白名单",
    "M8": "AppleDouble 伴生文件归噪音",
}


def _run_guard(tool_path: pathlib.Path) -> tuple[bool, str]:
    env = dict(os.environ)
    env["VDL_AUDIT_TOOL"] = str(tool_path)
    p = subprocess.run([PY, str(GUARD)], capture_output=True, text=True, env=env, timeout=600)
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    before = hashlib.sha256(TOOL.read_bytes()).hexdigest()
    print(f"真实工具 sha256（开工前）：{before[:16]}")

    ok, out = _run_guard(TOOL)
    if not ok:
        print("❌ 基线失败：未变异的守卫就不全绿，本次结果作废")
        print("\n".join([l for l in out.splitlines() if "❌" in l][:8]))
        return 1
    print("✅ 基线：未变异时守卫全绿\n")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_audit_mut_"))
    all_ok = True
    try:
        for name, old, new in MUTATIONS:
            src = TOOL.read_text(encoding="utf-8")
            if old not in src:
                print(f"⚠️  {name}: 锚点未命中，跳过（请人工核对）")
                all_ok = False
                continue
            mut = tmp / "audit_mut.py"
            mut.write_text(src.replace(old, new, 1), encoding="utf-8")
            passed, out = _run_guard(mut)
            if passed:
                print(f"❌ {name}: 改坏后守卫仍全绿 —— 断言没咬住！")
                all_ok = False
            else:
                reds = [l.strip() for l in out.splitlines() if l.strip().startswith("❌")]
                expect = EXPECT_RED.get(name.split()[0], "")
                hit_expect = any(expect and expect in r for r in reds) if expect else None
                tag = "（命中期望断言）" if hit_expect else ""
                print(f"✅ {name}: 如期变红 {tag} {reds[0][:68] if reds else '非零退出'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    after = hashlib.sha256(TOOL.read_bytes()).hexdigest()
    print(f"\n真实工具 sha256（收工后）：{after[:16]}")
    same = before == after
    print("✅ 真实源码零改动" if same else "❌ 真实源码被改动（严重）")
    print("\n" + ("🎉 全部变异如期变红" if (all_ok and same) else "❌ 存在未咬住/被污染项"))
    return 0 if (all_ok and same) else 1


if __name__ == "__main__":
    sys.exit(main())
