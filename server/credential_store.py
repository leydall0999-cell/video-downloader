"""server/credential_store.py — 账号 token 的凭据存储（macOS Keychain 优先）。

为什么需要它：
token 原先明文写在 `~/.video-downloader/membership*.json` 的 `meta.account.token`。
任何人拷走这个文件，就能冒充该用户调云端（消耗积分、解绑设备、看会员权益）。
Keychain 由系统级 ACL 保护：条目由本 App 创建，默认只有本 App 能读，
第三方进程（含另一个用户账号下的进程）读不到明文。

设计原则（**fail-safe 优先于加固**）：
- Keychain 不可用（非 macOS / security 命令缺失 / 被系统拒绝）时**回落到本地 JSON**，
  绝不能因为加固让用户掉登录态 —— 掉登录比 token 泄露更影响体验且更难排查。
- 读取顺序：JSON（历史明文，兼容老数据）→ Keychain。命中 JSON 时会顺手迁移到
  Keychain 并清空 JSON 里的明文。
- 纯函数、不 import app，便于离线单测。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

SERVICE = "com.videodownloader.desktop"
_TIMEOUT = 5


def _base_dir() -> Path:
    try:
        from auth_store import _base_dir as _bd
        return _bd()
    except Exception:
        return Path.home() / ".video-downloader"


def _fallback_path() -> Path:
    return _base_dir() / "credentials.json"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _run_security(args: list[str]) -> tuple[bool, str]:
    """调用 security 命令。返回 (是否成功, stdout)。"""
    try:
        p = subprocess.run(["security", *args], capture_output=True,
                           text=True, timeout=_TIMEOUT)
        if p.returncode == 0:
            return True, (p.stdout or "").strip()
        return False, (p.stderr or "").strip()
    except (OSError, subprocess.SubprocessError):
        return False, ""


def _read_fallback() -> dict[str, str]:
    p = _fallback_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_fallback(data: dict[str, str]) -> bool:
    try:
        p = _fallback_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def set_token(email: str, token: str) -> str:
    """保存 token。返回实际落点：'keychain' 或 'file'（均为尽力而为）。"""
    email = (email or "").strip().lower()
    if not email or not token:
        return "none"
    if _is_macos():
        ok, _ = _run_security(["add-generic-password", "-U", "-a", email,
                               "-s", SERVICE, "-w", token])
        if ok:
            # 已从明文 JSON 迁走：清掉历史明文，避免两份都在
            _migrate_out_of_json(email)
            return "keychain"
    data = _read_fallback()
    data[email] = token
    return "file" if _write_fallback(data) else "none"


def get_token(email: str) -> str:
    """读取 token：JSON 历史明文优先（兼容），否则查 Keychain。"""
    email = (email or "").strip().lower()
    if not email:
        return ""
    legacy = _read_fallback().get(email) or ""
    if legacy:
        return legacy
    if _is_macos():
        ok, out = _run_security(["find-generic-password", "-a", email,
                                 "-s", SERVICE, "-w"])
        if ok and out:
            return out
    return ""


def delete_token(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    if _is_macos():
        _run_security(["delete-generic-password", "-a", email, "-s", SERVICE])
    data = _read_fallback()
    if email in data:
        data.pop(email, None)
        _write_fallback(data)


def _migrate_out_of_json(email: str) -> None:
    """Keychain 写成功后，把 JSON 里的历史明文清掉（只清这一个账号）。"""
    data = _read_fallback()
    if email in data:
        data.pop(email, None)
        _write_fallback(data)


def resolve_token(acc: Optional[dict]) -> str:
    """从账号字典解析 token —— 各读取点统一走这里。

    兼容两种存储：JSON 里还留着明文的（老数据）直接用；否则按 email 查 Keychain。
    """
    acc = acc or {}
    t = str(acc.get("token") or "").strip()
    if t:
        return t
    return get_token(str(acc.get("email") or ""))
