#!/usr/bin/env python3
"""守卫：App 内「扩展版本比对 → 更新提示」必须只在【内置版本更新】时提示升级。

背景（2026-10-01 用户实测）：用户手动把扩展升到 v1.0.40 后，面板却弹出
「⚠ 扩展有新版本（已装 v1.0.40 → 最新 v1.0.39）」——原实现只判 `pkgVer !== installed`，
方向没比。照这个提示点「更新扩展」会下载 App 内置的旧版 zip，**把用户的新版降级**。

本测试从 web/js/desktop-app.js 源码里**抽出真实的 cmpExtVer 函数**用 node 执行
（而不是在 Python 里另写一份等价逻辑，否则守卫不到源码漂移），并锁住调用点。
"""
import json
import os
import re
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
APP_JS = os.path.normpath(os.path.join(HERE, "..", "..", "web", "js", "desktop-app.js"))


def _read_src() -> str:
    with open(APP_JS, encoding="utf-8") as f:
        return f.read()


def _extract_cmp_fn(src: str) -> str:
    m = re.search(r"const cmpExtVer = \(a, b\) => \{.*?\n    \};", src, re.S)
    assert m, "desktop-app.js 里找不到 cmpExtVer 定义（被改名/删除？守卫失效）"
    return m.group(0)


def _node() -> str:
    exe = shutil.which("node")
    if exe:
        return exe
    managed = os.path.expanduser(
        "~/.workbuddy/binaries/node/versions/22.22.2-3/bin/node")
    return managed if os.path.exists(managed) else ""


def test_version_cmp_semantics():
    """语义：a 严格新于 b 才为 true；缺失/非法一律 false。"""
    src = _read_src()
    fn = _extract_cmp_fn(src)
    node = _node()
    if not node:
        print("   ⚠️ 跳过：本机没有 node")
        return
    cases = [
        ("1.0.40", "1.0.39", True),
        ("1.0.39", "1.0.40", False),   # ★ 关键：新版绝不能被判成旧版（防降级）
        ("1.0.40", "1.0.40", False),
        ("1.10.0", "1.9.0", True),     # 数值比较，不是字符串比较
        ("1.0", "1.0.0", False),
        ("1.0.0.1", "1.0.0", True),
        ("", "1.0.0", False),
        ("?", "1.0.0", False),
        ("1.0.0", "", False),
    ]
    js = (fn + "\n"
          "const cases = " + json.dumps(cases) + ";\n"
          "let bad = 0;\n"
          "for (const [a, b, exp] of cases) {\n"
          "  const got = cmpExtVer(a, b);\n"
          "  if (got !== exp) { bad++; console.error('FAIL', JSON.stringify(a), JSON.stringify(b),\n"
          "    'got', got, 'want', exp); }\n"
          "}\n"
          "if (bad) process.exit(1);\n"
          "console.log('cmp ok');\n")
    r = subprocess.run([node, "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, "cmpExtVer 语义不符：\n" + r.stdout + r.stderr


def test_update_hint_gated_by_cmp():
    """调用点：提示升级前必须过 cmpExtVer，不能只判「不相等」。"""
    src = _read_src()
    assert "if (!cmpExtVer(pkgVer, installed))" in src, (
        "更新提示没有用 cmpExtVer 把关 —— 已装更高版本时会被提示「更新」，"
        "点下去等于降级（回归 2026-10-01 的 bug）")
    # 旧写法不得复活
    assert "if (pkgVer === '?' || pkgVer === installed) return; // 拿不到版本不误报" in src, \
        "版本未知/相等时的短路判断被改动"
    # 「已装比内置新」时必须给用户明说，而不是沉默或误报
    assert "比 App 内置的 v" in src, "缺「你的扩展比 App 内置更新」的说明文案"


def test_no_string_inequality_gate():
    """回归守卫：不得再出现「仅凭 !== 就决定升级」的写法。"""
    src = _read_src()
    bad = re.search(r"if \(pkgVer === '\?' \|\| pkgVer === installed\) \{\s*"
                    r"extBanner\.className = 'vdl-sniff-extbanner warn';", src)
    assert not bad, "更新横幅又回到了「只判不相等」的旧逻辑"


if __name__ == "__main__":
    test_version_cmp_semantics()
    test_update_hint_gated_by_cmp()
    test_no_string_inequality_gate()
    print("🎉 扩展版本比对守卫测试全部通过")
