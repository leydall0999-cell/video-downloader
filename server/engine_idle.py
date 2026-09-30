"""AI 引擎空闲自动卸载（为内存吃紧的用户省内存）。

背景（2026-09-30 实测）：
  - 好消息：numpy / onnxruntime / cv2 / PIL 的 import 全在函数内部，**不用就不加载**。
  - 坏消息：一旦用过一次，会话就**永久常驻** —— matting_ai 的 `_SESSIONS` / `_SAM_SESSIONS`、
    dewatermark_ai 的 `_SESSIONS` 三个全局字典从不清理，全仓也没有任何 unload 调用。
    而内存大头恰恰是**模型权重**，不是库本身。

本模块做的事：注册这些「可释放的引擎」，各自记住最后使用时间，
空闲超过 TTL（默认 180 秒 = 3 分钟）就在后台线程里把会话清掉，下次用重新加载。

为什么**不需要** in-flight 计数（这是本设计的关键）：
  Python 是引用计数的。释放动作只是把字典里的引用删掉；如果此刻正有推理在跑，
  那个函数的栈里还握着 session 的引用，对象不会被销毁，推理会**安全跑完**，
  等栈退出引用归零才真正释放。所以不存在「边用边释放」的崩溃风险。
  反过来也说明：释放是「尽力而为」，不会打断任何进行中的任务。

⚠️ 期望要说清（别让用户的期待落空）：
  真正能收回的是**模型权重**（几十~几百 MB）。onnxruntime / numpy 这类 C 扩展
  一旦 import 就未必把内存还给 OS（`del sys.modules` 只解引用），
  所以释放后不会回到「刚启动」的水平，这是 Python 的固有限制。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import atomic_io

# 默认空闲多久卸载（秒）。用户明确要求 3 分钟。
DEFAULT_TTL = 180
MIN_TTL = 30
MAX_TTL = 3600

_CONFIG_LOCK = threading.Lock()

# 引擎注册表：name -> {"release": callable, "count": callable, "last_used": float}
_ENGINES: dict[str, dict[str, Any]] = {}
_REGISTRY_LOCK = threading.Lock()

_SWEEPER_STARTED = False
_SWEEPER_LOCK = threading.Lock()
_SWEEP_INTERVAL = 15  # 每 15 秒巡检一次（3 分钟 TTL 下足够精确，开销可忽略）


# ── 配置持久化 ────────────────────────────────────────────────────────
def _config_dir() -> Path:
    # ⚠️ 铁律：本地存储必须尊重 VDL_DATA_DIR，否则离线测试桩会写脏真实家目录
    # （2026-09-30 Keychain 污染事故的同源教训）。
    env = (os.environ.get("VDL_DATA_DIR") or "").strip()
    if env:
        base = Path(env)
    elif sys.platform == "win32" and getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / "VideoDownloader"
    else:
        base = Path.home() / ".video-downloader"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def _config_path() -> Path:
    return _config_dir() / "engine_idle.json"


def _read_raw() -> dict[str, Any]:
    p = _config_path()
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def get_config() -> dict[str, Any]:
    """读取配置：环境变量 > JSON 文件 > 默认值。"""
    raw = _read_raw()
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        enabled = True  # 默认开启：这个功能的目标就是帮内存吃紧的用户
    try:
        ttl = int(raw.get("ttl_seconds") or DEFAULT_TTL)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL
    ttl = max(MIN_TTL, min(MAX_TTL, ttl))

    env_on = (os.environ.get("VDL_ENGINE_IDLE_UNLOAD") or "").strip().lower()
    if env_on in ("0", "off", "false", "no"):
        enabled = False
    elif env_on in ("1", "on", "true", "yes"):
        enabled = True
    env_ttl = (os.environ.get("VDL_ENGINE_IDLE_TTL") or "").strip()
    if env_ttl.isdigit():
        ttl = max(MIN_TTL, min(MAX_TTL, int(env_ttl)))
    return {"enabled": enabled, "ttl_seconds": ttl}


def save_config(data: dict[str, Any]) -> dict[str, Any]:
    """持久化配置（前端开关）。返回生效后的配置。"""
    cur = _read_raw()
    if "enabled" in data and isinstance(data["enabled"], bool):
        cur["enabled"] = data["enabled"]
    if "ttl_seconds" in data:
        try:
            ttl = int(data["ttl_seconds"])
        except (TypeError, ValueError):
            ttl = DEFAULT_TTL
        cur["ttl_seconds"] = max(MIN_TTL, min(MAX_TTL, ttl))
    with _CONFIG_LOCK:
        atomic_io.atomic_write_json(_config_path(), cur)
    return get_config()


# ── 引擎注册 ──────────────────────────────────────────────────────────
def register(name: str, release: Callable[[], int], count: Callable[[], int]) -> None:
    """注册一个可释放的引擎。

    release：清空自己的会话缓存，返回释放掉的会话数。
    count  ：当前已加载的会话数（用于状态展示，不调用 release 也能查）。
    """
    with _REGISTRY_LOCK:
        _ENGINES[name] = {
            "release": release,
            "count": count,
            "last_used": time.time(),
        }


def touch(name: str) -> None:
    """标记「刚用过」——任何拿到会话的地方都要调用，否则刚加载就被回收。"""
    with _REGISTRY_LOCK:
        e = _ENGINES.get(name)
        if e is not None:
            e["last_used"] = time.time()
    ensure_started()


def _sweep_once(now: float | None = None) -> list[str]:
    """巡检一轮：把空闲超时的引擎释放掉。返回被释放的引擎名列表。"""
    cfg = get_config()
    if not cfg["enabled"]:
        return []
    now = time.time() if now is None else now
    ttl = cfg["ttl_seconds"]
    freed: list[str] = []
    with _REGISTRY_LOCK:
        items = list(_ENGINES.items())
    for name, e in items:
        try:
            if e["count"]() <= 0:
                continue
            if now - e["last_used"] < ttl:
                continue
            e["release"]()
            freed.append(name)
        except Exception:  # noqa: BLE001 - 释放失败绝不能影响主流程
            pass
    return freed


def _sweeper_loop() -> None:
    while True:
        try:
            _sweep_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_SWEEP_INTERVAL)


def ensure_started() -> None:
    """启动后台巡检线程（幂等；daemon 线程，不阻碍进程退出）。"""
    global _SWEEPER_STARTED
    with _SWEEPER_LOCK:
        if _SWEEPER_STARTED:
            return
        t = threading.Thread(target=_sweeper_loop, name="vdl-engine-idle", daemon=True)
        t.start()
        _SWEEPER_STARTED = True


def status() -> dict[str, Any]:
    """给前端看的状态：开关、TTL、各引擎是否已加载 / 已空闲多久 / 还有多久释放。"""
    cfg = get_config()
    now = time.time()
    engines = []
    with _REGISTRY_LOCK:
        items = list(_ENGINES.items())
    for name, e in items:
        try:
            n = e["count"]()
        except Exception:  # noqa: BLE001
            n = 0
        idle = max(0.0, now - e["last_used"])
        engines.append(
            {
                "name": name,
                "loaded": n,
                "idle_seconds": round(idle, 1),
                "releases_in": round(max(0.0, cfg["ttl_seconds"] - idle), 1) if (cfg["enabled"] and n) else None,
            }
        )
    return {"enabled": cfg["enabled"], "ttl_seconds": cfg["ttl_seconds"], "engines": engines}


def release_all() -> int:
    """立即释放所有已加载会话（供「手动释放」按钮 / 测试使用）。返回释放的引擎数。"""
    freed = 0
    with _REGISTRY_LOCK:
        items = list(_ENGINES.items())
    for _name, e in items:
        try:
            if e["count"]() > 0:
                e["release"]()
                freed += 1
        except Exception:  # noqa: BLE001
            pass
    return freed
