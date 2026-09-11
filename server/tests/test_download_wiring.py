#!/usr/bin/env python3
"""「结果下载」链路回归测试：桌面保存面板 + 前端下载委托接线（纯离线）。

背景（2026-09-11 线上问题）：
    高效压缩列表项点「下载」没反应。两个原因叠加：
      1) `web/app.js` 里 `.uc-item-download` 的点击拦截委托漏挂了压缩列表
         （WKWebView 不弹 `<a download>` 保存框，没被拦截就等于点了没反应）；
      2) 拦截逻辑的 href 正则只匹配 `/api/convert/`，且桌面桥只查
         `app.CONVERT_JOBS`，而压缩任务在 `routers.compress.COMPRESS_JOBS`
         → 即便挂上也只会报「任务不存在或已过期」。

本测试锁住这两侧，避免以后新增列表再犯：
  A. Python 桥 `save_convert_file_dialog` 能命中两个任务注册表，且提示语按类型区分；
  B. `web/app.js` 中每个渲染 `.uc-item-download` 的列表都挂了拦截委托，
     href 正则同时覆盖 convert 与 compress 两类任务。

安全性：A 段 monkeypatch 掉 `subprocess.run`，不弹任何系统窗口；
数据目录用 VDL_DATA_DIR 指向临时目录，绝不写用户家目录。

运行：
    cd server && python tests/test_download_wiring.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "server"
DESKTOP = REPO / "desktop"
for _p in (str(SERVER), str(DESKTOP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 隔离数据目录：必须在 import app 之前设好
_TMPROOT = tempfile.mkdtemp(prefix="vdl_dlwiring_")
os.environ["VDL_DATA_DIR"] = _TMPROOT

import app  # noqa: E402
import routers.compress as compress_mod  # noqa: E402
import routers.sr as sr_mod  # noqa: E402
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
    def __init__(self, rc=1, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def call_bridge(job_id, suggested, dest=None):
    """调 save_convert_file_dialog，拦掉 osascript。

    dest 为空 → 模拟用户取消（rc=1）；否则模拟用户选了 dest（rc=0 + stdout=dest）。
    返回 (桥返回值, 实际喂给 osascript 的脚本内容)。
    """
    captured = {}

    def fake_run(cmd, capture_output=False, text=False, timeout=None, env=None):
        try:
            captured["script"] = Path(cmd[1]).read_text(encoding="utf-8")
        except Exception:
            captured["script"] = ""
        if dest:
            return _FakeProc(0, dest + "\n")
        return _FakeProc(1, "", "User canceled.")

    real_run = subprocess.run
    subprocess.run = fake_run
    try:
        api = dl.VdlApi()
        rv = api.save_convert_file_dialog(job_id, suggested)
    finally:
        subprocess.run = real_run
    return rv, captured.get("script", "")


def test_bridge_resolves_compress_jobs():
    """A1：压缩任务（COMPRESS_JOBS）必须能被保存面板解析到并拷到目标路径。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "compress_out.jpg"
        src.write_bytes(b"compressed-bytes-1234567890")
        dest = Path(td) / "saved" / "[已压缩]photo.jpg"
        compress_mod.COMPRESS_JOBS["cpjob_test1"] = {
            "status": "completed", "out_path": str(src),
            "filename": "[已压缩]photo.jpg",
        }
        rv, script = call_bridge("cpjob_test1", "[已压缩]photo.jpg", dest=str(dest))
        check("压缩任务命中并保存成功", rv == str(dest), rv)
        check("产物真的被拷贝", dest.is_file() and dest.read_bytes() == src.read_bytes(),
              dest if dest.is_file() else "missing")
        check("保存面板提示语为「保存压缩结果」", "保存压缩结果" in script, script[:80])
        check("默认文件名透传（含扩展名）", "[已压缩]photo.jpg" in script, script[:120])
        compress_mod.COMPRESS_JOBS.pop("cpjob_test1", None)


def test_bridge_resolves_sr_jobs():
    """A3：高清修复任务（SR_JOBS）必须能被保存面板解析到（2026-09-12 新增的第三个下载入口）。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "sr_out.png"
        src.write_bytes(b"upscaled-bytes-abcdefg")
        dest = Path(td) / "saved" / "[2x]photo.png"
        sr_mod.SR_JOBS["srjob_test1"] = {
            "status": "completed", "out_path": str(src),
            "filename": "[2x]photo.png",
        }
        rv, script = call_bridge("srjob_test1", "[2x]photo.png", dest=str(dest))
        check("高清修复任务命中并保存成功", rv == str(dest), rv)
        check("产物真的被拷贝", dest.is_file() and dest.read_bytes() == src.read_bytes(),
              dest if dest.is_file() else "missing")
        sr_mod.SR_JOBS.pop("srjob_test1", None)


def test_bridge_still_resolves_convert_jobs():
    """A2：原有转换/桥接任务（app.CONVERT_JOBS）不能被这次改动改坏。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "convert_out.mp4"
        src.write_bytes(b"converted-bytes-abcdefg")
        dest = Path(td) / "saved" / "out.mp4"
        app.CONVERT_JOBS["cvjob_test1"] = {"status": "completed", "out_path": str(src)}
        rv, script = call_bridge("cvjob_test1", "out.mp4", dest=str(dest))
        check("转换任务仍然命中并保存成功", rv == str(dest), rv)
        check("转换产物被拷贝", dest.is_file() and dest.read_bytes() == src.read_bytes())
        check("保存面板提示语为「保存转码结果」", "保存转码结果" in script, script[:80])
        app.CONVERT_JOBS.pop("cvjob_test1", None)


def test_bridge_cancel_and_missing_job():
    """A3：取消不产生文件；未知 job / 产物丢失给出明确错误文案。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "x.mp4"
        src.write_bytes(b"x")
        compress_mod.COMPRESS_JOBS["cpjob_test2"] = {"status": "completed", "out_path": str(src)}
        rv, _ = call_bridge("cpjob_test2", "x.mp4", dest=None)
        check("用户取消返回 CANCELLED", rv == "CANCELLED", rv)
        compress_mod.COMPRESS_JOBS.pop("cpjob_test2", None)

        rv, _ = call_bridge("no_such_job", "x.mp4", dest=str(Path(td) / "y.mp4"))
        check("未知 job 报「任务不存在或已过期」",
              isinstance(rv, str) and rv.startswith("ERROR:") and "任务不存在" in rv, rv)

        compress_mod.COMPRESS_JOBS["cpjob_test3"] = {
            "status": "completed", "out_path": str(Path(td) / "gone.mp4"),
        }
        rv, _ = call_bridge("cpjob_test3", "gone.mp4", dest=str(Path(td) / "z.mp4"))
        check("产物已丢失时报错而非静默失败",
              isinstance(rv, str) and rv.startswith("ERROR:") and "丢失" in rv, rv)
        compress_mod.COMPRESS_JOBS.pop("cpjob_test3", None)


APP_JS_PATH = REPO / "web" / "app.js"
INDEX_HTML_PATH = REPO / "web" / "index.html"

# 渲染出 .uc-item-download 的前端函数 → 它所在的列表容器。
# ⚠️ 新增一个会渲染下载链接的列表时，必须同时补进这张表并在 app.js 里 wire。
EMITTERS = {
    "renderUcList": "el.ucList",
    "mcRender": "mcListEl",
    "musRender": "el.musList",
    "imgRender": "el.imgList",
    "cpRender": "el.cpList",
    "srRender": "el.srList",   # 高清修复（2026-09-12 新增）
}


def _fn_ranges(src):
    """顶层（2 空格缩进）函数/箭头函数的起止位置，用于判断某段代码属于哪个渲染函数。"""
    pat = re.compile(r"\n  (?:const (\w+) = \([^)]*\) => \{|function (\w+)\()")
    marks = [(m.start(), m.group(1) or m.group(2)) for m in pat.finditer(src)]
    out = {}
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(src)
        out[name] = (pos, end)
    return out


def test_frontend_wiring():
    """B1：每个渲染下载链接的列表都必须挂上点击拦截委托（本次 bug 的直接原因）。"""
    src = APP_JS_PATH.read_text(encoding="utf-8")
    ranges = _fn_ranges(src)
    wired = set(m.strip() for m in re.findall(r"wireSaveConvertDownload\(([^)]+)\)", src))

    check("至少存在一份拦截委托", len(wired) >= 1, sorted(wired))
    for fn, container in EMITTERS.items():
        rng = ranges.get(fn)
        check(f"找到渲染函数 {fn}", rng is not None)
        if not rng:
            continue
        body = src[rng[0]:rng[1]]
        check(f"{fn} 确实渲染下载链接", "uc-item-download" in body)
        check(f"{fn} 的容器 {container} 已挂拦截委托", container in wired, sorted(wired))
        check(f"{fn} 的容器 {container} 与 innerHTML 目标一致",
              f"{container}.innerHTML" in body, container)

    # 出现次数守卫：新增渲染点必须显式补进 EMITTERS（否则这里会红，强迫同步更新）
    n_links = src.count('class="uc-item-download"')
    check("下载链接出现次数与已登记渲染函数数一致", n_links == len(EMITTERS),
          f"app.js 里 {n_links} 处，登记 {len(EMITTERS)} 个")

    # 压缩列表容器确实存在于 el 表与 DOM
    check("el 表已注册 cpList", re.search(r"cpList:\s*\$\('cpList'\)", src) is not None)
    check("index.html 存在 id=cpList 的列表",
          'id="cpList"' in INDEX_HTML_PATH.read_text(encoding="utf-8"))
    # 高清修复列表（第三个渲染点）
    check("el 表已注册 srList", re.search(r"srList:\s*\$\('srList'\)", src) is not None)
    check("index.html 存在 id=srList 的列表",
          'id="srList"' in INDEX_HTML_PATH.read_text(encoding="utf-8"))


def test_frontend_href_regex_covers_both():
    """B2：拦截逻辑的 href 正则必须同时认得 convert 与 compress 两类任务链接。"""
    src = APP_JS_PATH.read_text(encoding="utf-8")
    literal = r"/\/api\/(?:convert|compress|sr)\/([^/?#]+)/"
    check("源码内的 href 正则同时覆盖 convert/compress/sr", literal in src)

    pat = re.compile(r"/api/(?:convert|compress|sr)/([^/?#]+)")
    check("convert 链接能解析出 jobId",
          pat.search("/api/convert/abc123/file?device=xyz").group(1) == "abc123")
    check("compress 链接能解析出 jobId",
          pat.search("/api/compress/44e782b29712/file").group(1) == "44e782b29712")
    check("旧正则（只认 convert）确实解析不出压缩链接",
          re.compile(r"/api/convert/([^/?#]+)").search("/api/compress/aa11/file") is None)
    check("sr 链接能解析出 jobId",
          pat.search("/api/sr/9f3c2a71b0de/file").group(1) == "9f3c2a71b0de")
    check("漏了 sr 分支的旧正则确实解析不出高清修复链接",
          re.compile(r"/api/(?:convert|compress)/([^/?#]+)").search("/api/sr/aa11/file") is None)

    # 前端调用的桥方法名必须与 Python 侧一致
    check("前端调用 save_convert_file_dialog", "save_convert_file_dialog" in src)
    check("Python 桥同名方法存在",
          "def save_convert_file_dialog" in (DESKTOP / "desktop_launcher.py").read_text(encoding="utf-8"))

    # 防哑守卫：解析不出 jobId 时必须放行原生行为，不能先 preventDefault 再 return
    # （否则任何漏改的链接都会变成「点了没反应」的死链）
    body_start = src.index("function wireSaveConvertDownload(")
    body = src[body_start:body_start + 3000]
    i_guard = body.find("if (!jobId) return;")
    i_prevent = body.find("e.preventDefault();")
    check("先判 jobId 再 preventDefault（不许吞掉无法处理的点击）",
          0 <= i_guard < i_prevent, f"guard@{i_guard} preventDefault@{i_prevent}")


def main():
    print("▶ 结果下载链路：桌面桥 + 前端委托接线")
    print("\n[A] 桌面桥任务解析")
    test_bridge_resolves_compress_jobs()
    test_bridge_still_resolves_convert_jobs()
    test_bridge_cancel_and_missing_job()
    print("\n[B] 前端接线守卫")
    test_frontend_wiring()
    test_frontend_href_regex_covers_both()

    print("")
    print("=========================================")
    print(f"  通过: {PASS}   失败: {FAIL}")
    print("=========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
