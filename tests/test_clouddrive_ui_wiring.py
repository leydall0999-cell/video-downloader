"""网盘下载「前端接线」守卫（2026-10-23 新增）。

为什么要有这份测试：网盘入口横跨 index.html（tab + 视图 + 输入框）、app.js
（视图切换 / 解析 / 下载 / 登录态保存）三处，任何一处被后来的改动摘掉，
界面上就会变成「点了没反应」或「解析出来下载不了」，而后端测试全绿也发现不了。

本测试的定位是**接线守卫**，不是视觉回归：
- HTML 必须提供 app.js 会去取的那批 id
- app.js 必须把新视图接进 switchView（含 downloadView 互斥，否则两个视图同屏）
- 解析 / 下载 / 保存登录态三个请求必须打到正确端点，且**下载必须带上 cookie**
  （百度 dlink 匿名下载会被拒，漏传 cookie = 用户看到「解析成功却下载失败」）

运行：仓库根目录 `pytest tests/test_clouddrive_ui_wiring.py -q`
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "web" / "index.html"
APPJS = ROOT / "web" / "app.js"
CSS = ROOT / "web" / "styles.css"
SERVER_ROUTER = ROOT / "server" / "routers" / "cloud_drive.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


HTML_TXT = _read(HTML)
APP_TXT = _read(APPJS)
CSS_TXT = _read(CSS)


def _has_html_id(i: str) -> bool:
    return f'id="{i}"' in HTML_TXT


def test_html_proids_ids_js_reads():
    """app.js 通过 $('xxx') 取的所有元素，HTML 里必须真的存在。"""
    for i in ("clouddriveView", "cdUrl", "cdPwd", "cdResolve", "cdStatus",
              "cdList", "cdCookie", "cdCookieSave", "cdCookieState",
              "cdCredBox", "tabCloudDrive", "sTabCloudDrive"):
        assert _has_html_id(i), f"index.html 缺少 id={i}"
        assert f"$('{i}')" in APP_TXT, f"app.js 没有取 ${i}"


def test_both_entries_switch_the_view():
    """顶部 tab 与侧栏 item 都要能切到网盘视图（少一个 = 桌面端找不到入口）。"""
    assert 'data-view="clouddrive"' in HTML_TXT
    assert "switchView('clouddrive')" in APP_TXT
    assert "[el.sTabCloudDrive, 'clouddrive']" in APP_TXT


def test_switch_view_excludes_download_view():
    """进网盘视图时必须把下载视图隐藏掉，否则两个 panel 会同时显示。"""
    assert "const isCd = view === 'clouddrive'" in APP_TXT
    assert "el.clouddriveView.hidden = !isCd" in APP_TXT
    # downloadView.hidden 那一行必须带上 isCd
    dl_line = [ln for ln in APP_TXT.splitlines()
               if "el.downloadView.hidden =" in ln]
    assert dl_line, "找不到 downloadView.hidden 赋值行"
    assert "isCd" in dl_line[0], "downloadView 没有随网盘视图隐藏（会双屏）"
    # 顶部「下载」高亮态也要排除网盘视图
    act_line = [ln for ln in APP_TXT.splitlines()
                if "el.tabDownload.classList.toggle('is-active'" in ln]
    assert act_line and "!isCd" in act_line[0]


def test_resolve_and_download_endpoints():
    """解析 / 下载 / 保存登录态三个端点必须对上后端路由，且下载带 cookie。"""
    assert "/api/cloud-drive/resolve" in APP_TXT
    assert "/api/cloud-drive/credential" in APP_TXT
    assert SERVER_ROUTER.exists(), "后端路由文件没了"
    router = _read(SERVER_ROUTER)
    assert "'/api/cloud-drive/resolve'" in router
    assert "'/api/cloud-drive/credential'" in router
    # 🔴 下载必须带上解析返回的 cookie：百度 dlink 匿名会 403（error 31326）
    assert "cookie: f.cookie" in APP_TXT, "下载请求漏了登录态 cookie"


def test_login_gate_hint_wired():
    """need_login 时要把登录态填写区展开，否则用户不知道为什么下载不了。"""
    assert "data.need_login" in APP_TXT or "!!data.need_login" in APP_TXT
    assert "el.cdCredBox.open = true" in APP_TXT


def test_has_styles_for_list():
    """文件列表至少要有一套样式，不然渲染出来是一坨无样式的 li。"""
    for cls in (".cd-item", ".cd-item-name", ".cd-item-size", ".cd-status"):
        assert cls in CSS_TXT, f"styles.css 缺少 {cls}"


def test_compliance_note_present():
    """合规边界要写在界面上：只下自己的/分享给自己的文件，登录态只存本机。"""
    assert "只存这台电脑" in HTML_TXT or "只存本机" in HTML_TXT
