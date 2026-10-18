"""下载页安装包按钮「点了没反应」回归守卫（2026-10-18）。

背景：用户反馈未登录访客在 `/download` 点「下载 Windows / 下载 macOS」没有任何反应。
根因：按钮写成 `<a ... download>` 指向本站 `/download/installer?p=...`，而该地址
会 302 到跨域 CDN（`download.hanyuxz.top`）。HTML 的 `download` 属性只对同源生效，
部分浏览器（Safari / 微信内置浏览器）遇到「download 属性 + 跨域跳转」会静默不下载，
而该页**没有任何 JS** 可以兜底 ⇒ 表现就是「点了没反应」。

修复：安装包按钮去掉 `download` 属性，改普通导航（`target="_blank" rel="noopener"`），
并为每个平台补一条直连 CDN 兜底链接；页脚补「微信 / QQ 请用浏览器打开」提示。

本测试钉住（防回归）：
  1. 任何指向 `/download/installer` 的链接都**不得**带 `download` 属性；
  2. 两个安装包按钮都带 `target="_blank"` + `rel="noopener"`；
  3. 直连兜底链接存在，且与服务端 `_R2_INSTALLER_URLS` 的对象名**完全一致**
     （升版时只改一边就会红）；
  4. 页脚有微信/QQ 提示；
  5. 扩展包按钮保留 `download`（同源 zip，属正常用法，别一刀切删掉）。

纯读文件，不起服务、不联网。
"""
import os
import re
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SERVER = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_dlpage_"))

PAGE = _ROOT / "web" / "download" / "index.html"

# 只匹配锚点开口标签，避免把正文里的 "download" 当属性
_A_TAG = re.compile(r"<a\b[^>]*>")


def _html() -> str:
    return PAGE.read_text(encoding="utf-8")


def _anchors(needle: str) -> list[str]:
    return [t for t in _A_TAG.findall(_html()) if needle in t]


def _anchors_by_href(href: str) -> list[str]:
    """按 href 精确匹配（避免 p=macos 误吞 p=macos-zip）。"""
    exact = f'href="{href}"'
    return [t for t in _A_TAG.findall(_html()) if exact in t]


def _has_download_attr(tag: str) -> bool:
    """判断开口标签里是否真有 download 属性（href 里的 download 不算）。"""
    return re.search(r"\sdownload(\s|=|/?>)", tag) is not None


def test_page_exists():
    assert PAGE.is_file(), f"下载页不存在：{PAGE}"
    assert "下载客户端" in _html(), "下载页内容异常"


def test_installer_links_have_no_download_attr():
    """核心回归：跨域 302 的下载链接不能带 download 属性。"""
    tags = _anchors("/download/installer")
    assert tags, "下载页应至少有一个 /download/installer 链接"
    bad = [t for t in tags if _has_download_attr(t)]
    assert not bad, (
        "以下安装包链接又带上了 download 属性，会让 Safari / 微信内置浏览器"
        "「点了没反应」：\n" + "\n".join(bad)
    )


def test_installer_buttons_open_new_tab():
    for need in ("/download/installer?p=macos", "/download/installer?p=windows"):
        tags = _anchors_by_href(need)
        assert len(tags) == 1, f"应恰有一个 {need} 按钮，实际 {len(tags)}"
        tag = tags[0]
        assert 'target="_blank"' in tag, f"{need} 缺少 target=_blank（点后本页会被跳走）"
        assert 'rel="noopener"' in tag, f"{need} 缺少 rel=noopener"


def test_direct_cdn_fallback_matches_server_object():
    """直连兜底地址必须与 extension.py 的 R2 对象名一致（升版只改一边就红）。"""
    from routers import extension as ext  # noqa: E402  延迟导入，避免无谓依赖

    html = _html()
    for plat in ("macos", "windows"):
        url = ext._R2_INSTALLER_URLS[plat]
        assert url in html, (
            f"下载页缺少 {plat} 的直连兜底地址（应在页面上出现 {url}）；"
            "升版时请与 extension.py 的 _R2_INSTALLER_URLS 一起改"
        )
    # 直连链接本身必须是可点的锚点，而不是只写在文案里
    tags = _anchors(ext._R2_INSTALLER_URLS["windows"])
    assert tags and 'rel="noopener"' in tags[0]


def test_wechat_browser_hint_present():
    """微信 / QQ 内置浏览器会拦安装包，页面必须给出「在浏览器打开」的指引。"""
    html = _html()
    assert "在浏览器打开" in html
    assert "微信" in html


def test_extension_link_keeps_download_attr():
    """扩展包是同源 zip，download 属性是正确用法，不能被一刀切删掉。"""
    tags = _anchors("/api/extension/package")
    assert len(tags) == 1
    assert _has_download_attr(tags[0]), "扩展包链接应保留 download 属性"


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
