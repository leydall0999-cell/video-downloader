"""共享「配置 / 状态文件」的原子落盘与读改写临界区（全功能通用）。

为什么必须统一（2026-09-16 同类隐患清查，承接 users.json 那次）：
本仓曾有多处「读全量 → 改一处 → 写全量」的 JSON 落盘，写法是**固定名**临时文件
（`xxx.json.tmp`）甚至**直接覆盖写**。两者都会在并发 / 中断下毁掉整份用户数据：

1. 固定名临时文件 —— 两个写者并发时共用同一个临时路径：
   A 打开截断 → B 打开再截断 → A 先 `replace`（此时文件里是 B 写了一半的内容）
   → 目标文件解析失败 = **整份配置消失**（会员状态、API Key、保险箱索引都会这样丢）。
2. 直接 `Path.write_text` —— 它先截断原文件再写。进程被杀 / 断电就留下半截文件；
   更糟的是**并发读者**（`/api/quota/status`、子进程 worker）会读到写了一半的内容，
   解析失败后按「空状态」处理 → 免费云端额度被**整体复原**（变现漏洞）。

本模块给出唯一实现：
  · 唯一临时名（同目录、`.` 前缀、带 pid + 线程 id + 自增序号）→ 写者之间永不互撞；
  · `os.open(..., mode)` + 显式 `chmod`（不依赖 umask）→ 凭据文件权限只会收紧不会放宽；
  · flush + fsync（再尽力 fsync 父目录）→ 断电最多丢一次写，不会留下空文件；
  · `os.replace` → 同文件系统内原子换入，读者要么看到旧内容要么看到新内容；
  · `mutation(path)` → 按路径共享的进程内 RLock，用于包住「载入 → 改 → 写回」**整段**。
    ⚠️ 只锁落盘等于没锁：两个线程照样各自读到同一份旧快照，后写的覆盖先写的。

⚠️ `quota.py` 未使用本模块：它要与解说管线的 `scripts/quota.py` **逐字节同源**
（两个仓库各持一份，diff 校验守护），而本模块只在 app 侧随包分发，故那份自带等价实现。
⚠️ `auth_store.py` 亦保留自己那份等价实现（账号表专用，另带跨进程 flock），它本就不是
固定名写者，未强求合并以免动已验证的账号链路。
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Union

__all__ = [
    "mutation",
    "file_lock",
    "unique_temp_path",
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
]

PathLike = Union[str, os.PathLike]

_TMP_PREFIX = "."
_TMP_SUFFIX = ".tmp"

_seq_guard = threading.Lock()
_seq = 0

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _next_seq() -> int:
    global _seq
    with _seq_guard:
        _seq += 1
        return _seq


def _lock_key(path: PathLike) -> str:
    return os.path.abspath(os.path.expanduser(os.fspath(path)))


def file_lock(path: PathLike) -> threading.RLock:
    """取该路径的进程内锁（按绝对路径共享同一把；可重入）。"""
    key = _lock_key(path)
    with _LOCKS_GUARD:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[key] = lk
        return lk


@contextmanager
def mutation(path: PathLike) -> Iterator[None]:
    """「载入 → 改 → 写回」临界区。必须包住读，只锁落盘等于没锁。"""
    with file_lock(path):
        yield


def _tmp_path(p: Path) -> Path:
    """同目录唯一临时名。同目录才是原子 replace 的前提。"""
    return p.with_name(
        f"{_TMP_PREFIX}{p.name}.{os.getpid()}.{threading.get_ident()}.{_next_seq()}{_TMP_SUFFIX}"
    )


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def unique_temp_path(path: PathLike, *, suffix: Optional[str] = None) -> Path:
    """同目录唯一临时路径（**只给名字，不负责写入**）。

    用于「外部工具自己往文件里写」的场景：ffmpeg 抽帧要按扩展名猜封装格式、
    `urlretrieve` 下 100MB+ 模型不能走内存。这些用不了 `atomic_write_*`，
    但同样不能忍受固定名 —— 两个并发写者（双实例 / 父子进程）会互截断同一个文件。
    ⚠️ 调用方用完必须自己 `unlink(missing_ok=True)` 清理失败路径的残留。
    """
    p = Path(path)
    ext = p.suffix if suffix is None else suffix
    return p.with_name(
        f"{_TMP_PREFIX}{p.stem}.{os.getpid()}.{threading.get_ident()}.{_next_seq()}{ext}"
    )


def atomic_write_bytes(
    path: PathLike,
    payload: bytes,
    *,
    mode: int = 0o600,
    chmod: Optional[Callable[[Path], None]] = None,
) -> None:
    """原子写字节。**绝不**在目标路径上就地截断（这是本模块存在的全部理由）。

    chmod：平台特定的权限钩子（如 Windows 的 icacls）。给了就用它，否则 `os.chmod(tmp, mode)`。
    """
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(p)
    try:
        # mode 经 umask 只会更严；之后再显式 chmod 到精确值（umask 0 也不会放宽）。
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        try:
            if chmod is not None:
                chmod(tmp)
            else:
                os.chmod(tmp, mode)
        except Exception:
            pass
        os.replace(tmp, p)
        _fsync_dir(p.parent)
    finally:
        if tmp.exists():          # 失败路径不留垃圾临时文件
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_text(
    path: PathLike,
    text: str,
    *,
    mode: int = 0o600,
    chmod: Optional[Callable[[Path], None]] = None,
) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode, chmod=chmod)


def atomic_write_json(
    path: PathLike,
    obj: Any,
    *,
    mode: int = 0o600,
    indent: Optional[int] = 2,
    ensure_ascii: bool = False,
    chmod: Optional[Callable[[Path], None]] = None,
) -> None:
    atomic_write_text(
        path,
        json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent),
        mode=mode,
        chmod=chmod,
    )
