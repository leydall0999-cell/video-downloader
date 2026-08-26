"""routers/narrato.py — 「AI 解说体验」tab 后端。

在桌面 App 内以子进程本地拉起 NarratoAI（Streamlit 应用），前端用 iframe 嵌入，
让用户先体验第三方解说工具；体验后再决定是否把优点并入 commentary-pipeline（Phase 2）。

铁律：原生窗口内 iframe 嵌入，绝不跳外部浏览器；该路由仅桌面 App 使用，web-dev 不挂。
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

NARRATO_PORT = 8510
NARRATO_HOST = "127.0.0.1"
NARRATO_DEFAULT_DIR = Path.home() / "Downloads" / "NarratoAI-0.8.7"
NARRATO_DIR_ENV = "VDL_NARRATOAI_DIR"
NARRATO_LOG = Path.home() / ".video-downloader" / "narrato.log"
NARRATO_LOG.parent.mkdir(parents=True, exist_ok=True)

# 模块级单例状态（router 模块仅导入一次）
_state: dict = {
    "proc": None,
    "dir": None,
    "launched": False,
    "lock": threading.Lock(),
}


# --------------------------------------------------------------------------- #
# 目录 / 启动器 解析
# --------------------------------------------------------------------------- #
def _resolve_dir() -> Path:
    env = os.environ.get(NARRATO_DIR_ENV)
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
    return NARRATO_DEFAULT_DIR


def _resolve_uv() -> str | None:
    candidates = [
        shutil.which("uv"),
        Path.home() / ".local" / "bin" / "uv",
        Path("/opt/homebrew/bin/uv"),
        Path.home() / ".cargo" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


def _ffmpeg_bin_dir() -> str | None:
    # 打包后 ffmpeg 在 MacOS/bin；开发机依赖系统 PATH 上的 ffmpeg
    exe = Path(sys.executable)
    cand = exe.parent / "bin"
    if (cand / "ffmpeg").exists():
        return str(cand)
    return None


def _resolve_launcher(narrato_dir: Path) -> list[str]:
    """返回启动命令；找不到可用启动器则抛 RuntimeError。"""
    uv = _resolve_uv()
    if uv:
        return [
            uv, "run", "streamlit", "run", "webui.py",
            f"--server.port={NARRATO_PORT}",
            f"--server.address={NARRATO_HOST}",
            "--server.headless=true",
            "--server.enableCORS=true",
            "--server.enableXsrfProtection=false",
            "--browser.gatherUsageStats=false",
        ]
    # 回退：目录内已有 .venv（用户自行 uv sync / pip 装过）
    venv_streamlit = narrato_dir / ".venv" / "bin" / "streamlit"
    if venv_streamlit.exists():
        return [
            str(venv_streamlit), "run", "webui.py",
            f"--server.port={NARRATO_PORT}",
            f"--server.address={NARRATO_HOST}",
            "--server.headless=true",
            "--server.enableCORS=true",
            "--server.enableXsrfProtection=false",
            "--browser.gatherUsageStats=false",
        ]
    raise RuntimeError(
        "未找到 uv 启动器，请先安装：curl -LsSf https://astral.sh/uv/install.sh | sh"
    )


# --------------------------------------------------------------------------- #
# config.toml 处理（仅本地，不入库）
# --------------------------------------------------------------------------- #
def _config_path(narrato_dir: Path) -> Path:
    return narrato_dir / "config.toml"


def _ensure_config(narrato_dir: Path) -> None:
    cfg = _config_path(narrato_dir)
    if cfg.exists():
        return  # 已存在则尊重用户既有配置
    example = narrato_dir / "config.example.toml"
    if not example.exists():
        raise RuntimeError(f"缺少 config.example.toml：{example}")
    text = example.read_text(encoding="utf-8")
    # 预填 DeepSeek（OpenAI 兼容）作为文本 LLM；配音先用免费 edge_tts 跑通
    text = re.sub(
        r'^text_openai_base_url\s*=\s*"[^"]*"',
        'text_openai_base_url = "https://api.deepseek.com/v1"',
        text, flags=re.MULTILINE,
    )
    text = re.sub(
        r'^text_openai_model_name\s*=\s*"[^"]*"',
        'text_openai_model_name = "deepseek-chat"',
        text, flags=re.MULTILINE,
    )
    text = re.sub(
        r'^tts_engine\s*=\s*"[^"]*"',
        'tts_engine = "edge_tts"',
        text, flags=re.MULTILINE,
    )
    # key 留空，待用户粘贴
    cfg.write_text(text, encoding="utf-8")


def _has_key(narrato_dir: Path) -> bool:
    cfg = _config_path(narrato_dir)
    if not cfg.exists():
        return False
    m = re.search(r'text_openai_api_key\s*=\s*"([^"]*)"', cfg.read_text(encoding="utf-8"))
    return bool(m and m.group(1).strip())


def _set_key_in_config(narrato_dir: Path, key: str) -> None:
    cfg = _config_path(narrato_dir)
    if not cfg.exists():
        _ensure_config(narrato_dir)
    text = cfg.read_text(encoding="utf-8")
    if re.search(r'text_openai_api_key\s*=', text):
        text = re.sub(
            r'^(text_openai_api_key\s*=\s*)"[^"]*"',
            lambda m: f'{m.group(1)}"{key}"',
            text, flags=re.MULTILINE,
        )
    else:
        text += f'\ntext_openai_api_key = "{key}"\n'
    cfg.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 健康检查 / 生命周期
# --------------------------------------------------------------------------- #
def _is_ready() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex((NARRATO_HOST, NARRATO_PORT)) == 0
    finally:
        s.close()


def ensure_start() -> dict:
    with _state["lock"]:
        narrato_dir = _resolve_dir()
        _state["dir"] = str(narrato_dir)
        if not narrato_dir.exists():
            return {"status": "missing_dir", "dir": str(narrato_dir)}
        _ensure_config(narrato_dir)
        if not _has_key(narrato_dir):
            return {"status": "need_key", "dir": str(narrato_dir)}
        proc = _state.get("proc")
        if proc is not None and proc.poll() is None and _is_ready():
            return {"status": "ready", "port": NARRATO_PORT}
        # 拉起
        try:
            cmd = _resolve_launcher(narrato_dir)
        except RuntimeError as e:
            return {"status": "need_uv", "msg": str(e)}
        env = os.environ.copy()
        ffmpeg_bin = _ffmpeg_bin_dir()
        if ffmpeg_bin:
            env["PATH"] = ffmpeg_bin + os.pathsep + env.get("PATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(narrato_dir),
                env=env,
                stdout=open(NARRATO_LOG, "ab"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001
            return {"status": "launch_failed", "msg": str(e)}
        _state["proc"] = proc
        _state["launched"] = True
        return {"status": "starting", "port": NARRATO_PORT, "dir": str(narrato_dir)}


def stop() -> None:
    with _state["lock"]:
        proc = _state.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:
                    pass
        _state["proc"] = None
        _state["launched"] = False


def status() -> dict:
    narrato_dir = _resolve_dir()
    with _state["lock"]:
        proc = _state.get("proc")
        alive = proc is not None and proc.poll() is None
        return {
            "status": "ready" if (alive and _is_ready()) else (
                "starting" if alive else "stopped"
            ),
            "port": NARRATO_PORT,
            "dir": str(narrato_dir),
            "dir_exists": narrato_dir.exists(),
            "has_key": _has_key(narrato_dir),
            "log": str(NARRATO_LOG),
        }


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
class _KeyReq(BaseModel):
    key: str


@router.get("/api/narrato/status")
def narrato_status() -> dict:
    return status()


@router.post("/api/narrato/ensure")
def narrato_ensure() -> dict:
    return ensure_start()


@router.post("/api/narrato/key")
def narrato_set_key(req: _KeyReq) -> dict:
    narrato_dir = _resolve_dir()
    if not narrato_dir.exists():
        return {"ok": False, "msg": f"NarratoAI 目录不存在：{narrato_dir}"}
    _set_key_in_config(narrato_dir, req.key.strip())
    # 已拉起的进程读的是旧配置，key 变更需重启才生效
    stop()
    return {"ok": True, "has_key": True}


@router.post("/api/narrato/stop")
def narrato_stop() -> dict:
    stop()
    return {"ok": True}
