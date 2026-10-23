"""tests/test_desktop_choose_files_win.py —— 「添加文件」在 Windows 点了没反应 的回归守卫。

背景（2026-10-20 用户截图实测）：
    桌面版六个视图（视频格式转换 / 音乐转换 / 图片转换 / 高效压缩 / 视频合并 / 高清修复）
    的「添加文件」按钮在 Windows 上**点了毫无反应**：不报错、不弹框、不提示。

根因链路（三段都无提示，所以事后极难查）：
    1. 前端 `desktop.chooseFiles()` → `window.pywebview.api.choose_files()`
    2. Python `VdlApi.choose_files()` 历史实现**只有 macOS 的 osascript 子进程**
       （desktop_launcher.py 里 6 处真调用全是 `["osascript", ...]`）
    3. Windows 上 `osascript` 不存在 → `subprocess.run` 抛 FileNotFoundError →
       被 `except Exception: pass` 吞掉 → `return ""` → 前端 `norm("")` 得 `[]` →
       调用点 `if (list && list.length)` 不成立 → 静默什么都不做

修法：`choose_files` / `pick_voice_sample` 在 win32 分支走
      `_windows_choose_files()`（ctypes 直调 comdlg32.GetOpenFileNameW，零依赖）。

本文件用 ast 抽出纯函数离线验证（launcher 整体 import 会拉起 WebView/pyobjc，不能直接 import）。
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


def _tree():
    return ast.parse(_read())


def _func_node(name):
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _load_pure(name):
    """把单个模块级纯函数抽出来执行，避免 import 整个 launcher。"""
    node = _func_node(name)
    assert node is not None, f"desktop_launcher.py 里找不到 {name}()"
    mod = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {"os": os, "sys": sys}
    exec(compile(mod, LAUNCHER, "exec"), ns)  # noqa: S102 - 只执行项目自己的纯函数
    return ns[name]


# ── 一、多选结果解析（Windows 不返回数组，只往缓冲里写「目录\0文件\0…」） ──

def test_parse_single_file_returns_full_path():
    parse = _load_pure("_parse_openfile_result")
    raw = "C:\\Users\\me\\Videos\\a.mp4\0"
    assert parse(raw) == ["C:\\Users\\me\\Videos\\a.mp4"]


def test_parse_multi_files_join_directory():
    parse = _load_pure("_parse_openfile_result")
    raw = "C:\\Users\\me\\Videos\0a.mp4\0b.mov\0c.mkv\0\0"
    got = parse(raw)
    assert got == [
        os.path.join("C:\\Users\\me\\Videos", "a.mp4"),
        os.path.join("C:\\Users\\me\\Videos", "b.mov"),
        os.path.join("C:\\Users\\me\\Videos", "c.mkv"),
    ]


def test_parse_empty_buffer_returns_empty_list():
    parse = _load_pure("_parse_openfile_result")
    assert parse("") == []
    assert parse("\0") == []
    assert parse("\0\0") == []


def test_parse_keeps_unicode_names():
    parse = _load_pure("_parse_openfile_result")
    raw = "D:\\素材\\视频\0第一集 正片.mp4\0第二集.mov\0\0"
    got = parse(raw)
    assert got == [
        os.path.join("D:\\素材\\视频", "第一集 正片.mp4"),
        os.path.join("D:\\素材\\视频", "第二集.mov"),
    ]


# ── 二、接线棘轮：Windows 分支必须在，且不能被 osascript 抢走 ──

def _win32_branch_line(fn_name):
    """返回「sys.platform == 'win32'」分支里调用 `_windows_choose_files` 的那条 If 行号。

    分支体既可能是 `return _windows_choose_files(...)`（choose_files），
    也可能是「先赋值再判类型返回」（pick_voice_sample 要取首元素），故按分支体源码判定。
    """
    raw = _read()
    fn = _func_node(fn_name)
    for n in ast.walk(fn):
        if not isinstance(n, ast.If):
            continue
        if 'sys.platform == "win32"' not in (ast.get_source_segment(raw, n.test) or ""):
            continue
        body_src = "\n".join(ast.get_source_segment(raw, s) or "" for s in n.body)
        if "_windows_choose_files" in body_src:
            return n.lineno
    return None


def _osascript_call_line(fn_name):
    """返回函数里第一条真正的 `[... "osascript" ...]` 列表字面量所在行号（忽略 docstring/注释）。"""
    fn = _func_node(fn_name)
    for n in ast.walk(fn):
        if isinstance(n, ast.List) and n.elts:
            first = n.elts[0]
            if isinstance(first, ast.Constant) and first.value == "osascript":
                return n.lineno
    return None


def test_choose_files_has_win32_branch_before_osascript():
    src = ast.get_source_segment(_read(), _func_node("choose_files")) or ""
    assert "_windows_choose_files" in src, "choose_files 没有走 Windows 原生对话框"

    win_line = _win32_branch_line("choose_files")
    osa_line = _osascript_call_line("choose_files")
    assert win_line is not None, "choose_files 缺少 win32 → return _windows_choose_files() 分支"
    assert osa_line is not None, "参照点变了：choose_files 里找不到 osascript 调用"
    assert win_line < osa_line, (
        f"win32 分支（第 {win_line} 行）必须在 osascript（第 {osa_line} 行）之前 return，"
        "否则 Windows 仍会落到 osascript 静默失败"
    )


def test_pick_voice_sample_has_win32_branch():
    src = ast.get_source_segment(_read(), _func_node("pick_voice_sample")) or ""
    assert "_windows_choose_files" in src, "pick_voice_sample 在 Windows 上仍会静默返回空串"

    win_line = _win32_branch_line("pick_voice_sample")
    osa_line = _osascript_call_line("pick_voice_sample")
    assert win_line is not None, "pick_voice_sample 缺少 win32 分支"
    assert win_line < osa_line, "pick_voice_sample 的 win32 分支必须早于 osascript"


def test_windows_dialog_uses_real_win32_flags():
    src = ast.get_source_segment(_read(), _func_node("_windows_choose_files")) or ""
    for token in (
        "GetOpenFileNameW",
        "OFN_ALLOWMULTISELECT",   # 不支持多选 → 「可多选」的承诺失效
        "OFN_FILEMUSTEXIST",      # 允许不存在路径 → 后面转码必然 404
        "OFN_EXPLORER",
        "CommDlgExtendedError",   # 不区分「取消」和「真失败」→ 又变成静默
        "CoInitializeEx",         # 未初始化 COM 时对话框可能直接不出
    ):
        assert token in src, f"_windows_choose_files 缺少 {token}"


def test_openfilenamew_struct_fields_complete():
    """结构体字段一个都不能少 —— 少一个 lStructSize 就对不上，对话框直接返回 0。"""
    node = None
    for n in ast.walk(_tree()):
        if isinstance(n, ast.ClassDef) and n.name == "_OPENFILENAMEW":
            node = n
    # 结构体定义在函数内部，ast.walk 也能找到
    assert node is not None, "找不到 _OPENFILENAMEW 结构体定义"
    fields = []
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and getattr(stmt.targets[0], "id", "") == "_fields_":
            fields = [elt.elts[0].value for elt in stmt.value.elts]
    expected = [
        "lStructSize", "hwndOwner", "hInstance", "lpstrFilter", "lpstrCustomFilter",
        "nMaxCustFilter", "nFilterIndex", "lpstrFile", "nMaxFile", "lpstrFileTitle",
        "nMaxFileTitle", "lpstrInitialDir", "lpstrTitle", "Flags", "nFileOffset",
        "nFileExtension", "lpstrDefExt", "lCustData", "lpfnHook", "lpTemplateName",
        "pvReserved", "dwReserved", "FlagsEx",
    ]
    assert fields == expected, f"字段不匹配：{fields}"


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 校验 Win32 结构体尺寸")
def test_struct_size_matches_win32_definition():
    import ctypes
    from ctypes import wintypes

    got = {}

    def _capture(raw, prompt, *, multiple=True):
        return ""

    # 结构体是函数内局部类，无法直接取 → 用同名字段复算（与源码逐字段对齐）
    class _OFW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID), ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", wintypes.LPVOID), ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    got["size"] = ctypes.sizeof(_OFW)
    assert got["size"] == (152 if ctypes.sizeof(ctypes.c_void_p) == 8 else 116), got


# ── 四、其余「选择 / 保存」入口：同一批 osascript-only 缺陷的回归守卫 ──
#
# 2026-10-20 复查（用户追问「很多个地方点添加文件都不行有查一遍吗」）：
# 全量扫 VdlApi 后确认同源缺陷共 3 处 —— choose_folder、choose_extension_dir
# 与模块级 _choose_save_path（后者被 8 个保存方法共用）。三者原先都没有 win32 分支。

def _win32_branch_returns(fn_name, callee):
    """确认函数体里有 `if sys.platform == "win32":`，且该分支内**直接或间接**调用了 callee。

    不强制 `return callee(...)` 的字面形式：`pick_voice_sample` 需要把单选结果的
    list 转成单个字符串（先赋值再判断再 return），这也算正确接线。所以判据是
    「win32 分支的语句子树里出现过该调用」。
    """
    raw = _read()
    fn = _func_node(fn_name)
    assert fn is not None, f"desktop_launcher.py 里找不到 {fn_name}()"
    for n in ast.walk(fn):
        if not isinstance(n, ast.If):
            continue
        cond = ast.get_source_segment(raw, n.test) or ""
        if 'sys.platform == "win32"' not in cond:
            continue
        for stmt in n.body:
            for sub in ast.walk(stmt):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                name = getattr(f, "id", None) or getattr(f, "attr", None)
                if name == callee:
                    return True
    return False


@pytest.mark.parametrize("fn_name,callee", [
    ("choose_files", "_windows_choose_files"),
    ("pick_voice_sample", "_windows_choose_files"),
    ("choose_folder", "_windows_choose_folder"),
    ("choose_extension_dir", "_windows_choose_folder"),
    ("_choose_save_path", "_windows_choose_save_path"),
])
def test_entry_has_win32_branch(fn_name, callee):
    assert _win32_branch_returns(fn_name, callee), (
        f"{fn_name}() 缺少 win32 分支（应 return {callee}(...)）—— "
        "Windows 上会静默返回空串，表现为点了没反应"
    )


def test_choose_extension_dir_no_longer_hard_rejects_windows():
    """回归棘轮：原先写的是 `if sys.platform != "darwin": return ""` 直接把 Windows 挡掉。"""
    raw = _read()
    fn = _func_node("choose_extension_dir")
    body_src = ast.get_source_segment(raw, fn) or ""
    head = body_src.split("try:")[0]
    assert 'sys.platform != "darwin"' not in head or "_windows_choose_folder" in head, (
        "choose_extension_dir 又变回「非 darwin 直接 return \"\"」了"
    )


def test_windows_choose_folder_frees_pidl():
    """SHBrowseForFolderW 返回的 pidl 必须 CoTaskMemFree 归还，否则每次选目录泄漏内存。"""
    src = ast.get_source_segment(_read(), _func_node("_windows_choose_folder")) or ""
    assert "SHBrowseForFolderW" in src, "选目录没有走 Shell 原生 API"
    assert "SHGetPathFromIDListW" in src, "没把 pidl 转成路径"
    assert "CoTaskMemFree" in src, "pidl 未释放 → 内存泄漏"
    assert "BIF_RETURNONLYFSDIRS" in src, "缺少「只返回目录」标志（会允许选文件）"


def test_windows_choose_save_path_uses_save_dialog():
    src = ast.get_source_segment(_read(), _func_node("_windows_choose_save_path")) or ""
    assert "GetSaveFileNameW" in src, "另存为没有走 Win32 保存对话框"
    assert "CANCELLED" in src, "取消语义必须与 macOS 侧一致（前端据此区分取消/失败）"
    assert "OFN_OVERWRITEPROMPT" in src, "缺少覆盖确认，会静默覆盖同名文件"


def test_openfilenamew_struct_is_shared():
    """打开/保存共用一份结构定义，避免两份字段漂移导致读写错位。"""
    raw = _read()
    assert raw.count("class _OPENFILENAMEW") == 1, (
        "OPENFILENAMEW 结构体被定义了多份 —— 字段一旦不同步，"
        "对话框会「能弹但返回值错乱」，这类 bug 极难从现象反推"
    )
    files_src = ast.get_source_segment(raw, _func_node("_windows_choose_files")) or ""
    save_src = ast.get_source_segment(raw, _func_node("_windows_choose_save_path")) or ""
    assert "_openfilenamew_struct()" in files_src
    assert "_openfilenamew_struct()" in save_src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
