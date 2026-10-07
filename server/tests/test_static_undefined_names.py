#!/usr/bin/env python3
"""静态守卫：全量扫描 server/ 与 deploy/ 是否存在「运行时才炸」的未定义名（undefined name）。

为什么必须有这个测试
--------------------
本项目已**三次**栽在同一类缺陷上：模块用了某个名字却没 import，
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
  * `deploy/pay_server.py`（2026-10-08）`_gen_order_id()` 用了 `secrets.choice()`
    却漏 `import secrets` → 每次 `/api/pay/create` 抛 NameError，**支付全程不可用**。
    🔴 该文件在 `deploy/`，而本守卫原先**只扫 `server/`** —— 这正是它漏网的原因。
    现扫描范围已扩到 `deploy/`，把部署侧脚本一并纳入。

判定口径
--------
只把 pyflakes 的 `undefined name` 当作失败（这是唯一能确定性抓出上述缺陷的类别）。
未使用导入、重定义等风格类告警**不**参与，避免噪音导致守卫被绕过。

依赖
----
需要 `pyflakes`。它已写进 `requirements.txt`（构建 venv 会装）；缺失时**大声跳过**
而非静默通过，以便一眼看出守卫此刻并未生效。
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
    repo_dir = server_dir.parent             # 仓库根
    # 🔴 deploy/ 下的部署脚本（pay_server.py / license_server.py / gateway.py …）同样会
    #    「用了名字却没 import」而只在生产炸 —— pay_server 的 `secrets` 事故即属此类。
    scan_dirs = [server_dir]
    deploy_dir = repo_dir / "deploy"
    if deploy_dir.is_dir():
        scan_dirs.append(deploy_dir)

    try:
        from pyflakes import __version__ as pf_version
        from pyflakes.api import checkRecursive
        from pyflakes.reporter import Reporter
    except ImportError:
        # 🔴 缺失依赖一律**判失败**，不再「大声跳过」。
        #    2026-10-08 教训：本守卫原先在 pyflakes 缺失时 return 0 跳过，而 pyflakes
        #    从未写进 requirements.txt ⇒ 守卫在每次构建里都形同虚设、却始终显示「绿」，
        #    于是 pay_server 漏 `import secrets` 的致命缺陷一路混到线上。
        #    pyflakes 已补进 requirements.txt，构建 venv 必然带它；缺失即视为环境异常。
        print("❌ 未安装 pyflakes —— 静态未定义名守卫无法生效。")
        print("   pyflakes 已在 requirements.txt 中声明，构建 venv 应自带。")
        print("   安装：%s -m pip install pyflakes" % sys.executable)
        return 1

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

    print("▶ pyflakes %s 扫描 %s" % (pf_version, " + ".join(str(d) for d in scan_dirs)))
    rep = _Collect()
    checkRecursive([str(d) for d in scan_dirs], rep)

    undefined = [(f, m) for f, m in rep.messages if "undefined name" in m]
    if undefined:
        print("\n❌ 发现 %d 处『未定义名』（会在运行时抛 NameError）：" % len(undefined))
        for f, m in sorted(undefined):
            rel = f
            try:
                rel = str(Path(f).relative_to(repo_dir))
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
