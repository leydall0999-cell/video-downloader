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
