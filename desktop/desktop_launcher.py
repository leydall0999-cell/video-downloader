"""VideoDownloader 桌面版启动器（由 PyInstaller 打包为 .app / .exe）。

职责：
1. 定位捆绑的资源（ffmpeg / server 代码 / 前端静态文件 / yt-dlp 插件）
2. 自动挑选空闲端口（默认 8321，被占用时顺延，避免启动即崩溃）
3. 启动本地 FastAPI 服务（uvicorn）
4. 自动打开浏览器到本地地址，并弹系统通知告知访问地址

普通用户双击 .app / .exe 即可，无需安装 Python、ffmpeg 或任何依赖。
抓视频用的是用户自己的网络出口（家庭宽带 IP），因此 B站/抖音等国内站也能下，
且不需要任何代理配置。退出：macOS 在 Dock 右键 Quit，或关掉后进程随系统；Windows 在系统托盘退出。
"""

import os
import sys
import time
import socket
import signal
import webbrowser
import threading
import subprocess
from pathlib import Path

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
    """优先用捆绑的 ffmpeg。.app 内位于 Contents/MacOS/bin/ffmpeg；单文件夹/Windows 位于可执行文件同目录 bin/。"""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "bin" / "ffmpeg")
    candidates.append(BASE / "bin" / "ffmpeg")
    for c in candidates:
        if c.exists():
            return str(c)
    return None


_ff = _detect_ffmpeg()
if _ff:
    # app.py 读取 VDL_FFMPEG_BIN 作为 ffmpeg 路径；用户机器通常没装 ffmpeg，必须用捆绑的
    os.environ["VDL_FFMPEG_BIN"] = _ff

# 把 server 与 yt-dlp 插件目录加入导入路径
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


def _notify(title: str, msg: str) -> None:
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                check=False, capture_output=True,
            )
        except Exception:
            pass


def _open_browser() -> None:
    time.sleep(2.0)
    try:
        webbrowser.open(URL)
    except Exception:
        pass


def _handle_exit(*_args) -> None:
    os._exit(0)


def main() -> None:
    import uvicorn

    signal.signal(signal.SIGTERM, _handle_exit)
    signal.signal(signal.SIGINT, _handle_exit)

    threading.Thread(target=_open_browser, daemon=True).start()
    _notify("VideoDownloader 已启动", f"浏览器将打开 {URL}（退出请在 Dock 右键 Quit）")

    # 冻结模式：模块已打包为顶层模块，app_dir 用资源根目录；开发模式：用 server 目录
    app_dir = str(BASE) if getattr(sys, "frozen", False) else str(SERVER_DIR)
    try:
        uvicorn.run(
            "app:app",
            app_dir=app_dir,
            host=HOST,
            port=PORT,
            log_level="info",
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
