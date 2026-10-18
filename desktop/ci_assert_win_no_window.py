"""CI 真机断言：Windows 上「子进程不弹黑窗」补丁确实生效。

2026-10-18 事故：GUI 进程 fork 控制台程序（aria2c / ffmpeg / deno）时，Windows 会
为子进程新建黑框窗口，用户侧表现为「一开下载就连弹好几个黑窗，关不掉还越来越多」。

headless runner 看不到窗口，本地开发机又是 macOS，谁都证明不了修复生效 —— 只能等
用户反馈。这个文件就是补上这块：跑在 windows-latest 上，把能断言的部分全部钉死。

⚠️ 为什么写成独立文件而不是 workflow 里的 heredoc：
YAML block scalar 里每一行都带缩进，而 bash heredoc 的终止符必须在**行首**
（`<<-` 只吃 tab、不吃空格），缩进的 `PY` 不会被识别 ⇒ 脚本读到 EOF 直接语法错，
表现为「步骤 0 秒失败」。本文件由 workflow 直接 `python <本文件>` 调用，没有这个坑。

退出码 0 = 通过；非 0 = 断言失败（CI 会红）。
"""
from __future__ import annotations

import os
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000


def main() -> int:
    if os.name != "nt":
        print(f"SKIP: 非 Windows（os.name={os.name}），本断言只在 windows-latest 上生效")
        return 0

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo, "server"))
    import win_no_window

    # 1) 补丁必须真装上（POSIX 上必须为 False，否则 creationflags 会抛 ValueError）
    assert win_no_window.install() is True, "Windows 上 install() 必须返回 True"
    assert getattr(subprocess.Popen, "_vdl_no_window", False) is True, "补丁标记未置位"

    # 2) 真起一个子进程：creationflags 非法会在 Windows 上直接抛错，跑通即证明可用
    p = subprocess.Popen([sys.executable, "-c", "print(1)"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 0, f"子进程非零退出 rc={p.returncode} err={err!r}"
    assert out.strip() == b"1", f"子进程输出异常: {out!r}"

    # 3) 复验标志真的被注入（只测 install 返回 True 是弱断言）
    class _F:
        def __init__(self, *a, **k):
            self.k = k

    win_no_window.patch_popen(_F)
    assert _F(["x"]).k.get("creationflags") == CREATE_NO_WINDOW, "CREATE_NO_WINDOW 未注入"
    assert win_no_window.patch_popen(_F) is False, "补丁必须幂等"

    # 4) 源码必须被 app.py 引用，否则模块根本不进包，前面全绿也等于没修
    with open(os.path.join(repo, "server", "app.py"), encoding="utf-8") as f:
        src = f.read()
    assert "win_no_window.install()" in src, "app.py 未调用 install()，补丁不会生效"

    print("OK: Windows 无窗口补丁真机断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
