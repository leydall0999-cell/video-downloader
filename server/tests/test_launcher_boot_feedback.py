"""桌面启动「静默失败」加固守卫（2026-10-17）。

背景：用户 Windows 装机后打不开，唯一现象是 WebView2 窗口「127.0.0.1 拒绝连接」
（后端未监听成功）。复盘发现 launcher 有两个体验缺陷 + 一个探测缺陷：
  1. 探测缺陷：_find_free_port 用 connect_ex 判断空闲——Windows 的 Hyper-V/WSL 会
     静默保留整段 TCP 端口（excludedportrange），保留段内 connect 连不上被误判
     「空闲」，选中后 uvicorn bind 必失败。改为真 bind 探测。
  2. 后端线程死亡（异常已写日志）后，ready 轮询仍傻等满 240s 才开错误窗口。
     改为循环内检测 server_thread.is_alive()，死了立即 break。
  3. 未就绪时只 print（GUI 双击启动 stdout 不可见 = 静默失败），用户不知道该去哪
     找日志。改为弹窗指明 %USERPROFILE%\\.vdl_launch.log。

本文件三组断言：
  A. _find_free_port 真跑（本地占用/空闲端口，验证 bind 探测语义）；
  B. main() ready 轮询段源码棘轮（线程死亡即时检测 + 弹窗接线）；
  C. 弹窗文案必须带日志文件名（否则用户还是找不到北）。
"""
import ast
import pathlib
import socket
import sys

LAUNCHER = pathlib.Path(__file__).resolve().parents[2] / "desktop" / "desktop_launcher.py"
PASSED = []
FAILED = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{detail}]" if detail and not cond else ""))


src = LAUNCHER.read_text(encoding="utf-8")


def _extract(names):
    """ast 只抽出目标函数的源码执行，返回 (命名空间, 命中集合)。

    注入 socket：_find_free_port 用到它，但 launcher 顶层的 import 不会随 exec 进来。
    """
    tree = ast.parse(src)
    ns = {"socket": socket}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(ast.Module([node], []), str(LAUNCHER), "exec"), ns)  # noqa: S102
            found.add(node.name)
    return ns, found


print("== A. _find_free_port bind 探测语义（真跑）==")
ns, found = _extract({"_find_free_port"})
check("抽出 _find_free_port", "_find_free_port" in found)
finder = ns.get("_find_free_port")

if finder:
    # 基准端口取高位段：既避开默认 8321（本机开发实例常驻），也避开常见服务端口。
    # A1：占用 B 后不得再返回它（connect_ex 时代的失效场景）
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 28321))
        holder.listen(1)
        picked = finder(28321, tries=8)
        check("28321 被占 → 顺延到 28322+", picked > 28321, f"picked={picked}")
    finally:
        holder.close()

    # A2：无占用时仍应首选传入端口（不破坏默认端口）
    free_pick = finder(28421, tries=5)
    check("空闲端口 28421 首选返回", free_pick == 28421, f"free_pick={free_pick}")

    # A3：连占两个也正确顺延（28322/28323 占 → 28324）
    h1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    h2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        h1.bind(("127.0.0.1", 28322)); h1.listen(1)
        h2.bind(("127.0.0.1", 28323)); h2.listen(1)
        picked2 = finder(28322, tries=8)
        check("连占 28322/28323 → 28324", picked2 == 28324, f"picked2={picked2}")
    finally:
        h1.close(); h2.close()

print("== B. main() ready 轮询段源码棘轮 ==")
i_main = src.find("def main() -> None:")
main_body = src[i_main:] if i_main >= 0 else ""
i_wait = main_body.find("_READY_WAIT_SEC = 240")
wait_body = main_body[i_wait:] if i_wait >= 0 else ""
# 只取到轮询段结束（弹窗之后）做范围断言，避免被 main 后半段干扰
i_end = wait_body.find('print(f"服务器启动较慢')
wait_block = wait_body[:i_end] if i_end > 0 else wait_body

check("轮询循环内检测 server_thread.is_alive()",
      "server_thread.is_alive()" in wait_block)
check("线程死亡时写启动日志（提前结束等待）",
      "后端线程已退出" in wait_block)
check("未就绪时调用 _show_windows_message_box",
      "_show_windows_message_box(" in wait_block)
check("未就绪时调用 _show_error_dialog（macOS 生效）",
      "_show_error_dialog(" in wait_block)
check("未就绪时写启动日志（弹窗告知用户）",
      "弹窗告知用户" in wait_block)
check("保留 _launch_log 未就绪记录",
      "_launch_log(" in wait_block)

print("== C. 弹窗文案必须带日志文件名 ==")
check("Windows 弹窗文案含 .vdl_launch.log",
      ".vdl_launch.log" in wait_block)
check("Windows 弹窗文案含 %USERPROFILE%（用户可直接 Win+R 粘贴）",
      "%USERPROFILE%" in wait_block)
check("macOS 弹窗文案含 ~/.vdl_launch.log",
      "~/.vdl_launch.log" in wait_block)

print()
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    for f in FAILED:
        print("  - " + f)
    sys.exit(1)
