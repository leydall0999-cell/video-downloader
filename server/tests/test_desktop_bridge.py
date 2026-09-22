#!/usr/bin/env python3
"""桌面桥「选择文件」类型白名单回归测试（纯离线，不弹任何系统对话框）。

背景（2026-09-11 线上问题）：
    高效压缩视图点「添加文件」弹的是「选择要桥接的视频/音频文件」，图片全部置灰选不了
    —— 因为 `VdlApi.choose_files()` 把扩展名与提示语写死成视频/音频。
修复：给它加 `kind` 参数（media 默认 / image / any），前端压缩传 'any'、图片转换传 'image'。

本测试用 monkeypatch 拦掉 subprocess.run（不真的调 osascript），
只检查生成的 AppleScript 命令行内容，因此可安全在 CI/沙盒里跑。
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DESKTOP = REPO / "desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import desktop_launcher as dl  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


class _FakeProc:
    stdout = ""
    stderr = ""


def capture_script(kind, call_without_arg=False):
    """调用 choose_files 并返回它生成的 osascript -e 参数列表。"""
    captured = {}

    def fake_run(cmd, capture_output=False, text=False, timeout=None, **kw):
        captured["env"] = kw.get("env") or {}
        captured["cmd"] = list(cmd)
        return _FakeProc()

    real_run = subprocess.run
    subprocess.run = fake_run
    try:
        api = dl.VdlApi()
        if call_without_arg:
            api.choose_files()
        else:
            api.choose_files(kind)
    finally:
        subprocess.run = real_run

    cmd = captured.get("cmd") or []
    # 期望形态：osascript -e <line1> -e <line2> ...
    lines = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-e"]
    return cmd, lines


def exts_of(first_line):
    inner = first_line.split("of type ", 1)[1].split(" with", 1)[0]
    return [e.strip().strip('"') for e in inner.strip("{}").split(",")]


def _direct_save_section():
    """「直接保存到本机」：必须先弹系统保存位置面板，再往用户选的地方落盘。

    背景（2026-09-22 用户报）：点了直存不弹任何位置选择框，直接塞进「下载」文件夹；
    用户要求「也要弹出来可选择保存的位置弹窗」，与去水印/文案/二维码三处保存一致。

    全程拦掉 subprocess.run 与 requests，不真的调 osascript、不真的下载。
    """
    import tempfile
    import types

    import os
    # ---- applet 源码级棘轮（2026-09-22 用户报「面板是英文」的根治点）----
    src = dl._SAVE_PANEL_APPLET_SRC
    check("applet 源码用 choose file name 弹面板", "choose file name" in src, src[:60])
    check("applet 源码兼容 CR 换行（do shell script 会把 LF 转成 CR，只按 linefeed 切会永远读不到入参）",
          "text item delimiters to return" in src, "")
    check("applet 源码默认位置为「下载」文件夹", "path to downloads folder" in src, "")
    launcher_src = Path(dl.__file__).read_text(encoding="utf-8")
    check("applet 声明中文本地化（缺了面板仍按英文渲染）",
          "CFBundleLocalizations" in launcher_src and "zh-Hans" in launcher_src, "")

    tmpdir = Path(tempfile.mkdtemp(prefix="vdl_direct_save_test_"))
    chosen = str(tmpdir / "我选的目录" / "我的视频.mp4")
    captured = {}

    real_run = subprocess.run
    real_ensure = dl._ensure_save_panel_applet
    real_sp_dir = dl._save_panel_dir
    fake_applet = str(tmpdir / "SavePanel.app")
    (tmpdir / "SavePanel.app").mkdir(parents=True, exist_ok=True)
    # 入参/结果都写进临时目录，绝不碰用户真实的 ~/.video-downloader/save_panel
    dl._save_panel_dir = lambda: str(tmpdir)
    dl._ensure_save_panel_applet = lambda *a, **kw: fake_applet

    # ---- 第一层：中文本地化 applet（面板全中文；用户报的「英文替换提示」根治点）----
    def fake_run_applet(cmd, capture_output=False, text=False, timeout=None, **kw):
        captured["cmd"] = list(cmd)
        if cmd and cmd[0] == "open":
            parts = (tmpdir / "in.txt").read_text(encoding="utf-8").splitlines()
            captured["in_parts"] = parts
            (tmpdir / "out.txt").write_text(f"{parts[0]}\n{chosen}\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    subprocess.run = fake_run_applet
    try:
        got = dl._choose_save_path("保存视频到", "我的视频.mp4", "")
    finally:
        subprocess.run = real_run

    check("面板经 applet 调起（open -W）",
          captured.get("cmd", [None, None])[:2] == ["open", "-W"], captured.get("cmd", [])[:2])
    check("目标就是中文本地化的 SavePanel.app",
          str(captured.get("cmd", ["", "", ""])[2]).endswith("SavePanel.app"), captured.get("cmd", []))
    parts = captured.get("in_parts") or []
    check("入参第 1 行是随机数（识别本次结果）", len(parts) > 0 and len(parts[0]) > 8, parts[:1])
    check("入参第 2 行是提示语", len(parts) > 1 and parts[1] == "保存视频到", parts)
    check("入参第 3 行是默认文件名", len(parts) > 2 and parts[2] == "我的视频.mp4", parts)
    check("applet：返回用户选定的绝对路径", got == chosen, got)

    def fake_run_applet_cancel(cmd, capture_output=False, text=False, timeout=None, **kw):
        if cmd and cmd[0] == "open":
            parts = (tmpdir / "in.txt").read_text(encoding="utf-8").splitlines()
            (tmpdir / "out.txt").write_text(f"{parts[0]}\nCANCELLED\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    subprocess.run = fake_run_applet_cancel
    try:
        got_cancel_applet = dl._choose_save_path("保存视频到", "x.mp4", "")
    finally:
        subprocess.run = real_run
    check("applet：取消 → 'CANCELLED'", got_cancel_applet == "CANCELLED", got_cancel_applet)

    def fake_run_applet_dead(cmd, capture_output=False, text=False, timeout=None, **kw):
        if cmd and cmd[0] == "open":
            try:
                (tmpdir / "out.txt").unlink()
            except OSError:
                pass
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=0, stdout=chosen + "\n", stderr="")

    subprocess.run = fake_run_applet_dead
    try:
        got_applet_fallback = dl._choose_save_path("保存视频到", "x.mp4", "")
    finally:
        subprocess.run = real_run
    check("applet 没产出结果 → 回落 osascript 仍拿到路径（不误判成取消）",
          got_applet_fallback == chosen, got_applet_fallback)

    # ---- 第二层：applet 不可用 → 回落 osascript（功能一致，面板文案是英文）----
    dl._ensure_save_panel_applet = lambda *a, **kw: ""
    captured.clear()

    def fake_run(cmd, capture_output=False, text=False, timeout=None, **kw):
        captured["env"] = kw.get("env") or {}
        captured["cmd"] = list(cmd)
        # 脚本文件是临时文件，必须在调用期间读（调用返回后会被删掉）
        try:
            captured["script"] = Path(cmd[1]).read_text(encoding="utf-8")
        except Exception as exc:  # pragma: no cover
            captured["script"] = f"<读不到脚本: {exc}>"
        return types.SimpleNamespace(returncode=0, stdout=chosen + "\n", stderr="")

    subprocess.run = fake_run
    try:
        got_os = dl._choose_save_path("保存视频到", "我的视频.mp4", "")
    finally:
        subprocess.run = real_run

    check("回落路径：面板被调起（osascript）", captured.get("cmd", [None])[0] == "osascript",
          captured.get("cmd", [])[:1])
    check("回落路径：返回选定路径", got_os == chosen, got_os)
    script = captured.get("script", "")
    check("用 choose file name（选保存位置）", "choose file name" in script, script[:80])
    check("提示语进脚本（中文经 argv 会乱码）", "保存视频到" in script, script[:80])
    check("默认文件名进脚本", "我的视频.mp4" in script, script[:80])
    check("默认位置为「下载」文件夹", "path to downloads folder" in script, script[:80])

    # 用户点「取消」：osascript 退出码 1 → 必须返回 CANCELLED（不是空串、不是报错）
    def fake_cancel(cmd, capture_output=False, text=False, timeout=None, **kw):
        return types.SimpleNamespace(returncode=1, stdout="",
                                     stderr="execution error: User canceled. (-128)")

    subprocess.run = fake_cancel
    try:
        got_cancel = dl._choose_save_path("保存视频到", "x.mp4", "")
    finally:
        subprocess.run = real_run
    check("取消面板 → 'CANCELLED'", got_cancel == "CANCELLED", got_cancel)

    # osascript 不可用（没有 GUI/被拦）→ 返回空串，交给调用方兜底，不能抛异常
    def fake_missing(cmd, capture_output=False, text=False, timeout=None, **kw):
        raise FileNotFoundError("osascript not found")

    subprocess.run = fake_missing
    try:
        got_missing = dl._choose_save_path("保存视频到", "x.mp4", "")
    finally:
        subprocess.run = real_run
    check("两级都不可用 → 空串（调用方兜底）", got_missing == "", repr(got_missing))

    # ---- save_direct_url 行为 ----
    api = dl.VdlApi()

    # ① 用户取消：直接返回 CANCELLED，不发起任何请求（不能误触发服务器下载兜底）
    http_calls = []

    class _Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_content(self, chunk_size=0):
            return iter([b"x" * 1024])

    fake_requests = types.ModuleType("requests")

    def fake_get(url, headers=None, stream=False, timeout=None, **kw):
        http_calls.append({"url": url, "headers": headers or {}})
        return _Resp()

    fake_requests.get = fake_get
    real_requests = sys.modules.get("requests")
    sys.modules["requests"] = fake_requests

    real_choose = dl._choose_save_path
    try:
        dl._choose_save_path = lambda *a, **kw: "CANCELLED"
        out_cancel = api.save_direct_url("https://cdn.example.com/v.mp4", "标题.mp4",
                                        "https://www.douyin.com/")
        check("直存：取消 → 返回 CANCELLED", out_cancel == "CANCELLED", out_cancel)
        check("直存：取消时不得发起下载请求", http_calls == [], http_calls)

        # ② 选了位置：落到该位置，且带 Referer/UA（字节系 CDN 缺 Referer 会 403）
        target = tmpdir / "选定" / "片子.mp4"
        dl._choose_save_path = lambda *a, **kw: str(target)
        out1 = api.save_direct_url("https://cdn.example.com/v.mp4", "标题.mp4",
                                   "https://www.douyin.com/", "UA-Test/1.0")
        check("直存：落到用户选定的路径", out1 == str(target), out1)
        check("直存：文件真的写出来了（有内容）",
              target.exists() and target.stat().st_size > 0, target)
        check("直存：自动创建所选目录", target.parent.is_dir(), target.parent)
        check("直存：残留 .part 已清理",
              not (target.parent / (target.name + ".part")).exists())
        check("直存：请求带 Referer（防盗链）",
              http_calls[-1]["headers"].get("Referer") == "https://www.douyin.com/",
              http_calls[-1]["headers"])
        check("直存：请求带 UA", http_calls[-1]["headers"].get("User-Agent") == "UA-Test/1.0",
              http_calls[-1]["headers"])

        out2 = api.save_direct_url("https://cdn.example.com/v.mp4", "标题.mp4",
                                   "https://www.douyin.com/")
        check("直存：同名不覆盖，自动加 (1)", out2 == str(target.with_name("片子(1).mp4")), out2)

        # ③ 文件名清洗：路径分隔符/非法字符不得逃出所选目录，且必须带扩展名
        target2 = tmpdir / "选定2" / "占位.mp4"
        dl._choose_save_path = lambda *a, **kw: str(target2)
        out3 = api.save_direct_url("https://cdn.example.com/v.mp4", "../a/b:c*?.mp4")
        check("直存：文件名非法字符被清洗", "/" not in Path(out3).name and ":" not in Path(out3).name,
              out3)
        check("直存：清洗后仍在所选目录内", Path(out3).parent == target2.parent, out3)

        # ④ 源站 4xx → 报错文本（前端据此回落服务器下载），且不留下半截文件
        class _Bad:
            status_code = 403

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        fake_requests.get = lambda *a, **kw: _Bad()
        target3 = tmpdir / "选定3" / "x.mp4"
        dl._choose_save_path = lambda *a, **kw: str(target3)
        out4 = api.save_direct_url("https://cdn.example.com/v.mp4", "x.mp4",
                                   "https://www.douyin.com/")
        check("直存：403 如实报错（含状态码）", out4.startswith("ERROR:") and "403" in out4, out4)
        check("直存：失败不留半截文件", not target3.exists(), target3)

        # ⑤ 非法直链直接拒绝
        out5 = api.save_direct_url("javascript:alert(1)", "x.mp4")
        check("直存：非 http(s) 直链被拒", out5.startswith("ERROR:"), out5)
    finally:
        dl._choose_save_path = real_choose
        dl._ensure_save_panel_applet = real_ensure
        dl._save_panel_dir = real_sp_dir
        if real_requests is not None:
            sys.modules["requests"] = real_requests
        else:  # pragma: no cover
            sys.modules.pop("requests", None)
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    print("▶ 桌面桥文件选择：kind 类型白名单")

    # 1) 默认（不传参）= media，向后兼容旧前端/旧行为
    cmd, lines = capture_script("media", call_without_arg=True)
    check("默认调用产生 osascript -e 序列", cmd and cmd[0] == "osascript", cmd[:1])
    check("默认调用是老提示语（桥接）", "桥接" in lines[0], lines[0][:60])
    default_exts = exts_of(lines[0])
    check("默认含视频 mp4", "mp4" in default_exts)
    check("默认不含图片 png（压缩视图曾因此选不了图）", "png" not in default_exts)

    # 2) kind='image' → 只要有图片，且没有任何视频/音频；且全部是后端可解析的位图
    _, lines = capture_script("image")
    img_exts = exts_of(lines[0])
    check("image 含 png/jpg/webp", {"png", "jpg", "jpeg", "webp"} <= set(img_exts), img_exts)
    check("image 含 bmp/tif/gif（图片转换视图 UPLOAD_IMAGE_EXTS）", {"bmp", "tif", "tiff", "gif"} <= set(img_exts), img_exts)
    check("image 不含 mp4/mp3", not ({"mp4", "mp3"} & set(img_exts)), img_exts)
    check("image 不含 Pillow 开不了的 heic/avif", not ({"heic", "avif"} & set(img_exts)), img_exts)
    check("image 提示语为「图片」", "图片" in lines[0], lines[0][:60])

    # 3) kind='any' → 视频+音频+可压缩图片（高效压缩视图）
    _, lines = capture_script("any")
    any_exts = exts_of(lines[0])
    check("any 含视频+音频+图片", {"mp4", "mp3", "png", "jpg"} <= set(any_exts), any_exts)
    check("any 覆盖全部 media 扩展名", set(default_exts) <= set(any_exts))
    check("any 提示语含三种类型", all(k in lines[0] for k in ("视频", "音频", "图片")), lines[0][:80])
    # 关键守卫：压缩后端只吃 png/jpg/jpeg/webp，其余图片格式不能出现在对话框里
    check("any 不含压缩后端不支持的图（heic/avif/bmp/tif/gif）",
          not ({"heic", "avif", "bmp", "tif", "tiff", "gif"} & set(any_exts)), any_exts)
    check("any 的图片部分恰为 png/jpg/jpeg/webp",
          {e for e in any_exts if e not in set(default_exts)} == {"png", "jpg", "jpeg", "webp"},
          sorted({e for e in any_exts if e not in set(default_exts)}))

    # 3b) kind 大小写不敏感（'IMAGE'/'Any' 也要正常命中）
    _, lines_upper = capture_script("IMAGE")
    check("kind='IMAGE' 大小写不敏感命中 image", "png" in exts_of(lines_upper[0]), exts_of(lines_upper[0]))

    # 4) 未知/异常 kind 回退 media（前端传错不能把用户困在空筛选里）
    for bad in ("", "bogus", None):
        try:
            _, lines_bad = capture_script(bad, call_without_arg=(bad is None))
        except Exception as exc:  # pragma: no cover
            check(f"kind={bad!r} 不应抛异常", False, repr(exc))
            continue
        bad_exts = exts_of(lines_bad[0])
        check(f"kind={bad!r} 回退到 media 扩展名", "mp4" in bad_exts and "png" not in bad_exts, bad_exts)

    # 5) 脚本结构守卫：choose file 必须与 of type 同行；语句用独立 -e 传（真换行）
    _, lines = capture_script("any")
    check("choose file 与 of type 同一行", "choose file of type" in lines[0], lines[0][:50])
    check("带 multiple selections allowed", "multiple selections allowed" in lines[0])
    check("语句拆成多个 -e（不用内嵌换行）", len(lines) >= 5 and not any("\n" in ln for ln in lines), len(lines))
    check("末尾 return out", lines[-1].strip() == "return out", lines[-1])

    print("")
    print("▶ 桌面桥「直接保存到本机」：保存位置面板 + 落盘行为")
    _direct_save_section()

    print("")
    print("=========================================")
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
