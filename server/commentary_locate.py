"""解说管线定位（统一三处硬编码路径）。

集中解析「解说管线(commentary-pipeline)在哪、用哪个 Python 跑」，
供桌面启动器(desktop_launcher)与服务端(app._CommentaryRuntime)共用，
消除原先散落在三处的重复扫描逻辑。

解析优先级（frozen 打包版以「包内捆绑」为准，践行自包含铁律；dev/外部仅兜底）：

1. 包内捆绑（自包含首选）：
   - macOS .app:        <Resources>/commentary
   - PyInstaller 单目录: <可执行文件同级>/commentary
   - PyInstaller 单文件: sys._MEIPASS/commentary
   - 判定：该目录含 process.py 即视为有效；跑 process.py 的解释器用
     sys.executable 自身重入(--vdl-commentary-worker，由 #198 实现)。
2. 显式环境变量：VDL_COMMENTARY_DIR（+ 可选 VDL_COMMENTARY_PYTHON）。
3. 外部扫描（开发/兜底）：~/WorkBuddy/问问题/commentary-pipeline、
   ~/commentary-pipeline，取其各自的 .venv 解释器。
4. 仅 WorkBuddy 默认 python：~/.workbuddy/binaries/python/envs/default/.venv。

返回 CommentaryLocation(root, python, bundled, source)；找不到返回 None。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class CommentaryLocation:
    """定位到的解说管线信息。"""

    def __init__(self, root: Path, python: str, bundled: bool, source: str):
        self.root = root          # 管线根目录（含 process.py / scripts / models），子进程 cwd
        self.python = python      # 跑 process.py 的解释器；bundled 时为 sys.executable（重入）
        self.bundled = bundled    # True=包内捆绑（自包含），False=外部/显式
        self.source = source      # 诊断用来源说明

    def __repr__(self) -> str:
        return (
            f"CommentaryLocation(root={self.root}, python={self.python}, "
            f"bundled={self.bundled}, source={self.source!r})"
        )


def _venv_python(root: Path) -> Path:
    """跨平台返回某 venv 的解释器路径。"""
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _has_process(root: Path) -> bool:
    """目录是否像一个有效的 commentary-pipeline（含 process.py）。"""
    return root.is_dir() and (root / "process.py").is_file()


def _bundled_root() -> Path | None:
    """返回包内捆绑的管线根目录（若存在），否则 None。仅 frozen 模式有意义。"""
    if not getattr(sys, "frozen", False):
        return None
    cands: list[Path] = []
    # PyInstaller 单文件模式：临时解压目录
    if getattr(sys, "_MEIPASS", None):
        cands.append(Path(sys._MEIPASS) / "commentary")
    # macOS .app / 单目录模式：可执行文件所在目录往上推导
    _exe = Path(sys.executable)
    _macos_dir = _exe.parent
    _resources = (_macos_dir / ".." / "Resources").resolve()
    cands.append(_resources / "commentary")   # .app: Contents/Resources/commentary
    cands.append(_macos_dir / "commentary")   # 单目录: <exe_dir>/commentary
    for c in cands:
        if _has_process(c):
            return c
    return None


def locate_commentary() -> CommentaryLocation | None:
    """定位解说管线，返回首选位置或 None。

    优先级见模块文档；frozen 模式首选包内捆绑（自包含），其余情况走外部/显式。
    """
    # 1) 包内捆绑（自包含首选）
    b = _bundled_root()
    if b is not None:
        return CommentaryLocation(b, sys.executable, True, "bundled-in-package")

    # 2) 显式环境变量
    cdir = (os.environ.get("VDL_COMMENTARY_DIR") or "").strip()
    if cdir and _has_process(Path(cdir)):
        py = (os.environ.get("VDL_COMMENTARY_PYTHON") or "").strip()
        if not py:
            vp = _venv_python(Path(cdir))
            py = str(vp) if vp.exists() else ""
        return CommentaryLocation(Path(cdir), py or sys.executable, False, "explicit-env")

    # 3) 外部扫描（开发/兜底）
    for cand in [
        Path.home() / "WorkBuddy" / "问问题" / "commentary-pipeline",
        Path.home() / "commentary-pipeline",
    ]:
        if _has_process(cand):
            vp = _venv_python(cand)
            py = str(vp) if vp.exists() else ""
            if not py:
                dft = _venv_python(
                    Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "default"
                )
                py = str(dft) if dft.exists() else sys.executable
            return CommentaryLocation(cand, py, False, f"scan:{cand}")

    # 4) 仅 WorkBuddy 默认 python（极端兜底，根目录退化为常见路径）
    dft = _venv_python(
        Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "default"
    )
    if dft.exists():
        return CommentaryLocation(
            Path.home() / "commentary-pipeline", str(dft), False, "workbuddy-default"
        )
    return None
