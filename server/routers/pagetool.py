# -*- coding: utf-8 -*-
"""文件变网页（页面生成器）路由。

把本机文件（图片 / PDF / 音频 / 视频 / 文本）打包成**一个自包含的 HTML 页面**：
文件本体以 base64 内嵌，页面不依赖任何外部资源，可离线分发、可直接发给别人。

为什么要做（2026-09-30 实测结论，勿凭直觉改）
---------------------------------------------
* 图片 / PDF / 音频内嵌后浏览器都能正常渲染；视频**取决于编码而非容器**——
  实测 MPEG-4 Visual 的 mp4 在 Chrome 直接 `MEDIA_ERR_SRC_NOT_SUPPORTED`(error.code=4)，
  转 H.264 后同段画面正常。所以页面对视频额外给出编码提示。
* base64 会让体积膨胀约 33%，且整页要一次性进浏览器内存 ⇒ 必须有体积闸门
  （单文件 80MB / 合计 200MB），否则用户会做出一个几百 MB、打开就卡的 HTML。

两个入口对应两个客户端：
* `POST /api/pagetool/build`          桌面端：给本机路径，页面写到源文件同目录。
* `POST /api/pagetool/build-upload`   网页端：上传文件，页面写到「下载/网页生成/」。

零第三方依赖：只用标准库。
"""
from __future__ import annotations

import base64
import html
import mimetypes
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter()

# 单文件上限。base64 后约 ×1.33，80MB 源文件 → 约 107MB 页面，仍可接受。
MAX_ITEM_BYTES = 80 * 1024 ** 2
# 合计上限（多文件成一个页面时）
MAX_TOTAL_BYTES = 200 * 1024 ** 2

UPLOAD_SUBDIR = "网页生成"
PAGE_SUFFIX = ".网页.html"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".amr"}
VIDEO_EXT = {".mp4", ".m4v", ".webm", ".ogv", ".mov", ".mkv", ".avi", ".flv", ".ts", ".wmv", ".3gp"}
PDF_EXT = {".pdf"}
TEXT_EXT = {".txt", ".md", ".log", ".csv", ".json", ".xml", ".yaml", ".yml", ".ini", ".conf"}

# 浏览器确定不支持的视频编码容器（只给提示，不阻断）
_NON_PLAYABLE_HINT = ("该视频若为 MKV / AVI / FLV 等容器，或视频轨是 MPEG-4 Visual、H.265 等编码，"
                      "浏览器可能无法直接播放 —— 建议先用「视频格式转换」转成 H.264 的 MP4。")


def _kind_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    if ext in PDF_EXT:
        return "pdf"
    if ext in TEXT_EXT:
        return "text"
    return "file"


def _human(n: int) -> str:
    if n < 1024:
        return "%d B" % n
    if n < 1024 ** 2:
        return "%.1f KB" % (n / 1024)
    return "%.2f MB" % (n / 1024 ** 2)


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


PAGE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#f5f7fb;--card:#fff;--line:#e4e9f2;--ink:#131a26;--sub:#69758c;--blue:#2f6bff}
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
background:var(--bg);color:var(--ink);line-height:1.7;padding:34px 18px 64px;-webkit-font-smoothing:antialiased}
.wrap{max-width:880px;margin:0 auto}
header{margin-bottom:26px}
h1{font-size:25px;letter-spacing:.4px;margin-bottom:8px;word-break:break-all}
header .sub{color:var(--sub);font-size:13.5px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;
margin-bottom:18px;box-shadow:0 1px 3px rgba(19,26,38,.04)}
.card .hd{display:flex;align-items:baseline;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.card .nm{font-size:14.5px;font-weight:600;word-break:break-all;flex:1}
.card .sz{font-size:12.5px;color:var(--sub);white-space:nowrap}
.card .tag{font-size:11.5px;padding:2px 8px;border-radius:5px;background:#eef3ff;color:var(--blue);font-weight:600}
img.pic{width:100%;border-radius:10px;display:block;cursor:zoom-in;box-shadow:0 6px 22px rgba(19,26,38,.12)}
video{width:100%;border-radius:10px;background:#0d1522;display:block}
audio{width:100%}
iframe{width:100%;height:70vh;min-height:420px;border:1px solid var(--line);border-radius:10px;background:#fff}
pre{background:#0e1622;color:#d7e3f4;padding:16px;border-radius:10px;overflow:auto;font-size:13px;
font-family:ui-monospace,Menlo,monospace;max-height:56vh;line-height:1.6}
.note{font-size:12.5px;color:#8a6d1f;background:#fdf6e3;border:1px solid #f0e0b8;border-radius:9px;
padding:9px 12px;margin-top:12px}
.dl{display:inline-block;padding:8px 18px;border-radius:8px;background:var(--blue);color:#fff;
text-decoration:none;font-size:13px;font-weight:600}
.filebox{background:#f2f5fa;border:1px dashed #c9d4e6;border-radius:10px;padding:22px;text-align:center;
font-size:14px;color:#46536b}
footer{color:var(--sub);font-size:12.5px;text-align:center;margin-top:30px}
.lb{position:fixed;inset:0;background:rgba(6,12,22,.93);display:none;align-items:center;
justify-content:center;cursor:zoom-out;z-index:9}
.lb.on{display:flex}
.lb img{max-width:96vw;max-height:96vh;border-radius:8px}
"""


def _viewer(kind: str, uri: str, name: str, ext: str, vid: str) -> tuple[str, str, str]:
    """返回 (主体 HTML, 附加提示 HTML, 下载锚点的取值方式)。

    第三个值决定「下载原文件」怎么拿到字节，**刻意避免把 base64 写两遍**：
      * 'element' —— 页面里已有该媒体元素，锚点由末尾脚本回填它的 src/currentSrc；
      * 'text'    —— 文本，锚点由脚本用 <pre> 的文本现场拼 Blob；
      * 'direct'  —— 没有可预览元素（未知类型），只能让锚点自己带 data URI。
    2026-09-30 实测：原先两类元素各带一份 data URI，页面体积直接翻倍（1.33 倍膨胀被放大成 2.68 倍）。
    """
    if kind == "image":
        return ('<img id="%s" class="pic" src="%s" alt="%s" '
                'onclick="document.getElementById(\'lb\').classList.add(\'on\')">'
                % (vid, uri, _esc(name))), "", "element"
    if kind == "audio":
        return '<audio id="%s" controls src="%s"></audio>' % (vid, uri), "", "element"
    if kind == "video":
        return ('<video id="%s" controls playsinline preload="metadata" src="%s"></video>' % (vid, uri)), \
               ('<div class="note">%s</div>' % _esc(_NON_PLAYABLE_HINT)), "element"
    if kind == "pdf":
        return ('<iframe id="%s" src="%s" title="%s"></iframe>' % (vid, uri, _esc(name))), \
               '<div class="note">部分移动端浏览器不支持内嵌 PDF，可点下方「下载原文件」查看。</div>', "element"
    if kind == "text":
        return '<pre id="%s">PLACEHOLDER_TEXT</pre>' % vid, "", "text"
    return ('<div class="filebox">该类型（%s）无法在网页内预览，请点下方按钮下载原文件。</div>'
            % _esc(ext or "未知")), "", "direct"


def build_page(items: list[dict], title: str) -> str:
    """items 元素：{name, size, kind, ext, uri, text}"""
    cards = []
    labels = {"image": "图片", "video": "视频", "audio": "音频", "pdf": "PDF",
              "text": "文本", "file": "文件"}
    for idx, it in enumerate(items):
        vid = "pgv%d" % (idx + 1)
        body, extra, mode = _viewer(it["kind"], it["uri"], it["name"], it["ext"], vid)
        if it["kind"] == "text":
            body = body.replace("PLACEHOLDER_TEXT", _esc(it.get("text") or ""))
        if mode == "direct":
            dl = '<a class="dl" download="%s" href="%s">下载原文件</a>' % (_esc(it["name"]), it["uri"])
        elif mode == "text":
            dl = ('<a class="dl" href="#" data-text-target="%s" download="%s">下载原文件</a>'
                  % (vid, _esc(it["name"])))
        else:
            dl = ('<a class="dl" href="#" data-src-target="%s" download="%s">下载原文件</a>'
                  % (vid, _esc(it["name"])))
        cards.append(
            '<div class="card"><div class="hd"><span class="tag">%s</span>'
            '<span class="nm">%s</span><span class="sz">%s</span></div>%s%s'
            '<div style="margin-top:14px">%s</div></div>'
            % (labels.get(it["kind"], "文件"), _esc(it["name"]), _human(int(it["size"])),
               body, extra, dl)
        )
    body_html = "\n".join(cards)
    lightbox = ('<div class="lb" id="lb" onclick="this.classList.remove(\'on\')"><img src="" alt=""></div>'
                if any(it["kind"] == "image" for it in items) else "")
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>%(title)s</title>
<style>%(css)s</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>%(title)s</h1>
    <p class="sub">由「视频工坊 · 生成网页」生成 · 共 %(n)d 个文件 · 内容已内嵌，本页可离线打开</p>
  </header>
  %(body)s
  <footer>本页面单文件自包含，可直接通过微信 / QQ / 邮件发送</footer>
</div>
%(lb)s
<script>
// 灯箱：点空白处关闭
document.querySelectorAll('.lb').forEach(function(box){
  box.addEventListener('click', function(ev){
    if (ev.target.tagName === 'IMG') return;
    box.classList.remove('on');
  });
});
// 「下载原文件」锚点回填 —— 刻意不在 HTML 里写第二份 base64（会让页面体积翻倍）
document.querySelectorAll('a.dl[data-src-target]').forEach(function(a){
  var t = document.getElementById(a.getAttribute('data-src-target'));
  if (!t) return;
  a.href = t.currentSrc || t.src || '';
  if (!a.href) a.style.display = 'none';
});
document.querySelectorAll('a.dl[data-text-target]').forEach(function(a){
  var t = document.getElementById(a.getAttribute('data-text-target'));
  if (!t) return;
  var blob = new Blob([t.textContent], {type: 'text/plain;charset=utf-8'});
  a.href = URL.createObjectURL(blob);
});
</script>
</body>
</html>
""" % {"title": _esc(title), "css": PAGE_CSS, "n": len(items), "body": body_html, "lb": lightbox}


# 显式 MIME 表 —— **不要直接用 mimetypes.guess_type**
# 实测（2026-09-30）：Python 把 `.m4a` 猜成 `audio/mp4a-latm`，Chrome 对该 MIME 直接
# `MEDIA_ERR_SRC_NOT_SUPPORTED`(error.code=4) —— 音频在页面里完全放不出声音，
# 而文件本身是好的。这类「容器没错、标注的 MIME 错」极难从代码上看出，故全部写死。
MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".avif": "image/avif", ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".opus": "audio/ogg", ".amr": "audio/amr",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".ogv": "video/ogg", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".flv": "video/x-flv", ".ts": "video/mp2t",
    ".wmv": "video/x-ms-wmv", ".3gp": "video/3gpp",
    ".pdf": "application/pdf",
}


def _mime_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in MIME_MAP:
        return MIME_MAP[ext]
    if ext in TEXT_EXT:
        return "text/plain;charset=utf-8"
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _load_item(path: Path) -> dict:
    size = path.stat().st_size
    if size > MAX_ITEM_BYTES:
        raise HTTPException(status_code=400,
                            detail="「%s」%s 超过单文件 %s 上限，请先压缩或改用「生成二维码」（不走内嵌）。"
                                   % (path.name, _human(size), _human(MAX_ITEM_BYTES)))
    raw = path.read_bytes()
    ext = path.suffix.lower()
    kind = _kind_of(path.name)
    item = {"name": path.name, "size": size, "kind": kind, "ext": ext,
            "uri": "data:%s;base64,%s" % (_mime_of(path), base64.b64encode(raw).decode("ascii"))}
    if kind == "text":
        try:
            item["text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                item["text"] = raw.decode("gbk")
            except UnicodeDecodeError:
                item["text"] = "(非 UTF-8 / GBK 文本，无法直接渲染)"
    return item


def _unique_path(p: Path) -> Path:
    """重名时追加 (1) (2)…，且**序号插在整个后缀之前**。

    ⚠️ 不能直接用 `Path.stem`：本模块的产物是 `<主名>.网页.html`，而
    `Path("图.网页.html").stem` 只剥掉最后一个后缀，得到 `图.网页` ⇒
    会生成 `图.网页(1).html` 这种把「网页」切开的丑名字。所以先按 PAGE_SUFFIX 拆。
    """
    if not p.exists():
        return p
    name = p.name
    if name.endswith(PAGE_SUFFIX):
        base = name[: -len(PAGE_SUFFIX)]
        tail = PAGE_SUFFIX
    else:
        base, tail = p.stem, p.suffix
    for i in range(1, 200):
        c = p.with_name("%s(%d)%s" % (base, i, tail))
        if not c.exists():
            return c
    return p.with_name("%s(%d)%s" % (base, int(time.time()), tail))


def _write_page(paths: list[Path], title: str) -> dict:
    """生成页面并返回结果字典；写入位置优先「源文件同目录」，不可写则回落下载目录。"""
    total = 0
    for p in paths:
        total += p.stat().st_size
    if total > MAX_TOTAL_BYTES:
        raise HTTPException(status_code=400,
                            detail="所选文件合计 %s，超过 %s 上限。内嵌会整体膨胀约 33%%，"
                                   "请分批处理。" % (_human(total), _human(MAX_TOTAL_BYTES)))

    items = [_load_item(p) for p in paths]
    page = build_page(items, title)

    stem = re.sub(r'[\\/:*?"<>|]+', "_", paths[0].stem)[:60] or "页面"
    primary = _unique_path(paths[0].parent / (stem + PAGE_SUFFIX))
    out = primary
    try:
        out.write_text(page, encoding="utf-8")
    except OSError:
        fallback = Path.home() / "Downloads" / UPLOAD_SUBDIR
        fallback.mkdir(parents=True, exist_ok=True)
        out = _unique_path(fallback / (stem + PAGE_SUFFIX))
        out.write_text(page, encoding="utf-8")

    html_size = out.stat().st_size
    return {
        "ok": True,
        "out_path": str(out),
        "out_name": out.name,
        "file_url": out.as_uri(),
        "html_size": html_size,
        "source_size": total,
        "inflated": round(html_size / total, 2) if total else 1.0,
        "fallback_used": out != primary,
        "items": [{"name": it["name"], "size": it["size"], "kind": it["kind"]} for it in items],
    }


class BuildReq(BaseModel):
    paths: list[str]
    title: str = ""


@router.post("/api/pagetool/build")
def build_from_paths(req: BuildReq) -> dict:
    """桌面端：把本机文件（路径）生成自包含 HTML 页面。"""
    raw = [p for p in (req.paths or []) if str(p).strip()]
    if not raw:
        raise HTTPException(status_code=400, detail="请先选择文件。")
    paths: list[Path] = []
    for p in raw:
        q = Path(p).expanduser()
        if not q.is_file():
            raise HTTPException(status_code=400, detail="文件不存在或不可读：%s" % p)
        paths.append(q)
    title = (req.title or "").strip() or paths[0].stem
    return _write_page(paths, title)


@router.post("/api/pagetool/build-upload")
async def build_from_upload(files: list[UploadFile] = File(...),
                            title: str = Form("")) -> dict:
    """网页端：上传文件后生成自包含 HTML 页面。"""
    if not files:
        raise HTTPException(status_code=400, detail="请先选择文件。")

    # ⚠️ 必须先把上传内容落盘再处理：UploadFile 在响应返回后底层文件对象即被关闭，
    #    不能跨请求/跨线程持有（扫码分享踩过同一个坑，见 vdl-scan-share §二.6）。
    import shutil
    import tempfile

    tmpdir = Path(tempfile.mkdtemp(prefix="pagetool_"))
    pairs: list[tuple[Path, str]] = []      # (临时落盘路径, 用户原始文件名)
    try:
        for idx, uf in enumerate(files):
            origin = os.path.basename((uf.filename or "file").replace("\\", "/")) or "file"
            # 落盘名加序号前缀：两个上传文件同名时不会互相覆盖（原始名仍单独记着）
            dst = tmpdir / ("%03d_%s" % (idx, origin))
            dst.write_bytes(await uf.read())
            pairs.append((dst, origin))

        total = sum(p.stat().st_size for p, _ in pairs)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(status_code=400,
                                detail="上传内容合计 %s，超过 %s 上限，请分批处理。"
                                       % (_human(total), _human(MAX_TOTAL_BYTES)))

        items = []
        for src, origin in pairs:
            it = _load_item(src)
            it["name"] = origin          # 页面里展示用户原始文件名，不是临时落盘名
            items.append(it)

        title_final = (title or "").strip() or items[0]["name"]
        page = build_page(items, title_final)

        outdir = Path.home() / "Downloads" / UPLOAD_SUBDIR
        outdir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r'[\\/:*?"<>|]+', "_", Path(items[0]["name"]).stem)[:60] or "页面"
        out = _unique_path(outdir / (stem + PAGE_SUFFIX))
        out.write_text(page, encoding="utf-8")

        html_size = len(page.encode("utf-8"))
        return {
            "ok": True,
            "out_path": str(out),
            "out_name": out.name,
            "html_size": html_size,
            "source_size": total,
            "inflated": round(html_size / total, 2) if total else 1.0,
            # 网页端访问者拿不到服务端的磁盘路径 ⇒ 页面内容**随响应回传**，
            # 前端用 Blob + <a download> 直接存到访客本机（真浏览器支持，桌面壳不支持）。
            "page_html": page,
            "items": [{"name": it["name"], "size": it["size"], "kind": it["kind"]} for it in items],
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
