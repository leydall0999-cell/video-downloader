"""tests/test_win_version_guard.py —— Win7/8/8.1 启动拦截守卫。

背景（2026-10-17 用户问「目前支持 win10/11，其他 windows 能不能也支持」）：
结论是不支持、也不建议支持——上游三处硬约束，不是改代码能绕过的：
  ① CPython 3.13 官方只支持 Windows 8.1+（要 Win7 得锁 Python 3.8）；
  ② Edge WebView2 Runtime 109 是最后一个支持 Win7/8/8.1 的版本（2023-01 起新
     SDK 不再支持，且该渲染层已无安全更新）；
  ③ deno 2.x / onnxruntime 等原生依赖均要求 Win10+。
与其让用户装完打不开再来问，不如 `desktop/desktop_launcher.py` 启动即弹窗引导
（详见 `_check_windows_version` 注释）。

launcher 整体 import 会拉 pyobjc / 起 WebView，不能直接 import；本文件用 ast 抽出
目标函数，在带桩的命名空间里离线验证：
  · `_windows_version_major`  版本判定纯函数（Win7/8/8.1→6，Win10/11→10）
  · `_check_windows_version`  拦截行为（拦 Win7/8、放行 Win10/11/非 Windows）
  · 弹窗文案必须带网页版地址（否则被拦的用户无处可去）
外加接线棘轮：main() 必须在起服务线程之前调用检查（fail-fast）。
"""
import ast
import os
import pathlib
import sys

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(SERVER_DIR)
LAUNCHER = os.path.join(REPO, "desktop", "desktop_launcher.py")

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


def _extract(names):
    """ast 只抽出目标函数的源码执行，返回 (命名空间, 命中集合)。

    命名空间预置 os（检查要读 VDL_ALLOW_OLD_WINDOWS 环境变量）；sys 由各用例
    按场景替换成假对象。_launch_log / _show_windows_message_box 由行为用例装桩。
    """
    src = pathlib.Path(LAUNCHER).read_text(encoding="utf-8")
    tree = ast.parse(src)
    ns = {"os": os}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(ast.Module([node], []), LAUNCHER, "exec"), ns)  # noqa: S102
            found.add(node.name)
    return ns, found


class _Ver:
    def __init__(self, major, minor=0, build=0):
        self.major, self.minor, self.build = major, minor, build


class _FakeSys:
    """假 sys：platform 与 getwindowsversion 可控；exit 抛 SystemExit 便于断言。"""

    def __init__(self, platform, version=None, boom=False):
        self.platform = platform
        self._version = version
        self._boom = boom

    def getwindowsversion(self):
        if self._boom:
            raise OSError("simulated failure")
        return self._version

    def exit(self, code=0):
        raise SystemExit(code)


class _NoVerSys:
    """没有 getwindowsversion 属性的假 sys（模拟异常环境）。"""
    platform = "win32"


def test_version_major():
    print("── 版本判定 _windows_version_major ──")
    ns, found = _extract({"_windows_version_major"})
    check("函数存在", "_windows_version_major" in found)
    if "_windows_version_major" not in ns:
        return
    f = ns["_windows_version_major"]
    cases = [
        (_FakeSys("win32", _Ver(6, 1)), 6, "Win7（6.1）"),
        (_FakeSys("win32", _Ver(6, 2)), 6, "Win8（6.2）"),
        (_FakeSys("win32", _Ver(6, 3)), 6, "Win8.1（6.3）"),
        (_FakeSys("win32", _Ver(10, 0)), 10, "Win10（10.0）"),
        (_FakeSys("win32", _Ver(10, 0, 22000)), 10, "Win11（10.0 build 22000，与 Win10 同主版本号）"),
        (_FakeSys("darwin"), None, "macOS → None（不误伤）"),
        (_FakeSys("linux"), None, "Linux → None（不误伤）"),
        (_FakeSys("win32", boom=True), None, "取版本失败 → None（不误伤）"),
        (_NoVerSys(), None, "无 getwindowsversion → None（不误伤）"),
    ]
    for fake, want, why in cases:
        ns["sys"] = fake
        got = f()
        check(f"{why} → {want}", got == want, f"got={got!r}")


def test_block_behavior():
    print("── 拦截行为 _check_windows_version ──")
    ns, found = _extract({"_check_windows_version", "_windows_version_major"})
    check("两个函数都在 launcher 里", {"_check_windows_version", "_windows_version_major"} <= found,
          str(found))
    if "_check_windows_version" not in ns or "_windows_version_major" not in ns:
        return
    check_fn = ns["_check_windows_version"]

    def run(fake_sys, allow=False):
        """跑一次检查，返回 (kind, code, dialogs, logs)：kind ∈ ok/exit。"""
        ns["sys"] = fake_sys
        os.environ.pop("VDL_ALLOW_OLD_WINDOWS", None)
        if allow:
            os.environ["VDL_ALLOW_OLD_WINDOWS"] = "1"
        dialogs, logs = [], []
        ns["_launch_log"] = logs.append
        ns["_show_windows_message_box"] = (
            lambda msg, title="视频工坊": (dialogs.append(msg), True)[1]
        )
        try:
            rc = check_fn()
            return ("ok", rc, dialogs, logs)
        except SystemExit as e:
            return ("exit", e.code, dialogs, logs)
        finally:
            os.environ.pop("VDL_ALLOW_OLD_WINDOWS", None)

    # ── 拦截：Win7 / Win8 / Win8.1 ──
    for ver, why in ((_Ver(6, 1), "Win7"), (_Ver(6, 2), "Win8"), (_Ver(6, 3), "Win8.1")):
        kind, code, dialogs, logs = run(_FakeSys("win32", ver))
        check(f"{why} → 阻止启动（SystemExit）", kind == "exit", f"kind={kind}")
        check(f"{why} → 弹窗已显示", len(dialogs) == 1, f"dialogs={dialogs}")
        check(f"{why} → 弹窗带网页版地址（用户有出路）",
              bool(dialogs) and "hanyuxz.top" in dialogs[0])
        check(f"{why} → 写了启动日志（事后可查）", any("major" in l for l in logs), str(logs))

    # ── 放行：Win10 / Win11 / 非 Windows / 取不到版本 ──
    for ver, why in ((_Ver(10, 0), "Win10"), (_Ver(10, 0, 22000), "Win11")):
        kind, code, dialogs, logs = run(_FakeSys("win32", ver))
        check(f"{why} → 放行（不退出）", kind == "ok", f"kind={kind} code={code}")
        check(f"{why} → 不弹窗", not dialogs)
    kind, code, dialogs, _ = run(_FakeSys("darwin"))
    check("macOS → 放行不弹窗", kind == "ok" and not dialogs)
    kind, code, dialogs, _ = run(_FakeSys("win32", boom=True))
    check("取版本失败 → 放行（不误伤）", kind == "ok" and not dialogs)
    kind, code, dialogs, _ = run(_FakeSys("win32", _Ver(6, 1)), allow=True)
    check("VDL_ALLOW_OLD_WINDOWS=1 → 强制放行（调试逃生口）", kind == "ok" and not dialogs)


def test_wiring():
    print("── 接线棘轮 ──")
    src = pathlib.Path(LAUNCHER).read_text(encoding="utf-8")
    tree = ast.parse(src)
    main_fn = next((n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    check("main() 存在", main_fn is not None)
    if main_fn is None:
        return
    called = any(
        isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
        and isinstance(stmt.value.func, ast.Name) and stmt.value.func.id == "_check_windows_version"
        for stmt in main_fn.body
    )
    check("main() 调用 _check_windows_version()", called)
    # 必须发生在起服务线程之前（fail-fast：别等后端拉起来才弹窗）
    idx_check = idx_thread = None
    for i, stmt in enumerate(main_fn.body):
        seg = ast.get_source_segment(src, stmt) or ""
        if idx_check is None and "_check_windows_version" in seg:
            idx_check = i
        if idx_thread is None and "threading.Thread" in seg:
            idx_thread = i
    check("检查发生在起服务线程之前",
          idx_check is not None and idx_thread is not None and idx_check < idx_thread,
          f"check@{idx_check} thread@{idx_thread}")


def main():
    print("=== Win7/8 启动拦截守卫 ===")
    test_version_major()
    test_block_behavior()
    test_wiring()
    print()
    if FAILS:
        print(f"❌ {FAILS}/{CHECKS} 项未通过")
        return 1
    print(f"✅ Win7/8 启动拦截守卫全部通过（{CHECKS} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
