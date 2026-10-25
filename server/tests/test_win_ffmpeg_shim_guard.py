"""tests/test_win_ffmpeg_shim_guard.py —— Windows 打包「ffmpeg shim」陷阱守卫。

背景（2026-10-26 真机验收连挂两轮，user 反馈「做 Windows 版老是卡住」的真凶）：
CI 上 PATH 里的 ffmpeg 是 Chocolatey **shim**（转发器，几 KB），靠相对路径
``..\\lib\\ffmpeg\\tools\\ffmpeg\\bin\\ffmpeg.exe`` 找真身。构建时若直接
``cp "$(command -v ffmpeg)"`` 把 shim 拷进包 ⇒ 相对路径在包内失效 ⇒
守门实跑 ``--version`` 报 ``Cannot find file at '..\\lib\\ffmpeg\\...'`` ⇒
**整轮中止，安装包根本产不出来**。

第一版修复（e3e7895）仍然被拦：排除规则写成小写 ``*/chocolatey/bin/*``，
而真实路径是 **大写** ``/c/ProgramData/Chocolatey/bin/ffmpeg`` ⇒ 不匹配，
shim 在原地能跑通 ``-version`` ⇒ 顺利通过校验返回（0f7d180 才修掉）。

本文件不是文本 grep，而是**把 build_win.sh 里的 _resolve_real_bin 原样抽出来
在假目录树上真跑一遍**，确保两件事永不退化：
  ① PATH 分支对 chocolatey/bin 的排除必须**大小写不敏感**；
  ② 优先命中 Chocolatey lib 下的真身，而不是 PATH 上先撞见的 shim。

注意：用 tr 转小写而非 ``${var,,}`` —— 后者是 bash 4+ 语法，macOS 自带
bash 3.2 不支持，会导致本机跑不了这个守卫（CI 的 Git Bash 虽支持但不可移植）。
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(SERVER_DIR)
BUILD_WIN = os.path.join(REPO, "desktop", "build_win.sh")

FAILS = 0
CHECKS = 0


def check(desc, cond, extra=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print(f"  \u2705 {desc}")
    else:
        FAILS += 1
        print(f"  \u274c {desc} | {extra}")


def extract_function(src: str, name: str) -> str:
    """按花括号配平抽出 `_resolve_real_bin() { ... }`（不执行脚本其余部分）。"""
    m = re.search(rf"^{re.escape(name)}\(\)\s*\{{", src, re.M)
    if not m:
        return ""
    start = m.start()
    depth = 0
    for i in range(m.end() - 1, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return ""


def make_fake_bin(path: pathlib.Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def main():
    global FAILS
    src = pathlib.Path(BUILD_WIN).read_text(encoding="utf-8")
    # 静态断言只看**可执行代码**：这些字符串在注释里也有（正是事故说明本身），
    # 不剥注释会误报（首跑已踩：两处均在 # 注释行里）。
    code = "\n".join(
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    )

    # —— 静态层：源码里不许再出现「无脑 cp PATH 上的 ffmpeg」——
    check("build_win.sh 存在", os.path.isfile(BUILD_WIN))
    check(
        "不再直接 cp PATH 上的 ffmpeg（必须经 _resolve_real_bin）",
        '_resolve_real_bin ffmpeg' in code
        and not re.search(r'cp\s+"\$\(command -v ffmpeg\)"', code),
        "出现 cp \"$(command -v ffmpeg)\" ⇒ shim 会再进包",
    )
    check(
        "不使用 bash 4+ 的 ${var,,}（macOS bash 3.2 跑不了守卫）",
        "${cand,,}" not in code,
        "改用 tr '[:upper:]' '[:lower:]'",
    )
    check(
        "chocolatey/bin 的排除是大小写不敏感（转小写后再 case）",
        "tr '[:upper:]' '[:lower:]'" in code or 'tr "[:upper:]"' in code,
        "写成小写字面量 ⇒ 大写 Chocolatey 会漏网（第二轮就是这么挂的）",
    )

    fn = extract_function(src, "_resolve_real_bin")
    if not fn:
        check("抽出 _resolve_real_bin 函数体", False, "未找到函数定义")
        return

    # —— 功能层：把真函数放进假目录树跑一遍 ——
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_shim_"))
    try:
        root = tmp / "ProgramData" / "Chocolatey"   # 故意用**大写**，复现真实环境
        # shim：放在 bin/ 下（原地也能跑通 -version，正是它蒙混过关的原因）
        make_fake_bin(root / "bin" / "ffmpeg", "#!/bin/sh\necho 'ffmpeg version fake-shim'\n")
        make_fake_bin(root / "bin" / "ffprobe", "#!/bin/sh\necho 'ffprobe version fake-shim'\n")
        # 真身：放在 lib/<pkg>/tools/... 下（shim 相对目标）
        real_ff = root / "lib" / "ffmpeg" / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
        real_fp = root / "lib" / "ffmpeg" / "tools" / "ffmpeg" / "bin" / "ffprobe.exe"
        make_fake_bin(real_ff, "#!/bin/sh\necho 'ffmpeg version REAL'\n")
        make_fake_bin(real_fp, "#!/bin/sh\necho 'ffprobe version REAL'\n")

        harness = (
            "#!/bin/bash\n"
            "set -u\n"
            "REPO=/nonexistent-repo\n"
            f"{fn}\n"
            "echo \"FF=$(_resolve_real_bin ffmpeg '')\"\n"
            "echo \"FP=$(_resolve_real_bin ffprobe '')\"\n"
        )
        hp = tmp / "harness.sh"
        hp.write_text(harness, encoding="utf-8")
        hp.chmod(0o755)

        env = dict(os.environ)
        env["VDL_CHOCO_LIB"] = str(root / "lib")
        # PATH 里只放 shim 目录：模拟「PATH 上先撞见的是 shim」这一真实情形
        env["PATH"] = f"{root / 'bin'}:{env.get('PATH', '')}"

        r = subprocess.run(["/bin/bash", str(hp)], capture_output=True, text=True, env=env)
        out = r.stdout
        got_ff = re.search(r"FF=(\S*)", out)
        got_fp = re.search(r"FP=(\S*)", out)
        ff = got_ff.group(1) if got_ff else ""
        fp = got_fp.group(1) if got_fp else ""

        check("ffmpeg 解析到真身（不是 bin/ 下的 shim）",
              ff.endswith("lib/ffmpeg/tools/ffmpeg/bin/ffmpeg.exe"),
              f"实际={ff!r} stdout={out.strip()[:200]} stderr={r.stderr.strip()[:200]}")
        check("ffprobe 解析到真身（不是 bin/ 下的 shim）",
              fp.endswith("lib/ffmpeg/tools/ffmpeg/bin/ffprobe.exe"),
              f"实际={fp!r}")
        check("解析结果不含 chocolatey/bin（大小写不敏感）",
              "chocolatey/bin" not in ff.lower() and "chocolatey/bin" not in fp.lower(),
              f"ff={ff} fp={fp}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    print(f"\n{CHECKS - FAILS}/{CHECKS} 通过")
    sys.exit(1 if FAILS else 0)
