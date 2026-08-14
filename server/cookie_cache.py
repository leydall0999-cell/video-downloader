"""本机 Cookie 缓存。

把从本机浏览器解密出的登录态按站点缓存到用户目录（仅当前用户可读，chmod 600），
解析 / 下载 / 观看链路在用户未手动粘贴 Cookie 时优先用缓存，缺失再实时解密浏览器 DB。

设计原则（合规）：
- 仅本机：文件落在 ~/.videodownloader/cookies/，不外传、不共享、不上报服务器。
- 不跨用户：每个用户只读自己的浏览器与自己的缓存，不涉及任何「共享库」。
- 有时效：Cookie 会过期，缓存 7 天后自动重新解密，避免拿到失效凭证
  （日常使用只要浏览器仍登录该平台即无缝续命，无需手动操作）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_COOKIE_CACHE_DIR = Path.home() / ".videodownloader" / "cookies"
_COOKIE_TTL = 7 * 24 * 3600  # 秒；超时自动重新解密浏览器，避免用到失效 Cookie


def _cache_file(host: str) -> Path:
    safe = (host or "unknown").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _COOKIE_CACHE_DIR / f"{safe}.json"


def get_cached_cookie_header(host: str) -> str | None:
    """返回缓存的 Cookie 请求头；缺失 / 过期 / 解密失败则返回 None。"""
    try:
        f = _cache_file(host)
        if f.exists():
            data = json.loads(f.read_text())
            if time.time() - data.get("ts", 0) < _COOKIE_TTL:
                return data.get("header") or None
    except Exception:
        pass
    # 实时解密浏览器并写缓存
    try:
        from downloader import get_browser_cookie_header
        header = get_browser_cookie_header(host, f"https://{host}/")
        if header:
            _save(host, header)
            return header
    except Exception:
        pass
    return None


def _save(host: str, header: str) -> None:
    try:
        _COOKIE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f = _cache_file(host)
        f.write_text(json.dumps({"header": header, "ts": int(time.time())}))
        os.chmod(f, 0o600)  # 仅当前用户可读写，保护登录凭证
    except Exception:
        pass


def clear_cookie_cache() -> int:
    """清除全部缓存，返回删除的文件数。"""
    try:
        if not _COOKIE_CACHE_DIR.exists():
            return 0
        n = 0
        for p in _COOKIE_CACHE_DIR.glob("*.json"):
            try:
                p.unlink()
                n += 1
            except Exception:
                pass
        return n
    except Exception:
        return 0


def refresh_cookie(host: str) -> str | None:
    """强制重新解密该站点 Cookie 并刷新缓存。"""
    try:
        _cache_file(host).unlink(missing_ok=True)
    except Exception:
        pass
    return get_cached_cookie_header(host)
