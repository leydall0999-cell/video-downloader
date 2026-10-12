"""tests/test_launcher_window_guard.py —— 桌面窗口「被导航到应用外就导回」护栏守卫。

背景（2026-10-12 用户实机截图）：App 主窗口整页显示 `{"detail":"…乱码…"}`。
根因是一类**复发两次**的老问题——WKWebView 会把主框架导航到非 UI 资源：
  · 2026-09-21 点「保存二维码」→ 主框架导航到 blob: 图片（那次只修了那一条路径）；
  · 2026-10-12 某条链接回退走 `window.open(url)` → API 地址的 JSON 错误被当文档渲染，
    JSON 无 charset 被按 GBK 解码 → 满屏乱码，用户看不懂也回不去，只能重启。

护栏在 `desktop/desktop_launcher.py`：每次页面加载完成核对窗口地址，不是应用自己的源
就 `load_url()` 导回。判定写成纯函数 `_window_url_is_foreign`，本文件用 ast 抽出它离线验证
（launcher 模块整体 import 会拉 pyobjc / 起 WebView，不能直接 import）。

本文件只测纯函数 + 接线在位（棘轮），不启动窗口。
"""
import ast
import os
import pathlib
import sys

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(SERVER_DIR)
LAUNCHER = os.path.join(REPO, "desktop", "desktop_launcher.py")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

FAILS = 0
CHECKS = 0


def check(desc, cond, extra=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print(f"  ✅ {desc}")
    else:
        FAILS += 1
        print(f"  ❌ {desc} | {extra}")


def _load_pure_funcs():
    """用 ast 只抽出目标函数的源码执行 —— 避开 launcher 的重依赖与副作用。"""
    src = pathlib.Path(LAUNCHER).read_text(encoding="utf-8")
    tree = ast.parse(src)
    ns = {}
    wanted = {"_origin_of", "_window_url_is_foreign"}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(compile(ast.Module([node], []), LAUNCHER, "exec"), ns)  # noqa: S102
            found.add(node.name)
    return ns, found


def test_window_url_is_foreign():
    print("── 主窗口地址判定 ──")
    ns, found = _load_pure_funcs()
    check("两个纯函数都在 launcher 里", {"_origin_of", "_window_url_is_foreign"} <= found,
          str(found))
    if "_window_url_is_foreign" not in ns:
        return
    f = ns["_window_url_is_foreign"]
    app = "http://127.0.0.1:8321"
    stay = [
        ("http://127.0.0.1:8321", "应用根"),
        ("http://127.0.0.1:8321/", "应用根（带斜杠）"),
        ("http://127.0.0.1:8321/index.html", "应用内页"),
        ("http://127.0.0.1:8321/ops-board", "应用内子页"),
        ("http://127.0.0.1:8321/api/tasks/x/file", "同源 API（fetch 目标，不会成为文档）"),
        ("", "空地址（启动期取不到 URL）"),
        ("about:blank", "启动中间态"),
    ]
    leave = [
        ("blob:http://127.0.0.1:8321/uuid", "blob 下载（2026-09-21 真事故形态）"),
        ("file:///Users/x/a.png", "本地文件"),
        ("https://xhslink.cn/o/abc", "站外链接"),
        ("http://8.138.223.3:18890/api/x", "远端服务地址"),
        ("http://127.0.0.2:8321/", "同 host 不同端口"),
    ]
    for url, why in stay:
        check(f"留在应用：{why}", f(url, app) is False, url)
    for url, why in leave:
        check(f"导回：{why}", f(url, app) is True, url)
    # 应用地址本身异常时宁可不动作，也不误伤用户
    check("应用地址异常时不动作（不误伤）", f("https://x.com/", "") is False)
    check("源比较大小写无关", f("http://127.0.0.1:8321/x", "HTTP://127.0.0.1:8321") is False)


def test_launcher_wiring():
    print("── 接线棘轮 ──")
    src = pathlib.Path(LAUNCHER).read_text(encoding="utf-8")
    check("注册了 loaded 事件", "window.events.loaded += _on_loaded" in src)
    check("loaded 回调里核对当前地址", "window.get_current_url()" in src)
    check("离开应用时导回根地址", "window.load_url(URL)" in src)
    check("导回前有防抖（避免 load_url 失败时死循环）", "_last_recover" in src
          and "3.0" in src)
    check("导回动作写启动日志（事后可查）", "已导回应用首页" in src)
    check("回调整体吞异常（绝不让窗口事件抛错打断主循环）",
          src.count("except Exception") >= 3)


def main():
    print("=== 桌面窗口自愈护栏守卫 ===")
    test_window_url_is_foreign()
    test_launcher_wiring()
    print()
    if FAILS:
        print(f"❌ {FAILS}/{CHECKS} 项未通过")
        return 1
    print(f"✅ 窗口自愈护栏守卫全部通过（{CHECKS} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())