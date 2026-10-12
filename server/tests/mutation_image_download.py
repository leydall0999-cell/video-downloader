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
# 2026-10-12：把桌面桥套件一并纳入 —— save_task_file / 前端保存接线 的缺陷
# 只有 test_desktop_bridge.py 咬得住，只跑图片套件会让这些变异「假绿」。
TESTS = ["tests/test_image_download.py", "tests/test_desktop_bridge.py"]

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
     '    name = hint or cd_name or url_name or "image"',
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
    ("M17 父任务不下发 children（图片在服务端但界面一张都不显示）",
     "image_download.py", "                     children=[c.id for c in created])", "                     )"),
    ("M18 to_public_dict 不带 children（前端拿不到子任务 ID）",
     "tasks.py", '            "children": list(self.children or []),', ""),
    ("M19 前端不建子卡片（回归：只剩「已展开 N 张」的汇总卡片）",
     "../web/app.js", "    ensureChildCards(task);", "    void task;"),
    # —— 2026-10-12 「我上哪找去」：可读落盘名 + 桌面另存桥 ——
    ("M20 derive_filename 忽略 name_hint（落盘名又变回 CDN 哈希天书）",
     "image_download.py", "    name = hint or cd_name or url_name or \"image\"",
     "    name = cd_name or url_name or \"image\""),
    ("M21 父任务不给子任务可读名（展开的图片全是天书名）",
     "image_download.py", '                    name_hint=f"图片-{idx}",', ""),
    ("M22 local_path 不再按桌面/网页分流（把服务器路径下发给网页版）",
     "tasks.py", "            if is_desktop() and self.filepath:",
     "            if self.filepath:"),
    ("M23 另存不弹面板（直接塞进「下载」，用户要的「自己选位置」没了）",
     "../desktop/desktop_launcher.py",
     '            chosen = _choose_save_path("保存到", suggested, "/tmp/vdl_task_save.log")',
     "            chosen = \"\""),
    ("M24 前端桌面壳下不移除 href（回到 <a download> 静默吞掉的老状态）",
     "../web/app.js", "      refs.save.removeAttribute('href');", "      void 0;"),
    ("M25 前端桌面版也自动点保存（多张图会连弹一串系统面板）",
     "../web/app.js", "    const webAutoSave = autoSave && !isDesktopShell();",
     "    const webAutoSave = autoSave;"),
    ("M26 折叠态不显示「文件在哪」那行（完成卡默认折叠 ⇒ 用户看不到路径）",
     "../web/styles.css", ".task.is-collapsed .save-hint.save-hint--path { display: block; }",
     ".task.is-collapsed .save-hint.save-hint--path { display: none; }"),
]


REPO = os.path.dirname(SERVER)


def _stage_tree() -> str:
    """把 server/ 复制到临时目录并返回其路径（web/ 用软链指回真实目录）。

    🔴 2026-10-12 实测踩死过：harness 直接改**真实源码**，进程被工具超时 SIGTERM
    打断时 finally 根本没机会跑 ⇒ 源码里留下被注入的缺陷代码（`return _XHS_TOKEN_RE...`），
    下一个跑测试的人看到的是「 inexplicable 的失败」，而 harness 自身毫无察觉。
    ⇒ 铁律：**变异只在副本上做**，真实源码全程只读。
    """
    root = tempfile.mkdtemp(prefix="vdl_mut_tree_")
    dst = os.path.join(root, "server")
    subprocess.run(["rsync", "-a", "--exclude", "__pycache__", "--exclude", ".pytest_cache",
                    f"{SERVER}/", f"{dst}/"], check=True, capture_output=True)
    os.symlink(os.path.join(REPO, "web"), os.path.join(root, "web"))
    # desktop/ 也要有副本：test_desktop_bridge.py 会 import desktop_launcher，
    # 且它按 `__file__.parents[2]` 推仓库根 —— 只搬 .py 顶层文件即可（不含
    # .release_baselines 那 1.6G 产物），变异 desktop_launcher.py 也就只动副本。
    dst_desktop = os.path.join(root, "desktop")
    os.makedirs(dst_desktop, exist_ok=True)
    subprocess.run(["rsync", "-a", "--include", "*.py", "--exclude", "*",
                    f"{os.path.join(REPO, 'desktop')}/", dst_desktop + "/"],
                   check=True, capture_output=True)
    return root          # 返回根目录（server/ 与 web/ 都在它下面）


def _target_server(root: str) -> str:
    return os.path.join(root, "server")


def run_test(server_dir: str) -> tuple[bool, str]:
    env = dict(os.environ)
    env.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_mut_"))
    env.setdefault("VDL_LOGIN_GATE", "0")
    out_all = []
    ok_all = True
    for rel in TESTS:
        p = subprocess.run([PY, rel], cwd=server_dir, env=env,
                           capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            ok_all = False
        out_all.append(f"---- {rel} (rc={p.returncode}) ----\n"
                       + (p.stdout or "") + (p.stderr or ""))
    return ok_all, "\n".join(out_all)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    stage = _stage_tree()
    print(f"变异沙盒（真实源码只读）：{stage}")

    def _target(rel: str) -> pathlib.Path:
        return pathlib.Path(stage, "server", rel)

    # 基线自检：未变异必须先全绿。否则后续每一项「变红」都可能是环境问题的
    # 假阳性（曾踩：base python 缺 requests，注入缺陷后 import 就失败，
    # 8 项变异全「如期变红」但原因完全不对）。
    base_ok, base_out = run_test(_target_server(stage))
    if not base_ok:
        print("❌ 基线自检失败：未变异的测试套件未全绿，本次变异结果全部作废")
        for ln in [l for l in base_out.splitlines() if l.strip()][-10:]:
            print("   ", ln)
        return 1
    print("✅ 基线自检：未变异全绿，开始注入缺陷")
    ok = True
    pristine: dict[str, str] = {}
    for _n, rel, _o, _w in MUTATIONS:
        if rel not in pristine:
            pristine[rel] = _target(rel).read_text(encoding="utf-8")
    for name, rel, old, new in MUTATIONS:
        path = _target(rel)
        src = pristine[rel]
        # 锚点必须是**完整行且唯一**：否则 12 空格会命中 16 空格行（实测踩过，
        # 变异打到预期之外的代码上，结论全错）。
        old_lines = old.split("\n")
        if src.count(old) != 1 or any(not src.count(ln) for ln in old_lines):
            print(f"⚠️  {name}: 锚点不是唯一整行（命中 {src.count(old)} 次），跳过")
            ok = False
            continue
        mutated = src.replace(old, new, 1)
        if _sha(mutated) == _sha(src):
            print(f"⚠️  {name}: 变异后内容与原文件相同（无效变异），跳过")
            ok = False
            continue
        try:
            path.write_text(mutated, encoding="utf-8")
            passed, out = run_test(_target_server(stage))
            if passed:
                print(f"❌ {name}: 注入缺陷后测试仍然全绿 —— 断言没咬住！")
                ok = False
            elif "ModuleNotFoundError" in out or "ImportError" in out:
                print(f"❌ {name}: 变红原因是 import 失败而非断言 —— 环境问题，结果作废")
                ok = False
            else:
                bad = [ln for ln in out.splitlines() if "AssertionError" in ln]
                if not bad:
                    # test_desktop_bridge 逐项打「❌ 描述」计数、不抛异常 —— 不认这种
                    # 失败风格的话，原因栏只会写「非零退出」，看不出是断言还是崩溃。
                    bad = [ln for ln in out.splitlines() if ln.strip().startswith("❌")]
                why = (bad[0].strip()[:70] if bad
                       else "非零退出（未见任何失败项，需人工核对）")
                print(f"✅ {name}: 如期变红（{why}）")
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  {name}: harness 异常 {e}")
            ok = False
        finally:
            path.write_text(src, encoding="utf-8")
    # 还原后必须全绿（副本已还原，真实源码从未被动过）
    passed, out = run_test(_target_server(stage))
    if not passed:
        tail = [ln for ln in out.splitlines() if ln.strip()][-12:]
        print("---- 还原后失败输出尾部 ----")
        for ln in tail:
            print("   ", ln)
    print("✅ 还原后全绿（变异全程只发生在副本，真实源码未被改动）" if passed
          else "❌ 还原后仍红：副本还原链路有问题")
    # 最后再核对一次真实源码确实没被动过（sha 与开工前一致）
    drift = []
    for name, rel, _o, _w in MUTATIONS:
        real = pathlib.Path(SERVER, rel).read_text(encoding="utf-8")
        if _sha(real) != _sha(pristine[rel]):
            drift.append(rel)
    if drift:
        print(f"❌ 真实源码疑似被改动：{drift}")
        ok = False
    return 0 if (ok and passed and not drift) else 1


if __name__ == "__main__":
    sys.exit(main())
