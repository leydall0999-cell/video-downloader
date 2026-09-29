#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「文件变网页」页面生成器 离线回归测试（2026-09-30 新增）。

背景
----
新增功能：把本机文件（图片 / PDF / 音频 / 视频 / 文本）打包成**一个自包含 HTML 页面**。
用户诉求是「一个文件发给谁都能看」⇒ 页面必须零外部依赖，因此文件本体走 base64 内嵌。

本文件锁住的行为
----------------
  _kind_of        扩展名 → 类型（分类错会让页面用错渲染器）
  build_page      页面结构 / base64 内嵌 / 文本转义（**防 HTML 注入**）
  _load_item      单文件体积闸门
  _unique_path    重名不覆盖
  _write_page     写到源文件同目录；不可写时回落「下载/网页生成」
  build_from_paths / build_from_upload   两个入口端到端

设计约束
--------
* **绝不联网**、**绝不写用户真实家目录**：所有输出目录都指向临时目录。
* 体积闸门用「假装文件很大」的方式测（`os.stat_result` 打桩），不真写 80MB。

运行：
    cd server && python tests/test_pagetool.py
    cd server && python -m pytest tests/test_pagetool.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from routers import pagetool as pg  # noqa: E402

# 1×1 透明 PNG（最小合法图片）
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001" "0d0a2db4"
    "0000000049454e44ae426082"
)
PDF_MIN = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

_failures: list[str] = []
_passed = 0


def check(cond: bool, label: str) -> None:
    global _passed
    if cond:
        _passed += 1
        print("  ok   %s" % label)
    else:
        _failures.append(label)
        print("  FAIL %s" % label)


def eq(got, want, label: str) -> None:
    check(got == want, "%s  (got=%r want=%r)" % (label, got, want))


class _patch_home:
    """把 Path.home() 指向临时目录，避免碰到用户真实家目录。"""

    def __init__(self, home: Path):
        self.home = home

    def __enter__(self):
        self._orig = Path.home
        Path.home = staticmethod(lambda: self.home)  # type: ignore[assignment]
        return self.home

    def __exit__(self, *exc):
        Path.home = self._orig  # type: ignore[assignment]
        return False


def test_kind_of() -> None:
    print("[1] _kind_of 分类")
    eq(pg._kind_of("a.JPG"), "image", "大写扩展名也算图片")
    eq(pg._kind_of("a.png"), "image", "png → image")
    eq(pg._kind_of("a.mp3"), "audio", "mp3 → audio")
    eq(pg._kind_of("a.m4a"), "audio", "m4a → audio")
    eq(pg._kind_of("a.mp4"), "video", "mp4 → video")
    eq(pg._kind_of("a.mkv"), "video", "mkv → video（不可播但仍归视频）")
    eq(pg._kind_of("a.pdf"), "pdf", "pdf → pdf")
    eq(pg._kind_of("a.md"), "text", "md → text")
    eq(pg._kind_of("a.zip"), "file", "未知类型 → file")
    eq(pg._kind_of("noext"), "file", "无扩展名 → file")


def test_build_page_structure() -> None:
    print("[2] build_page 结构 / 内嵌 / 转义")
    items = [
        {"name": "图.png", "size": 100, "kind": "image", "ext": ".png",
         "uri": "data:image/png;base64,AAAA"},
        {"name": "视频.mp4", "size": 200, "kind": "video", "ext": ".mp4",
         "uri": "data:video/mp4;base64,BBBB"},
        {"name": "音.m4a", "size": 300, "kind": "audio", "ext": ".m4a",
         "uri": "data:audio/mp4;base64,CCCC"},
        {"name": "文.pdf", "size": 400, "kind": "pdf", "ext": ".pdf",
         "uri": "data:application/pdf;base64,DDDD"},
        {"name": "说明.txt", "size": 5, "kind": "text", "ext": ".txt",
         "uri": "data:text/plain;base64,EEEE", "text": "hello <world>"},
        {"name": "包.zip", "size": 600, "kind": "file", "ext": ".zip",
         "uri": "data:application/zip;base64,FFFF"},
    ]
    page = pg.build_page(items, "我的页面")
    check(page.startswith("<!DOCTYPE html>"), "输出是完整 HTML 文档")
    check("我的页面" in page, "标题写进页面")
    check(page.count('class="card"') == 6, "每个文件一张卡（6 张）")
    check('src="data:image/png;base64,AAAA"' in page, "图片走 base64 内嵌")
    check("<video" in page and "data:video/mp4;base64,BBBB" in page, "视频用 video 标签")
    check("<audio" in page and "data:audio/mp4;base64,CCCC" in page, "音频用 audio 标签")
    check("<iframe" in page and "data:application/pdf;base64,DDDD" in page, "PDF 用 iframe")
    check("<pre" in page, "文本用 pre")
    check("包.zip" in page and "filebox" in page, "未知类型给下载兜底块")
    # 转义：文本里的 < 不能原样进 HTML
    check("hello &lt;world&gt;" in page, "文本内容已 HTML 转义（防注入）")
    check("hello <world>" not in page, "原文尖括号未被原样写入")
    # 6 个文件里「文本」走 <pre> 文本、不进 data URI，其余 5 个各内嵌一份 ⇒ 恰好 5 处
    eq(page.count("data:"), 5, "每个非文本文件恰好内嵌一份资源（无外部依赖）")
    check("http://" not in page and "https://" not in page,
          "页面不含任何 http(s) 外链（真·离线可用）")
    # 回归：base64 只能出现一次/文件 —— 媒体元素与下载按钮各带一份会让页面体积翻倍
    # （2026-09-30 实测：1.33 倍的正常膨胀被放大成 2.68 倍）
    for uri, label in (("data:image/png;base64,AAAA", "图片"),
                       ("data:video/mp4;base64,BBBB", "视频"),
                       ("data:audio/mp4;base64,CCCC", "音频"),
                       ("data:application/pdf;base64,DDDD", "PDF")):
        eq(page.count(uri), 1, "%s的 base64 只写了一份（页面不膨胀一倍）" % label)
    check('data-src-target=' in page, "可预览类型用「回填 src」的方式做下载锚点")
    check('data-text-target=' in page, "文本用「现场拼 Blob」的方式做下载锚点")
    check('id="pgv1"' in page and 'id="pgv5"' in page, "每个可预览元素都有唯一 id 供回填")


def test_build_page_escapes_filename() -> None:
    print("[3] 文件名注入防护")
    page = pg.build_page(
        [{"name": '<script>alert(1)</script>.txt', "size": 1, "kind": "text",
          "ext": ".txt", "uri": "data:text/plain;base64,AA", "text": "x"}],
        '<img src=x onerror=alert(1)>',
    )
    check("<script>alert(1)</script>" not in page, "文件名里的 <script> 被转义")
    check("onerror=alert(1)>" not in page, "标题里的 onerror 被转义")
    check("&lt;script&gt;" in page, "转义后仍保留可读文本")


def test_load_item_and_size_gate() -> None:
    print("[4] _load_item 类型识别与体积闸门")
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        png = t / "pic.png"
        png.write_bytes(PNG_1PX)
        it = pg._load_item(png)
        eq(it["kind"], "image", "png 识别为 image")
        check(it["uri"].startswith("data:image/png;base64,"), "MIME 推断为 image/png")

        txt = t / "n.txt"
        txt.write_text("中文内容", encoding="utf-8")
        it2 = pg._load_item(txt)
        eq(it2["text"], "中文内容", "文本内容被读入")

        # 体积闸门：把 stat 打桩成超大文件
        big = t / "big.mp4"
        big.write_bytes(b"\0" * 16)
        orig_stat = Path.stat
        try:
            Path.stat = lambda self, **kw: os.stat_result(
                (0o100644, 0, 0, 1, 0, 0, pg.MAX_ITEM_BYTES + 1, 0, 0, 0))
            raised = None
            try:
                pg._load_item(big)
            except Exception as exc:  # HTTPException
                raised = exc
            check(raised is not None, "超过单文件上限时抛错")
            check("上限" in str(getattr(raised, "detail", "")), "错误信息里写清上限")
        finally:
            Path.stat = orig_stat


def test_unique_path() -> None:
    print("[5] _unique_path 不覆盖已有文件")
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        first = t / "a.网页.html"
        eq(pg._unique_path(first).name, "a.网页.html", "不存在时用原名")
        first.write_text("x", encoding="utf-8")
        eq(pg._unique_path(first).name, "a(1).网页.html", "已存在时加 (1)")
        (t / "a(1).网页.html").write_text("y", encoding="utf-8")
        eq(pg._unique_path(first).name, "a(2).网页.html", "再冲突时加 (2)")


def test_write_page_layout() -> None:
    print("[6] _write_page：写源文件同目录 + 命名 + 回落")
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        t = Path(td)
        src = t / "素材图.png"
        src.write_bytes(PNG_1PX)
        with _patch_home(Path(home)):
            res = pg._write_page([src], "标题A")
        out = Path(res["out_path"])
        eq(out.name, "素材图.网页.html", "输出名为「<主名>.网页.html」")
        eq(str(out.parent), str(t), "默认写到源文件所在目录")
        eq(res["fallback_used"], False, "正常情况不触发回落")
        check(out.exists() and out.stat().st_size == res["html_size"], "文件真的落盘且体积一致")
        check(res["inflated"] > 1.0, "报告了 base64 膨胀率")
        eq(res["items"][0]["name"], "素材图.png", "清单里是原始文件名")

        # 目录不可写 → 回落到「下载/网页生成」
        ro = t / "ro"
        ro.mkdir()
        src2 = ro / "只读图.png"
        src2.write_bytes(PNG_1PX)
        os.chmod(ro, 0o500)
        try:
            with _patch_home(Path(home)):
                res2 = pg._write_page([src2], "标题B")
            eq(res2["fallback_used"], True, "目录不可写时标记回落")
            check(str(res2["out_path"]).startswith(str(Path(home) / "Downloads")),
                  "回落到「下载/网页生成」")
            check(Path(res2["out_path"]).exists(), "回落后的页面同样落盘")
        finally:
            os.chmod(ro, 0o700)


def test_endpoints() -> None:
    print("[7] 两个入口端点（TestClient）")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(pg.router)
    client = TestClient(app)

    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
        t = Path(td)
        img = t / "封面.png"
        img.write_bytes(PNG_1PX)
        audio = t / "voice.m4a"
        audio.write_bytes(b"\0" * 64)

        with _patch_home(Path(home)):
            # 路径入口
            r = client.post("/api/pagetool/build",
                            json={"paths": [str(img), str(audio)], "title": "合集"})
            eq(r.status_code, 200, "build 返回 200")
            data = r.json()
            eq(data["ok"], True, "build ok=True")
            eq(len(data["items"]), 2, "两个文件都进了页面")
            eq(Path(data["out_path"]).name, "封面.网页.html", "以首个文件名为输出名")
            page = Path(data["out_path"]).read_text(encoding="utf-8")
            check("合集" in page, "自定义标题生效")
            check("data:image/png;base64," in page, "图片已内嵌")
            check('id="lb"' in page, "有图片时输出灯箱容器")

            # 空路径 / 不存在路径
            r = client.post("/api/pagetool/build", json={"paths": []})
            eq(r.status_code, 400, "空路径 → 400")
            r = client.post("/api/pagetool/build", json={"paths": [str(t / "nope.png")]})
            eq(r.status_code, 400, "不存在的路径 → 400")

            # 上传入口
            r = client.post("/api/pagetool/build-upload",
                            files=[("files", ("我的图.png", PNG_1PX, "image/png")),
                                   ("files", ("说明.txt", b"abc", "text/plain"))],
                            data={"title": "上传生成"})
            eq(r.status_code, 200, "build-upload 返回 200")
            up = r.json()
            eq(len(up["items"]), 2, "两个上传文件都进了页面")
            eq(up["items"][0]["name"], "我的图.png", "页面里保留用户原始文件名")
            eq(Path(up["out_path"]).name, "我的图.网页.html", "输出名取自原始文件名")
            upage = Path(up["out_path"]).read_text(encoding="utf-8")
            check("上传生成" in upage, "上传入口的标题生效")
            check(up.get("page_html", "").startswith("<!DOCTYPE html>"),
                  "网页端能在响应里直接拿到页面内容（服务端磁盘路径对访客无意义）")
            eq(up["html_size"], len(up["page_html"].encode("utf-8")),
               "回传内容长度与落盘体积一致")
            # 同名两文件同批上传：必须都保留（落盘名带序号，不会互相覆盖）
            # 判据用「体积」而不是名字 —— 若临时落盘名撞车，后一个会盖掉前一个，
            # 清单里就会出现两个相同体积的条目。
            r2 = client.post("/api/pagetool/build-upload",
                             files=[("files", ("同名.png", PNG_1PX, "image/png")),
                                    ("files", ("同名.png", b"\0" * 5000, "image/png"))],
                             data={})
            eq(r2.status_code, 200, "同名文件同批上传不报错")
            sizes = sorted(i["size"] for i in r2.json()["items"])
            eq(sizes, [len(PNG_1PX), 5000], "同名两文件都完整保留（未被互相覆盖）")
            eq([i["name"] for i in r2.json()["items"]], ["同名.png", "同名.png"],
               "两个条目都显示用户原始文件名")


def test_mime_map() -> None:
    print("[8] MIME 标注（错一个 MIME 整个媒体放不出来）")
    # 回归：Python 的 mimetypes 把 .m4a 猜成 audio/mp4a-latm，Chrome 会
    # MEDIA_ERR_SRC_NOT_SUPPORTED —— 页面里音频静默不可播（2026-09-30 实测抓到）
    eq(pg._mime_of(Path("a.m4a")), "audio/mp4", "m4a → audio/mp4（不能用 mp4a-latm）")
    eq(pg._mime_of(Path("a.mp3")), "audio/mpeg", "mp3 → audio/mpeg")
    eq(pg._mime_of(Path("a.MP4")), "video/mp4", "大写扩展名同样命中")
    eq(pg._mime_of(Path("a.jpg")), "image/jpeg", "jpg → image/jpeg")
    eq(pg._mime_of(Path("a.pdf")), "application/pdf", "pdf → application/pdf")
    eq(pg._mime_of(Path("a.txt")), "text/plain;charset=utf-8", "文本显式带 charset")
    check("mp4a-latm" not in pg._mime_of(Path("x.m4a")), "绝不产出 mp4a-latm")
    # 未收录的扩展名回落到系统推断，而不是变成 octet-stream 之外的空值
    check(bool(pg._mime_of(Path("a.unknownxyz"))), "未收录扩展名仍有 MIME 兜底")


def main() -> int:
    for fn in (test_kind_of, test_build_page_structure, test_build_page_escapes_filename,
               test_load_item_and_size_gate, test_unique_path, test_write_page_layout,
               test_endpoints, test_mime_map):
        try:
            fn()
        except Exception:
            _failures.append("%s 抛异常" % fn.__name__)
            print("  FAIL %s 抛异常：" % fn.__name__)
            traceback.print_exc()
    print("\n通过 %d 项" % _passed)
    if _failures:
        print("失败 %d 项：" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
