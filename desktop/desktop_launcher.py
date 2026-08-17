"""VideoDownloader 桌面版启动器（由 PyInstaller 打包为 .app / .exe）。

职责：
1. 定位捆绑的资源（ffmpeg / server 代码 / 前端静态文件 / yt-dlp 插件）
2. 自动挑选空闲端口（默认 8321，被占用时顺延，避免启动即崩溃）
3. 后台启动 FastAPI 服务（uvicorn）
4. 在原生窗口中打开 Web UI（优先 pywebview，回退浏览器）

普通用户双击 .app / .exe 即可，无需安装 Python、ffmpeg 或任何依赖。
退出：关掉窗口即可。
"""
import os
import sys
import time
import socket
import signal
import subprocess
import threading
from pathlib import Path

try:
    import fcntl as _fcntl  # macOS / Linux
except ImportError:  # pragma: no cover - Windows 走 .exe 单实例
    _fcntl = None

# ---- 定位资源目录 ----
if getattr(sys, "frozen", False):
    _exe = Path(sys.executable)
    _macos_dir = _exe.parent
    _resources = (_macos_dir / ".." / "Resources").resolve()
    if getattr(sys, "_MEIPASS", None):
        BASE = Path(sys._MEIPASS)
    elif (_resources / "web").exists():
        BASE = _resources
    else:
        BASE = _macos_dir
else:
    BASE = Path(__file__).resolve().parent.parent

SERVER_DIR = BASE / "server"
WEB_DIR = BASE / "web"
PLUGINS_DIR = BASE / "yt_dlp_plugins"
# 提前把 server 目录注入路径，供 _detect_commentary 直接 import commentary_locate
sys.path.insert(0, str(SERVER_DIR))


def _detect_ffmpeg() -> str | None:
    exe = ".exe" if sys.platform == "win32" else ""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "bin" / f"ffmpeg{exe}")
    candidates.append(BASE / "bin" / f"ffmpeg{exe}")
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _venv_python(root: Path) -> Path:
    """跨平台返回某 venv 的解释器路径（Win: .venv\\Scripts\\python.exe / POSIX: .venv/bin/python）。"""
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _detect_commentary() -> tuple[str | None, str | None]:
    """定位 commentary-pipeline 目录及其解释器；找不到时返回 (None, None)。

    统一走 server/commentary_locate.locate_commentary()（含包内捆绑候选）。
    包内捆绑模式不暴露 python（由 worker 重入自身处理，见 #198），仅返回目录。
    """
    from commentary_locate import locate_commentary
    loc = locate_commentary()
    if not loc:
        return None, None
    return str(loc.root), (None if loc.bundled else loc.python)


_ff = _detect_ffmpeg()
if _ff:
    os.environ["VDL_FFMPEG_BIN"] = _ff              # app.py 自己用
    os.environ["FFMPEG_LOCATION"] = str(Path(_ff).parent)  # yt-dlp 找 ffmpeg 用
    os.environ["PATH"] = str(Path(_ff).parent) + os.pathsep + os.environ.get("PATH", "")  # 兜底
    # 同目录探测 ffprobe（Windows 上为 ffprobe.exe），注入给 app.py 用
    _fp = Path(_ff).with_name("ffprobe" + (".exe" if sys.platform == "win32" else ""))
    if _fp.exists():
        os.environ["VDL_FFPROBE_BIN"] = str(_fp)

_c_dir, _c_py = _detect_commentary()
if _c_dir:
    os.environ["VDL_COMMENTARY_DIR"] = _c_dir
if _c_py:
    os.environ["VDL_COMMENTARY_PYTHON"] = _c_py

# ---- 从用户配置文件读取 VDL_* 代理设置（让打包后的 .app 也能用国内/海外出口，无需改系统代理）----
def _load_external_config() -> dict:
    """读取用户可写配置目录下的 config.json，把其中的 VDL_PROXY_CN / VDL_PROXY 等注入 os.environ。

    用途：用户租了国内 VPS 或买了付费国内节点后，把代理地址写进配置文件，.app 双击即可生效，
    不必去改 macOS 系统代理，也不必重打包。downloader._resolve_proxy 在每次请求时读取这些变量。

    同样支持把百度网盘开放平台应用凭据写进配置文件（VDL_BAIDU_APP_KEY / VDL_BAIDU_APP_SECRET /
    VDL_BAIDU_REDIRECT_URI），从而启用「百度网盘」下载/上传而无需重打包。凭据仅存于本机配置文件。
    另支持 VDL_PORT 固定端口——百度 OAuth 的 redirect_uri 必须精确匹配运行端口，固定端口可避免端口顺延导致回调不匹配。

    优先级：运行时已存在的环境变量(launchd/shell 注入) > 配置文件 > _resolve_proxy 自动检测系统代理。
    """
    import json

    candidates = []
    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "VideoDownloader" / "config.json")
    candidates.append(Path.home() / ".config" / "videodownloader" / "config.json")
    candidates.append(BASE / "config.json")  # 便携 / 调试用

    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text("utf-8"))
        except Exception as e:  # 坏 JSON 不致命，跳过并报警
            print(f"[VDL] 配置文件损坏已忽略 {path}: {e}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            continue
        applied = []
        for key in (
            "VDL_PROXY_CN",
            "VDL_PROXY",
            "VDL_MAX_FILE_MB",
            "VDL_PORT",
            "VDL_BAIDU_APP_KEY",
            "VDL_BAIDU_APP_SECRET",
            "VDL_BAIDU_APP_ID",
            "VDL_BAIDU_REDIRECT_URI",
            "VDL_BAIDU_APP_NAME",
        ):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                if key not in os.environ:        # 显式环境变量优先于配置文件
                    os.environ[key] = val.strip()
                    applied.append(f"{key}=<set>")
                else:
                    applied.append(f"{key}(env-override)")
        if applied:
            print(f"[VDL] 已从 {path} 应用配置: {', '.join(applied)}", file=sys.stderr)
        return data  # 命中第一个存在的配置文件即止
    return {}


_load_external_config()

if PLUGINS_DIR.exists():
    sys.path.insert(0, str(BASE))


# ---- 单二进制双角色：解说管线 worker 重入 ----
def _app_data_dir() -> Path:
    """跨平台返回本应用的可写数据目录（input/output/work 重定向到这里）。"""
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(Path.home() / ".vdl")))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "VideoDownloader"
    return Path.home() / ".local" / "share" / "videodownloader"


def _rebind_std_streams() -> None:
    """Windows windowed(.exe 无控制台)下，重绑标准流到 nul，避免 ffmpeg/print 写已关闭 fd 崩溃。"""
    try:
        dn = open(os.devnull, "w")
        os.dup2(dn.fileno(), 1)
        os.dup2(dn.fileno(), 2)
        if sys.stdin is not None:
            os.dup2(dn.fileno(), 0)
    except Exception:
        pass


def _run_commentary_worker(argv: list[str]) -> int:
    """以 --vdl-commentary-worker 重入自身时，把主程序当作解说管线 worker 运行。

    依赖随包内置（config 已砍 torch），无需外部 Python / pip；脚本与模型在包内只读资源目录，
    input/output/work 重定向到可写目录(COMMENTARY_WORK_ROOT)。详见 server/commentary_locate.py。
    """
    import multiprocessing
    multiprocessing.freeze_support()  # PyInstaller 子进程兼容（保险）

    # 保险丝：防止递归重入（worker 不应再拉起 worker）
    depth = int(os.environ.get("VDL_WORKER_DEPTH", "0") or "0")
    if depth > 0:
        print("[worker] 检测到重入保险丝(VDL_WORKER_DEPTH>0)，拒绝二次重入")
        return 1
    os.environ["VDL_WORKER_DEPTH"] = str(depth + 1)

    from commentary_locate import locate_commentary
    loc = locate_commentary()
    if loc is None or not loc.bundled:
        print("[worker] 未找到包内捆绑的解说管线，无法以 worker 模式运行")
        return 1

    # 注入工作环境：模型/脚本在包内(只读)，工作目录重定向到可写位置
    os.environ["VDL_COMMENTARY_BUNDLED"] = "1"
    os.environ.setdefault("COMMENTARY_BASE", str(loc.root))
    _model_dir = os.path.join(str(loc.root), "models", "whisper-base")
    if os.path.isdir(_model_dir):
        os.environ["COMMENTARY_MODEL_DIR"] = _model_dir
    _work_root = _app_data_dir() / "commentary"
    os.environ["COMMENTARY_WORK_ROOT"] = str(_work_root)

    # Windows windowed 无控制台，重绑标准流避免崩溃
    if sys.platform == "win32":
        _rebind_std_streams()

    # 把包内管线根注入 sys.path，使 import process 命中包内版本（process.py 自行接管 scripts/）
    sys.path.insert(0, str(loc.root))
    try:
        import process
    except Exception as exc:
        print(f"[worker] 导入包内 process 失败: {exc}")
        return 1

    # 去掉哨兵后交给 process.main（其内部直接读 sys.argv）
    # argv[0] 是 sys.executable，需跳过——否则 argparse 会把它当 video positional，
    # 真正传入的 in_file 会变成第二个 positional 报「unrecognized arguments」。
    worker_argv = [a for a in argv[1:] if a != "--vdl-commentary-worker"]
    sys.argv = ["process.py", *worker_argv]
    try:
        process.main()
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 0
    return 0


def _find_free_port(start: int = 8321, tries: int = 80) -> int:
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


_env_port = (os.environ.get("VDL_PORT") or "").strip()
PORT = int(_env_port) if _env_port else _find_free_port()
HOST = "127.0.0.1"
API_URL = f"http://{HOST}:{PORT}"          # 后端 FastAPI 地址（API 调用用这个）
# 使用 http:// 模式加载（相对路径天然工作，CSS/JS/图片无跨协议问题）。
# 通过 PyObjC 配置 WKWebViewConfiguration 绕过系统代理/PAC/VPN 网络扩展(NE)，
# 解决 Karing 等 NE 在网卡层劫持 WKWebView 导致的 "Load failed"。
URL = f"http://{HOST}:{PORT}"


# Dock Quit / Cmd+Q 退出标记：AppDelegate.applicationShouldTerminate_ 被调用时置 True，
# 使窗口 closing 拦截放行；红叉（windowShouldClose）不置此标记 → 仍最小化返回桌面。
_app_terminating = False


class VdlApi:
    """暴露给前端 JS 的桥接 API（仅 pywebview 桌面模式生效）。

    桌面版无法通过 <a download> 触发系统保存框（WebKit 限制），
    因此解说成片的「保存到本机」改由前端调用本 API，由 Python 直接把文件
    复制到用户「下载」文件夹；返回保存路径或 "ERROR: ..."。
    """

    def open_external(self, url: str) -> str:
        """在系统默认浏览器中打开外部 URL（如百度 OAuth 授权页）。

        pywebview 的 WKWebView 不支持 window.open() 弹窗（会被静默拦截），
        因此百度授权改由 Python 调系统浏览器打开，前端轮询 /api/cloud/baidu/token
        检测授权完成。返回 "OK" 或 "ERROR: ..."。
        """
        import webbrowser
        try:
            webbrowser.open(url)
            return "OK"
        except Exception as exc:  # 把错误回传前端展示
            return f"ERROR: {exc}"

    def get_baidu_dlink(self, share_url: str, fs_id: int = 0, pwd: str = "") -> str:
        """复用已登录的 WebView 实例打开百度分享页，提取下载直链。

        核心事实（2026-08-16 修正）：
          - 百度现在下载分享文件**强制要求登录态**：yunData.sign 由登录态生成，
            游客 sign 为空 → sharedownload 返回 errno=2 → 无法拿直链。
          - 因此本函数**不再尝试游客下载**，而是复用 baidu_login() 已登录的
            同一个 WebView 实例（self._baidu_wv），该实例内存中已持有登录 cookie，
            能正常生成 sign，从而拿到 dlink。
          - 若用户尚未登录（self._baidu_wv 无登录态），返回 NOT_LOGGED_IN，
            由前端引导用户在弹出窗口登录后重试。
          - pwd: 分享提取码（如有），WebView 加载后会自动填入以跳过密码弹窗

        流程：
          1. 检查 self._baidu_wv 是否已登录；未登录则返回 NOT_LOGGED_IN
          2. 复用该实例，导航到 share_url（同一 WKWebView，cookie 内存在）
          3. 注入 fetch/XHR 拦截器 + 主动构造 sharedownload 的 __vdl_build
          4. 轮询等待 yunData 就绪（含 sign）
          5. 触发 __vdl_build(fs_id) → 拦截器捕获 dlink
        """
        import json
        import webview as _wv
        import time as _time
        import threading as _th
        from pathlib import Path as _Path

        result_holder = {"value": None, "event": _th.Event()}
        _log_path = "/tmp/vdl_baidu_dlink.log"

        def _log(msg):
            try:
                with open(_log_path, "a") as f:
                    f.write("[" + _time.strftime("%H:%M:%S") + "] " + msg + "\n")
            except Exception:
                pass

        try:
            open(_log_path, "w").close()
        except Exception:
            pass

        _log("=== 开始: url=" + share_url[:60] + "... fs_id=" + str(fs_id) + " ===")

        # 核心思路（2026-08-16 修正）：
        #   公开分享链接的 dlink 提取不依赖登录态——页面 yunData 自带
        #   shareid/uk/sign/bdstoken，构造 sharedownload 即可拿 dlink，无需先登录。
        #   baidu_login() 仅作为"私密/登录可见分享"的可选降级，不是必经前置。

        def _extract():
            temp = None
            try:
                # 关键修复（2026-08-16）：百度现在下载分享文件强制要求登录态。
                # yunData.sign 由登录态生成，游客 sign 为空 → sharedownload 返回 errno=2。
                # 而 WKWebView 跨新窗口的 Cookie 同步是异步延迟的 → 新窗口读不到刚登录
                # 的 cookie（uk=0）。因此必须复用 baidu_login 的同一个 WebView 实例，
                # 该实例内存中已有登录 cookie，sign 才能正常生成。
                wv = getattr(self, "_baidu_wv", None)
                if wv is None:
                    # 从未登录过（self._baidu_wv 由 baidu_login 首次创建时保存）
                    result_holder["value"] = json.dumps({
                        "ok": False,
                        "error": "NOT_LOGGED_IN",
                        "message": "百度网盘需登录后才能下载分享文件，请在弹出的窗口登录后重试。"
                    })
                    _log("=== NOT_LOGGED_IN (尚未登录) ===")
                    return
                temp = wv
                # 确保窗口可见（之前登录后已 hide），避免屏幕外 SPA 不渲染
                try:
                    temp.show()  # 如果窗口之前被 hide，先显示
                    temp.move(50, 50)  # 再移到屏幕内可见位置
                except Exception:
                    pass
                # 用 native load_url + location.reload() 强制完整重新加载分享页
                # 关键发现（Playwright 实测）：evaluate_js("location.href='分享页'")
                # 会被百度服务端 302 重定向到 /share/init?surl=... 中间页（body=71 框架，无下载按钮）。
                # 只有 location.reload() 才能加载真正的 /s/... 分享页。
                # load_url（pywebview native）也走 location.href 等价路径，
                # 所以 load_url 之后必须再 reload 一次。
                try:
                    temp.load_url(share_url)
                    _time.sleep(2)  # 等百度完成 /share/init 重定向
                    try:
                        temp.evaluate_js("location.reload()")
                        _log("已调用 load_url + location.reload()（应对百度 /share/init 重定向）")
                    except Exception as e_rel:
                        _log("location.reload() 异常: " + str(e_rel))
                except Exception as e_load:
                    _log("load_url 异常: " + str(e_load))
                _log("已调用 load_url 加载分享页（同一实例，cookie 在内存）")

                # 4. 注入网络拦截器 + 主动构造 sharedownload 请求的函数
                interceptor_js = """
                (function() {
                    if (window.__vdl_ready) return;
                    window.__vdl_ready = true;
                    window.__vdl_dlink_result = null;
                    function setResult(o) { window.__vdl_dlink_result = JSON.stringify(o); }
                    // ---- fetch 拦截 ----
                    var origFetch = window.fetch;
                    window.fetch = function() {
                        var urlArg = arguments[0];
                        var urlStr = (typeof urlArg === 'string') ? urlArg : ((urlArg && urlArg.url) || '');
                        return origFetch.apply(this, arguments).then(function(resp) {
                            var lower = (urlStr || '').toLowerCase();
                            if (lower.indexOf('sharedownload') !== -1 || (lower.indexOf('rest/2.0/xpan/file') !== -1 && lower.indexOf('method=download') !== -1)) {
                                resp.clone().json().then(function(data) {
                                    if (data && data.errno === 0 && data.dlink) {
                                        setResult({ok:true, dlink:data.dlink, filename:(data.list && data.list[0] && data.list[0].filename) || 'file'});
                                    }
                                }).catch(function(){});
                            }
                            return resp;
                        });
                    };
                    // ---- XHR 拦截 ----
                    var origOpen = XMLHttpRequest.prototype.open;
                    var origSend = XMLHttpRequest.prototype.send;
                    XMLHttpRequest.prototype.open = function(m, u) { this.__vdl_u = u; return origOpen.apply(this, arguments); };
                    XMLHttpRequest.prototype.send = function() {
                        var self = this, url = self.__vdl_u || '';
                        this.addEventListener('load', function() {
                            try {
                                if (url.indexOf('sharedownload') !== -1 || (url.indexOf('rest/2.0/xpan/file') !== -1 && url.indexOf('method=download') !== -1)) {
                                    var data = JSON.parse(self.responseText);
                                    if (data && data.errno === 0 && data.dlink) {
                                        setResult({ok:true, dlink:data.dlink, filename:(data.list && data.list[0] && data.list[0].filename) || 'file'});
                                    }
                                }
                            } catch(e){}
                        });
                        return origSend.apply(this, arguments);
                    };
                    // ---- 主动构造 sharedownload 请求（新策略 2026-08-16 实测验证）----
                    // 关键发现（Playwright 实测）：百度新版分享页
                    //   1. yunData 里没有 sign（keys 无 sign 字段）
                    //   2. sign 来自点击"下载"按钮后页面自动调 GET /share/tplconfig?fields=sign,timestamp
                    //   3. 拿到 sign 后页面再调 POST /api/sharedownload?sign=... 拿 dlink
                    // 所以最简做法：模拟点击"下载"按钮，让页面自己走完整流程，
                    // 拦截器（上方 fetch/XHR monkey-patch）自动捕获 sharedownload 响应里的 dlink。
                    // 优先点"下载"（不是"保存到网盘"，避免转存副作用）。
                    // ---- 主动构造 sharedownload 请求（纯 fetch 构造法，2026-08-16 重大修复）----
                    // 关键发现（Playwright 实测 + 用户真机日志）：
                    //   旧策略"点击 .bottom_download_btn + 拦截 fetch/XHR"失败，原因是
                    //   点击下载按钮后，百度前端**直接用 <a download> / location.href 触发浏览器下载**，
                    //   dlink 根本不经过 fetch/XHR，拦截器永远抓不到 → "拦截未命中"。
                    // 新策略：不点任何 UI 按钮，直接用 fetch 复刻百度内部的下载接口链：
                    //   1. GET  /share/tplconfig?fields=sign,timestamp   → 拿 sign/timestamp
                    //      （百度自己加载页面时就用 surl 调这个接口，已验证 errno=0 成功；
                    //        登录态下用 shareid+uk 也能拿到 sign，游客态 uk=0 → errno=-12/2）
                    //   2. POST /api/sharedownload?sign=...&timestamp=... → 拿 dlink
                    //   结果直接 setResult，不再依赖 UI 拦截。
                    window.__vdl_build = function(fsId, pwd) {
                        var run = async function() {
                            try {
                                // ★ 2026-08-16 最终方案：不调 /share/verify（该 API 反复出错：非阻塞→errno:2, 阻塞→errno:-12）
                                // 密码验证完全由 sharedownload body 中的 &pwd= 参数负责。
                                // 如果 sharedownload 返回 errno:2（密码未验证），自动重试（服务端会逐步建立 session）。

                                // ★ Step 0.5: 检测登录 cookie（2026-08-16 关键修复）
                                // errno:2 的真根因 = WebView 里没有 BDUSS/STOKEN 登录 cookie。
                                // yunData.uk/bdstoken 是分享页自带数据（不需要登录就有），不是用户登录态。
                                // sharedownload 必须有真实登录 cookie 才返回 dlink。
                                // ★ 关键：WKWebView 导航后 cookie 从 WKWebsiteDataStore 异步重新加载，
                                //   可能延迟几秒才出现在 document.cookie 中。这里轮询等待最多 10 秒，
                                //   避免误判 NO_LOGIN_COOKIE（会导致反复弹登录窗）。
                                var _cookieReady = false, _cookieWait = 20;
                                while (_cookieWait-- > 0) {
                                    try {
                                        var _ck = (document.cookie || '');
                                        if (_ck.indexOf('BDUSS') !== -1 || _ck.indexOf('STOKEN') !== -1 || _ck.indexOf('BAIDUID') !== -1) {
                                            _cookieReady = true;
                                            window.__vdl_cookie_check = {ready:true, waited:(20-_cookieWait), len:_ck.length, has_BDUSS:_ck.indexOf('BDUSS')!==-1, preview:_ck.slice(0,120)};
                                            break;
                                        }
                                    } catch(ce) { window.__vdl_cookie_err = String(ce); }
                                    await new Promise(function(r){ setTimeout(r, 500); });
                                }
                                if (!_cookieReady) {
                                    var _fc = (document.cookie || '');
                                    window.__vdl_cookie_check = {ready:false, len:_fc.length, has_BDUSS:_fc.indexOf('BDUSS')!==-1, has_STOKEN:_fc.indexOf('STOKEN')!==-1, has_BAIDUID:_fc.indexOf('BAIDUID')!==-1, preview:_fc.slice(0,120)};
                                    setResult({ok:false, errno:'NO_LOGIN_COOKIE', message:'WebView 无百度登录cookie(BDUSS/STOKEN/BAIDUID均缺失)。需先在app内登录百度网盘。'});
                                    return;
                                }

                                // 等待 yunData（分享数据）就绪，最多 10 秒
                                var yd = null, waitYun = 20;
                                while (waitYun-- > 0) {
                                    yd = window.yunData || {};
                                    if (yd.shareid && (yd.uk || yd.uk === 0)) break;
                                    await new Promise(function(r){ setTimeout(r, 500); });
                                }
                                yd = window.yunData || {};
                                var shareid = yd.shareid, uk = yd.uk;
                                if (!shareid) { setResult({ok:false, errno:-100, message:'yunData.shareid 缺失（页面未就绪）'}); return; }
                                // 校验关键参数（body过短时 yunData 数据可能不完整）
                                var bdstoken = yd.bdstoken || '';
                                if (uk === 0 || uk === '0') { setResult({ok:false, errno:-101, message:'yunData.uk=0（未登录或页面未就绪）'}); return; }
                                if (!bdstoken) { setResult({ok:false, errno:-102, message:'yunData.bdstoken 缺失（页面未完全加载）'}); return; }
                                // 从 URL 提取 surl（pan.baidu.com/s/<surl>）
                                var surl = '';
                                var pm = location.pathname.match(new RegExp('/s/([A-Za-z0-9_-]+)'));
                                if (pm) surl = pm[1];
                                if (!surl) { var qm = location.search.match(/[?&]surl=([A-Za-z0-9_-]+)/); if (qm) surl = qm[1]; }
                                // tplconfig 候选 URL：surl 优先，shareid+uk 兜底
                                var tplUrls = [];
                                if (surl) tplUrls.push('https://pan.baidu.com/share/tplconfig?fields=sign,timestamp&view_mode=1&channel=chunlei&web=1&app_id=250528&bdstoken=&clienttype=0&surl=' + encodeURIComponent(surl));
                                if (shareid && uk) tplUrls.push('https://pan.baidu.com/share/tplconfig?fields=sign,timestamp&channel=chunlei&web=1&app_id=250528&clienttype=0&shareid=' + shareid + '&uk=' + uk);
                                var sign = null, timestamp = null, tplErr = 'no_candidate';
                                for (var ti = 0; ti < tplUrls.length; ti++) {
                                    try {
                                        var tr = await fetch(tplUrls[ti], {credentials:'include'});
                                        var td = await tr.json();
                                        if (td && td.errno === 0 && td.data && td.data.sign) { sign = td.data.sign; timestamp = td.data.timestamp; tplErr = null; break; }
                                        else { tplErr = (td && td.errno !== undefined) ? td.errno : 'bad_resp'; }
                                    } catch(e) { tplErr = String(e); }
                                }
                                if (!sign) { setResult({ok:false, errno:tplErr, message:'tplconfig 未返回 sign(errno=' + tplErr + ')'}); return; }
                                // sharedownload 拿 dlink
                                // 2026-08-16 关键：用 XHR 替代 fetch（WKWebView 中 XHR 的 cookie 发送更可靠）
                                var sdlUrl = 'https://pan.baidu.com/api/sharedownload?sign=' + encodeURIComponent(sign) + '&timestamp=' + encodeURIComponent(timestamp) + '&bdstoken=' + encodeURIComponent(bdstoken) + '&channel=chunlei&clienttype=0&web=1&channel_url=&product=share';
                                // ★ 2026-08-16 关键修复：在 body 里加 pwd（提取码）
                                // 百度服务端从 body 读取 pwd 验证，不再依赖前端 UI 输入
                                var sdlBody = 'encrypt=0&product=share&uk=' + uk + '&shareid=' + shareid + '&fid_list=[' + fsId + ']&type=nolimit&channel=chunlei&clienttype=0&web=1';
                                if (pwd) sdlBody += '&pwd=' + encodeURIComponent(pwd);
                                // 调试：记录关键参数（含 pwd 标记）
                                window.__vdl_sdl_params = {uk:uk, shareid:shareid, bdstoken:bdstoken.slice(0,20)+'..', sign:sign.slice(0,20)+'..', body_len:document.body?document.body.innerText.length:0, has_pwd:!!pwd, verify_ok:!!window.__vdl_verify_ok};
                                // Cookie 诊断：记录 BDUSS 是否存在
                                try {
                                    var _cookies = document.cookie || '';
                                    window.__vdl_sdl_params.has_BDUSS = _cookies.indexOf('BDUSS') !== -1;
                                    window.__vdl_sdl_params.cookie_preview = _cookies.slice(0, 100);
                                } catch(e) { window.__vdl_sdl_params.cookie_err = String(e); }
                                // 用 XHR（withCredentials=true 确保发送 cookie）
                                // ★ 2026-08-16 关键修复：带重试机制
                                // 密码分享有时 verify 刚通过服务端还没完全同步 → errno:2
                                // 重试一次（等 2 秒让服务端状态同步）
                                var sd = null;
                                for (var sdlRetry = 0; sdlRetry < 2; sdlRetry++) {
                                    if (sdlRetry > 0) await new Promise(function(r){ setTimeout(r, 2000); });
                                    sd = await new Promise(function(resolve) {
                                        var xhr = new XMLHttpRequest();
                                        xhr.open('POST', sdlUrl, true);
                                        xhr.withCredentials = true;
                                        xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
                                        xhr.onload = function() { try { resolve(JSON.parse(xhr.responseText)); } catch(e) { resolve({errno:-1, raw:xhr.responseText.slice(0,200)}); } };
                                        xhr.onerror = function() { resolve({errno:-1, message:'XHR network error'}); };
                                        xhr.send(sdlBody);
                                    });
                                    if (sd && sd.errno === 0 && sd.dlink) break;
                                    // 记录重试信息
                                    window.__vdl_sdl_retry = {attempt: sdlRetry+1, errno: (sd&&sd.errno), has_pwd:!!pwd};
                                }
                                if (sd && sd.errno === 0 && sd.dlink) {
                                    setResult({ok:true, dlink:sd.dlink, filename:(sd.list && sd.list[0] && sd.list[0].filename) || 'file'});
                                } else {
                                    var _params = window.__vdl_sdl_params || {};
                                    setResult({ok:false, errno:(sd && sd.errno !== undefined) ? sd.errno : 'no_dlink', message:'sharedownload 失败: ' + JSON.stringify(sd).slice(0,200) + ' | params: uk='+_params.uk+' shareid='+_params.shareid+' bd='+_params.bdstoken+' body='+_params.body_len});
                                }
                            } catch(e) { setResult({ok:false, errno:-1, message:String(e)}); }
                        };
                        run();
                    };
                    // ---- 阻止 window.open 弹窗转发系统浏览器 ----
                    var origWO = window.open;
                    window.open = function(u) {
                        if (u && (u.indexOf('pan.baidu.com') !== -1 || u.indexOf('download') !== -1)) { return null; }
                        return origWO.apply(this, arguments);
                    };
                })()
                """
                try:
                    temp.evaluate_js(interceptor_js)
                    _log("Phase-0: 拦截器注入（在新 load_url 架构下注入的是旧页面 JS 上下文，"
                         "新页面加载后会被清空；真正的注入见 Phase-1.5）")
                except Exception as e2:
                    _log("Phase-0: 异常 " + str(e2))

                # 注意：原 Phase-0.5（自动填提取码）已删除——
                # 它在新 load_url 架构下会在旧页面（网盘主页）执行，找不到分享页密码框。
                # 提取码填入逻辑已移到 Phase-1.6（新页面加载完成后）。

                # 5. 轮询等待分享页渲染
                # 等待分享页渲染 + 页面全局变量 yunData 就绪（含 shareid/uk/sign/bdstoken）
                # 关键：yunData=1 是比 body_len 更强的信号——说明百度 JS 已执行并注入了分享数据
                # （即使 WKWebView 未完全展开 DOM，yunData 里的 sign/bdstoken 已可用于构造 sharedownload）
                _log("Phase-1: 轮询等待分享页 + yunData...")
                page_ready = False
                yun_ready = False
                for wait_i in range(45):  # 最多等 45 秒（百度分享页 SPA 渲染慢）
                    _time.sleep(1)
                    try:
                        state = temp.evaluate_js("document.readyState")
                        body_len = temp.evaluate_js("(document.body?document.body.innerText.length:0)")
                        yun = temp.evaluate_js("(typeof window.yunData!=='undefined' && window.yunData && window.yunData.shareid) ? 1 : 0")
                        if wait_i % 5 == 0:
                            _log("  等待" + str(wait_i+1) + "s: state=" + str(state) + " body=" + str(body_len) + " yunData=" + str(yun))
                            # 每5秒打印一次URL，检测是否被重定向
                            try:
                                _cur_url = temp.evaluate_js("location.href")
                                if _cur_url and isinstance(_cur_url, str):
                                    _log("  URL: " + (_cur_url[:120] if len(_cur_url)>120 else _cur_url))
                            except Exception:
                                pass
                            # ★ body<500 时 dump 页面 HTML 片段（诊断密码框/骨架页）
                            if isinstance(body_len, int) and body_len < 500:
                                try:
                                    _html_snap = temp.evaluate_js("""(function(){
                                        var b = document.body;
                                        if (!b) return 'no body';
                                        return 'tag=' + b.tagName + ' childCount=' + b.children.length +
                                            ' | innerHTML(300): ' + b.innerHTML.slice(0, 300) +
                                            ' | inputs: ' + JSON.stringify(
                                                Array.from(document.querySelectorAll('input')).map(function(el){
                                                    return {type:el.type, ph:el.placeholder, cls:el.className.slice(0,40), vis:el.offsetParent!==null};
                                                })
                                            ) +
                                            ' | allEls(10): ' + JSON.stringify(
                                                Array.from(b.querySelectorAll('*')).slice(0, 15).map(function(el){
                                                    return el.tagName + '.' + el.className.slice(0,30).replace(/\\s+/g,'.') + '#' + el.id;
                                                })
                                            );
                                    })()""")
                                    _log("  📄 HTML快照: " + str(_html_snap)[:500])
                                except Exception:
                                    pass
                        if yun == 1:
                            yun_ready = True
                        # 检测下载按钮是否渲染（最可靠的"页面就绪"信号）
                        btn_visible = False
                        try:
                            btn_visible = temp.evaluate_js("""!!(document.querySelector('.bottom_download_btn') ||
                                Array.from(document.querySelectorAll('button,a,span')).find(b => b.textContent.trim() === '下载'))""")
                        except Exception:
                            pass
                        # 完全就绪：state=complete + yunData + body>1500
                        # 2026-08-16 关键修正：/share/init 中间页也有"下载"按钮(btn_visible=True)但 body 只有~175
                        # → btn_visible 不可信，必须以 body 长度为唯一判据（真实分享页文件列表 >1500 字符）
                        _body_ok = isinstance(body_len, int) and body_len > 1500
                        if state == "complete" and yun == 1 and _body_ok:
                            page_ready = True
                            _log("  ★ 页面就绪 (body=" + str(body_len) + ", btn=" + str(btn_visible) + ")")
                            _log("  ★ 页面就绪 (body=" + str(body_len) + ", btn=" + str(btn_visible) + ")")
                            break
                    except Exception as ep:
                        _log("  等待" + str(wait_i+1) + "s: err " + str(ep))

                # 即使 body 不够长，只要 yunData 有完整数据（uk>0 + bdstoken）就可以尝试
                if not page_ready and yun_ready:
                    # 诊断 + 校验：uk>0 且 bdstoken 非空才允许继续（防止脏数据 → errno:113）
                    try:
                        _diag = temp.evaluate_js("""(function(){
                            var yd = window.yunData || {};
                            return JSON.stringify({
                                url: location.href,
                                shareid: yd.shareid || '(empty)',
                                uk: yd.uk || '(empty)',
                                bdstoken: (yd.bdstoken||'').slice(0,20) + '...',
                                keys: Object.keys(yd).join(',')
                            });
                        })()""")
                        _log("  📋 yunData诊断: " + str(_diag))
                        _uk_check = temp.evaluate_js("(window.yunData && window.yunData.uk > 0) ? 1 : 0")
                        _bd_check = temp.evaluate_js("(window.yunData && window.yunData.bdstoken) ? 1 : 0")
                        if _uk_check != 1 or _bd_check != 1:
                            _log("  ❌ yunData 数据不完整(uk=" + str(_uk_check) + ", bdstoken=" + str(_bd_check) + ")，需等待页面完全加载")
                        else:
                            _log("  ★ yunData 完整(uk>0, bdstoken有值)，body较短但数据可用")
                            page_ready = True
                    except Exception:
                        pass

                if not page_ready:
                    # 未登录或分享失效：检测页面提示文字
                    try:
                        body_txt = temp.evaluate_js("(document.body?document.body.innerText.slice(0,300):'')")
                    except Exception:
                        body_txt = ''
                    if isinstance(body_txt, str) and ('登录' in body_txt or '客户端' in body_txt):
                        result_holder["value"] = json.dumps({
                            "ok": False,
                            "error": "NOT_LOGGED_IN",
                            "message": "百度网盘未登录或登录态已失效，请在弹出的窗口登录后重试。"
                        })
                        _log("=== NOT_LOGGED_IN (页面提示) ===")
                        return
                    # 最终兜底：等了 45 秒后，如果 yunData 有完整数据(uk>0+bdstoken)，强制尝试
                    try:
                        _final_uk = temp.evaluate_js("(window.yunData && window.yunData.uk > 0) ? 1 : 0")
                        _final_bd = temp.evaluate_js("(window.yunData && window.yunData.bdstoken) ? 1 : 0")
                        if _final_uk == 1 and _final_bd == 1:
                            _log("  ★ 最终兜底：等了" + str(45) + "s 后 body 仍短但 yunData 完整，强制尝试构造 sharedownload")
                            page_ready = True
                    except Exception:
                        pass
                    result_holder["value"] = json.dumps({
                        "ok": False,
                        "error": "PAGE_NOT_READY",
                        "message": "分享页未正常加载（链接失效、需密码或网络异常）。"
                    })
                    _log("=== PAGE_NOT_READY ===")
                    return

                # 注意（2026-08-16 关键修复）：这里**不再检查 yunData.sign**。
                # 新版百度分享页 SPA 已不再把 sign 写入 window.yunData（keys 里没有 sign 字段），
                # 而是在 fetch sharedownload 时动态计算放在 URL 参数里。所以 sign 为空是正常现象，
                # 不代表未登录。真正的 sign 获取交给 Phase-2 的新 __vdl_build（fetch 拦截提取）。

                _time.sleep(1)

                # 页面导航后 JS 上下文被重置，Phase-0 注入的 __vdl_build 已丢失，
                # 必须在页面就绪后重新注入，否则 Phase-2 调用会找不到该方法。
                try:
                    temp.evaluate_js(interceptor_js)
                    _log("Phase-1.5: 重新注入拦截器（页面加载后）")
                except Exception as e15:
                    _log("  Phase-1.5 异常: " + str(e15))

                # Phase-1.6: 自动填提取码（如需）——密码分享页会显示密码框，不填则 sharedownload 返回 errno:2
                # 2026-08-16 关键修复：页面 body 可能只有~183（SPA 未完全渲染），密码框还没出现
                # → 必须重试填码，不能只试一次
                if pwd:
                    try:
                        _log("Phase-1.6: 尝试自动填提取码（最多重试 10 次）...")
                        pwd_json = json.dumps(pwd)
                        filled = "no_input"
                        for fill_retry in range(10):
                            fill_js = (
                                "(function(){"
                                "  try {"
                                "    /* 策略1: 标准 input[type=password] */"
                                "    var inp = document.querySelector('input[type=password]');"
                                "    /* 策略2: placeholder 匹配 */"
                                "    if (!inp) inp = document.querySelector('input[placeholder*=提取码], input[placeholder*=密码], input[placeholder*=访问码], input[placeholder*=请输入]');"
                                "    /* 策略3: 任何可见 text/input（百度可能用 type=text）*/"
                                "    if (!inp) {"
                                "      var allInp = document.querySelectorAll('input');"
                                "      for (var ii=0;ii<allInp.length;ii++){"
                                "        var r=allInp[ii].getBoundingClientRect();"
                                "        if (r.width>0 && r.height>0){inp=allInp[ii];break;}"
                                "      }"
                                "    }"
                                "    /* 策略4: contenteditable div（富文本密码框）*/"
                                "    if (!inp) {"
                                "      var ce=document.querySelector('[contenteditable=true]');"
                                "      if (ce){ inp=ce; inp._isCE=true; }"
                                "    }"
                                "    if (inp && !inp.value && !inp._isCE) {"
                                "      var setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;"
                                "      setter.call(inp, " + pwd_json + ");"
                                "      ['input','change','blur','keyup'].forEach(function(ev){"
                                "        inp.dispatchEvent(new Event(ev, {bubbles:true}));"
                                "      });"
                                "      var btns = document.querySelectorAll('button, a, span, div[class*=btn], [class*=submit], [class*=confirm]');"
                                "      for (var i=0;i<btns.length;i++){ var t=(btns[i].textContent||'').trim();"
                                "        var r2=btns[i].getBoundingClientRect();"
                                "        if ((t==='确定'||t==='提取'||t==='确认'||t==='提交') && r2.width>0){ btns[i].click(); return 'clicked:'+t; }"
                                "      }"
                                "      return 'filled_no_btn';"
                                "    }"
                                "    if (inp && inp._isCE) {"
                                "      inp.textContent = " + pwd_json + ";"
                                "      inp.dispatchEvent(new Event('input', {bubbles:true}));"
                                "      return 'filled_CE';"
                                "    }"
                                "    if (inp && inp.value) return 'already_filled';"
                                "    return 'no_input';"
                                "  } catch(e){ return 'err:'+e; }"
                                "})()"
                            )
                            filled = temp.evaluate_js(fill_js)
                            if filled and filled not in ("no_input",):
                                _log("  填码结果(" + str(fill_retry+1) + "次): " + str(filled))
                                break
                            if fill_retry < 9:
                                _time.sleep(1)
                        if filled == "no_input":
                            _log("  填码结果: 10次重试后仍找不到密码框（可能无密码或页面未渲染）")
                        if filled and filled not in ("no_input",):
                            _time.sleep(3)  # 等 SPA 用密码刷新分享数据
                            # 填码后页面可能重置 JS 上下文，重新注入拦截器
                            try:
                                temp.evaluate_js(interceptor_js)
                                _log("  Phase-1.6b: 重新注入拦截器（填码后）")
                            except Exception as e16:
                                _log("  Phase-1.6b 异常: " + str(e16))
                    except Exception as ef:
                        _log("  Phase-1.6 异常: " + str(ef))

                # 主动构造 sharedownload 请求（纯 fetch 构造法）
                # 预检：确认仍在分享页（未被重定向到 /disk 主页）+ 记录 uk 值
                try:
                    _pre_url = temp.evaluate_js("location.href")
                    _pre_uk = temp.evaluate_js("(window.yunData && window.yunData.uk) || '(none)'")
                    _pre_bd = temp.evaluate_js("(window.yunData && window.yunData.bdstoken) ? 'has' : 'none'")
                    _log("Phase-2 预检: url=" + (str(_pre_url)[:80] if _pre_url else '?') + " uk=" + str(_pre_uk) + " bdstoken=" + str(_pre_bd))
                    if isinstance(_pre_url, str) and '/disk' in _pre_url and '/s/' not in _pre_url:
                        result_holder["value"] = json.dumps({
                            "ok": False,
                            "error": "NOT_LOGGED_IN",
                            "message": "分享页被重定向到网盘主页（/disk），说明未登录或链接失效。请先登录百度网盘。"
                        })
                        _log("=== NOT_LOGGED_IN (重定向到 /disk) ===")
                        return
                except Exception:
                    pass

                _log("Phase-2: 触发 __vdl_build(" + str(int(fs_id)) + ", pwd=" + (pwd or '') + ")")
                try:
                    _pwd_js = json.dumps(pwd or '')
                    temp.evaluate_js("window.__vdl_build(" + str(int(fs_id)) + ", " + _pwd_js + ")")
                except Exception as eb:
                    _log("  触发异常: " + str(eb))

                # 等待拦截器捕获 dlink 或返回错误
                _log("Phase-3: 等待 dlink (20s)...")
                final_result = None
                for ci in range(20):
                    _time.sleep(1)
                    try:
                        rc = temp.evaluate_js("(function(){return window.__vdl_dlink_result||null;})()")
                        if rc and isinstance(rc, str) and rc.startswith("{"):
                            try:
                                ic = json.loads(rc)
                                if ic.get("ok") and ic.get("dlink"):
                                    final_result = rc
                                    _log("  ★ 捕获到 dlink (" + str(ci+1) + "s)")
                                    break
                                else:
                                    # 任何失败（含 ok=false）都把携带的 message 透传，便于诊断
                                    en = ic.get("errno")
                                    err_msg = ic.get("message") or ("errno=" + str(en))
                                    _log("  __vdl_build 返回失败: " + err_msg)
                                    # 额外记录 sharedownload 调用参数
                                    try:
                                        _p = temp.evaluate_js("(function(){return window.__vdl_sdl_params||null;})()")
                                        if _p: _log("  📋 sharedownload参数: " + str(_p))
                                        _vr = temp.evaluate_js("(function(){return window.__vdl_verify_result||null;})()")
                                        if _vr: _log("  🔐 提取码验证结果: " + str(_vr))
                                    except Exception: pass
                                    login_codes = (-6, -20, -9, 2, -101, -102)
                                    if en in login_codes:
                                        result_holder["value"] = json.dumps({
                                            "ok": False,
                                            "error": "NOT_LOGGED_IN",
                                            "message": "百度网盘登录态已失效或需重新登录：" + err_msg
                                        })
                                        _log("=== NOT_LOGGED_IN (errno=" + str(en) + ") ===")
                                        return
                                    elif en == -12:
                                        result_holder["value"] = json.dumps({
                                            "ok": False,
                                            "error": "LINK_ERR",
                                            "message": "百度返回链接出错(errno=-12)，可能登录态不足或接口参数变化：" + err_msg
                                        })
                                        _log("=== LINK_ERR (errno=-12) ===")
                                        return
                                    else:
                                        result_holder["value"] = json.dumps({
                                            "ok": False,
                                            "error": "DLINK_ERR",
                                            "message": "获取直链失败：" + err_msg
                                        })
                                        _log("=== DLINK_ERR (errno=" + str(en) + ") ===")
                                        return
                            except (json.JSONDecodeError, TypeError):
                                pass
                    except Exception:
                        pass

                if final_result:
                    result_holder["value"] = final_result
                    _log("=== 成功 ===")
                else:
                    result_holder["value"] = json.dumps({"ok": False, "error": "all_failed", "message": "未能获取直链（拦截未命中）。"})
                    _log("=== 全部失败 ===")

            except Exception as e:
                _log("外层异常: " + type(e).__name__ + ": " + str(e))
                result_holder["value"] = json.dumps({"ok": False, "error": type(e).__name__ + ": " + str(e)})
            finally:
                # 关键修复：不要销毁窗口！temp 是复用的登录实例 self._baidu_wv，
                # 必须保留以便下次下载复用（否则又得重新登录）。
                # 用 native hide() 隐藏（比 move(-20000) 可靠），保留实例与登录态。
                if temp:
                    try:
                        temp.hide()
                        _log("窗口已隐藏（保留实例供下次复用）")
                    except Exception:
                        pass
                result_holder["event"].set()

        t = _th.Thread(target=_extract, daemon=True)
        t.start()
        result_holder["event"].wait(timeout=80)
        ret = result_holder.get("value") or json.dumps({"ok": False, "error": "timeout"})
        _log("=== 返回: " + ret[:200] + " ===")
        return ret

    def baidu_login(self) -> str:
        """打开一个可见的 WebView 窗口让用户登录百度网盘。

        关键修复（2026-08-16）：
          - 百度现在下载分享文件**强制要求登录态**（yunData.sign 由登录态生成，
            游客 sign 为空 → sharedownload 返回 errno=2）。
          - WKWebView 跨新窗口的 Cookie 同步是异步延迟的 → 新开的下载窗口
            读不到刚登录的 cookie（uk=0）→ 必须复用同一个 WebView 实例。
          - 因此登录成功后**不关闭窗口**，保存 self._baidu_wv 供 get_baidu_dlink 复用，
            确保 cookie 在同一实例内存中始终可用。
        """
        import json
        import webview as _wv
        import time as _t
        import threading as _th

        result_holder = {"value": {"ok": False, "error": "TIMEOUT"}, "event": _th.Event()}
        _log_path = "/tmp/vdl_baidu_login.log"

        def _log(msg):
            try:
                with open(_log_path, "a") as f:
                    f.write("[" + _t.strftime("%H:%M:%S") + "] " + msg + "\n")
            except Exception:
                pass

        try:
            open(_log_path, "w").close()
        except Exception:
            pass
        _log("=== baidu_login 开始 ===")

        # 复用已登录的窗口实例（同一 WKWebView → cookie 在内存立即可用，无需重新登录）
        # 登录态判断用 URL（登录成功会跳 /disk，未登录停在登录页），
        # 不能用 yunData.loginstate（网盘主页没有 yunData，会误判未登录）。
        existing = getattr(self, "_baidu_wv", None)
        if existing is not None:
            try:
                href = existing.evaluate_js("location.href")
                if isinstance(href, str) and ('/disk' in href or '/main' in href or 'yun.baidu.com' in href):
                    _log("复用已登录窗口，跳过重新登录")
                    return json.dumps({"ok": True, "logged": True, "reused": True, "message": "已登录，可直接下载。"})
            except Exception:
                pass

        def _run():
            w = None
            try:
                w = _wv.create_window(
                    title="VDL-登录百度网盘",
                    # 直接加载 disk/main（而非 pan.baidu.com 欢迎页），让百度自动跳登录页
                    # 登录页 redirecturl 参数会保证登录后跳回 /disk/main，满足我们的检测
                    url="https://pan.baidu.com/disk/main",
                    width=480, height=760,
                    min_size=(420, 600),
                )
                _log("登录窗口已创建，等待用户登录...")
                logged = False
                for i in range(180):  # 最多 3 分钟
                    _t.sleep(1)
                    try:
                        href = w.evaluate_js("location.href")
                        if isinstance(href, str) and ('/disk' in href or 'yun.baidu.com' in href or '/main' in href):
                            logged = True
                            _log("检测到已跳转到网盘主页，登录成功")
                            break
                    except Exception:
                        pass
                # 关键修复：不要关闭窗口！保留 WebView 实例供 get_baidu_dlink 复用。
                # 新开的 WKWebView 窗口读不到刚登录的 cookie（异步同步延迟，uk=0），
                # 只有复用同一个实例，cookie 在内存中才立即可用。
                self._baidu_wv = w
                try:
                    w.hide()  # 用 native hide（比 move(-20000) 可靠），保留实例供下载复用
                    _log("登录窗口已隐藏（保留实例供下载复用）")
                except Exception:
                    pass
                result_holder["value"] = {
                    "ok": True,
                    "logged": logged,
                    "message": "登录完成，百度网盘登录态已保存。"
                }
            except Exception as e:
                _log("登录窗口异常: " + str(e))
                result_holder["value"] = {"ok": False, "error": "EXCEPTION", "message": str(e)}
            finally:
                result_holder["event"].set()

        _th.Thread(target=_run, daemon=True).start()
        result_holder["event"].wait(timeout=200)
        return json.dumps(result_holder["value"])

    def save_commentary_file(self, job_id: str, filename: str) -> str:
        import requests
        from pathlib import Path
        url = f"http://{HOST}:{PORT}/api/commentary/{job_id}/file"
        downloads = Path.home() / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        name = filename or "解说成片.mp4"
        dest = downloads / name
        # 避免覆盖已有文件
        if dest.exists():
            stem, suf = dest.stem, dest.suffix
            i = 1
            while dest.exists():
                dest = downloads / f"{stem}({i}){suf}"
                i += 1
        try:
            r = requests.get(url, timeout=(10, 600))
            r.raise_for_status()
            dest.write_bytes(r.content)
        except Exception as exc:  # 把错误回传前端展示
            return f"ERROR: {exc}"
        return str(dest)

    def save_dw_file(self, job_id: str, kind: str, filename: str) -> str:
        """去水印结果写盘（桌面版原生下载，绕开 WKWebView 的 <a download> 限制）。

        kind: 'image' | 'pdf'；请求 /api/dw/{kind}/{job_id}/file，存到「下载」文件夹。
        返回保存路径或 "ERROR: ..."。
        """
        import requests
        from pathlib import Path
        if kind not in ("image", "pdf"):
            return "ERROR: 未知的去水印类型"
        url = f"http://{HOST}:{PORT}/api/dw/{kind}/{job_id}/file"
        downloads = Path.home() / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        name = filename or ("dewatered.png" if kind == "image" else "dewatered.pdf")
        dest = downloads / name
        # 避免覆盖已有文件
        if dest.exists():
            stem, suf = dest.stem, dest.suffix
            i = 1
            while dest.exists():
                dest = downloads / f"{stem}({i}){suf}"
                i += 1
        try:
            r = requests.get(url, timeout=(10, 600))
            r.raise_for_status()
            dest.write_bytes(r.content)
        except Exception as exc:  # 把错误回传前端展示
            return f"ERROR: {exc}"
        return str(dest)

    def save_dw_file_dialog(self, job_id: str, kind: str, suggested_name: str) -> str:
        """弹出系统保存面板让用户自选去水印结果位置（桌面版原生下载）。

        与 save_commentary_file_dialog 同一套机制：用 osascript `choose file name`
        弹原生窗口，避免 pywebview 主线程 run loop 阻塞（NSSavePanel 在主线程被同步
        JS 调用卡死）。取消返回 "CANCELLED"；osascript 不可用时退化为存「下载」文件夹。
        """
        import json
        import os
        import tempfile
        import requests
        import subprocess
        import datetime as _dt
        import threading as _th
        from pathlib import Path

        if kind not in ("image", "pdf"):
            return "ERROR: 未知的去水印类型"
        url = f"http://{HOST}:{PORT}/api/dw/{kind}/{job_id}/file"
        suggested = suggested_name or ("dewatered.png" if kind == "image" else "dewatered.pdf")
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()

        def _log(msg):
            try:
                with open("/tmp/vdl_dw_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        dest = None
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            prompt = "保存去水印结果" if kind == "image" else "保存去水印 PDF"
            # 写入临时 .applescript 文件(UTF-8) 再 osascript <file> 执行，
            # 避免中文经 argv 传给 osascript 被错误解码导致面板不弹。
            script = (
                f'set p to choose file name with prompt "{prompt}" '
                f'default name {name_json} '
                'default location (path to downloads folder)\n'
                'POSIX path of p'
            )
            fd, scpt = tempfile.mkstemp(suffix=".applescript")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(script)
            try:
                r = subprocess.run(
                    ["osascript", scpt],
                    capture_output=True, text=True, timeout=600,
                    env={**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
                )
                if r.returncode == 0 and r.stdout.strip():
                    dest = r.stdout.strip()
                    _log(f"osascript chose: {dest}")
                else:
                    _log(f"osascript cancelled/failed rc={r.returncode} err={r.stderr.strip()!r}")
                    return "CANCELLED"
            finally:
                try:
                    os.remove(scpt)
                except Exception:
                    pass
        except Exception as e:
            _log(f"osascript exception: {e!r}")
            dest = None

        if not dest:
            # 兜底：osascript 不可用时存到下载文件夹（符合默认行为）。
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            resp = requests.get(url, timeout=(10, 600))
            resp.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            # 避免覆盖已有文件
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            target.write_bytes(resp.content)
        except Exception as exc:
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {target}")
        return str(target)

    def save_commentary_file_dialog(self, cid: str, suggested_name: str) -> str:
        """弹出系统保存面板（默认目录=下载文件夹、预填文件名），用户可改位置/重命名。

        实现：用 osascript `choose file name` 子进程弹原生窗口。它不受 pywebview
        主线程 run loop 阻塞影响（之前的 NSSavePanel 在主线程被同步 JS 调用卡住，
        导致面板永远弹不出、后台线程永久挂起）。取消返回 "CANCELLED"；若 osascript
        不可用则退化为直接保存到「下载」文件夹（即默认行为）。
        """
        import json
        import os
        import tempfile
        import requests
        import subprocess
        import datetime as _dt
        import threading as _th
        from pathlib import Path

        def _log(msg):
            try:
                with open("/tmp/vdl_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        url = f"http://{HOST}:{PORT}/api/commentary/{cid}/file"
        suggested = suggested_name or "解说成片.mp4"
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()
        _log(f"enter thread={_th.current_thread().name} main={_th.current_thread() is _th.main_thread()} suggested={suggested!r}")

        dest = None
        # 主路径：osascript choose file name —— 真正的原生保存窗口，
        # 默认位置=下载文件夹、预填文件名，用户可改位置/重命名。任意线程可用。
        # 关键：必须写入临时 .applescript 文件（UTF-8）再 `osascript <file>` 执行，
        # 不能走 `osascript -e <脚本>` —— 中文经 argv 传给 osascript 时会被错误解码，
        # 触发「syntax error: 预期是引号，却找到未知的记号」导致面板不弹。
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            script = (
                'set p to choose file name with prompt "保存解说成片" '
                f'default name {name_json} '
                'default location (path to downloads folder)\n'
                'POSIX path of p'
            )
            fd, scpt = tempfile.mkstemp(suffix=".applescript")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(script)
            try:
                r = subprocess.run(
                    ["osascript", scpt],
                    capture_output=True, text=True, timeout=600,
                    env={**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
                )
                if r.returncode == 0 and r.stdout.strip():
                    dest = r.stdout.strip()
                    _log(f"osascript chose: {dest}")
                else:
                    _log(f"osascript cancelled/failed rc={r.returncode} err={r.stderr.strip()!r}")
                    return "CANCELLED"
            finally:
                try:
                    os.remove(scpt)
                except Exception:
                    pass
        except Exception as e:
            _log(f"osascript exception: {e!r}")
            dest = None

        if not dest:
            # 兜底：osascript 不可用时直接存到下载文件夹（符合默认行为）。
            _log("fallback -> ~/Downloads copy")
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            resp = requests.get(url, timeout=(10, 600))
            resp.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
            # 避免覆盖已有文件
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            target.write_bytes(resp.content)
        except Exception as exc:
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {target}")
        return str(target)

    # ── 窗口关闭 / 退出 行为区分（macOS 原生窗口）──
    # 设计：
    #   * 点窗口红叉 / 页面 X  → 仅「返回桌面」(最小化窗口，软件继续在后台运行)
    #   * 只有在窗口内显式点击「退出」按钮 → 真正退出整程序
    # 这样避免用户误触红叉把整个软件关掉。
    def hide_to_desktop(self) -> None:
        """返回桌面：最小化窗口，软件继续后台运行（点页面 X / 窗口红叉走这条）。"""
        try:
            if getattr(self, "window", None) is not None:
                self.window.minimize()
        except Exception:
            pass

    def quit_app(self) -> None:
        """真正退出软件（仅当用户在窗口内显式点击「退出」按钮时调用）。"""
        global _quitting
        _quitting = True  # 标记正在退出，让 closing 拦截器放行
        os._exit(0)       # 强制退出（不经过 closing 事件循环）


def _handle_exit(*_args) -> None:
    os._exit(0)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_self_build_version() -> str:
    """读取当前 .app 的构建指纹（由 build_mac.sh 写入 Resources/build_version.txt）。

    用于启动时的版本自检——若已有实例的版本比当前 .app 旧，则自动接管，
    彻底避免「双击后仍在跑旧版」导致的反复调试浪费。
    """
    try:
        p = BASE / "build_version.txt"
        if p.exists():
            return p.read_text("utf-8").strip()
    except Exception:
        pass
    return ""


def _kill_process_tree(pid: int, timeout: float = 3.0) -> bool:
    """向目标进程发 SIGTERM，等待其退出；超时则 SIGKILL。返回是否成功终止。"""
    import time as _t
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return not _pid_alive(pid)
    _deadline = _t.monotonic() + timeout
    while _t.monotonic() < _deadline:
        if not _pid_alive(pid):
            return True
        _t.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return not _pid_alive(pid)


def _activate_existing_window() -> None:
    """重复启动时把已有 VideoDownloader 窗口提到最前（macOS），并兜底打开浏览器。

    关键修复：原本仅靠 System Events 的 `set frontmost`，而该操作需要「辅助功能」
    权限，未授权时静默失败、双击看起来像「打不开」。现改为：先尽力用 System
    Events 提窗，失败（无权限/被拒）则直接打开浏览器访问已运行的本地服务，
    保证用户双击总能获得可用界面，绝不静默无响应。
    """
    port = None
    try:
        lp = Path.home() / ".vdl_instance.lock"
        if lp.exists():
            parts = lp.read_text().strip().split()
            if len(parts) >= 2 and parts[1].isdigit():
                port = int(parts[1])
    except Exception:
        pass
    # 兜底：直接打开浏览器访问已运行的本地服务（不依赖任何辅助功能权限）
    if port:
        try:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:
            pass
    # 尽力把原生窗口提到最前（无权限时失败也不影响上面的浏览器兜底）
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to set frontmost of '
                 '(every process whose name contains "VideoDownloader") to true'],
                check=False, capture_output=True,
            )
        except Exception:
            pass


def _ensure_single_instance():
    """同一用户只保留一个 GUI 实例，且确保运行的是最新构建版本。

    锁文件格式：`PID PORT BUILD_VERSION`
    - 拿到锁（无别的实例）→ 写自己信息，返回锁 handle，正常启动
    - 拿不到锁（有别实例）→
        * 旧实例版本 == 当前版本 → 激活窗口并退出（保持单实例，避免重复窗口）
        * 旧实例版本 ≠ 当前版本（旧版在跑、新版双击）→ **自动终止旧实例**，
          自己成为唯一实例（彻底解决「双击后仍在跑旧版」导致反复调试浪费的问题）
    """
    lock_path = Path.home() / ".vdl_instance.lock"
    # 清理僵尸锁（上次异常退出未释放且持有进程已死）
    try:
        if lock_path.exists():
            old = lock_path.read_text().strip()
            old_pid = old.split()[0] if old else ""
            if old_pid.isdigit() and not _pid_alive(int(old_pid)):
                try:
                    lock_path.unlink()
                except OSError:
                    pass
    except Exception:
        pass

    self_build = _read_self_build_version()

    # 尝试拿锁；拿到说明无别的实例
    try:
        f = open(lock_path, "w")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            _fcntl.flock(f, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        f.write(f"{os.getpid()} {PORT} {self_build}\n")
        f.flush()
        return f  # 调用方必须持有此对象直到退出
    except Exception:
        try:
            f.close()
        except Exception:
            pass

    # ── 拿不到锁：有别的实例在跑 ──
    old_pid = None
    old_build = ""
    try:
        old = lock_path.read_text().strip().split()
        if old and old[0].isdigit():
            old_pid = int(old[0])
        if len(old) >= 3:
            old_build = old[2]
    except Exception:
        pass

    # 旧实例版本 ≠ 当前版本（含旧格式锁文件无版本号的情况）→ 自动接管
    if old_pid and _pid_alive(old_pid) and old_build != self_build:
        print(
            f"[VDL] 检测到旧版本实例({old_build or '未知'})仍在运行，"
            f"当前已是最新({self_build})，自动接管并关闭旧实例…",
            file=sys.stderr,
        )
        _kill_process_tree(old_pid)
        # 旧进程退出后 flock 由内核自动释放；保险起见重写锁文件并占用
        try:
            lock_path.unlink()
        except OSError:
            pass
        try:
            f2 = open(lock_path, "w")
            _fcntl.flock(f2, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            f2.write(f"{os.getpid()} {PORT} {self_build}\n")
            f2.flush()
            return f2
        except Exception:
            try:
                f2.close()
            except Exception:
                pass

    # 版本相同 → 激活已有窗口并退出（保持单实例）
    _activate_existing_window()
    sys.exit(0)


def main() -> None:
    import uvicorn

    signal.signal(signal.SIGTERM, _handle_exit)
    signal.signal(signal.SIGINT, _handle_exit)

    # 主进程也注入 COMMENTARY_WORK_ROOT，让 _commentary_root 始终走用户可写目录，
    # 不污染包内的 Contents/Resources/commentary 与 Contents/Frameworks/commentary
    os.environ.setdefault("COMMENTARY_WORK_ROOT", str(_app_data_dir() / "commentary"))

    # 单实例：已有窗口则激活并退出，绝不创建第二个页面
    _lock = _ensure_single_instance()
    if _lock is None:
        _activate_existing_window()
        sys.exit(0)

    # 后台启动 FastAPI 服务
    server_thread = threading.Thread(
        target=uvicorn.run,
        kwargs={
            "app": "app:app",
            "app_dir": str(BASE) if getattr(sys, "frozen", False) else str(SERVER_DIR),
            "host": HOST,
            "port": PORT,
            "log_level": "info",
        },
        daemon=True,
    )
    server_thread.start()

    # 等服务器就绪
    for _ in range(30):
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        print(f"服务器启动超时，请手动访问 {URL}")
        server_thread.join()
        return

    # 开窗口 → 优先原生窗口（pywebview），回退浏览器
    try:
        import webview

        # ── macOS 退出语义修复：区分「红叉(Cmd+W)=返回桌面」与「Dock Quit(Cmd+Q)=彻底退出」──
        # pywebview 6.x 把两者都导向同一个 closing 事件；其 cocoa 后端中：
        #   * 红叉 / Cmd+W      → WindowDelegate.windowShouldClose_ → closing
        #   * Dock Quit / Cmd+Q → AppDelegate.applicationShouldTerminate_ → 同样经 closing
        # 由于我们让 closing 返回 False 实现「红叉=最小化」，会连带把 Dock Quit 也取消，
        # 导致右键 Quit 退不出来。修复：拦截 applicationShouldTerminate_（仅 Dock Quit 走这条），
        # 置 _app_terminating 标志并放行；红叉仍走 windowShouldClose → closing 返回 False 最小化。
        if sys.platform == "darwin":
            try:
                import Foundation as _Foundation
                import webview.platforms.cocoa as _cocoa
                _AD = _cocoa.BrowserView.AppDelegate

                def _patch_app_should_terminate(self, app):
                    global _app_terminating
                    _app_terminating = True  # 来自 Dock Quit / Cmd+Q
                    return _Foundation.YES    # 始终允许退出

                _AD.applicationShouldTerminate_ = _patch_app_should_terminate
            except Exception as _e:
                print(
                    f"[VDL] 无法 patch applicationShouldTerminate（Dock Quit 可能退不出）: {_e}",
                    file=sys.stderr,
                )

        api = VdlApi()
        window = webview.create_window(
            title="VideoDownloader",
            url=URL,
            width=1100,
            height=750,
            min_size=(800, 500),
            text_select=True,
            js_api=api,
        )
        # 把 window 引用交给桥接 API，供「返回桌面」/「退出」按钮调用
        api.window = window

        # 窗口行为说明（macOS 原生窗口）：
        #   - 点窗口红叉 / Cmd+W  → closing 拦截 → 最小化到 Dock（返回桌面，软件常驻）
        #   - 前端「返回桌面」按钮 → api.hide_to_desktop() → window.minimize()（最小化常驻）
        #   - 前端「退出」按钮    → api.quit_app() → os._exit(0) 强制退出
        #   - 顶部菜单 Cmd+Q / Dock 右键 Quit → 彻底退出（经 AppDelegate.applicationShouldTerminate_）
        # ── 窗口关闭 vs 退出软件（macOS 原生窗口）──
        # 全局标志：quit_app() 设 _quitting；Dock Quit/Cmd+Q 经 applicationShouldTerminate_ 设 _app_terminating；
        # 二者任一为真时 closing 拦截器放行真正退出，否则红叉最小化（返回桌面）。
        _quitting = False

        def _on_closing(*_args):
            """拦截窗口关闭事件：
            - 点红叉 / Cmd+W → 最小化到 Dock（返回桌面，软件常驻）
            - Dock Quit / Cmd+Q（_app_terminating）或 显式「退出」按钮（_quitting）→ 放行退出
            """
            if _quitting or _app_terminating:
                return True  # 正在退出，放行
            try:
                window.minimize()
            except Exception:
                pass
            return False  # 取消关闭，用最小化代替（返回桌面）

        window.events.closing += _on_closing

        # Windows 端退出 webview 后清理资源
        webview.start()
        os._exit(0)
    except ImportError:
        import webbrowser
        import subprocess as _sp
        # 桌面通知仅 macOS 支持；Windows 无 osascript，必须用 try 包裹避免崩溃
        if sys.platform == "darwin":
            try:
                _sp.run(
                    ["osascript", "-e",
                     f'display notification "VideoDownloader 已启动" with title "{URL}"'],
                    check=False, capture_output=True,
                )
            except Exception:
                pass
        webbrowser.open(URL)
        # 保持进程存活直到 uvicorn 退出
        server_thread.join()


if __name__ == "__main__":
    if "--vdl-commentary-worker" in sys.argv:
        # 单二进制双角色：自身重入为解说管线 worker（依赖随包内置，无需外部 Python）
        raise SystemExit(_run_commentary_worker(sys.argv))
    main()
