#!/usr/bin/env python3
"""桌面桥「选择文件」类型白名单回归测试（纯离线，不弹任何系统对话框）。

背景（2026-09-11 线上问题）：
    无损压缩视图点「添加文件」弹的是「选择要桥接的视频/音频文件」，图片全部置灰选不了
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

    def fake_run(cmd, capture_output=False, text=False, timeout=None):
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

    # 3) kind='any' → 视频+音频+可压缩图片（无损压缩视图）
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
    print("=========================================")
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
