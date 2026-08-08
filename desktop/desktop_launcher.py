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


def _detect_ffmpeg() -> str | None:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "bin" / "ffmpeg")
    candidates.append(BASE / "bin" / "ffmpeg")
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _detect_commentary() -> tuple[str | None, str | None]:
    """定位 commentary-pipeline 目录及其 .venv 解释器；找不到时返回 (None, None)。"""
    cdir = os.environ.get("VDL_COMMENTARY_DIR", "").strip()
    if not cdir:
        for cand in [
            Path.home() / "WorkBuddy" / "问问题" / "commentary-pipeline",
            Path.home() / "commentary-pipeline",
        ]:
            if cand.is_dir() and (cand / "process.py").is_file():
                cdir = str(cand)
                break
    if not cdir:
        return None, None
    venv_py = Path(cdir) / ".venv" / "bin" / "python"
    if venv_py.exists():
        return cdir, str(venv_py)
    # 没有 .venv 时，尝试 WorkBuddy default python（需用户自行装好依赖）
    default_py = Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "default" / "bin" / "python"
    if default_py.exists():
        return cdir, str(default_py)
    return cdir, None


_ff = _detect_ffmpeg()
if _ff:
    os.environ["VDL_FFMPEG_BIN"] = _ff              # app.py 自己用
    os.environ["FFMPEG_LOCATION"] = str(Path(_ff).parent)  # yt-dlp 找 ffmpeg 用
    os.environ["PATH"] = str(Path(_ff).parent) + os.pathsep + os.environ.get("PATH", "")  # 兜底

_c_dir, _c_py = _detect_commentary()
if _c_dir:
    os.environ["VDL_COMMENTARY_DIR"] = _c_dir
if _c_py:
    os.environ["VDL_COMMENTARY_PYTHON"] = _c_py

sys.path.insert(0, str(SERVER_DIR))
if PLUGINS_DIR.exists():
    sys.path.insert(0, str(BASE))


def _find_free_port(start: int = 8321, tries: int = 80) -> int:
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


_env_port = (os.environ.get("VDL_PORT") or "").strip()
PORT = int(_env_port) if _env_port else _find_free_port()
HOST = "127.0.0.1"
URL = f"http://{HOST}:{PORT}"


def _handle_exit(*_args) -> None:
    os._exit(0)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _activate_existing_window() -> None:
    """重复启动时把已有 VideoDownloader 窗口提到最前（macOS）。"""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to set frontmost of '
             '(every process whose name contains "VideoDownloader") to true'],
            check=False, capture_output=True,
        )
    except Exception:  # pragma: no cover - 激活失败不影响退出
        pass


def _ensure_single_instance():
    """同一用户只保留一个 GUI 实例。重复启动返回 None（调用方应激活并退出）。"""
    if _fcntl is None:
        return None
    lock_path = Path.home() / ".vdl_instance.lock"
    # 清理僵尸锁（上次异常退出未释放且持有进程已死）
    try:
        if lock_path.exists():
            old = lock_path.read_text().strip()
            if old.isdigit() and not _pid_alive(int(old)):
                try:
                    lock_path.unlink()
                except OSError:
                    pass
    except Exception:
        pass
    try:
        f = open(lock_path, "w")
        _fcntl.flock(f, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        f.write(str(os.getpid()))
        f.flush()
        return f  # 调用方必须持有此对象直到退出
    except OSError:
        return None


def main() -> None:
    import uvicorn

    signal.signal(signal.SIGTERM, _handle_exit)
    signal.signal(signal.SIGINT, _handle_exit)

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
        window = webview.create_window(
            title="VideoDownloader",
            url=URL,
            width=1100,
            height=750,
            min_size=(800, 500),
            text_select=True,
        )
        # Windows 端退出 webview 后清理资源
        webview.start()
        os._exit(0)
    except ImportError:
        import webbrowser
        import subprocess as _sp
        _sp.run(
            ["osascript", "-e",
             f'display notification "VideoDownloader 已启动" with title "{URL}"'],
            check=False, capture_output=True,
        )
        webbrowser.open(URL)
        # 保持进程存活直到 uvicorn 退出
        server_thread.join()


if __name__ == "__main__":
    main()
