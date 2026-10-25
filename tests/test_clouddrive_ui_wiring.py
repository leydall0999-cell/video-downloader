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
              "cdCredBox", "tabCloudDrive", "cdSlot"):
        assert _has_html_id(i), f"index.html 缺少 id={i}"
        assert f"$('{i}')" in APP_TXT, f"app.js 没有取 ${i}"


def test_netdisk_is_inline_mode_not_a_view():
    """网盘是下载页内的第三种类型（2026-10-24），不再是独立视图。

    判据：
    - #clouddriveView 有挂载点 cdSlot，且启动时被 appendChild 搬进下载页；
    - setDownloadMode 认识 'netdisk' 并用 mode-netdisk 类控制让位；
    - switchView 把 'clouddrive' 翻译成「下载页 + 网盘模式」，不会真的切走。
    """
    assert 'id="cdSlot"' in HTML_TXT, "index.html 缺网盘挂载点 cdSlot"
    assert "el.cdSlot.appendChild(el.clouddriveView)" in APP_TXT, "启动时未把网盘面板搬进下载页"
    assert "_nd = dlMode === 'netdisk'" in APP_TXT, "setDownloadMode 不认 netdisk"
    assert "classList.toggle('mode-netdisk'" in APP_TXT, "缺 mode-netdisk 切换"
    assert "if (view === 'clouddrive')" in APP_TXT, "switchView 未拦截 clouddrive"
    # 旧路子必须彻底断掉：侧栏项已删，且网盘面板不再随视图显隐
    assert 'id="sTabCloudDrive"' not in HTML_TXT, "侧栏网盘项已要求删除"
    assert "el.clouddriveView.hidden = !isCd" not in APP_TXT, "网盘面板不该再随视图显隐"


def test_no_double_panel_when_netdisk_mode():
    """网盘内联化（2026-10-24）后仍不能出现「两块面板同时显示」。

    以前靠 `el.clouddriveView.hidden = !isCd` 让两块 panel 互斥；现在网盘面板
    本身就在下载页里，互斥改由 CSS 的 mode-netdisk 保证：进入网盘模式时，
    视频/图片的输入内容、结果区、顶部大标题与视频引导都要让位。
    """
    assert "const isCd = view === 'clouddrive'" in APP_TXT
    assert "el.clouddriveView.hidden = !isCd" not in APP_TXT, "网盘面板不该再随视图显隐"
    # 第二版（2026-10-24）：交互与视频/图片一致，页面结构一律不动；
    # 只让位「视频结果区」和「网盘面板里重复的输入行」。
    for rule in ("#downloadView.mode-netdisk .result-panel",
                 "#downloadView.mode-netdisk .cd-form"):
        assert rule in CSS_TXT, f"styles.css 缺少互斥规则：{rule}"
    # 反向守卫：不许再把大标题/引导/说明整块藏掉（那会让用户觉得换了页面）
    for forbidden in ("mode-netdisk > .hero", "mode-netdisk > details",
                      "mode-netdisk .input-panel > *:not(form)"):
        assert forbidden not in CSS_TXT, f"网盘模式不应隐藏页面骨架：{forbidden}"
    # 顶部「下载」tab 仍要能容纳网盘模式（clouddrive 已被翻译成 download）
    dl_line = [ln for ln in APP_TXT.splitlines()
               if "el.downloadView.hidden =" in ln]
    assert dl_line, "找不到 downloadView.hidden 赋值行"


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
    """need_login 时必须给出可执行的引导，否则用户不知道为什么下载不了。

    2026-10-25 用户定档：百度登录态改由管理员提供，用户自助填写的入口已隐藏
    （#cdCredBox 加了 hidden，app.js 里不再自动展开它）。所以这里的契约从
    「展开填写区」改成「给出能解决问题的引导」——两条路都说清楚：
      ① 浏览器/扩展里已登录过 ⇒ 会自动带上；
      ② 没有 ⇒ 联系管理员提供。
    同时守住红线：不能再出现展开已隐藏填写框的代码，否则界面与状态自相矛盾。
    """
    assert "data.need_login" in APP_TXT or "!!data.need_login" in APP_TXT
    # 引导文案必须精确命中网盘那句（app.js 里「管理员」到处都有，泛匹配会误判通过）
    assert "请联系管理员提供百度登录态" in APP_TXT, \
        "缺少「联系管理员提供百度登录态」的引导"
    assert "浏览器登录过百度网盘会自动带上" in APP_TXT, \
        "缺少「浏览器已登录会自动带上」的引导"
    # 🔴 不能再自动展开已隐藏的填写区（入口已按用户要求隐藏）
    assert "el.cdCredBox.open = true" not in APP_TXT, \
        "自助填写入口已隐藏，不该再自动展开它"


def test_has_styles_for_list():
    """文件列表至少要有一套样式，不然渲染出来是一坨无样式的 li。"""
    for cls in (".cd-item", ".cd-item-name", ".cd-item-size", ".cd-status"):
        assert cls in CSS_TXT, f"styles.css 缺少 {cls}"


def test_compliance_note_present():
    """合规边界要写在界面上：只下自己的/分享给自己的文件，登录态只存本机。"""
    assert "只存这台电脑" in HTML_TXT or "只存本机" in HTML_TXT


def test_mode_switch_has_three_options():
    """「视频下载 / 图片下载 / 网盘下载」三个并列选项必须都在切换器里。

    2026-10-24 用户要求：网盘不能藏在侧栏（要翻折叠组才找得到），也不能
    点一下跳到另一个页面；它必须和视频/图片并排，点了就地切换。
    """
    idx = HTML_TXT.find('class="mode-switch"')
    assert idx > 0, "找不到下载类型切换器"
    block = HTML_TXT[idx:idx + 900]
    for label in ("视频下载", "图片下载", "网盘下载"):
        assert label in block, f"切换器里缺少「{label}」"


def test_main_input_hands_netdisk_links_over():
    """主下载框粘网盘链接要交给网盘流程，而不是掉进通用解析。

    真实反馈（2026-10-24）：用户在首页主框粘「链接 + 提取码」的整段分享文案，
    点解析得到「该平台已收录（pan.baidu.com），但暂未实现该站的解析器」——
    功能其实做了，只是这条路没接上，用户于是认为「网盘没做」。
    """
    assert "const isNetDiskShare" in APP_TXT, "缺少网盘链接识别函数"
    assert "const extractSharePwd" in APP_TXT, "缺少提取码解析函数"
    # 识别逻辑必须真的用在主解析流程里（函数定义了没调用 = 打了白工）
    body = APP_TXT.split("const handleResolve =")[1].split("const startDownload")[0]
    assert "isNetDiskShare(" in body, "主解析没有调用网盘识别"
    assert "el.cdUrl.value = url" in body, "主解析没有把链接搬到网盘输入框"
    assert "el.cdResolve.click()" in body, "主解析没有自动触发网盘解析"
    # 提取码要能吃下百度分享文案里的三种写法
    pwd_fn = APP_TXT.split("const extractSharePwd")[1].split("const handleResolve")[0]
    for token in ("提取码", "pwd"):
        assert token in pwd_fn, f"提取码识别漏了 {token} 写法"

