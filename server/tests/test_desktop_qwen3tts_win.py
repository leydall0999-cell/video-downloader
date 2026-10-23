"""tests/test_desktop_qwen3tts_win.py —— Qwen3-TTS 本地服务在 Windows 上的启动/停止守卫。

背景（2026-10-20 全量普查）：
    用户报「很多个地方点添加文件都不行」，顺查 VdlApi 时发现 `stop_qwen3tts`
    用的是 `pkill -f start_qwen3tts_server.py` —— **pkill 是 POSIX 命令，Windows 没有**。
    调用必然抛 FileNotFoundError，被 except 捕获后返回「停止失败」，而服务仍在跑、
    继续占着 GB 级内存（用户切回 edge-tts 后本地模型白跑）。

    查这对方法时又发现 `start_qwen3tts` 的 venv 候选全是 POSIX 布局 `bin/python`，
    Windows 的 venv 是 `Scripts\\python.exe` —— 即使真装了环境也必然判定
    「没找到 Python 环境」，引擎在 Windows 上根本起不来。所以启动与停止一起修。

修法：
    · stop → `_windows_stop_port_listener(7871)`：netstat 定位 LISTENING PID 再 taskkill。
      按端口而非镜像名，因为 python.exe 有很多个，按名字杀会误伤宿主进程。
    · start → venv 目录按平台切 `Scripts/python.exe`；日志改 %LOCALAPPDATA%。

⚠️ 本文件第 4 组守的是一个**真踩到的坑**：中文 Windows 的 netstat 输出是 GBK，
   `subprocess.run(..., text=True)` 按 UTF-8 解码会在 reader 线程抛
   UnicodeDecodeError → stdout 变空 → 永远判定「未在运行」→ 停止静默失效。
"""
import ast
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAUNCHER = os.path.join(REPO, "desktop", "desktop_launcher.py")


def _read():
    with open(LAUNCHER, encoding="utf-8") as f:
        return f.read()


def _func_node(name):
    for node in ast.walk(ast.parse(_read())):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _load_pure(name):
    node = _func_node(name)
    assert node is not None, f"desktop_launcher.py 里找不到 {name}()"
    mod = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {}
    exec(compile(mod, LAUNCHER, "exec"), ns)  # noqa: S102 - 只执行项目自己的纯函数
    return ns[name]


# ── 一、netstat 解析：只认 LISTENING，按最后一个冒号取端口 ──

SAMPLE_CN = """\r
活动连接\r
\r
  协议  本地地址          外部地址        状态           PID\r
  TCP    127.0.0.1:7871        0.0.0.0:0              LISTENING       12345\r
  TCP    127.0.0.1:7871        127.0.0.1:52344        ESTABLISHED     12345\r
  TCP    0.0.0.0:135           0.0.0.0:0              LISTENING       890\r
"""


def test_parse_finds_listening_pid():
    f = _load_pure("_parse_netstat_listening_pids")
    assert f(SAMPLE_CN, 7871) == {12345}


def test_parse_ignores_non_listening_states():
    """ESTABLISHED 里也有同一端口和 PID，但那是连接不是监听 —— 按它杀会误伤。"""
    f = _load_pure("_parse_netstat_listening_pids")
    raw = "  TCP    127.0.0.1:7871    127.0.0.1:9999    ESTABLISHED    777\r\n"
    assert f(raw, 7871) == set()


def test_parse_handles_ipv6_form():
    f = _load_pure("_parse_netstat_listening_pids")
    raw = "  TCP    [::1]:7871    [::]:0    LISTENING    42\r\n"
    assert f(raw, 7871) == {42}


def test_parse_tolerates_gbk_garbage():
    """GBK 表头按 utf-8 + replace 解出来会是替换字符，不能影响 ASCII 行解析。"""
    f = _load_pure("_parse_netstat_listening_pids")
    raw = "\ufffd\ufffd\ufffd\ufffd\n" + SAMPLE_CN.replace("活动连接", "\ufffd\ufffd")
    assert f(raw, 7871) == {12345}


def test_parse_does_not_match_port_prefix():
    """7871 不能匹配到 78710（否则会杀错进程）。"""
    f = _load_pure("_parse_netstat_listening_pids")
    raw = "  TCP    127.0.0.1:78710    0.0.0.0:0    LISTENING    55\r\n"
    assert f(raw, 7871) == set()


def test_parse_empty_and_garbage_input():
    f = _load_pure("_parse_netstat_listening_pids")
    assert f("", 7871) == set()
    assert f("complete nonsense\n", 7871) == set()


# ── 二、stop_qwen3tts 必须走 Windows 分支 ──

def test_stop_qwen3tts_has_win32_branch():
    src = ast.get_source_segment(_read(), _func_node("stop_qwen3tts")) or ""
    assert 'sys.platform == "win32"' in src, "stop_qwen3tts 缺少 win32 分支"
    assert "_windows_stop_port_listener" in src, "没有走端口定位实现"
    win_at = src.find('sys.platform == "win32"')
    pkill_at = src.find('"pkill"')
    assert pkill_at != -1, "参照点变了：找不到 pkill 调用"
    assert win_at < pkill_at, "win32 分支必须早于 pkill，否则 Windows 仍抛 FileNotFoundError"


# ── 三、start_qwen3tts 的 venv 布局必须随平台切换 ──

def test_start_qwen3tts_uses_platform_venv_layout():
    src = ast.get_source_segment(_read(), _func_node("start_qwen3tts")) or ""
    assert '"Scripts"' in src and "python.exe" in src, (
        "venv 候选没有 Windows 布局（Scripts\\python.exe）—— "
        "Windows 上即使装了环境也会报「没找到 Python 环境」"
    )
    assert '"bin"' in src, "POSIX 布局被删掉了？macOS 会退化成找不到环境"


def test_start_qwen3tts_log_path_not_macos_only():
    src = ast.get_source_segment(_read(), _func_node("start_qwen3tts")) or ""
    assert "LOCALAPPDATA" in src, "日志目录没有 Windows 分支，会在用户目录建出无意义的 Library\\Logs"


# ── 四、棘轮：netstat/taskkill 不能用 text=True（中文 Windows GBK 解码会炸） ──

def test_stop_listener_does_not_use_text_mode():
    raw = _read()
    fn = _func_node("_windows_stop_port_listener")
    src = ast.get_source_segment(raw, fn) or ""

    # 只看真实调用参数，不看注释（源码注释里专门解释了为什么不能用 text=True，
    # 直接文本搜索会误判成「用了」）
    for sub in ast.walk(fn):
        if not isinstance(sub, ast.Call):
            continue
        callee = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
        if callee == "run":
            kw = [k.arg for k in sub.keywords]
            assert "text" not in kw, (
                "subprocess.run(..., text=True) 会把 GBK 输出按 UTF-8 解 → "
                "reader 线程抛 UnicodeDecodeError → stdout 变空 → 永远判定「未在运行」"
            )

    assert 'decode("utf-8", "replace")' in src, "缺少按字节读取+宽松解码"
    assert "taskkill" in src and "/PID" in src, "没有按 PID 结束进程"
    assert "CREATE_NO_WINDOW" in src, "缺 CREATE_NO_WINDOW，桌面 App 里会闪黑框"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
