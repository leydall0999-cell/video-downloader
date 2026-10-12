# -*- coding: utf-8 -*-
"""变异测试：逐项注入缺陷，确认测试套件会红（不会绿灯放行回归）。"""
import hashlib
import os
import pathlib
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
    ("M2 页面分支不触发（分享页链接永远识别成「不是图片」）",
     "image_download.py", "                        if _looks_like_html(head, content_type):",
     "                        if False and _looks_like_html(head, content_type):"),
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
     "    # 图片任务（kind=image）：单文件直传、无 .part 续传，暂停没有恢复语义 ——\n"
     "    # 若照样置 pausing，没有任何执行器消费该标志，任务会永久卡在中间态（P2-19 同款坑）。\n"
     "    if getattr(task, 'kind', 'video') == 'image':\n"
     "        return {'task_id': task_id, 'paused': False,",
     "    # 图片任务（kind=image）：单文件直传、无 .part 续传，暂停没有恢复语义 ——\n"
     "    # 若照样置 pausing，没有任何执行器消费该标志，任务会永久卡在中间态（P2-19 同款坑）。\n"
     "    if False:\n"
     "        return {'task_id': task_id, 'paused': False,"),
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
    # —— 2026-10-12 分享页解析（小红书分享链接实测触发）——
    ("M9 Accept-Encoding 退回 identity（WAF 302 去登录墙）",
     "image_download.py", '"Accept-Encoding": "gzip, deflate",',
     '"Accept-Encoding": "identity",'),
    ("M10 UA 与 sec-ch-ua 版本不一致（自相矛盾被识别为机器人）",
     "image_download.py", "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36\")",
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36\")"),
    ("M11 不做 gzip/deflate 解压（魔数嗅到压缩流 → 误判不是图片）",
     "image_download.py", '    if "gzip" in enc:\n        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)\n    elif "deflate" in enc:\n        dec = zlib.decompressobj(-zlib.MAX_WBITS)   # raw deflate',
     '    if False:\n        dec = None'),
    ("M12 页面提取不过滤静态资源（把站点 logo 当笔记配图）",
     "image_download.py", "        if _PAGE_JUNK_RE.search(u):\n            return",
     "        if False:\n            return"),
    ("M13 同图去重不忽略协议头（http/https 同一张图下两次）",
     "image_download.py", '    no_scheme = re.sub(r"^https?://", "", u or "", flags=re.I)\n    return _XHS_TOKEN_RE.sub("", no_scheme)',
     '    return _XHS_TOKEN_RE.sub("", u or "")'),
    ("M14 _fetch_page_text 不再逐跳校验 SSRF（公网入口 302 到内网）",
     "image_download.py", "            assert_public_url(cur)          # 每一跳都过 SSRF 护栏（fail-closed）",
     "            pass"),
    ("M15 登录墙场景不走浏览器指纹兜底（永久 0 候选）",
     "image_download.py", "                            if not images:", "                            if False:"),
    ("M16 执行器不把页面展开成子任务（分享链接拿不到任何图）",
     "image_download.py", "            created.append(child)", "            pass"),
]


def run_test() -> tuple[bool, str]:
    env = dict(os.environ)
    env.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_mut_"))
    env.setdefault("VDL_LOGIN_GATE", "0")
    p = subprocess.run([PY, TEST], cwd=SERVER, env=env,
                       capture_output=True, text=True, timeout=600)
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    # 🔴 原文件内容**全程留在内存**，任何中断/异常都用它写回（不依赖 .mutbak 文件）——
    # 2026-10-12 实测踩过：变异跑完后源码里残留着 `try: pass`（被注入的缺陷代码），
    # 说明「落 bak 文件再 copy 回来」这条链路不够硬：进程被杀/并发跑就会留残根。
    pristine: dict[str, str] = {}
    for _n, _rel, _o, _w in MUTATIONS:
        if _rel not in pristine:
            pristine[_rel] = pathlib.Path(SERVER, _rel).read_text(encoding="utf-8")
    pristine_sha = {rel: _sha(txt) for rel, txt in pristine.items()}

    def _restore_all() -> list[str]:
        """把所有被改过的源文件按内存原样写回，返回仍不一致的文件名。"""
        bad = []
        for rel, txt in pristine.items():
            p = pathlib.Path(SERVER, rel)
            if not p.exists() or _sha(p.read_text(encoding="utf-8")) != pristine_sha[rel]:
                p.write_text(txt, encoding="utf-8")
                if _sha(p.read_text(encoding="utf-8")) != pristine_sha[rel]:
                    bad.append(rel)
        return bad

    # 基线自检：未变异必须先全绿。否则后续每一项「变红」都可能是环境问题的
    # 假阳性（曾踩：base python 缺 requests，注入缺陷后 import 即失败，
    # 8 项变异全「如期变红」但原因完全不对）。
    try:
        base_ok, base_out = run_test()
        if not base_ok:
            print("❌ 基线自检失败：未变异的测试套件未全绿，本次变异结果全部作废")
            for ln in [l for l in base_out.splitlines() if l.strip()][-10:]:
                print("   ", ln)
            return 1
        print("✅ 基线自检：未变异全绿，开始注入缺陷")
        ok = True
        for name, rel, old, new in MUTATIONS:
            path = pathlib.Path(SERVER, rel)
            src = path.read_text(encoding="utf-8")
            # 锚点必须是**完整行且唯一**：否则 12 空格会命中 16 空格行（实测踩过，
            # 变异打到预期之外的代码上，结论全错）。
            old_lines = old.split("\n")
            if src.count(old) != 1 or any(
                    not src.count(ln) for ln in old_lines):
                print(f"⚠️  {name}: 锚点不是唯一整行（命中 {src.count(old)} 次），跳过")
                ok = False
                continue
            mutated = src.replace(old, new, 1)
            if _sha(mutated) == pristine_sha[rel]:
                print(f"⚠️  {name}: 变异后内容与原文件相同（无效变异），跳过")
                ok = False
                continue
            try:
                path.write_text(mutated, encoding="utf-8")
                passed, out = run_test()
                if passed:
                    print(f"❌ {name}: 注入缺陷后测试仍然全绿 —— 断言没咬住！")
                    ok = False
                elif "ModuleNotFoundError" in out or "ImportError" in out:
                    print(f"❌ {name}: 变红原因是 import 失败而非断言 —— 环境问题，结果作废")
                    ok = False
                else:
                    bad = [ln for ln in out.splitlines()
                           if "AssertionError" in ln]
                    why = (bad[0].strip()[:70] if bad
                           else "非零退出（未见 AssertionError，需人工核对）")
                    print(f"✅ {name}: 如期变红（{why}）")
            except Exception as e:  # noqa: BLE001
                print(f"⚠️  {name}: harness 异常 {e}")
                ok = False
            finally:
                path.write_text(pristine[rel], encoding="utf-8")
    finally:
        # 无论正常结束还是被打断（KeyboardInterrupt / SIGKILL 前能跑到这）都还原
        residue = _restore_all()
        if residue:
            print(f"❌ 还原失败（源码仍是变异态）：{residue}")
            ok = False

    # 还原后必须全绿，且源文件必须与开工前逐字节一致
    passed, out = run_test()
    if not passed:
        tail = [ln for ln in out.splitlines() if ln.strip()][-12:]
        print("---- 还原后失败输出尾部 ----")
        for ln in tail:
            print("   ", ln)
    drift = [rel for rel, txt in pristine.items()
             if _sha(pathlib.Path(SERVER, rel).read_text(encoding="utf-8"))
             != pristine_sha[rel]]
    print(f"✅ 还原后全绿（源文件逐字节一致：{len(pristine) - len(drift)}/{len(pristine)}）"
          if passed and not drift else
          f"❌ 还原后状态异常（绿={passed} 漂移={drift}）")
    return 0 if (ok and passed and not drift) else 1


if __name__ == "__main__":
    sys.exit(main())
