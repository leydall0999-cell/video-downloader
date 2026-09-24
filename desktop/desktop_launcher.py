"""视频工坊桌面版启动器（由 PyInstaller 打包为 .app / .exe）。

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

    另支持 VDL_PORT 固定端口——打包后的 .app 双击即可固定本地服务端口，便于调试与规避端口冲突。

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

def _update_active_port(p: int, lock_file=None) -> None:
    """绑定成功后回写实际端口到全局变量与单实例锁文件，
    保证原生窗口、connect 轮询、VdlApi 保存对话框全部指向真实监听端口。"""
    global PORT, API_URL, URL
    PORT = p
    API_URL = f"http://{HOST}:{PORT}"
    URL = f"http://{HOST}:{PORT}"
    if lock_file is not None:
        try:
            lock_file.seek(0)
            cur = lock_file.read().strip()
            parts = cur.split()
            pid = parts[0] if parts else os.getpid()
            build = parts[2] if len(parts) >= 3 else ""
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(f"{pid} {PORT} {build}\n")
            lock_file.flush()
        except Exception:
            pass

def _run_server_with_retry(start_port: int, host: str, app_dir: str, lock_file=None) -> None:
    """启动 uvicorn；若首端口绑定失败（EADDRINUSE 等），自动顺延到下一端口重试，
    最多尝试 12 个端口，彻底消除“kill 旧实例后立刻启动新实例”的端口竞态导致的假启动。"""
    import uvicorn
    max_off = 12
    last_err = None
    for off in range(max_off):
        p = start_port + off
        try:
            _update_active_port(p, lock_file)
            _launch_log(f"尝试在端口 {p} 启动后端服务...")
            uvicorn.run("app:app", app_dir=app_dir, host=host, port=p, log_level="info")
            return
        except OSError as e:
            last_err = e
            _launch_log(f"端口 {p} 绑定失败（{e}），自动重试下一端口 {p+1}")
            continue
        except SystemExit as e:
            # 冻结版 uvicorn 在 bind 失败时抛 SystemExit(非0)，需当作可重试的绑定失败；
            # 仅 code==0 视为干净退出，不再重试。
            code = getattr(e, "code", 1) or 1
            if code == 0:
                return
            last_err = e
            _launch_log(f"端口 {p} 启动进程异常退出(code={code})，自动重试下一端口 {p+1}")
            continue
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "address already in use" in msg or "bind" in msg:
                last_err = e
                _launch_log(f"端口 {p} 启动失败（{e}），自动重试下一端口 {p+1}")
                continue
            raise
    _launch_log(f"所有候选端口({start_port}..{start_port+max_off-1})均绑定失败，后端无法启动: {last_err}")

def _logged_run_server(start_port: int, host: str, app_dir: str, lock_file=None) -> None:
    """包装 _run_server_with_retry：任何未捕获异常都写进启动日志再死。

    历史教训（2026-09-15）：Dock 启动（open -a）时后端线程曾静默崩溃，日志只留
    「服务器启动超时」，但 nohup 直跑一切正常，两者唯一差别是运行方式 → 排查成本
    极高。没有这段包装，下次还得靠猜。
    """
    try:
        _run_server_with_retry(start_port, host, app_dir, lock_file)
    except BaseException as exc:  # noqa: BLE001
        import traceback as _tb
        _launch_log("后端线程异常终止: %s: %s\n%s"
                    % (type(exc).__name__, exc, _tb.format_exc()))


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


def _choose_save_path_via_osascript(prompt: str, suggested: str, log_path: str = "") -> str:
    """★ 兜底路径（面板会是**英文**）：直接调 osascript 的 `choose file name`。

    弹 macOS 原生「保存文件」面板，返回用户选定的绝对路径；取消返回 "CANCELLED"。

    为什么用 osascript 子进程而不是 pywebview/NSSavePanel：桌面壳的主线程跑在
    pywebview 的 run loop 上，从 JS 同步桥调用里弹 NSSavePanel 会把 run loop 卡死。
    另起 osascript 进程则完全不碰主线程。

    中文提示词/默认名必须写进**临时 .applescript 文件（UTF-8）**再交给 osascript 执行，
    经 argv 传中文会被错误解码导致面板根本不弹（历史踩坑）。

    返回 "CANCELLED" 与 "ERROR: ..." 是约定值，前端据此区分「用户主动取消」与「真失败」。
    """
    import json
    import tempfile
    import subprocess

    def _log(msg: str) -> None:
        if not log_path:
            return
        try:
            import datetime as _dt
            with open(log_path, "a") as f:
                f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
        except Exception:
            pass

    script = (
        f'set p to choose file name with prompt "{prompt}" '
        f'default name {json.dumps(suggested, ensure_ascii=False)} '
        'default location (path to downloads folder)\n'
        'POSIX path of p'
    )
    scpt = ""
    try:
        fd, scpt = tempfile.mkstemp(suffix=".applescript")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        r = subprocess.run(
            ["osascript", scpt],
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
        )
        if r.returncode == 0 and r.stdout.strip():
            chosen = r.stdout.strip()
            _log(f"choose-save-path -> {chosen}")
            return chosen
        # 用户点了「取消」，osascript 退出码为 1 且 stderr 形如 "User canceled."。
        _log(f"choose-save-path cancelled rc={r.returncode} err={r.stderr.strip()!r}")
        return "CANCELLED"
    except Exception as exc:  # noqa: BLE001 — osascript 缺失/异常 → 交给调用方兜底
        _log(f"choose-save-path exception: {exc!r}")
        return ""
    finally:
        if scpt:
            try:
                os.remove(scpt)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# 原生「保存到哪里」面板（中文本地化版）
#
# ★ 2026-09-22 用户报「面板/替换提示是英文，改中文」。根因实测（非推断）：
#   系统语言确实是中文（AppleLanguages = zh-Hans-CN），但 CLI 形态的 `osascript`
#   自身**没有中文本地化资源**，它弹出来的面板一律走英文 —— 连文件夹都显示成
#   "Downloads" 而不是「下载」。三种常见偏方**实测全部无效**：
#     ① env LANG/LC_ALL=zh_CN.UTF-8   ② osascript -AppleLanguages '(zh-Hans-CN)'
#     ③ 伪造 __CFBundleIdentifier + defaults 写 AppleLanguages
#   唯一有效：把同一段 AppleScript 用 `osacompile` 编成**真正的 .app 程序**，
#   并在 Info.plist 声明中文优先（CFBundleDevelopmentRegion=zh-Hans +
#   CFBundleLocalizations），再用 `open -W` 启动它 —— 面板立即全中文
#   （存储为 / 标签 / 位置: 下载 / 取消 / 存储，已实测截图核对）。
#
# 入参/结果用文件交换：`open --args` 传不进 applet 的 `on run argv`（实测无效），
# 所以用   <数据目录>/in.txt  = 随机数 \n 提示语 \n 默认文件名
#          <数据目录>/out.txt = 随机数 \n 选定路径|CANCELLED
# 随机数用来识别「这一次」的结果，避免读到上一次的残留文件而被误判。
# applet 只在首次（或版本升级）编译一次，之后一直复用。
# ---------------------------------------------------------------------------
_SAVE_PANEL_APPLET_SRC = r'''on run
	set basePath to (POSIX path of (path to home folder)) & ".video-downloader/save_panel/"
	set inPath to basePath & "in.txt"
	set outPath to basePath & "out.txt"
	set theNonce to "0"
	set thePrompt to "Save file to"
	set theName to "untitled"
	try
		set rawText to (do shell script "cat " & quoted form of inPath)
		set AppleScript's text item delimiters to linefeed
		set theParts to text items of rawText
		if (count of theParts) is less than 3 then
			set AppleScript's text item delimiters to return
			set theParts to text items of rawText
		end if
		if (count of theParts) is greater than or equal to 3 then
			set theNonce to item 1 of theParts
			set thePrompt to item 2 of theParts
			set theName to item 3 of theParts
		end if
		set AppleScript's text item delimiters to ""
	end try
	set theResult to "CANCELLED"
	-- ★ 2026-09-24：applet 现在是 LSUIElement 后台程序（不占 Dock、不占切换器），
	--   agent 程序不会自动激活，面板可能弹在别的窗口后面。先 activate 顶到最前。
	--   （对普通前台程序，activate 本就是默认行为，无副作用。）
	try
		activate
	end try
	try
		set chosenFile to choose file name with prompt thePrompt default name theName default location (path to downloads folder)
		set theResult to POSIX path of chosenFile
	end try
	do shell script "echo " & quoted form of theNonce & " > " & quoted form of outPath & "; echo " & quoted form of theResult & " >> " & quoted form of outPath
end run
'''

_SAVE_PANEL_APPLET_CACHE = {"path": None}

# ★ 2026-09-24：applet「形态」版本号。凡改动 Info.plist 侧行为（图标 / 本地化 / LSUIElement）
#   都要 bump 一次 —— 指纹里带上它，老用户机器上的 applet 才会被重编（否则改了不生效）。
_SAVE_PANEL_APPLET_VER = "2026-09-24-lsuielement"


def _save_panel_dir() -> str:
    """保存面板的入参/结果/applet 数据目录（`~/.video-downloader/save_panel`）。"""
    d = os.path.join(os.path.expanduser("~"), ".video-downloader", "save_panel")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        return ""
    return d


def _save_panel_icon_bytes() -> bytes:
    """读 App 正式图标（desktop/icon.icns），用于替换 osacompile 默认卷曲纸图标。

    查找顺序：源码态 `desktop/icon.icns`（与本文件同目录）→ 打包态
    `VideoDownloader.app/Contents/Resources/icon.icns`（sys.executable 同级的
    Resources）。都找不到返回 b""（applet 保持默认图标，功能不受影响）。

    背景（2026-09-22 用户截图）：osacompile 编译的 applet 用 AppleScript 默认
    「卷曲纸卷」图标，弹保存面板时 Dock/切换器里冒出陌生图标，用户截图来问
    「这是什么」。换成正式 App 图标后可辨识。
    """
    candidates = []
    try:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.icns"))
    except Exception:
        pass
    try:
        candidates.append(os.path.join(os.path.dirname(sys.executable), "..", "Resources", "icon.icns"))
    except Exception:
        pass
    for cand in candidates:
        try:
            with open(cand, "rb") as fh:
                data = fh.read()
            if data:
                return data
        except Exception:
            continue
    return b""


def _ensure_save_panel_applet(log_path: str = "") -> str:
    """确保存在「中文本地化」的保存面板 applet，返回其绝对路径；不可用返回 ""。

    失败（没有 osacompile / 无写权限 / 编译报错）时返回 ""，调用方会回落到
    osascript 那条老路 —— 功能一样，只是面板文案是英文。
    """
    import subprocess
    import shutil
    import hashlib

    cached = _SAVE_PANEL_APPLET_CACHE.get("path")
    if cached and os.path.isdir(cached):
        return cached

    d = _save_panel_dir()
    if not d:
        return ""
    app = os.path.join(d, "SavePanel.app")
    info = os.path.join(app, "Contents", "Info.plist")
    ver_file = os.path.join(d, "SavePanel.version")
    # ★ 按源码指纹判断是否需要重编：只按「plist 里有 zh-Hans」判断的话，
    #   以后改了 _SAVE_PANEL_APPLET_SRC，老用户会一直用旧 applet（改了不生效）。
    #   指纹同时纳入图标内容 —— 图标换版也要重编落盘（2026-09-22）。
    icon_bytes = _save_panel_icon_bytes()
    icon_sig = hashlib.sha1(icon_bytes).hexdigest()[:16] if icon_bytes else "noicon"
    sig = hashlib.sha1((hashlib.sha1(_SAVE_PANEL_APPLET_SRC.encode("utf-8")).hexdigest()
                        + icon_sig + _SAVE_PANEL_APPLET_VER).encode("utf-8")).hexdigest()[:16]

    if os.path.isfile(info) and os.path.isfile(ver_file):
        try:
            with open(ver_file, "r", encoding="utf-8", errors="ignore") as fh:
                same_ver = fh.read().strip() == sig
            with open(info, "r", encoding="utf-8", errors="ignore") as fh:
                plist_old = fh.read()
            # 除了中文优先级，还要确认「后台形态」已写进 plist：09-22 版 applet 没有
            # LSUIElement，会占着 Dock 一个磁贴（用户截图问「为什么多一个一样的图标」）。
            same_loc = ("zh-Hans" in plist_old) and ("LSUIElement" in plist_old)
            if same_ver and same_loc:
                _SAVE_PANEL_APPLET_CACHE["path"] = app
                return app
        except Exception:  # noqa: BLE001
            pass

    src = os.path.join(d, "SavePanel.applescript")
    try:
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(_SAVE_PANEL_APPLET_SRC)
        shutil.rmtree(app, ignore_errors=True)
        r = subprocess.run(
            ["osacompile", "-o", app, src],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0 or not os.path.isfile(info):
            return ""
        # ★ 用 App 正式图标覆盖 osacompile 的默认「卷曲纸」图标（2026-09-22）。
        #   不换的话弹面板时 Dock/切换器里出现陌生图标，用户不知道那是什么。
        if icon_bytes:
            try:
                with open(os.path.join(app, "Contents", "Resources", "applet.icns"), "wb") as fh:
                    fh.write(icon_bytes)
                # bump mtime 促使 LaunchServices/Dock 重读图标缓存
                subprocess.run(["touch", app], capture_output=True, timeout=10)
            except Exception:
                pass
        # ★ 关键一步：声明中文优先。不写这两项，系统面板仍按英文渲染。
        # ★ 追加 LSUIElement：让 applet 成为「后台程序」（agent）—— 面板照常弹、
        #   文案照旧中文，但**不再出现在 Dock 和 Cmd-Tab 里**。
        #   背景（2026-09-24 用户截图）：09-22 把 applet 图标/CFBundleName 都改成了
        #   「视频工坊」，于是弹保存面板时 Dock 里多出一个**一模一样的图标**（还带运行小圆点），
        #   看起来像 App 被双开。lsappinfo 实测该进程 type="Foreground" → 确有 Dock 磁贴。
        for args in (
            ["-replace", "CFBundleDevelopmentRegion", "-string", "zh-Hans"],
            ["-insert", "CFBundleLocalizations", "-json", '["zh-Hans","en"]'],
            ["-replace", "CFBundleName", "-string", "视频工坊"],
            ["-insert", "LSUIElement", "-bool", "true"],
        ):
            try:
                subprocess.run(["plutil"] + args + [info], capture_output=True, text=True, timeout=30)
            except Exception:  # noqa: BLE001
                pass
        try:
            with open(info, "r", encoding="utf-8", errors="ignore") as fh:
                _plist_after = fh.read()
            if "zh-Hans" not in _plist_after:
                return ""
            if "LSUIElement" not in _plist_after:
                return ""   # 没写进去就当不可用，回落 osascript（宁可英文面板，也不要多一个 Dock 图标）
        except Exception:  # noqa: BLE001
            return ""
        try:
            with open(ver_file, "w", encoding="utf-8") as fh:
                fh.write(sig)
        except Exception:  # noqa: BLE001
            pass
        _SAVE_PANEL_APPLET_CACHE["path"] = app
        return app
    except Exception:  # noqa: BLE001
        return ""


def _kill_save_panel_applet(app: str) -> None:
    """杀掉我们自己的保存面板 applet 进程（只匹配该 applet 的可执行路径，绝不误伤 App 本体）。

    用途：① 上一次面板还挂着（用户点红点关窗 / 面板被切到后台）时，新一次保存不应再叠一个
    面板；② 看门狗超时兜底，避免 applet 永久僵住。
    """
    import subprocess
    try:
        exe = os.path.join(app, "Contents", "MacOS", "applet")
        subprocess.run(["pkill", "-f", exe], capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _choose_save_path_via_applet(prompt: str, suggested: str, log_path: str = "",
                                 timeout_s: float = 1800.0) -> str:
    """走中文本地化 applet 弹面板：返回选定路径 / "CANCELLED"；机制不可用返回 ""。

    ⚠️ 只有「applet 压根不可用」（编译失败 / 目录不可写）才返回 "" 让调用方回落英文面板；
    applet 一旦成功弹出来，任何拿不到结果的情况都按 **"CANCELLED"** 处理 —— 否则会紧接着
    再弹一个英文 osascript 面板，用户看到的是「取消了一个又冒出一个」。
    `timeout_s` 是面板等待上限（看门狗），超时即杀掉 applet 并按取消处理。
    """
    import subprocess
    import time
    import secrets

    app = _ensure_save_panel_applet(log_path)
    if not app:
        return ""
    d = _save_panel_dir()
    if not d:
        return ""
    in_path = os.path.join(d, "in.txt")
    out_path = os.path.join(d, "out.txt")
    nonce = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    # 先清掉上一次遗留的 applet：面板没关/关窗后挂住时，Dock（老版本）或后台会残一个，
    # 而且两个面板叠着也没法用 —— 本次保存为准。
    _kill_save_panel_applet(app)
    try:
        os.remove(out_path)  # 清掉上一次的残留，只认本次随机数
    except OSError:
        pass
    try:
        with open(in_path, "w", encoding="utf-8") as fh:
            fh.write(f"{nonce}\n{prompt}\n{suggested}\n")
    except Exception:  # noqa: BLE001
        return ""
    try:
        # -W：等 applet 退出（= 用户关掉面板）再返回，天然同步。
        # 用 Popen + 看门狗而不是 run(timeout=…)：run 超时会抛异常、连通 returncode 都拿不到，
        # 老的兜底路径会再弹一个英文面板。这里超时就**杀 applet + 按取消**，干净利落。
        proc = subprocess.Popen(["open", "-W", app], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, text=True)
        remained = float(timeout_s)
        while proc.poll() is None and remained > 0:
            time.sleep(0.2)
            remained -= 0.2
        if proc.poll() is None:
            _kill_save_panel_applet(app)
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            return "CANCELLED"   # 超时不回落（避免第二个英文面板）
    except Exception:  # noqa: BLE001
        return ""                # open 都起不来 → 交给调用方回落
    if proc.returncode != 0:
        # applet 被我们自己杀掉（上一次遗留/看门狗）或异常退出 → 按取消处理，不回落。
        return "CANCELLED"
    try:
        with open(out_path, "r", encoding="utf-8") as fh:
            parts = fh.read().splitlines()
    except Exception:  # noqa: BLE001
        return "CANCELLED"
    if len(parts) < 2 or parts[0].strip() != nonce:
        return "CANCELLED"  # 结果不是本次的（被后续请求顶掉）→ 当作用户取消，不回落
    res = parts[1].strip()
    if not res or res == "CANCELLED":
        return "CANCELLED"
    return res


def _choose_save_path(prompt: str, suggested: str, log_path: str = "") -> str:
    """弹系统「保存文件」面板，返回用户选定的绝对路径；取消返回 "CANCELLED"。

    两级：先走**中文本地化 applet**（面板全中文）；applet 不可用时回落
    `osascript`（功能一致，但面板文案是英文）。
    返回 "" 表示面板机制整体不可用，由调用方决定兜底位置。
    """
    got = _choose_save_path_via_applet(prompt, suggested, log_path)
    if got:
        return got
    return _choose_save_path_via_osascript(prompt, suggested, log_path)


class VdlApi:
    """暴露给前端 JS 的桥接 API（仅 pywebview 桌面模式生效）。

    桌面版无法通过 <a download> 触发系统保存框（WebKit 限制），
    因此解说成片的「保存到本机」改由前端调用本 API，由 Python 直接把文件
    复制到用户「下载」文件夹；返回保存路径或 "ERROR: ..."。
    """

    def open_external(self, url: str) -> str:
        """在系统默认浏览器中打开外部 URL（如第三方授权页）。

        pywebview 的 WKWebView 不支持 window.open() 弹窗（会被静默拦截），
        因此外部授权改由 Python 调系统浏览器打开。返回 "OK" 或 "ERROR: ..."。
        """
        import webbrowser
        try:
            webbrowser.open(url)
            return "OK"
        except Exception as exc:  # 把错误回传前端展示
            return f"ERROR: {exc}"

    def choose_folder(self) -> str:
        """弹出系统文件夹选择框，返回所选目录绝对路径；用户取消或失败返回空串。

        ★ 2026-08-28 续33 关键修复：原先用 tkinter.Tk() / filedialog.askdirectory，
        但 pywebview 的 JS bridge 在 macOS cocoa 后端**未必在 main thread** 同步调
        这条链 → 在 non-main-thread 触 `TkMacOSXMakeRealWindowExist` →
        `NSWindow drag regions should only be invalidated on the Main Thread!`
        SIGTRAP → macOS 弹 force quit「应用没响应」+ 主循环饿死，整个桌面 app 卡死。

        改为走 pyobjc 自己的 NSOpenPanel（cocoa 主线程原生 panel），100% 不走 Tk。
        """
        try:
            import os as _os
            # AppleScript 弹「选择文件夹」原生 panel（cocoa 主线程，纯 macOS API）
            # 跨线程最稳：osascript 子进程隔离 NSWindow 主线程约束
            script = (
                'set theFolder to choose folder with prompt "选择剪映草稿导出目录"\n'
                'return POSIX path of theFolder\n'
            )
            try:
                proc = _os.popen(f"osascript -e '{script.replace(chr(10), "' -e '").replace("'", chr(39))}' 2>&1")
                out = proc.read().strip()
                if out and not out.startswith("execution error"):
                    return out.rstrip('/')
            except Exception:
                pass

            # osascript 兜底失败 → 落到纯手工输入（前端已有 fallback 回退）
            return ""
        except Exception as exc:
            return f"ERROR: {exc}"

    def choose_files(self, kind: str = "media") -> list[str] | str:
        """弹出系统多文件选择框，返回所选文件绝对路径列表；用户取消或失败返回空串。

        kind 决定可选扩展名与对话框标题（★ 2026-09-11 修复「高效压缩/图片转换里图片是灰的、选不了」）：
          - "media"（默认）：视频 + 音频（桥接/转码/字幕等原有场景，保持行为不变）
          - "image"        ：图片（图片转换视图，含 bmp/tif/gif）
          - "video"        ：纯视频（高清修复的视频档，2026-09-12 新增；
                             不含音频扩展名，避免用户选到 mp3 才被后端拒绝）
          - "any"          ：视频 + 音频 + png/jpg/jpeg/webp（高效压缩视图；
                             刻意不含 heic/avif/bmp/tif/gif，后端压缩不支持，避免「选得到却报错」）
        前端未传参时按 "media" 处理，向后兼容旧调用。

        ★ 2026-08-28 续33 关键修复：移除 tkinter.Tk() 调用（见 choose_folder 注释）。
        同样改用 osascript `choose file with multiple selections allowed` 子进程弹窗，
        不依赖 Tk，跨线程 100% 安全。

        ★ 2026-08-28 续45 关键修复：原版用 "\\\\n".join(...) 把多语句压成单行再走
        osascript -e，osascript 收不到真换行 → 整个脚本解析失败 → 前端 chooseFiles
        返回空数组 → 「添加文件」按钮无任何反应。改为 choose_folder 同款：每条语句
        单独 -e 喂入（真换行由 shell 拆 -e 参数实现）。
        """
        try:
            import subprocess as _sp
            _MEDIA_EXTS = [
                "mp4", "mov", "mkv", "webm", "avi", "flv", "ts", "wmv",
                "mpeg", "mpg", "3gp", "ogv", "m4v",
                "mp3", "m4a", "aac", "wav", "flac", "ogg", "opus",
            ]
            # 图片转换视图（UPLOAD_IMAGE_EXTS 等同）：Pillow 原生可开的位图
            _IMAGE_EXTS = [
                "png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif",
            ]
            # 高效压缩视图 only 吃 png/jpg/jpeg/webp（与 server/routers/compress.py 一致）；
            # 故意不含 heic/avif/bmp/tif/gif —— 后端不处理，放进来会变成「选得到但报不支持」。
            _COMPRESS_IMAGE_EXTS = ["png", "jpg", "jpeg", "webp"]
            _VIDEO_EXTS = [
                "mp4", "mov", "mkv", "webm", "avi", "flv", "wmv", "m4v",
            ]
            _KINDS = {
                "media": (_MEDIA_EXTS, "选择要桥接的视频/音频文件（可多选）"),
                "image": (_IMAGE_EXTS, "选择图片文件（可多选）"),
                "video": (_VIDEO_EXTS, "选择要增强的视频文件（可多选）"),
                "any": (_MEDIA_EXTS + _COMPRESS_IMAGE_EXTS, "选择视频/音频/图片文件（可多选）"),
            }
            exts, prompt = _KINDS.get(str(kind or "media").strip().lower(), _KINDS["media"])
            # AppleScript 列表必须用引号+逗号+空格：`{"mp4", "mov"}`。
            # `of type` 是 choose file 的子句，必须与 choose file 同一行。
            ext_list = "{" + ", ".join(f'"{e}"' for e in exts) + "}"
            # AppleScript 限制：choose file ... of type ... with ... 必须同一行；
            # 后续 repeat / return 可独立 -e 喂入。
            script_lines = [
                f'set theFiles to choose file of type {ext_list} with multiple selections allowed with prompt "{prompt}"',
                'set out to ""',
                'repeat with f in theFiles',
                '  set out to out & POSIX path of f & linefeed',
                'end repeat',
                'return out',
            ]
            # 每条语句独立 -e（与 choose_folder 同款），osascript 才会按真换行解析
            cmd = ["osascript"]
            for line in script_lines:
                cmd += ["-e", line]
            try:
                proc = _sp.run(cmd, capture_output=True, text=True, timeout=120)
                out = (proc.stdout or "").strip()
                if out and "execution error" not in out and "User canceled" not in out:
                    # osascript 多选用 linefeed 分隔，单选返回单行
                    paths = [line.strip() for line in out.split("\n") if line.strip()]
                    if len(paths) == 1:
                        return paths[0]
                    return paths
            except Exception:
                pass

            # 用户取消 或 失败 → 返回空串让前端走手动输入 fallback
            return ""
        except Exception as exc:
            return f"ERROR: {exc}"

    def pick_voice_sample(self) -> str:
        """选择「我的音色」参考音频：弹系统文件选择框，返回绝对路径；取消返回空串。

        与 choose_folder / choose_files 同套机制（osascript 子进程弹原生 panel），
        刻意不用 tkinter：bridge 未必跑在 main thread，Tk 会触发
        `NSWindow ... Main Thread` 约束直接 SIGTRAP，把整个 App 卡死。
        单选 + 限定音频类型（克隆样本只可能是音频）。
        """
        try:
            import subprocess as _sp
            exts = ["wav", "mp3", "m4a", "flac", "aac", "ogg", "opus", "mp4"]
            ext_list = "{" + ", ".join(f'"{e}"' for e in exts) + "}"
            script_lines = [
                f'set theFile to choose file of type {ext_list} with prompt '
                f'"选择你的音色样本（念一句话的录音，3~15 秒最佳）"',
                "return POSIX path of theFile",
            ]
            cmd = ["osascript"]
            for line in script_lines:
                cmd += ["-e", line]
            proc = _sp.run(cmd, capture_output=True, text=True, timeout=180)
            out = (proc.stdout or "").strip()
            if out and "execution error" not in out and "User canceled" not in out:
                return out
            return ""
        except Exception as exc:
            return f"ERROR: {exc}"

    def start_indextts_mlx(self) -> dict:
        """一键开启本地语音克隆（IndexTTS-MLX）。

        普通用户无需理解「端口/服务」等概念：本方法自动在常见位置寻找语音克隆包，
        启动其本地服务，并返回大白话结果。找不到包或启动失败时也给出可操作的提示，
        不会抛出未捕获异常（前端会友好展示 msg）。
        """
        import os
        import subprocess

        pack_dirs = [
            os.path.expanduser("~/Downloads/IndexTTS-MLX-1.5-Pack"),
            os.path.expanduser("~/Desktop/IndexTTS-MLX-1.5-Pack"),
            os.path.expanduser("~/Documents/IndexTTS-MLX-1.5-Pack"),
            os.path.expanduser("~/Applications/IndexTTS-MLX-1.5-Pack"),
            "/Applications/IndexTTS-MLX-1.5-Pack",
            os.path.expanduser("~/IndexTTS-MLX-1.5-Pack"),
        ]
        pack = next((d for d in pack_dirs if os.path.isdir(d)), None)
        if not pack:
            return {
                "ok": False,
                "msg": "还没装「语音克隆」包。请把 IndexTTS-MLX-1.5-Pack 解压到「下载」文件夹，再点一次此按钮即可。",
            }

        # 兼容不同解压结构：优先在包根目录找启动脚本，再递归向下找一层
        launch_scripts = ["app.py", "launch.py", "demo.py", "server.py", "start.py", "main.py"]
        target = None
        for s in launch_scripts:
            p = os.path.join(pack, s)
            if os.path.isfile(p):
                target = p
                break
        if target is None:
            for root, dirs, files in os.walk(pack):
                if "node_modules" in root or ".git" in root:
                    continue
                for f in files:
                    if f in launch_scripts:
                        target = os.path.join(root, f)
                        break
                if target:
                    break
        if target is None:
            return {
                "ok": False,
                "msg": "在「语音克隆」文件夹里没找到启动脚本，请确认解压完整（不要只解压了子文件夹）。",
            }

        # 选解释器：优先用包内 venv，否则退回系统 python3
        target_dir = os.path.dirname(target)
        py_cands = [
            os.path.join(pack, "venv", "bin", "python"),
            os.path.join(target_dir, "venv", "bin", "python"),
            os.path.join(pack, "venv", "Scripts", "python.exe"),
            "python3",
            "python",
        ]
        py = None
        for c in py_cands:
            if c in ("python3", "python"):
                py = c
                break
            if os.path.isfile(c):
                py = c
                break
        if py is None:
            py = "python3"

        try:
            log_path = os.path.expanduser("~/.vdl_indextts.log")
            with open(log_path, "w") as lf:
                subprocess.Popen(
                    [py, target],
                    cwd=target_dir,
                    stdout=lf,
                    stderr=lf,
                    start_new_session=True,
                )
            return {
                "ok": True,
                "msg": "已开始启动，约 10–30 秒后在「配音引擎」下拉里会显示「已就绪，可直接用」。",
            }
        except Exception as exc:  # 把错误回传前端展示
            return {"ok": False, "msg": f"启动失败：{exc}"}

    def start_qwen3tts(self) -> dict:
        """选中「Qwen3-TTS 本地语音克隆」引擎时由前端自动调用：拉起本机 7871 服务。

        与 IndexTTS-MLX 同思路：在常见位置找启动脚本与 venv，起服务，返回大白话结果。
        找不到/启动失败时给可操作提示，不抛未捕获异常。
        """
        import os
        import socket
        import subprocess

        dev_root = os.path.expanduser("~/WorkBuddy/问问题/commentary-pipeline")
        c_dir = os.environ.get("VDL_COMMENTARY_DIR", "")
        script_cands = [
            os.path.join(str(BASE), "commentary", "scripts", "start_qwen3tts_server.py"),
            os.path.join(c_dir, "scripts", "start_qwen3tts_server.py") if c_dir else None,
            os.path.join(dev_root, "scripts", "start_qwen3tts_server.py"),
        ]
        script = next((s for s in script_cands if s and os.path.isfile(s)), None)
        if not script:
            return {
                "ok": False,
                "msg": "没找到 Qwen3-TTS 启动脚本（应在 commentary/scripts/start_qwen3tts_server.py）。请确认解说管线已安装。",
            }

        # 🔴 优先 MLX venv（.venv_qwen3tts_mlx）：Apple GPU 直跑，实测 RTF≈1.05；
        #    旧 torch venv 在 macOS 13 无 MPS，纯 CPU 一句 3 字都要跑几分钟。
        #    两者都放候选里，脚本自身 --engine auto 也会在 torch venv 里 execv 切到 MLX。
        venv_cands = [
            os.path.join(os.path.dirname(script), ".venv_qwen3tts_mlx", "bin", "python"),
            os.path.join(os.path.dirname(script), ".venv_qwen3tts", "bin", "python"),
            os.path.join(dev_root, "scripts", ".venv_qwen3tts_mlx", "bin", "python"),
            os.path.join(dev_root, "scripts", ".venv_qwen3tts", "bin", "python"),
            os.path.join(c_dir, "scripts", ".venv_qwen3tts_mlx", "bin", "python") if c_dir else None,
            os.path.join(c_dir, "scripts", ".venv_qwen3tts", "bin", "python") if c_dir else None,
            os.path.expanduser("~/.video-downloader/venvs/qwen3tts_mlx/bin/python"),
            os.path.expanduser("~/.video-downloader/venvs/qwen3tts/bin/python"),
        ]
        py = next((p for p in venv_cands if p and os.path.isfile(p)), None)
        if not py:
            return {
                "ok": False,
                "msg": "没找到 Qwen3-TTS 的 Python 环境（.venv_qwen3tts_mlx / .venv_qwen3tts）。"
                       "请先安装本地克隆环境。",
            }

        # 已在运行则直接返回就绪，不重复起
        try:
            with socket.create_connection(("127.0.0.1", 7871), timeout=1):
                return {"ok": True, "msg": "Qwen3-TTS 服务已在运行，可直接使用。"}
        except Exception:
            pass

        try:
            log_path = os.path.expanduser("~/Library/Logs/qwen3tts_server.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a") as lf:
                subprocess.Popen(
                    [py, script, "--host", "127.0.0.1", "--port", "7871"],
                    stdout=lf,
                    stderr=lf,
                    start_new_session=True,
                )
            return {
                "ok": True,
                "msg": "已开始启动 Qwen3-TTS 服务，首次加载权重约 30–70 秒（MLX 走 Apple GPU）。"
                       "状态变绿（已就绪）后再开始渲染；即使提前点渲染，本轮也会等到服务就绪，"
                       "不会再静默回退 edge。",
            }
        except Exception as exc:
            return {"ok": False, "msg": f"启动失败：{exc}"}

    def stop_qwen3tts(self) -> dict:
        """切走「Qwen3-TTS 本地语音克隆」引擎时由前端自动调用：停掉本机 7871 服务，省内存。"""
        import subprocess
        try:
            subprocess.run(["pkill", "-f", "start_qwen3tts_server.py"], check=False)
            return {"ok": True, "msg": "已停止 Qwen3-TTS 本地服务（切换其他引擎时自动释放）。"}
        except Exception as exc:
            return {"ok": False, "msg": f"停止失败：{exc}"}

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

    def save_direct_url(self, url: str, filename: str, referer: str = "", ua: str = "") -> str:
        """把「直链视频」保存到用户**自选的位置**（桌面版原生直存）。

        流程：先弹系统「保存」面板（默认「下载」文件夹 + 视频标题），用户选定路径后由
        Python 带 Referer/UA 拉流写盘；用户点「取消」返回 "CANCELLED"（前端只提示不报错）。

        ★ 2026-09-22 修复「直接保存到本机 = 应用界面变成 403 页」：
          旧实现是前端 `<a href=直链 download>`，而桌面壳是 WKWebView —— 点击会把
          **整个应用界面导航**到该 URL；抖音/快手等字节系 CDN 校验 Referer，浏览器
          拿不到（Referer 是 forbidden header），于是返回 403 openresty 页面，
          应用界面直接消失，用户只能重开 App。
          现在改由本方法在 Python 侧带 Referer/UA 拉流写盘：既不导航 WebView，
          又满足防盗链（后端下载同源，仍不经过外网服务器）。

        ★ 2026-09-22 用户要求「也要弹出来可选择保存的位置弹窗」：落盘前先过
          `_choose_save_path()`（osascript 原生保存面板），不再固定塞进「下载」。

        referer/ua 由后端 resolve 下发的 video.direct_headers 提供；缺省时按
        「无防盗链的裸文件直链」处理（只发一个普通浏览器 UA）。
        返回保存的绝对路径；取消返回 "CANCELLED"；失败返回 "ERROR: ..."
        （前端据此自动回落服务器下载）。
        """
        import os as _os
        import re as _re
        import requests
        from pathlib import Path

        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return "ERROR: 无效的直链地址"
        # 文件名清洗：去掉路径分隔符与文件系统非法字符，兜底一个扩展名
        name = (filename or "视频").strip() or "视频"
        name = _re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", name).strip(" .") or "视频"
        if not _re.search(r"\.[A-Za-z0-9]{2,5}$", name):
            name += ".mp4"
        if len(name) > 120:  # 超长文件名部分文件系统会失败
            stem, dot, suf = name.rpartition(".")
            name = (stem[:110] + dot + suf) if dot else name[:120]

        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()

        # ★ 2026-09-22 用户要求：直存也要先弹系统「保存到哪里」面板（与去水印/文案/二维码
        #   保存一致），而不是一声不吭塞进「下载」文件夹。默认位置仍是「下载」，
        #   默认名是解析出来的视频标题，用户直接回车＝老行为（零学习成本）。
        chosen = _choose_save_path("保存视频到", name, "/tmp/vdl_direct_save.log")
        if chosen == "CANCELLED":
            return "CANCELLED"
        dest = Path(chosen) if chosen else (downloads / name)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return f"ERROR: 无法写入所选目录（{exc}）"
        if dest.exists():  # 不覆盖已有文件
            stem, suf = dest.stem, dest.suffix
            i = 1
            while dest.exists():
                dest = dest.parent / f"{stem}({i}){suf}"
                i += 1

        headers = {
            "User-Agent": (ua or "").strip() or (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if (referer or "").strip():
            headers["Referer"] = referer.strip()

        tmp = dest.with_name(dest.name + ".part")
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(15, 600)) as r:
                if r.status_code >= 400:
                    return f"ERROR: 源站返回 HTTP {r.status_code}（防盗链或链接已过期）"
                written = 0
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if chunk:
                            fh.write(chunk)
                            written += len(chunk)
            if written <= 0:
                try:
                    _os.remove(tmp)
                except OSError:
                    pass
                return "ERROR: 源站返回空文件"
            _os.replace(tmp, dest)
        except Exception as exc:  # 把错误回传前端展示，并让前端回落服务器下载
            try:
                if tmp.exists():
                    _os.remove(tmp)
            except OSError:
                pass
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
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path(prompt, suggested, "/tmp/vdl_dw_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            # 兜底：面板机制整体不可用时存到下载文件夹（符合默认行为）。
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

    def save_text_file_dialog(self, text: str, suggested_name: str) -> str:
        """弹出系统保存面板，把传入的文案文本直接写成 .txt 文件（桌面版原生保存）。

        与 save_dw_file_dialog 同套机制：用 osascript `choose file name` 子进程弹原生
        窗口，绕开 pywebview 主线程 run loop 阻塞。内容来自 JS 直接传入，不再走后端下载。
        取消返回 "CANCELLED"；osascript 不可用时退化为存「下载」文件夹（默认行为）。
        """
        import json
        import os
        import tempfile
        import subprocess
        import datetime as _dt
        from pathlib import Path

        suggested = (suggested_name or "提取文案.txt").strip() or "提取文案.txt"
        # 🔴 2026-09-22 缺陷修复：此前无条件 `not endswith('.txt') 就追加 .txt`，
        #    把「下载 SRT 字幕」传来的 xxx.srt 强改成 xxx.srt.txt —— 用户拿到
        #    双后缀文件，拖进播放器/剪映识别不了字幕。正确规则：只要文件名已带
        #    任意后缀就原样保留，仅对「无后缀」的裸名兜底补 .txt。
        if not os.path.splitext(suggested)[1]:
            suggested += ".txt"
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()

        def _log(msg):
            try:
                with open("/tmp/vdl_text_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        dest = None
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            prompt = "保存提取文案为文本"
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path(prompt, suggested, "/tmp/vdl_text_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            # 兜底：面板机制整体不可用时存到下载文件夹（符合默认行为）。
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 避免覆盖已有文件
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            target.write_text(text or "", encoding="utf-8")
        except Exception as exc:
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {target}")
        return str(target)

    def save_qr_image_dialog(self, data_url: str, suggested_name: str) -> str:
        """把「扫码分享」的二维码 PNG 存到用户选定位置（桌面版原生保存）。

        为什么必须走原生桥：**WKWebView 不支持 `<a download>` 的 blob 下载**——
        在 App 里点「保存二维码」不会弹任何保存框，而是把**主框架导航**到 blob: 图片，
        整个 App 界面被替换成一张二维码、只能重启（用户 2026-09-21 报的
        「点击保存二维码有问题」正是此现象，已在真机复现取证）。
        所以前端把已经取到的 PNG 转成 data URL 传进来，这里解 base64 直接落盘。

        与 save_text_file_dialog / save_matting_file_dialog 同套机制：
        弹系统原生保存窗口（中文本地化 applet；不可用时内部回落 osascript），绕开 pywebview 主线程
        run loop 阻塞。取消返回 "CANCELLED"；osascript 不可用时退化为存「下载」文件夹。
        """
        import base64
        import binascii
        import json
        import os
        import tempfile
        import subprocess
        import datetime as _dt
        from pathlib import Path

        def _log(msg):
            try:
                with open("/tmp/vdl_qr_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        raw = (data_url or "").strip()
        if raw.startswith("data:"):
            # data:image/png;base64,XXXX → 取逗号后的负载
            comma = raw.find(",")
            raw = raw[comma + 1:] if comma >= 0 else ""
        raw = "".join(raw.split())  # 去掉换行/空格，防止桥接传输插入的空白
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            _log(f"base64 decode failed: {exc!r}")
            return f"ERROR: 二维码数据解析失败（{exc}）"
        if not blob.startswith(b"\x89PNG\r\n\x1a\n"):
            _log(f"not a png, head={blob[:8]!r} len={len(blob)}")
            return "ERROR: 二维码数据不是有效 PNG"

        suggested = (suggested_name or "分享二维码.png").strip() or "分享二维码.png"
        if not suggested.lower().endswith(".png"):
            suggested += ".png"
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()
        _log(f"enter save_qr_image_dialog suggested={suggested!r} bytes={len(blob)}")

        dest = None
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path("保存二维码", suggested, "/tmp/vdl_qr_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            if target.suffix.lower() != ".png":
                target = target.with_suffix(target.suffix + ".png")
            target.parent.mkdir(parents=True, exist_ok=True)
            # 避免覆盖已有文件
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            target.write_bytes(blob)
        except Exception as exc:
            _log(f"write error: {exc!r}")
            return f"ERROR: {exc}"
        _log(f"saved -> {target} ({target.stat().st_size} bytes)")
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
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path("保存解说成片", suggested, "/tmp/vdl_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            # 兜底：面板机制整体不可用时直接存到下载文件夹（符合默认行为）。
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

    def save_matting_file_dialog(self, job_id: str, suggested_name: str) -> str:
        """弹出系统保存面板让用户自选「一键抠图」透明 PNG 位置（桌面版原生下载）。

        与 save_commentary_file_dialog / save_dw_file_dialog 同套机制：
        弹系统原生保存窗口（中文本地化 applet；不可用时内部回落 osascript），绕开 pywebview 主线程
        run loop 阻塞。取消返回 "CANCELLED"；osascript 不可用时退化为存「下载」文件夹。
        """
        import json
        import os
        import tempfile
        import requests
        import subprocess
        import datetime as _dt
        from pathlib import Path

        def _log(msg):
            try:
                with open("/tmp/vdl_matting_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        url = f"http://{HOST}:{PORT}/api/matting/image/{job_id}/file"
        suggested = suggested_name or "matting.png"
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()
        _log(f"enter save_matting_file_dialog job_id={job_id} suggested={suggested!r}")

        dest = None
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path("保存抠图结果", suggested, "/tmp/vdl_matting_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            resp = requests.get(url, timeout=(10, 600))
            resp.raise_for_status()
            target.parent.mkdir(parents=True, exist_ok=True)
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

    def save_convert_file_dialog(self, job_id: str, suggested_name: str) -> str:
        """弹出系统保存面板让用户自选「格式转换 / 高效压缩」结果位置（桌面版原生下载）。

        同时服务两条链路：转换/桥接任务（app.CONVERT_JOBS）与压缩任务
        （routers.compress.COMPRESS_JOBS），按 job_id 依次查两个注册表。

        与 save_commentary_file_dialog / save_dw_file_dialog 同套机制：
        弹系统原生保存窗口（中文本地化 applet；不可用时内部回落 osascript），绕开 pywebview 主线程
        run loop 阻塞（NSSavePanel 在主线程被同步 JS 调用卡死的问题）。取消返回
        "CANCELLED"；osascript 不可用时退化为存「下载」文件夹。
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
                with open("/tmp/vdl_convert_save.log", "a") as f:
                    f.write(f"[{_dt.datetime.now().isoformat()}] {msg}\n")
            except Exception:
                pass

        _log(f"enter save_convert_file_dialog thread={_th.current_thread().name} job_id={job_id} suggested={suggested_name!r}")

        # 进程内直接读任务注册表的 out_path —— launcher 与 FastAPI 服务在同一进程
        # （uvicorn.run 跑在 daemon 线程），共享同一份模块内存。这样彻底绕开之前的
        # `requests.get(http://127.0.0.1:PORT/api/convert/.../file)` 路径：launcher 端不携带
        # X-Device-Id header 也不带 device= query，会被设备隔离校验判 404（2026-08-28 实测）。
        # ⚠️ 两个注册表都要查（2026-09-11）：转换/桥接在 app.CONVERT_JOBS，压缩在
        # routers.compress.COMPRESS_JOBS。只查前者会让「高效压缩」的下载按钮报
        # 「任务不存在或已过期」。
        src_path = None
        is_compress = False
        try:
            import app as _vdl_app
            registries = []
            if hasattr(_vdl_app, "CONVERT_JOBS"):
                registries.append(("CONVERT_JOBS", _vdl_app.CONVERT_JOBS))
            try:
                from routers.compress import COMPRESS_JOBS as _CJOBS
                registries.append(("COMPRESS_JOBS", _CJOBS))
            except Exception as _e:
                _log(f"import COMPRESS_JOBS failed: {_e!r}")
            try:
                from routers.sr import SR_JOBS as _SJOBS
                registries.append(("SR_JOBS", _SJOBS))
            except Exception as _e:
                _log(f"import SR_JOBS failed: {_e!r}")
            for _name, _reg in registries:
                job = _reg.get(job_id) if isinstance(_reg, dict) else None
                if job and job.get("out_path"):
                    src_path = job.get("out_path")
                    is_compress = (_name == "COMPRESS_JOBS")
                    _log(f"in-memory hit registry={_name} status={job.get('status')} out={src_path!r}")
                    break
        except Exception as e:
            _log(f"in-memory read error: {e!r}")
            return f"ERROR: 读任务失败：{e}"

        if not src_path:
            return f"ERROR: 任务不存在或已过期（job_id={job_id}）"

        src_path = str(src_path)
        if not Path(src_path).exists():
            return f"ERROR: 产物文件已丢失（{src_path}）"

        suggested = (suggested_name or "").strip() or Path(src_path).name
        downloads = Path.home() / "Downloads"
        try:
            downloads.mkdir(parents=True, exist_ok=True)
        except Exception:
            downloads = Path.home()

        dest = None
        try:
            name_json = json.dumps(suggested, ensure_ascii=False)
            _prompt = "保存压缩结果" if is_compress else "保存转码结果"
            # ★ 统一走 `_choose_save_path`（中文本地化面板；不可用时内部自动回落 osascript）
            chosen = _choose_save_path(_prompt, suggested, "/tmp/vdl_convert_save.log")
            if chosen == "CANCELLED":
                return "CANCELLED"
            dest = chosen or None
            _log(f"chose: {dest!r}")
        except Exception as e:
            _log(f"choose-save-path exception: {e!r}")
            dest = None

        if not dest:
            _log("fallback -> ~/Downloads copy")
            dest = str(downloads / suggested)

        target = Path(dest)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                stem, suf = target.stem, target.suffix
                i = 1
                while target.exists():
                    target = target.parent / f"{stem}({i}){suf}"
                    i += 1
            # 直接从已落盘的产物拷到目标；同进程内 read + copy，不走 HTTP，零 404 风险
            import shutil as _sh
            _sh.copyfile(src_path, target)
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

    def trigger_update(self, version: str, token: str = None) -> dict:
        """原生 bridge：在 Python 端（不经过 WKWebView 的 fetch）向后端发起更新。
        后端立即返回 job_id，前端再轮询 update_status 获取进度。彻底规避 WebKit 偶发拦截。
        token：登录态 Bearer token（前端从 localStorage 取出后透传），满足后端 _require_user 鉴权；
        不传则后端返回 NO_AUTH，更新无法启动。"""
        try:
            import json as _json
            import urllib.request as _urllib
            url = f"http://{HOST}:{PORT}/api/system/update"
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + str(token)
            req = _urllib.Request(
                url,
                data=_json.dumps({"version": version}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with _urllib.urlopen(req, timeout=60) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": "触发更新失败：%s" % e}

    def update_status(self, job_id: str) -> dict:
        """原生 bridge：轮询更新任务进度（短 GET，不经过 WebKit fetch）。"""
        try:
            import json as _json
            import urllib.request as _urllib
            url = f"http://{HOST}:{PORT}/api/system/update/status?job={job_id}"
            with _urllib.urlopen(url, timeout=10) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"ok": False, "error": "查询更新状态失败：%s" % e}

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
    """重复启动时把已有 视频工坊 窗口提到最前（macOS）。

    提窗优先级（**绝不自动打开浏览器**，用户明确：App 就是 App，不跳网页）：
      1. AppKit `NSRunningApplication.activateWithOptions_` —— 官方"把另一实例带到
         前台"API，**无需任何「辅助功能」权限**；
      2. osascript System Events `set frontmost` —— 需要「辅助功能」权限，未授权会失败；
      3. 以上都失败只写日志，保持现状（提窗失败不跳浏览器）。
    修复背景：旧版无条件先开浏览器，导致已开着 app 再双击时每次都乱弹网页。
    """
    pid = None
    try:
        lp = Path.home() / ".vdl_instance.lock"
        if lp.exists():
            parts = lp.read_text().strip().split()
            if len(parts) >= 1 and parts[0].isdigit():
                pid = int(parts[0])
    except Exception:
        pass

    raised = False
    # 1) AppKit 无权限提窗（首选）
    if sys.platform == "darwin" and pid:
        try:
            from AppKit import NSRunningApplication, NSApplicationActivateAllWindows
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if app is not None:
                try:
                    # 组合 AllWindows|IgnoringOtherApps：把实例所有窗口带到前台
                    from AppKit import NSApplicationActivateIgnoringOtherApps
                    raised = bool(app.activateWithOptions_(
                        NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps))
                except Exception:
                    try:
                        raised = bool(app.activateWithOptions_(NSApplicationActivateAllWindows))
                    except Exception:
                        raised = bool(app.activate())
        except Exception:
            raised = False

    # 2) osascript System Events 提窗（需要「辅助功能」权限，失败静默降级）
    if not raised and sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to set frontmost of '
                 '(every process whose name contains "视频工坊" or name contains "VideoDownloader") to true'],
                check=False, capture_output=True, timeout=3,
            )
            raised = (r.returncode == 0)
        except Exception:
            raised = False

    # 3) 提窗失败：只写日志，绝不自动打开浏览器（用户明确：App 就是 App，不跳网页）
    if not raised:
        _launch_log("提窗失败（AppKit/osascript 均未生效），不打开浏览器，保持现状")

_LAUNCH_LOG = Path.home() / ".vdl_launch.log"

# PATH 兜底：从 Dock/Finder 启动时 PATH 只有 /usr/bin:/bin:/usr/sbin:/sbin，
# homebrew (/opt/homebrew/bin) 下的 ffmpeg/ffprobe/yt-dlp 会「存在但找不到」，
# 进而让依赖外部工具的初始化在 GUI 启动与终端启动下行为不一致。
VDL_PATH_BOOTSTRAP = True
_extra_paths = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
for _p in reversed(_extra_paths):
    if _p not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")

def _launch_log(msg: str) -> None:
    """把启动关键节点写入 ~/.vdl_launch.log，便于「双击打不开」时定位。"""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(_LAUNCH_LOG, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

def _release_lock(f, path) -> None:
    try:
        if _fcntl is not None:
            _fcntl.flock(f, _fcntl.LOCK_UN)
        f.close()
    except Exception:
        pass
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass

def _show_error_dialog(msg: str) -> None:
    """macOS 下用系统弹窗显示错误（不依赖 Python 包）。失败静默忽略。"""
    if sys.platform != "darwin":
        return
    try:
        safe = msg.replace('"', "'").replace("\\", "")
        # 仅取前两行做弹窗，详情留给日志，避免 AppleScript 超长报错
        short = "\\n".join(safe.splitlines()[:2])
        subprocess.run(
            ["osascript", "-e",
             f'display dialog "{short}" with title "视频工坊" '
             f'buttons {{"确定"}} default button "确定"'],
            check=False, capture_output=True, timeout=3,
        )
    except Exception:
        pass

def _browser_fallback(server_thread) -> None:
    """原生窗口不可用时的兜底：**不再自动打开浏览器**（用户明确：App 就是 App，不跳网页）。

    仅弹窗告知用户 + 写日志，并保持本地服务继续运行，等用户决定（可手动访问或退出）。
    """
    _launch_log("原生窗口不可用，不自动跳转浏览器（用户要求），仅提示并保持服务运行")
    _show_error_dialog(
        "视频工坊 无法打开原生窗口。\n"
        "请把 ~/.vdl_launch.log 内容发给开发者排查。"
    )
    try:
        server_thread.join()
    except Exception:
        pass

def _ensure_single_instance():
    """同一用户只保留一个 GUI 实例，且确保运行的是最新构建版本。

    健壮化（修复「双击打不开 / 闪退」根因）：
    - 用「端口是否被绑定」判定实例是否真的存活，替代 os.kill(pid,0) 的 PID 复用误判；
    - 抢锁前不再用 open('w') 截断锁文件（旧逻辑会清空持有者已写的 PID，造成僵尸锁）；
    - 仅在确认自己是 singleton 后才 seek(0)+truncate 重写锁；进程退出时 atexit 清理。
    - 锁文件格式：`PID PORT BUILD_VERSION`
   

    锁文件格式：`PID PORT BUILD_VERSION`
    - 拿到锁（无别的实例）→ 写自己信息，返回锁 handle，正常启动
    - 拿不到锁（有别实例）→
        * 旧实例版本 == 当前版本 → 激活窗口并退出（保持单实例，避免重复窗口）
        * 旧实例版本 ≠ 当前版本（旧版在跑、新版双击）→ **自动终止旧实例**，
          自己成为唯一实例（彻底解决「双击后仍在跑旧版」导致反复调试浪费的问题）
    """
    lock_path = Path.home() / ".vdl_instance.lock"
    self_build = _read_self_build_version()

    def _read_lock():
        try:
            parts = lock_path.read_text().strip().split()
        except Exception:
            return None, None, ""
        pid = int(parts[0]) if parts and parts[0].isdigit() else None
        port = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
        build = parts[2] if len(parts) >= 3 else ""
        return pid, port, build

    def _port_bound(port):
        if not port:
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False

    # 清理僵尸锁：锁存在，但对应端口未绑定且 PID 已死 → 直接删除
    try:
        if lock_path.exists():
            opid, oport, _ = _read_lock()
            if not _port_bound(oport) and not (opid and _pid_alive(opid)):
                try:
                    lock_path.unlink()
                except OSError:
                    pass
    except Exception:
        pass

    opid, oport, obuild = _read_lock() if lock_path.exists() else (None, None, "")

    # 端口被占用 = 真的有别的实例在提供本地服务（PID 复用也不会误判）
    if _port_bound(oport):
        if obuild and obuild != self_build and opid and _pid_alive(opid):
            _launch_log(f"检测到旧版本实例({obuild})在端口 {oport} 运行，自动接管并关闭")
            _kill_process_tree(opid)
            for _ in range(50):  # 最多等 5s 让端口释放
                if not _port_bound(oport):
                    break
                time.sleep(0.1)
        else:
            _launch_log("检测到已在运行的同版本实例，激活其窗口并退出（保持单实例）")
            _activate_existing_window()
            sys.exit(0)

    # ── 自己是唯一实例：拿文件锁（不提前截断，避免清空持有者已写内容）──
    try:
        f = open(lock_path, "a+")
        if _fcntl is not None:
            _fcntl.flock(f, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        f.seek(0)
        f.truncate()
        f.write(f"{os.getpid()} {PORT} {self_build}\n")
        f.flush()
    except Exception:
        try:
            f.close()
        except Exception:
            pass
        f = None
    if f is not None:
        try:
            import atexit
            atexit.register(_release_lock, f, lock_path)
        except Exception:
            pass
        _launch_log(f"成为 singleton，已写锁 (pid={os.getpid()} port={PORT} build={self_build})")
    return f

def main() -> None:
    import uvicorn

    signal.signal(signal.SIGTERM, _handle_exit)
    signal.signal(signal.SIGINT, _handle_exit)

    # 主进程也注入 COMMENTARY_WORK_ROOT，让 _commentary_root 始终走用户可写目录，
    # 不污染包内的 Contents/Resources/commentary 与 Contents/Frameworks/commentary
    os.environ.setdefault("COMMENTARY_WORK_ROOT", str(_app_data_dir() / "commentary"))

    # 单实例：已有窗口则激活并退出，绝不创建第二个页面
    _launch_log(f"main 启动 (pid={os.getpid()} port={PORT} frozen={getattr(sys, 'frozen', False)})")
    _lock = _ensure_single_instance()
    if _lock is None:
        _activate_existing_window()
        sys.exit(0)

    # 后台启动 FastAPI 服务（带 bind 失败自动顺延端口的兜底重试）
    _app_dir = str(BASE) if getattr(sys, "frozen", False) else str(SERVER_DIR)
    # 后端线程异常必须留痕：从 Dock/Finder（open -a）启动时 stdout/stderr 不进任何
    # 日志文件，线程一旦抛非 OSError 异常就**静默死亡**——表现为「窗口开了但后端
    # 永远不监听」，且 ~/.vdl_launch.log 只留一句「服务器启动超时」，无从下手。
    # 这里统一兜住并写入启动日志。
    server_thread = threading.Thread(
        target=_logged_run_server,
        args=(PORT, HOST, _app_dir, _lock),
        daemon=True,
    )
    # 🔍 诊断（2026-09-22）：后端线程曾卡死在 import 阶段的 open()（系统日志无从下手），
    # 让 faulthandler 每 30s 把全部线程的 Python 栈打到启动日志，卡在哪一行一目了然。
    try:
        import faulthandler as _fh
        _dump_path = Path.home() / ".vdl_pydump.log"
        _dump_fh = open(_dump_path, "w")
        _fh.dump_traceback_later(30, repeat=True, file=_dump_fh)
    except Exception:
        _dump_fh = None
    server_thread.start()

    # 等服务器就绪（窗口 240s：见下方 2026-09-15 实测）。daemon 线程不 join，
    # 即便超时 main 返回，server 线程仍随 main 进程存活直到用户主动退出/系统清理。
    #
    # 2026-09-15 实测（换包后首次启动）：热启动 2s，但**冷启动 97s** —— 661MB
    # 包刚换进 /Applications，磁盘缓存全冷 + ad-hoc 签名校验，后端 97s 才监听。
    # 旧窗口 60s 一到就放行 → 窗口比后端先开，用户看到加载失败页（且不会自动重试），
    # 而事实上后端再等 37s 就好了。放宽到 240s：就绪立即 break，热启动体感不变；
    # 冷启动多等一会儿但**打开就能用**。真慢到 240s 还没起，行为与旧版一致（不更差）。
    _READY_WAIT_SEC = 240
    for _ in range(int(_READY_WAIT_SEC / 0.2)):
        try:
            with socket.create_connection((HOST, PORT), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        _launch_log(f"服务器启动超时（{_READY_WAIT_SEC}s 内未就绪），但 main 不退出，server 线程继续后台尝试")
        # 注意：不能 server_thread.join()——会让 main 阻塞直到 thread 退出，
        # 而 thread 可能因 cookie_pool 等慢 init 永远不结束，整个 app 卡死。
        # daemon=True 表明 main 退出时 thread 一起死，但用户不主动退就没人杀 main。
        # 这里只警告，不阻塞 main。
        print(f"服务器启动较慢（>{_READY_WAIT_SEC}s），请稍候或手动访问 {URL}")

    _launch_log("后端服务就绪，准备打开界面")

    # ── ★ 2026-08-28 续33 关键护城河：拦截任何 lazy tkinter 初始化 ──
    # 根因：pywebview 的 JS bridge 在 macOS cocoa 后端 **未必在 main thread** 同步调 →
    # 任何依赖 Tk 创建窗口的代码（tkinter / PIL.ImageTk）在 non-main-thread 调 Tk() →
    # `TkMacOSXMakeRealWindowExist` → `NSWindow drag regions should only be invalidated
    # on the Main Thread!` SIGTRAP trap → macOS 弹 force quit「应用没响应」+ 主循环饿死。
    # 修复：把 tkinter / _tkinter 在 sys.modules 里替换为 stub，让任何 lazy 调用立即报
    # RuntimeError，绝不触发 Tk lib init。本 app 自有桌面 dialog（osascript/NSOpenPanel），
    # 不需要 Tk 跨平台回退。
    try:
        import sys as _sys
        class _TkGuard:
            """所有 tkinter API 调到这里抛 RuntimeError，绝不触发 Tk lib init。"""
            __slots__ = ()
            def __getattr__(self, name):
                raise RuntimeError(
                    f"[vdl-guard] tkinter.{name} 已被禁用（macOS main-thread-only trap 修复）。"
                    " 用 app 内置的 osascript / NSOpenPanel 桌面 dialog 替代。"
                )
        if _sys.modules.get('tkinter') is None:
            _sys.modules['tkinter'] = _TkGuard()
        if _sys.modules.get('_tkinter') is None:
            _sys.modules['_tkinter'] = _TkGuard()
        # 让 PIL 探测到 tkinter 不可用（features['tkinter'] = False），禁用 ImageTk 后端
        try:
            from PIL import features as _pf
            _pf.pilinfo  # noqa: B018
        except Exception:
            pass
        _launch_log("已激活 tkinter 主线程护栏（续33 修复）")
    except Exception as _mpe:
        _launch_log("tkinter 护栏注册失败（非致命）: " + repr(_mpe))

    # 开窗口 → 优先原生窗口（pywebview），回退浏览器
    try:
        import webview
    except ImportError:
        _launch_log("未捆绑 webview 模块，无法打开原生窗口（不跳浏览器，仅提示）")
        _browser_fallback(server_thread)
        return

    try:
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

        # ── 放行 WebView 麦克风（2026-09-19）：「我的音色 → ⏺ 直接录制」靠 WKWebView 的
        #    getUserMedia。macOS 12+ 采集前必须由 WKUIDelegate 的
        #    webView:requestMediaCapturePermissionForOrigin:initiatedByFrame:type:decisionHandler:
        #    给出决定；pywebview 没实现它，而 WKWebView 自己没有权限弹窗 →
        #    请求会一直挂着不落定（页面侧表现＝点录制没反应）。这里补上并直接 grant，
        #    系统 TCC 仍会照常弹一次「视频工坊想访问麦克风」，用户拒绝则前端给出中文指引。
        #    ⚠️ 签名必须显式写全（v@: + 每个实参一个编码，type 是 NSUInteger→Q，block→@?）：
        #       classAddMethods 校验的实参个数 = 签名里 self/_cmd 之后的个数，少写一个即
        #       BadPrototypeError，多写一个则 block 参数传不进来（拿不到 handler 必崩）。
        if sys.platform == "darwin":
            try:
                import objc as _objc
                import webview.platforms.cocoa as _cocoa_mic
                _mic_sel = (
                    b"webView:requestMediaCapturePermissionForOrigin:"
                    b"initiatedByFrame:type:decisionHandler:"
                )

                def _vdl_allow_media_capture(self, _wv, _origin, _frame, _mtype, handler):
                    """放行麦克风/摄像头采集（仅本机页面；TCC 会再问用户一次）。"""
                    try:
                        handler(1)   # WKPermissionDecisionGrant
                    except Exception:
                        pass

                _objc.classAddMethods(
                    _cocoa_mic.BrowserView.BrowserDelegate,
                    [_objc.selector(_vdl_allow_media_capture, selector=_mic_sel,
                                    signature=b"v@:@@@Q@?")],
                )
                _launch_log("已放行 WebView 媒体采集权限（我的音色·直接录制）")
            except Exception as _mic_err:
                _launch_log(f"无法放行 WebView 麦克风（「直接录制」将不可用）: {_mic_err!r}")

        api = VdlApi()

        # ---- 窗口尺寸/位置记忆（2026-09-17：用户要求按上次手动调整后的宽高打开） ----
        # 存 _app_data_dir()/window_frame.json；首次运行无文件 → 用默认 1100x750 居中。
        _win_frame_file = _app_data_dir() / "window_frame.json"

        def _load_win_frame():
            import json as _json
            try:
                d = _json.loads(_win_frame_file.read_text(encoding="utf-8"))
                w = max(800, min(3840, int(d.get("width", 1100))))
                h = max(500, min(2160, int(d.get("height", 750))))
                x = d.get("x"); y = d.get("y")
                x = int(x) if isinstance(x, (int, float)) else None
                y = int(y) if isinstance(y, (int, float)) else None
                return w, h, x, y
            except Exception:
                return 1100, 750, None, None

        _win_w, _win_h, _win_x, _win_y = _load_win_frame()
        window = webview.create_window(
            title="视频工坊",
            url=URL,
            width=_win_w,
            height=_win_h,
            x=_win_x,
            y=_win_y,
            min_size=(800, 500),
            text_select=True,
            js_api=api,
        )

        def _save_win_frame(*_args):
            """resized/moved 时持久化窗口框架（失败静默，不影响主流程）。"""
            import json as _json
            try:
                _win_frame_file.parent.mkdir(parents=True, exist_ok=True)
                _json.dump(
                    {
                        "width": int(window.width),
                        "height": int(window.height),
                        "x": int(window.x),
                        "y": int(window.y),
                    },
                    open(_win_frame_file, "w", encoding="utf-8"),
                )
            except Exception:
                pass

        window.events.resized += _save_win_frame
        try:
            window.events.moved += _save_win_frame  # 老版本 pywebview 无 moved 时忽略
        except AttributeError:
            pass

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
        _launch_log("原生窗口已创建，启动 webview 主循环")
        webview.start()
        _launch_log("webview 主循环结束，正常退出")
        os._exit(0)
    except Exception as _wv_err:  # pywebview 运行期异常（如 cocoa 初始化失败）→ 绝不静默闪退
        _launch_log(f"pywebview 运行异常({type(_wv_err).__name__})，无法打开原生窗口: {_wv_err!r}")
        _browser_fallback(server_thread)
        return

if __name__ == "__main__":
    if "--vdl-commentary-worker" in sys.argv:
        # 单二进制双角色：自身重入为解说管线 worker（依赖随包内置，无需外部 Python）
        raise SystemExit(_run_commentary_worker(sys.argv))
    main()
