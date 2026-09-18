"""server/clone_env.py — 本地语音克隆（Qwen3-TTS）运行环境的按需安装。

为什么要有这个模块
    解说页的「Qwen3-TTS 本地语音克隆」（本机 7871 服务）需要两样大资源：
      ① 一个**独立**的 Python 环境（`mlx` + `mlx-audio`，约 500MB）；
      ② Qwen3-TTS 的 MLX 量化权重（约 1.9GB，
         `mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit`）。
    合计约 2.4GB，远超发行包 <500MB 的体积目标，**不能随包**；而包内只有
    `libpython3.13.dylib`、没有可直接执行的解释器，也不能拿包内 Python 现场装。
    所以改成**首启按需下载**：用户显式点了才下、进度可轮询、失败给手动兜底。
    （与 matting_ai / sr / dewatermark 的「首次使用惰性下载到 ~/.vdl_models」同一思路，
      只是这里除了模型还要现搭一个 venv。）

落点刻意与管线的探测候选对齐（**别再发明第二套路径**）
    venv   → ~/.video-downloader/venvs/qwen3tts_mlx/bin/python
    权重   → ~/.cache/qwen3tts/Qwen3-TTS-12Hz-0.6B-Base-8bit
    对应 commentary-pipeline/scripts/start_qwen3tts_server.py::_find_mlx_python()/_find_mlx_model()
    与 scripts/edit_ffmpeg.py::_qwen3tts_find_python()。

两条硬纪律
    1. 🔴 **不在请求期改写进程级 `HF_ENDPOINT`**。历史缺陷（2026-09-16）：字幕模块在
       `_get_model()` 里改这个环境变量，顺手把扩散去水印等其它功能的下载端点也带偏，
       `server/tests/test_engine_isolation.py` 专门盯这条。这里把镜像地址**显式**作为
       `endpoint=` 参数传给 `snapshot_download()`，不碰 os.environ。
    2. 🔴 安装是分钟级长任务 → 必须后台线程 + 进度字典，绝不阻塞请求线程。

平台边界
    `mlx` 只在 Apple Silicon 上发 wheel。Intel Mac / Windows / Linux 上这条能力
    不成立（状态里如实说明，不做假装能装的降级）。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

# ───────────────────────── 落点与常量 ─────────────────────────

def _env_path(name: str, default: Path) -> Path:
    raw = (os.environ.get(name) or "").strip()
    return Path(raw).expanduser() if raw else default


VENV_DIR = _env_path("VDL_CLONE_MLX_VENV", Path.home() / ".video-downloader" / "venvs" / "qwen3tts_mlx")
WEIGHTS_ROOT = _env_path("VDL_CLONE_MLX_WEIGHTS_ROOT", Path.home() / ".cache" / "qwen3tts")
MODEL_DIRNAME = "Qwen3-TTS-12Hz-0.6B-Base-8bit"
WEIGHTS_DIR = WEIGHTS_ROOT / MODEL_DIRNAME

HF_REPO = (os.environ.get("VDL_CLONE_HF_REPO") or "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit").strip()
# 国内默认 hf-mirror（huggingface.co 直连常被卡）。用户可覆盖，但**只作用于本模块**。
HF_ENDPOINT = (os.environ.get("VDL_CLONE_HF_ENDPOINT") or "https://hf-mirror.com").strip().rstrip("/")
PIP_INDEX = (os.environ.get("VDL_CLONE_PIP_INDEX") or "https://pypi.tuna.tsinghua.edu.cn/simple").strip()

MLX_AUDIO_PIN = (os.environ.get("VDL_CLONE_MLX_AUDIO_VERSION") or "0.5.4").strip()
# mlx-audio 0.5.4 的 Qwen3-TTS 路径不依赖 mlx_lm（0.4.x 才依赖）；
# 更新的 mlx-audio 要求 mlx>=0.31.1（只有 macOS 14+ 的 wheel）→ 必须钉住 + --no-deps。
MLX_AUDIO_DEPS = ["huggingface_hub", "numpy", "scipy", "tqdm", "transformers",
                  "soundfile", "miniaudio", "sentencepiece"]

# 权重的关键文件与最小体积（用于「是否已下好」判定 + 下载进度分母）
_MAIN_WEIGHTS = WEIGHTS_DIR / "model.safetensors"
_TOK_WEIGHTS = WEIGHTS_DIR / "speech_tokenizer" / "model.safetensors"
_MIN_MAIN_BYTES = 1_000_000_000     # 实测 1.2GB
_MIN_TOK_BYTES = 500_000_000        # 实测 656MB
EXPECTED_WEIGHTS_MB = 1990.0        # 实测 1.9G，只用于进度百分比
EXPECTED_ENV_MB = 500.0             # venv + 依赖，实测 502MB

_LOG_MAX = 40

_LOCK = threading.Lock()
_STATE: dict = {
    "active": False,
    "phase": "idle",          # idle/python/venv/pip/weights/done/error/cancelled
    "pct": 0.0,
    "done_mb": 0.0,
    "total_mb": 0.0,
    "msg": "",
    "error": "",
    "log": [],
    "started_at": 0.0,
    "finished_at": 0.0,
}
_CANCEL = threading.Event()
_THREAD: threading.Thread | None = None


def _set(**kw) -> None:
    with _LOCK:
        _STATE.update(kw)


def _log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    with _LOCK:
        _STATE["log"] = (_STATE["log"] + [line])[-_LOG_MAX:]
        _STATE["msg"] = msg


def progress() -> dict:
    """供前端轮询：安装进度。"""
    with _LOCK:
        return dict(_STATE)


# ───────────────────────── 平台与解释器探测 ─────────────────────────

def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


_PY_CANDIDATES = [
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
]

_PY_CACHE: dict = {"at": 0.0, "path": "", "version": "", "all": []}
_PY_CACHE_TTL = 30.0
_MIN_PY = (3, 9)


def _probe_python(path: str) -> tuple[bool, str, tuple]:
    """跑一下解释器拿版本；不可用/过旧返回 (False, 原因, ())。"""
    try:
        p = subprocess.run(
            [path, "-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", ()
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "").strip()[:200] or "解释器不可用", ()
    ver_s = (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else ""
    try:
        ver = tuple(int(x) for x in ver_s.split(".")[:3])
    except Exception:  # noqa: BLE001
        return False, f"无法解析版本：{ver_s!r}", ()
    if ver[:2] < _MIN_PY:
        return False, f"版本过低（{ver_s}，需要 ≥ {_MIN_PY[0]}.{_MIN_PY[1]}）", ver
    return True, "", ver


def base_python(force: bool = False) -> dict:
    """找一个能用来建 venv 的系统 Python。

    优先级：`VDL_CLONE_PYTHON`（运维/排障显式指定）> 常见安装位置 > PATH 上的 python3。
    多个都可用时取**版本最高**的（mlx 生态对 3.13 支持最好）。
    结果进程内缓存 30s：状态接口可能被前端高频轮询，别每次都 fork 一堆子进程。
    """
    now = time.time()
    if not force and _PY_CACHE["path"] and (now - _PY_CACHE["at"]) < _PY_CACHE_TTL:
        return {"path": _PY_CACHE["path"], "version": _PY_CACHE["version"], "all": list(_PY_CACHE["all"])}

    explicit = (os.environ.get("VDL_CLONE_PYTHON") or "").strip()
    cands: list[str] = []
    if explicit:
        cands.append(explicit)
    cands += [c for c in _PY_CANDIDATES if os.path.isfile(c) and os.access(c, os.X_OK)]
    which = shutil.which("python3")
    if which and which not in cands:
        cands.append(which)

    best: tuple[str, str, tuple] | None = None
    seen: list[str] = []
    for c in cands:
        ok, _why, ver = _probe_python(c)
        seen.append(f"{c}{'' if ok else '（不可用）'}")
        if ok and (best is None or ver > best[2]):
            best = (c, ".".join(str(x) for x in ver), ver)

    with _LOCK:
        pass
    _PY_CACHE.update({"at": now, "path": best[0] if best else "",
                      "version": best[1] if best else "", "all": seen})
    return {"path": _PY_CACHE["path"], "version": _PY_CACHE["version"], "all": seen}


def venv_python() -> Path:
    return VENV_DIR / "bin" / "python"


def _venv_dirs() -> list[Path]:
    """所有可能已存在的克隆 venv 目录，**顺序即优先级**。

    🔴 必须把管线的候选一起算进来，否则会犯一个很难看的错：
    开发机/老用户的环境早就装好了（只是落在 `commentary-pipeline/scripts/`），
    而本模块只看 `~/.video-downloader/venvs/`，于是界面会对一个**已经能用**的用户
    反复喊「还要下 500MB」。候选顺序与
    `start_qwen3tts_server.py::_find_mlx_python()` / `edit_ffmpeg.py::_qwen3tts_find_python()` 对齐。
    """
    dirs = [VENV_DIR]
    c_dir = (os.environ.get("VDL_COMMENTARY_DIR") or "").strip()
    if c_dir:
        dirs.append(Path(c_dir) / "scripts" / ".venv_qwen3tts_mlx")
    dirs.append(Path.home() / "WorkBuddy" / "问问题" / "commentary-pipeline" / "scripts" / ".venv_qwen3tts_mlx")
    return dirs


def _venv_kind(d: Path) -> str:
    """判断某个目录是不是能用的克隆环境：'mlx'（有 mlx_audio）/'torch'（只有 qwen_tts）/''。"""
    py = d / "bin" / "python"
    if not (py.is_file() and os.access(py, os.X_OK)):
        return ""
    has_mlx = has_torch = False
    for sp in d.glob("lib/python3*/site-packages"):
        if (sp / "mlx_audio").is_dir():
            has_mlx = True
        if (sp / "qwen_tts").is_dir() or (sp / "indextts").is_dir():
            has_torch = True
    if has_mlx:
        return "mlx"
    return "torch" if has_torch else ""


def find_venv() -> tuple[Path | None, str]:
    """返回 (可用的克隆 venv 目录, 类型)。看「解释器在 + 依赖已装」，不做 import（状态接口要秒回）。"""
    for d in _venv_dirs():
        kind = _venv_kind(d)
        if kind:
            return d, kind
    return None, ""


def venv_ok() -> bool:
    return find_venv()[0] is not None


def weights_ok() -> bool:
    return (_MAIN_WEIGHTS.is_file() and _MAIN_WEIGHTS.stat().st_size > _MIN_MAIN_BYTES
            and _TOK_WEIGHTS.is_file() and _TOK_WEIGHTS.stat().st_size > _MIN_TOK_BYTES)


def _disk_free_mb(path: Path) -> int:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return int(shutil.disk_usage(p).free / (1024 * 1024))
    except Exception:  # noqa: BLE001
        return -1


def status() -> dict:
    """环境状态 + 安装进度，供前端一次性拿全。"""
    py = base_python()
    v_dir, v_kind = find_venv()
    w_ok = weights_ok()
    needed = (0.0 if v_dir else EXPECTED_ENV_MB) + (0.0 if w_ok else EXPECTED_WEIGHTS_MB)
    note = ""
    if not _is_apple_silicon():
        note = "本机不是 Apple Silicon（mlx 只在 Apple 芯片上有 wheel），本地语音克隆不可用"
    elif (not py["path"]) and not v_dir:
        note = ("没找到可用的 Python 3（需要 ≥ 3.9）。可装 Xcode 命令行工具"
                "（`xcode-select --install`）或 Homebrew 的 python，再回到这里点安装。")
    elif v_kind == "torch":
        note = "现有环境是 CPU(torch) 版，合成一句要几分钟；建议重装成 MLX 版（走 Apple GPU）。"
    return {
        "supported": _is_apple_silicon(),
        "note": note,
        "python": {"path": py["path"], "version": py["version"], "candidates": py["all"]},
        "venv": {"path": str(v_dir or VENV_DIR), "ok": bool(v_dir), "kind": v_kind,
                 "install_target": str(VENV_DIR)},
        "weights": {"path": str(WEIGHTS_DIR), "ok": w_ok, "repo": HF_REPO},
        "ready": bool(v_dir and w_ok),
        "needed_mb": int(needed),
        "disk_free_mb": _disk_free_mb(Path.home()),
        "endpoint": HF_ENDPOINT,
        "install": progress(),
    }


# ───────────────────────── 安装 ─────────────────────────

def _sub_env() -> dict:
    """安装子进程的环境。

    🔴 默认**摘掉代理变量**：pypi 镜像与 hf-mirror 都在国内，直连即可；
    而坏/残留代理（如 `HTTP_PROXY=http://127.0.0.1:<死端口>`）会让 pip 与下载直接失败，
    报出来的错还是「连不上镜像」这种误导性信息。确实需要走代理的用户设
    `VDL_CLONE_KEEP_PROXY=1` 即可保留。
    """
    env = dict(os.environ)
    if (env.get("VDL_CLONE_KEEP_PROXY") or "").strip().lower() not in ("1", "true", "yes"):
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(k, None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    return env


def _run(cmd: list[str], *, timeout: int = 1800) -> tuple[int, str]:
    """跑一条安装命令，实时把输出尾部塞进日志（前端能看到「卡在哪一步」）。"""
    _log(f"$ {' '.join(cmd[:4])}{' …' if len(cmd) > 4 else ''}")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_sub_env())
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"
    tail = ((p.stderr or "") + (p.stdout or "")).strip()
    if tail:
        _log(tail.splitlines()[-1][:300])
    return p.returncode, tail


def _mlx_pin() -> str:
    """mlx 版本：macOS 13 必须钉 0.29.3（0.30.0 起只发 macOS 14+ 的 wheel），14+ 交给最新。"""
    raw = (os.environ.get("VDL_CLONE_MLX_VERSION") or "").strip()
    if raw:
        return raw
    try:
        major = int((platform.mac_ver()[0] or "0").split(".")[0])
    except Exception:  # noqa: BLE001
        major = 0
    return "0.29.3" if 0 < major < 14 else ""


def _weights_bytes() -> int:
    total = 0
    if WEIGHTS_DIR.is_dir():
        for f in WEIGHTS_DIR.rglob("*"):
            if f.is_file() and not f.name.endswith((".part", ".incomplete")):
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    return total


def _watch_weights(stop: threading.Event) -> None:
    """下载期间轮询落盘体积刷进度（snapshot_download 本身不给回调）。"""
    base = _weights_bytes()
    while not stop.wait(2.0):
        cur = _weights_bytes()
        mb = cur / (1024 * 1024)
        pct = min(99.0, max(0.0, (mb - base / (1024 * 1024)) / EXPECTED_WEIGHTS_MB * 100.0))
        _set(done_mb=round(mb, 1), total_mb=EXPECTED_WEIGHTS_MB, pct=round(pct, 1))
        _log(f"下载权重… {mb:.0f}/{EXPECTED_WEIGHTS_MB:.0f} MB")


def _install_worker(force: bool = False) -> None:
    try:
        _set(active=True, phase="python", pct=0.0, done_mb=0.0,
             total_mb=EXPECTED_ENV_MB + EXPECTED_WEIGHTS_MB, error="", log=[],
             started_at=time.time(), finished_at=0.0)

        if not _is_apple_silicon():
            raise RuntimeError("本机不是 Apple Silicon，mlx 无可用 wheel，本地语音克隆不可用")

        # 需要装 MLX 环境的情形：本来就没有可用 venv，或（force 时）现有的是 torch 版要换成 MLX。
        # 🔴 不能只判 `not venv_ok()`：那会让 force 重装在「已有 torch venv」时静默什么都不做。
        need_mlx = force or (find_venv()[1] != "mlx")
        if need_mlx:
            # ① 基础解释器
            py = base_python(force=True)
            if not py["path"]:
                raise RuntimeError(
                    "没找到可用的 Python 3（需 ≥ 3.9）。请先安装 Xcode 命令行工具"
                    "（xcode-select --install）或 Homebrew 的 python 后重试。"
                )
            _log(f"使用基础解释器：{py['path']}（{py['version']}）")

            # ② 建 venv
            _set(phase="venv")
            VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
            if VENV_DIR.exists() and not venv_python().is_file():
                shutil.rmtree(VENV_DIR, ignore_errors=True)   # 半成品 venv 留着只会误导
            if not VENV_DIR.exists():
                rc, out = _run([py["path"], "-m", "venv", str(VENV_DIR)], timeout=600)
                if rc != 0 or not venv_python().is_file():
                    raise RuntimeError(f"创建 Python 环境失败（exit={rc}）：{out[-300:]}")
            vpy = str(venv_python())

            # ③ 装依赖
            _set(phase="pip")
            _log("升级 pip…")
            _run([vpy, "-m", "pip", "install", "-q", "-U", "pip",
                  "-i", PIP_INDEX, "--timeout", "30"], timeout=900)
            pin = _mlx_pin()
            mlx_req = f"mlx=={pin}" if pin else "mlx"
            _log(f"安装 {mlx_req} + mlx-audio=={MLX_AUDIO_PIN}（约 500MB，要几分钟）…")
            rc, out = _run([vpy, "-m", "pip", "install", "--no-cache-dir",
                            "-i", PIP_INDEX, "--timeout", "60", mlx_req], timeout=1800)
            if rc != 0:
                raise RuntimeError(f"安装 mlx 失败（exit={rc}）：{out[-300:]}")
            # 🔴 mlx-audio 必须 --no-deps：它的依赖约束会把上面刚装好的 mlx 顶到 macOS 14+ 的版本
            rc, out = _run([vpy, "-m", "pip", "install", "--no-cache-dir", "--no-deps",
                            "-i", PIP_INDEX, "--timeout", "60",
                            f"mlx-audio=={MLX_AUDIO_PIN}"], timeout=900)
            if rc != 0:
                raise RuntimeError(f"安装 mlx-audio 失败（exit={rc}）：{out[-300:]}")
            rc, out = _run([vpy, "-m", "pip", "install", "--no-cache-dir",
                            "-i", PIP_INDEX, "--timeout", "60", *MLX_AUDIO_DEPS], timeout=1800)
            if rc != 0:
                raise RuntimeError(f"安装 mlx-audio 依赖失败（exit={rc}）：{out[-300:]}")

        if _CANCEL.is_set():
            raise RuntimeError("已取消")

        # ④ 下权重
        if not weights_ok():
            _set(phase="weights", pct=0.0, done_mb=0.0, total_mb=EXPECTED_WEIGHTS_MB)
            _log(f"从 {HF_ENDPOINT} 下载权重 {HF_REPO}（约 {EXPECTED_WEIGHTS_MB:.0f}MB）…")
            WEIGHTS_DIR.parent.mkdir(parents=True, exist_ok=True)
            stop = threading.Event()
            watcher = threading.Thread(target=_watch_weights, args=(stop,), daemon=True)
            watcher.start()
            try:
                from huggingface_hub import snapshot_download   # 惰性 import：没装也不影响 App 启动
                snapshot_download(
                    repo_id=HF_REPO,
                    local_dir=str(WEIGHTS_DIR),
                    endpoint=HF_ENDPOINT,     # 🔴 显式传参，绝不改进程级 HF_ENDPOINT
                    max_workers=4,
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"下载权重失败：{type(exc).__name__}: {exc}。"
                    f"可设 VDL_CLONE_HF_ENDPOINT 换镜像，或手动把 {MODEL_DIRNAME} "
                    f"整个目录放到 {WEIGHTS_ROOT} 下。"
                ) from exc
            finally:
                stop.set()
                watcher.join(timeout=5)

            if not weights_ok():
                raise RuntimeError(
                    f"权重下载完但校验不过（可能被截断）。可删掉 {WEIGHTS_DIR} 重试，"
                    "或用 VDL_CLONE_HF_ENDPOINT 换镜像。"
                )

        _set(phase="done", pct=100.0, active=False, finished_at=time.time())
        _log("安装完成 ✅ 本地语音克隆可用（切一次引擎或重开页面即可看到「已就绪」）")
    except Exception as exc:  # noqa: BLE001
        cancelled = _CANCEL.is_set()
        _set(phase="cancelled" if cancelled else "error", active=False,
             error=str(exc)[:500], finished_at=time.time())
        _log(("已取消" if cancelled else f"安装失败：{exc}")[:300])


def start_install(force: bool = False) -> dict:
    """启动（幂等）后台安装。已就绪时除非 force 否则直接返回。"""
    global _THREAD
    if not _is_apple_silicon():
        return {"ok": False, "msg": "本机不是 Apple Silicon，mlx 无可用 wheel，无法安装本地语音克隆"}
    with _LOCK:
        if _STATE["active"]:
            return {"ok": False, "msg": "安装已在进行中，请看进度"}
        if _STATE["phase"] == "done" and not force:
            return {"ok": True, "msg": "环境已就绪，无需重复安装"}
    if venv_ok() and weights_ok() and not force:
        return {"ok": True, "msg": "环境已就绪，无需重复安装"}
    free = _disk_free_mb(Path.home())
    need = (0.0 if venv_ok() else EXPECTED_ENV_MB) + (0.0 if weights_ok() else EXPECTED_WEIGHTS_MB)
    if 0 <= free < (need + 500):
        return {"ok": False,
                "msg": f"磁盘余量不足：约需 {need:.0f}MB，当前可用 {free}MB。请先腾出空间。"}
    _CANCEL.clear()
    _THREAD = threading.Thread(target=_install_worker, args=(bool(force),),
                               daemon=True, name="clone-env-install")
    _THREAD.start()
    return {"ok": True,
            "msg": f"已开始下载安装（约 {need / 1024:.1f}GB），过程中可正常用其它功能"}


def cancel() -> dict:
    """请求取消。只在阶段之间生效（权重大文件下到一半不会中断，避免留半包数据）。"""
    if not progress()["active"]:
        return {"ok": False, "msg": "当前没有进行中的安装"}
    _CANCEL.set()
    _log("收到取消请求，将在当前步骤结束后停止…")
    return {"ok": True, "msg": "已请求取消（当前步骤跑完就停）"}
