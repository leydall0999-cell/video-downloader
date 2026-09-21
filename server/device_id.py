"""VDL 设备指纹（P2 一机一码 · 纯标准库，跨平台）。

用途：
  - 会员/卡密与「这台设备」绑定（见 membership.meta.device_fp 与云端 license 登记），
    防止把 membership.json 拷到另一台机器白嫖会员。

指纹取源（按平台取一个**系统级稳定 ID**，重装系统不变、不随硬件小改动漂移）：
  - macOS   : IOPlatformUUID（ioreg，等同「关于本机 → 系统报告」里的硬件 UUID）
  - Windows : MachineGuid（注册表 HKLM\\SOFTWARE\\Microsoft\\Cryptography）
  - Linux   : /etc/machine-id（systemd）→ /var/lib/dbus/machine-id 兜底

指纹 = SHA256("<源ID>|vdl-license-v1") 前 32 个 hex 字符。加 salt 是为了：
  ① 原始系统 UUID 不以明文出本机（隐私）；② 不同产品线即使同源 ID 也得到不同指纹。

降级：源 ID 全都取不到时，用 hostname + 平台 + 网卡 MAC 混合出**弱指纹**
（weak=True；换网卡/改主机名会变）。调用方（membership 校验）对弱指纹只记
日志不锁会员——宁可漏判也不误伤付费用户。

线程安全：首次调用后缓存（进程内不变）。subprocess 只在 macOS/Windows 跑一次。
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import uuid as _uuid
from functools import lru_cache
from typing import Optional, Tuple

_SALT = "vdl-license-v1"
_FP_RE = re.compile(r"^[0-9a-f]{32}$")


def is_fingerprint(value: object) -> bool:
    """是否像合法指纹（32 位小写 hex）。供写入前/读出后校验。"""
    return isinstance(value, str) and bool(_FP_RE.match(value))


def _macos_platform_uuid() -> Optional[str]:
    try:
        out = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return None
    m = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"', out)
    return m.group(1) if m else None


def _windows_machine_guid() -> Optional[str]:
    try:
        import winreg  # noqa: PLC0415  仅 Windows 可用
        k = winreg.OpenKey(  # type: ignore[attr-defined]
            winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
            r"SOFTWARE\Microsoft\Cryptography", 0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY)  # type: ignore[attr-defined]
        val, _ = winreg.QueryValueEx(k, "MachineGuid")  # type: ignore[attr-defined]
        winreg.CloseKey(k)  # type: ignore[attr-defined]
        return str(val) or None
    except Exception:
        return None


def _linux_machine_id() -> Optional[str]:
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                s = f.read().strip()
            if s:
                return s
        except OSError:
            continue
    return None


def _source_id() -> Tuple[Optional[str], bool]:
    """返回 (源ID, strong?)。strong=False 表示是弱降级源。"""
    system = platform.system()
    sid: Optional[str] = None
    if system == "Darwin":
        sid = _macos_platform_uuid()
    elif system == "Windows":
        sid = _windows_machine_guid()
    else:
        sid = _linux_machine_id()
    if sid:
        return sid, True
    # ── 弱降级：hostname + MAC（宁可弱也别没有；调用方对 weak 只记日志不锁会员）──
    try:
        mac = _uuid.getnode()
    except Exception:
        mac = 0
    weak_src = f"{platform.node()}|{platform.system()}|{mac}"
    return weak_src, False


def _digest(src: str) -> str:
    return hashlib.sha256(f"{src}|{_SALT}".encode("utf-8")).hexdigest()[:32]


@lru_cache(maxsize=1)
def fingerprint() -> Tuple[str, bool]:
    """当前设备指纹。返回 (fp, strong)。fp 恒为 32 hex；strong=False 时是弱指纹。"""
    sid, strong = _source_id()
    return _digest(sid), strong


def fingerprint_strict() -> str:
    """强指纹（取不到系统级 ID 时抛 RuntimeError）。供安全敏感路径使用。"""
    fp, strong = fingerprint()
    if not strong:
        raise RuntimeError("无法取得稳定的设备标识（IOPlatformUUID/MachineGuid/machine-id）")
    return fp
