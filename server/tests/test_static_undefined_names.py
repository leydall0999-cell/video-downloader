#!/usr/bin/env python3
"""静态守卫：全量扫描 server/ 是否存在「运行时才炸」的未定义名（undefined name）。

为什么必须有这个测试
--------------------
本项目已**两次**栽在同一类缺陷上：模块用了某个名字却没 import，
`py_compile` / `compileall` 只查语法，**完全抓不到**；单元测试若没覆盖到那条分支，
也照样全绿。缺陷只在生产环境被真实调用时才炸，而且往往被 `except Exception` 吞掉，
对外只留下一个毫无信息量的提示。

实例（真实事故）：
  * `server/routers/system.py` 用了 `json.loads` 却漏 `import json`
    → 自动更新检测整体失效。
  * `server/routers/system.py` 的 `_prepare_update()` 全量分支用了 `shutil`
    却漏 `import shutil`（只有同模块 `_apply_delta()` 内部局部 import 过）
    → 「检查更新」下载完 313MB 后必定报「更新准备失败」，
      且因为异常被 `except Exception: return None` 吞掉，排查成本极高。
      更隐蔽的是：**增量分支不碰 shutil，所以增量更新一直是好的**，
      掩盖了全量分支的致命问题。

判定口径
--------
只把 pyflakes 的 `undefined name` 当作失败（这是唯一能确定性抓出上述缺陷的类别）。
未使用导入、重定义等风格类告警**不**参与，避免噪音导致守卫被绕过。

依赖
----
需要 `pyflakes`。构建 venv（.build_venv）已预装；缺失时**大声跳过**而非静默通过，
以便一眼看出守卫此刻并未生效。
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    here = Path(__file__).resolve()
    server_dir = here.parent.parent          # server/tests/x.py -> server/
    if not server_dir.is_dir():
        print("❌ 找不到 server 目录：%s" % server_dir)
        return 1

    try:
        from pyflakes import __version__ as pf_version
        from pyflakes.api import checkRecursive
        from pyflakes.reporter import Reporter
    except ImportError:
        print("⚠️ 未安装 pyflakes —— 静态未定义名守卫【本次未生效】。")
        print("   安装：%s -m pip install pyflakes" % sys.executable)
        print("   （构建 venv 通常已自带；此处按『跳过』处理，不阻断构建。）")
        return 0

    class _Collect(Reporter):
        """收集所有告警，不直接落盘打印。"""

        def __init__(self) -> None:
            super().__init__(sys.stdout, sys.stderr)
            self.messages: list[tuple[str, str]] = []

        def unexpectedError(self, filename, msg):      # noqa: N802
            self.messages.append((str(filename), "unexpected error: %s" % msg))

        def syntaxError(self, filename, msg, lineno, offset, text):  # noqa: N802
            self.messages.append((str(filename), "syntax error: %s" % msg))

        def flake(self, message):                      # noqa: N802
            self.messages.append((str(getattr(message, "filename", "?")), str(message)))

    print("▶ pyflakes %s 扫描 %s" % (pf_version, server_dir))
    rep = _Collect()
    checkRecursive([str(server_dir)], rep)

    undefined = [(f, m) for f, m in rep.messages if "undefined name" in m]
    if undefined:
        print("\n❌ 发现 %d 处『未定义名』（会在运行时抛 NameError）：" % len(undefined))
        for f, m in sorted(undefined):
            rel = f
            try:
                rel = str(Path(f).relative_to(server_dir.parent))
            except ValueError:
                pass
            print("   %s: %s" % (rel, m))
        print("\n   修法：把缺的 import 补到模块顶部（不要只在某个函数里局部 import 了事，")
        print("        同模块其它函数用同一个名字时仍会炸）。")
        return 1

    print("✅ 未发现『未定义名』（扫描告警总数 %d，其中未定义名 0）" % len(rep.messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
