"""「关于」页「官方网站」入口守卫（2026-10-09）。

## 用户要求
「关于」页加一行「官方网站」，点击后自动用浏览器打开官网。
参考样式：地球图标 + 文字 + 右侧箭头（整行可点）。

## 为什么要有守卫（两个真实会踩的坑）
1. **桌面壳是 WKWebView，会静默拦截 `window.open`**（见 desktop_launcher
   `VdlApi.open_external` 的说明）。若有人把点击处理改回 `window.open(url)`，
   网页版正常、桌面端点下去**毫无反应**——且不报错，很难发现。必须走
   `_openExternalUrl()`（原生桥优先、网页版才回退）。
2. **不能用 `<a href>`**：桌面壳里锚点会尝试把**主框架**导航到外网，
   整个 App 界面被目标网页替换（项目里已有同类事故：二维码 blob 导航。
   故这里用 `<button type="button">` + JS 走桥）。

因此本守卫钉住：HTML 结构（button/唯一 id/URL/图标/箭头/位置）+
JS 接线（el 表登记 + 走 _openExternalUrl + 同一块内不得出现 window.open）+
CSS（样式规则存在、可点提示、hover、暗色）+ 两处 URL 常量一致。

运行：python3 test_profile_site_row.py
"""
from __future__ import annotations

import pathlib

_HERE = pathlib.Path(__file__).resolve().parent
_WEB = _HERE.parent.parent / "web"
_INDEX = _WEB / "index.html"
_APP_JS = _WEB / "app.js"
_CSS = _WEB / "styles.css"

SITE_URL = "https://hanyuxz.top/"

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (("  —— " + detail) if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def _rule_body(css: str, selector: str) -> str:
    """取以 `selector` 开头的规则体源码（不含花括号），兼容单行/多行两种写法。

    判据必须落在**规则行**上：只判「全文包含该选择器」会把「规则被注释掉」
    或「只留暗色覆盖、基础规则被删」误判为通过。故要求 `strip()` 后以
    `selector` 开头、且同一行内出现 `{`（选择器与花括号之间允许空白）。
    """
    lines = css.splitlines()
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s.startswith(selector):
            continue
        head = s[len(selector):]
        # 选择器后必须紧跟 `{`（允许空白）；否则说明是 `.pf-link-row-extra` 这类
        # 更长类名的前缀命中，不能算数。
        if not head.lstrip().startswith("{"):
            continue
        if "}" in head:                                  # 单行规则
            return head.split("{", 1)[1].rsplit("}", 1)[0]
        body: list[str] = []                              # 多行规则
        for nxt in lines[idx + 1:]:
            if nxt.strip() == "}":
                return "\n".join(body)
            body.append(nxt)
        return "\n".join(body)
    return ""


def _has_rule(css: str, selector: str) -> bool:
    return bool(_rule_body(css, selector))


def _binding_block(src: str) -> str:
    """取 `if (el.profSiteRow) ... });` 这段绑定源码（到最近的 `});` 为止）。"""
    key = "if (el.profSiteRow)"
    i = src.find(key)
    if i < 0:
        return ""
    j = src.find("});", i)
    return src[i:j + 3] if j > 0 else src[i:i + 600]


def main() -> int:
    html = _INDEX.read_text(encoding="utf-8")
    js = _APP_JS.read_text(encoding="utf-8")
    css = _CSS.read_text(encoding="utf-8")

    print("[A] 关于页 HTML 结构")
    check("A1 存在唯一 id=profSiteRow", html.count('id="profSiteRow"') == 1,
          f"实际 {html.count('id=\"profSiteRow\"')} 处")
    check("A2 该行文案是「官方网站」", ">官方网站<" in html)
    check("A3 用 <button type=\"button\"> 承载（非 <a href>，防主框架导航）",
          'class="pf-link-row" id="profSiteRow"' in html and 'type="button"' in html)
    check("A4 未使用锚点承载官网链接", 'href="https://hanyuxz.top' not in html)
    check(f"A5 携带 data-site-url={SITE_URL}", f'data-site-url="{SITE_URL}"' in html)
    check("A6 含地球图标（圆 + 经纬 path）",
          '<circle cx="12" cy="12" r="9">' in html and "M3.2 9h17.6M3.2 15h17.6" in html)
    check("A7 含右侧箭头 chevron", "M9.5 5.5 16 12l-6.5 6.5" in html)

    # 位置：必须落在「关于本应用」面板内，且在操作按钮行之前
    panel_at = html.find('id="profileAboutPanel"')
    row_at = html.find('id="profSiteRow"')
    actions_at = html.find('class="pf-about-actions"')
    panel_end = html.find('id="profErrorReportBtn"')  # 面板内的后置锚点
    check("A8 行位于 #profileAboutPanel 面板之内", panel_at > 0 and row_at > panel_at)
    check("A9 行位于 #profErrorReportBtn 之前（确在面板范围内）",
          panel_end > 0 and row_at < panel_end)
    check("A10 行位于操作按钮行（检查更新/错误上报）之前",
          actions_at > 0 and 0 < row_at < actions_at)

    print("[B] 前端接线（app.js）")
    check("B1 el 表已登记 profSiteRow", "profSiteRow: $('profSiteRow')" in js)
    block = _binding_block(js)
    check("B2 存在 click 绑定", bool(block) and "addEventListener('click'" in block)
    check("B3 走 _openExternalUrl(url)（原生桥优先）", "_openExternalUrl(url)" in block)
    check("B4 绑定块内不得直连 window.open（WKWebView 会静默拦截）",
          "window.open" not in block)
    check("B5 绑定块内有 URL 常量兜底", f"'{SITE_URL}'" in block)
    check("B6 HTML 与 JS 的官网地址完全一致",
          f'data-site-url="{SITE_URL}"' in html and f"'{SITE_URL}'" in js)

    print("[C] 样式（styles.css）")
    row_body = _rule_body(css, ".pf-link-row ")
    check("C1 .pf-link-row 基础规则存在（整行容器）", bool(row_body))
    check("C2 图标容器样式 .pf-link-row-ic 存在",
          _has_rule(css, ".pf-link-row-ic "))
    check("C3 文字样式 .pf-link-row-label 存在",
          _has_rule(css, ".pf-link-row-label "))
    check("C4 箭头样式 .pf-link-row-arrow 存在",
          _has_rule(css, ".pf-link-row-arrow "))
    check("C5 整行可点（基础规则内含 cursor: pointer）",
          "cursor: pointer" in row_body)
    check("C6 有 hover 反馈", _has_rule(css, ".pf-link-row:hover"))
    check("C7 有暗色主题覆盖", _has_rule(css, "html.theme-dark .pf-link-row "))

    print("[D] 反向自证")
    check("D1 关于面板内未出现裸外链锚点（页面中段扫描）",
          panel_at > 0 and 'href="https://' not in html[panel_at:panel_end if panel_end > 0 else panel_at + 4000])
    check("D2 官网入口不依赖外部图标库（自绘 svg，离线可用）",
          "<svg viewBox=\"0 0 24 24\" width=\"20\"" in html and "cdn" not in html[row_at:row_at + 900].lower())

    print()
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
