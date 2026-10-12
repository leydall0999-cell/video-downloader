# -*- coding: utf-8 -*-
"""变异测试：逐项注入缺陷，确认测试套件会红（不会绿灯放行回归）。"""
import os
import shutil
import subprocess
import sys
import tempfile

SERVER = "/Users/suixindelang/WorkBuddy/video-downloader-app/server"
# 🔴 必须用 .build_venv 的解释器：base python 缺 requests 等依赖，
# 注入缺陷后「变红」会是 import 失败的假阳性（红灯但原因不对）。
PY = os.path.join(os.path.dirname(SERVER), ".build_venv", "bin", "python")
TEST = "tests/test_image_download.py"

MUTATIONS = [
    ("M1 漏落 filepath=final（完成却取不到文件）",
     "image_download.py", "                 filepath=final)", "                 )"),
    ("M2 非图片内容不再早拒（错误页/伪装流落盘）",
     "image_download.py",
     '                    if ext is None:\n                        raise LinkError(\n                            "该链接返回的不是图片",\n                            "请确认粘贴的是图片直链（右键图片「复制图片地址」）")',
     '                    if ext is None:\n                        ext = ".png"'),
    ("M3 重定向不再逐跳 SSRF（公网入口 302 偷渡内网）",
     "image_download.py",
     "    def redirect_request(self, req, fp, code, msg, headers, newurl):\n        assert_public_url(newurl)",
     "    def redirect_request(self, req, fp, code, msg, headers, newurl):"),
    ("M4 重试不按 kind 分流（图片任务落回 yt-dlp 视频管线）",
     "routers/core.py",
     "    _is_image = getattr(task, 'kind', 'video') == 'image'",
     "    _is_image = False"),
    ("M5 图片任务暂停不再早拒（卡死 pausing 中间态）",
     "routers/core.py",
     "    if getattr(task, 'kind', 'video') == 'image':",
     "    if getattr(task, 'kind', 'video') == 'never':"),
    ("M6 app.py 漏注册 image router（前端 404 单测照样绿）",
     "app.py", "app.include_router(_image_rtr.router)", "pass"),
    ("M7 _sanitize 空值兜底抢先（所有图片被命名成 image.png）",
     "image_download.py",
     '    name = cd_name or url_name or "image"',
     '    name = "image"'),
    ("M8 非 http(s) scheme 不再显式拒绝（data: 偷渡）",
     "image_download.py",
     '    for bad in ("data:", "javascript:", "file:", "ftp:", "blob:", "about:", "vbscript:"):',
     '    for bad in ():'),
]


def run_test() -> tuple[bool, str]:
    env = dict(os.environ)
    env.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_mut_"))
    env.setdefault("VDL_LOGIN_GATE", "0")
    p = subprocess.run([PY, TEST], cwd=SERVER, env=env,
                       capture_output=True, text=True, timeout=600)
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    # 基线自检：未变异必须先全绿。否则后续每一项「变红」都可能是环境问题的
    # 假阳性（曾踩：base python 缺 requests，注入缺陷后 import 即失败，
    # 8 项变异全「如期变红」但原因完全不对）。
    base_ok, base_out = run_test()
    if not base_ok:
        print("❌ 基线自检失败：未变异的测试套件未全绿，本次变异结果全部作废")
        for ln in [l for l in base_out.splitlines() if l.strip()][-10:]:
            print("   ", ln)
        return 1
    print("✅ 基线自检：未变异全绿，开始注入缺陷")
    ok = True
    for name, rel, old, new in MUTATIONS:
        path = os.path.join(SERVER, rel)
        bak = path + ".mutbak"
        shutil.copy2(path, bak)
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            if old not in src:
                print(f"⚠️  {name}: 锚点未命中，跳过（请人工核对）")
                ok = False
                continue
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src.replace(old, new, 1))
            passed, out = run_test()
            if passed:
                print(f"❌ {name}: 注入缺陷后测试仍然全绿 —— 断言没咬住！")
                ok = False
            else:
                bad = [ln for ln in out.splitlines()
                       if ln.startswith("AssertionError") or "AssertionError" in ln]
                why = bad[0].strip()[:70] if bad else "非零退出（未见 AssertionError，需人工核对）"
                if "ModuleNotFoundError" in out or "ImportError" in out:
                    print(f"❌ {name}: 变红原因是 import 失败而非断言 —— 环境问题，结果作废")
                    ok = False
                else:
                    print(f"✅ {name}: 如期变红（{why}）")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  {name}: harness 异常 {e}")
            ok = False
        finally:
            shutil.copy2(bak, path)
            os.unlink(bak)
    # 还原后必须全绿
    passed, out = run_test()
    if not passed:
        tail = [ln for ln in out.splitlines() if ln.strip()][-12:]
        print("---- 还原后失败输出尾部 ----")
        for ln in tail:
            print("   ", ln)
    print(("✅ 还原后全绿" if passed else "❌ 还原后仍红：备份/还原链路有问题"))
    return 0 if (ok and passed) else 1


if __name__ == "__main__":
    sys.exit(main())
