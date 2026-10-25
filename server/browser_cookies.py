"""跨平台浏览器 Cookie 读取（自解密，不依赖 yt-dlp）。

为什么需要这个模块（2026-10-24）
--------------------------------
`downloader._BROWSER_COOKIE_PROFILES` 原先把路径写死成 macOS 的
`~/Library/Application Support/...`。在 Windows 上这些 glob 全部落空 ——
**「自动读取浏览器登录态」在 Windows 端等于从未实现**，这才是百度网盘解析
拿不到登录态、只能报「请填写 BDUSS」的真正原因。

本模块解决三件事：
1. **跨平台覆盖**：Chromium 系（Chrome / Edge / Brave / Chromium / Vivaldi /
   Opera）＋国产 Chromium 壳（UC / 360 / QQ / 搜狗 / 2345）＋ Firefox。
2. **优先当前正在运行的浏览器**：用户此刻登录的 profile 才是有效会话，
   装了但没开的浏览器往往留着几个月前的过期 Cookie。
3. **自己解密**：Windows 用 DPAPI 取 AES key + AES-GCM 解 v10/v11。
   yt-dlp 在 Windows 上读不了运行中的浏览器（upstream #7271，
   Chromium 对 Cookies 加独占锁），不能作为主路径。

不支持的：v20（Edge/Chrome 127+ 的 ABE 应用绑定加密）。它必须由浏览器自身的
Elevation Service 解码，第三方进程无解。遇到时**如实报告原因**，不静默当成
「未登录」，避免用户以为是自己没登。

合规边界：只读本机、只读当前用户、不落盘不上传；调用方（cookie_cache）
负责决定是否缓存。
"""
from __future__ import annotations

import glob
import os
import sqlite3
import sys
import time
from typing import Any
from urllib.parse import quote

# ── 浏览器清单 ────────────────────────────────────────────────────────
# key     : 稳定标识（同时作为 yt-dlp 的 browser 名，能对上的才回退给它）
# kind    : chromium / firefox
# procs   : 进程名（小写，用于判断「是否正在运行」）
# roots   : User Data 根目录候选（按平台存在性筛）
_BROWSER_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "key": "chrome", "kind": "chromium", "label": "Chrome",
        "procs": ("chrome.exe", "google chrome"),
        "roots": (
            r"%LOCALAPPDATA%\Google\Chrome\User Data",
            "~/Library/Application Support/Google/Chrome",
            "~/.config/google-chrome",
        ),
    },
    {
        "key": "edge", "kind": "chromium", "label": "Microsoft Edge",
        "procs": ("msedge.exe", "microsoft edge"),
        "roots": (
            r"%LOCALAPPDATA%\Microsoft\Edge\User Data",
            "~/Library/Application Support/Microsoft Edge",
            "~/.config/microsoft-edge",
        ),
    },
    {
        "key": "brave", "kind": "chromium", "label": "Brave",
        "procs": ("brave.exe", "brave browser"),
        "roots": (
            r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data",
            "~/Library/Application Support/BraveSoftware/Brave-Browser",
            "~/.config/BraveSoftware/Brave-Browser",
        ),
    },
    {
        "key": "chromium", "kind": "chromium", "label": "Chromium",
        "procs": ("chromium.exe", "chromium"),
        "roots": (
            r"%LOCALAPPDATA%\Chromium\User Data",
            "~/Library/Application Support/Chromium",
            "~/.config/chromium",
        ),
    },
    {
        "key": "vivaldi", "kind": "chromium", "label": "Vivaldi",
        "procs": ("vivaldi.exe", "vivaldi"),
        "roots": (
            r"%LOCALAPPDATA%\Vivaldi\User Data",
            "~/Library/Application Support/Vivaldi",
            "~/.config/vivaldi",
        ),
    },
    {
        "key": "opera", "kind": "chromium", "label": "Opera",
        "procs": ("opera.exe", "opera"),
        "roots": (
            r"%APPDATA%\Opera Software\Opera Stable",
            "~/Library/Application Support/com.operasoftware.Opera",
            "~/.config/opera",
        ),
    },
    # ── 国产 Chromium 壳：yt-dlp 不认识，只能自解密 ──
    {
        "key": "uc", "kind": "chromium", "label": "UC浏览器",
        "procs": ("uc.exe", "ucbrowser"),
        "roots": (r"%LOCALAPPDATA%\UC\User Data",),
    },
    {
        "key": "360se", "kind": "chromium", "label": "360安全浏览器",
        "procs": ("360se.exe", "360se6.exe", "360se"),
        "roots": (r"%APPDATA%\360se6\User Data",),
    },
    {
        "key": "360chrome", "kind": "chromium", "label": "360极速浏览器",
        "procs": ("360chrome.exe", "360chromex.exe"),
        "roots": (r"%LOCALAPPDATA%\360Chrome\Chrome\User Data", r"%LOCALAPPDATA%\360ChromeX\Chrome\User Data"),
    },
    {
        "key": "qqbrowser", "kind": "chromium", "label": "QQ浏览器",
        "procs": ("qqbrowser.exe",),
        "roots": (r"%LOCALAPPDATA%\Tencent\QQBrowser\User Data",),
    },
    {
        "key": "sogou", "kind": "chromium", "label": "搜狗浏览器",
        "procs": ("sogouexplorer.exe",),
        "roots": (r"%APPDATA%\SogouExplorer\Webkit",),
    },
    {
        "key": "2345", "kind": "chromium", "label": "2345浏览器",
        "procs": ("2345explorer.exe",),
        "roots": (r"%APPDATA%\2345Explorer\User Data",),
    },
    # ── Firefox：cookies.sqlite 明文，无加密 ──
    {
        "key": "firefox", "kind": "firefox", "label": "Firefox",
        "procs": ("firefox.exe", "firefox"),
        "roots": (
            r"%APPDATA%\Mozilla\Firefox\Profiles",
            "~/Library/Application Support/Firefox/Profiles",
            "~/.mozilla/firefox",
        ),
    },
)

_RUN_CACHE: dict[str, Any] = {"t": 0.0, "v": frozenset()}
_KEY_CACHE: dict[str, tuple[float, bytes | None]] = {}
# 「这个库打不开」的负缓存：同一进程里一次探测会走 find_source + locked_browsers
# 两条路径，把同一个被锁的库试两遍。Edge 的库实测每次失败要等 2×1.4s 的
# sqlite 超时，两遍就是 ~6s —— 接口直接卡住。这里把失败结果记 10 秒。
_LOCKED_CACHE: dict[str, float] = {}
_LOCKED_TTL = 10.0


# ── 进程探测 ─────────────────────────────────────────────────────────
def _windows_process_names() -> set[str]:
    """Windows 进程名集合。先走 ctypes（快、无子进程），失败回退 tasklist。

    必须带回退：某些受限制的运行环境（沙盒 / 低权限服务账号）里
    OpenProcess 会全被拒，ctypes 路径静默返回空集，结果就是
    「浏览器明明开着却判定为未运行」——优先用运行中浏览器的逻辑直接失效。
    """
    names: set[str] = set()
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.EnumProcesses.restype = wintypes.BOOL
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        arr = (wintypes.DWORD * 4096)()
        cb = wintypes.DWORD()
        if k32.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(cb)):
            n = cb.value // ctypes.sizeof(wintypes.DWORD)
            for i in range(n):
                pid = arr[i]
                if not pid:
                    continue
                h = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
                if not h:
                    continue
                try:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        names.add(os.path.basename(buf.value).lower())
                finally:
                    k32.CloseHandle(h)
    except Exception:
        names = set()
    if names:
        return names
    try:
        import subprocess
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=6, creationflags=flags,
        )
        for line in out.stdout.splitlines():
            parts = line.split('","')
            if len(parts) >= 2:
                names.add(parts[0].strip('"').lower())
    except Exception:
        pass
    return names


def running_process_names() -> frozenset[str]:
    """当前运行的进程名集合（小写），缓存 5 秒。

    用途：判断「哪个浏览器正开着」——开着的那个才是用户此刻在用的登录态。
    """
    now = time.time()
    if _RUN_CACHE["v"] and now - _RUN_CACHE["t"] < 5:
        return _RUN_CACHE["v"]
    if sys.platform == "win32":
        names = _windows_process_names()
    else:
        names = set()
        try:
            import subprocess
            out = subprocess.run(
                ["ps", "-Ao", "comm="], capture_output=True, text=True, timeout=3,
            )
            for line in out.stdout.splitlines():
                line = line.strip()
                if line:
                    names.add(os.path.basename(line).lower())
        except Exception:
            pass
    _RUN_CACHE["t"] = now
    _RUN_CACHE["v"] = frozenset(names)
    return _RUN_CACHE["v"]


def is_browser_running(src: dict[str, Any]) -> bool:
    running = running_process_names()
    return any(p in running for p in src["procs"])


# ── Profile / 数据库枚举 ──────────────────────────────────────────────
def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def iter_profiles(src: dict[str, Any]) -> list[tuple[str, str]]:
    """列出某个浏览器的 (profile 名, Cookie 数据库路径)。

    Chromium 新版本在 `<profile>/Network/Cookies`，旧版本在 `<profile>/Cookies`，
    两种都收；Opera 这类 root 本身就是 profile，也要处理。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for root in src["roots"]:
        rp = _expand(root)
        if not os.path.isdir(rp):
            continue
        if src["kind"] == "firefox":
            for sub in sorted(glob.glob(os.path.join(rp, "*"))):
                db = os.path.join(sub, "cookies.sqlite")
                if os.path.isfile(db) and db not in seen:
                    seen.add(db)
                    out.append((os.path.basename(sub), db))
            continue
        subs = [rp] + sorted(glob.glob(os.path.join(rp, "*")))
        for sub in subs:
            if not os.path.isdir(sub):
                continue
            for rel in (os.path.join("Network", "Cookies"), "Cookies"):
                db = os.path.join(sub, rel)
                if os.path.isfile(db) and db not in seen:
                    seen.add(db)
                    out.append((os.path.basename(sub) or src["key"], db))
                    break
    return out


def _local_state_for(db: str) -> str | None:
    """从 Cookie 库往上找 `Local State`（Chromium 存 AES key 的地方）。"""
    d = os.path.dirname(db)
    for _ in range(4):
        p = os.path.join(d, "Local State")
        if os.path.isfile(p):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _probe_readable(db: str) -> bool:
    """能不能读出这个文件的一个字节 —— 0 毫秒级的「是否被独占锁」判定。

    为什么需要这么一层：Windows 上 Chromium 系（Chrome/Edge/UC/360…）对
    Cookies 库加共享锁，被锁时 `open()` 立刻 PermissionError，而 sqlite 的
    connect 要**固定等 1.4 秒**才报「unable to open database file」（实测
    两次尝试 2.9 秒，接口直接卡住）。先用最便宜的方式探一下，
    被锁就立刻返回，省掉整段 sqlite 等待。
    """
    try:
        with open(db, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


def _open_db(db: str) -> tuple[sqlite3.Connection | None, str | None, bool]:
    """打开 Cookie 库。返回 (连接, 临时副本路径, 是否被独占锁)。

    顺序：可读性探测 → 免锁只读 → 普通只读 → 复制到临时文件再读。
    Windows 上浏览器运行时会独占锁住 Cookies（Chromium 反 cookie-stealer），
    此时 marked_locked=True，上层据此给出「请关闭浏览器」的可读提示，
    而不是当作「未登录」。
    """
    now = time.time()
    ts = _LOCKED_CACHE.get(db)
    if ts is not None and now - ts < _LOCKED_TTL:
        return None, None, True  # 10 秒内已确认打不开，别再花几秒重试
    if not _probe_readable(db):
        _LOCKED_CACHE[db] = now
        return None, None, True  # 被独占锁：sqlite 再试也是一样的结果，先撤
    # timeout 取 0.5s：这个超时只用来吸收别的进程**短暂写锁**；
    # 真被独占锁的情况上面已经拦掉了。
    uri_base = "file:" + quote(db.replace("\\", "/"), safe="/:")
    for qs in ("?mode=ro&immutable=1", "?mode=ro"):
        try:
            con = sqlite3.connect(uri_base + qs, uri=True, timeout=0.5)
            con.execute("SELECT 1")  # 触发真实打开
            _LOCKED_CACHE.pop(db, None)
            return con, None, False
        except Exception:
            pass
    try:
        import shutil
        import tempfile
        tmp = os.path.join(
            tempfile.gettempdir(), f"vdl_ck_{os.getpid()}_{abs(hash(db)) % 10**8}.db"
        )
        shutil.copy2(db, tmp)
        con = sqlite3.connect(tmp, timeout=0.5)
        _LOCKED_CACHE.pop(db, None)
        return con, tmp, False
    except Exception:
        _LOCKED_CACHE[db] = now
        return None, None, True


def _close_db(con: sqlite3.Connection | None, tmp: str | None) -> None:
    try:
        if con is not None:
            con.close()
    except Exception:
        pass
    if tmp:
        try:
            os.remove(tmp)
        except Exception:
            pass


# ── 解密（Windows DPAPI + AES-GCM）───────────────────────────────────
def _dpapi_unprotect(blob: bytes) -> bytes | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        def _mk(b: bytes):
            buf = ctypes.create_string_buffer(b, len(b))
            return _Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

        blob_in, keep = _mk(blob)
        blob_out = _Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.WinDLL("kernel32", use_last_error=True).LocalFree(blob_out.pbData)
    except Exception:
        return None


def master_key(local_state: str | None) -> bytes | None:
    """取 Chromium 的 AES key（Windows：DPAPI 解 os_crypt.encrypted_key）。

    返回 None 表示：macOS/Linux（密钥在钥匙串/PBKDF2，走 yt-dlp 路径）、
    或遇到 v20/ABE（app_bound_encrypted_key 存在但 encrypted_key 不可用）。
    """
    if not local_state or sys.platform != "win32":
        return None
    try:
        st = os.stat(local_state)
        cache_key = f"{local_state}:{st.st_mtime_ns}"
        if cache_key in _KEY_CACHE:
            return _KEY_CACHE[cache_key][1]
        import base64
        import json

        with open(local_state, "r", encoding="utf-8") as f:
            state = json.load(f)
        raw = (state.get("os_crypt") or {}).get("encrypted_key") or ""
        kv = None
        if raw:
            data = base64.b64decode(raw)
            if data[:5] == b"DPAPI":  # Chromium 在 Windows 上的固定前缀
                data = data[5:]
            kv = _dpapi_unprotect(data)
            if kv and len(kv) not in (16, 32):
                kv = None
        _KEY_CACHE[cache_key] = (st.st_mtime, kv)
        return kv
    except Exception:
        return None


def decrypt_value(enc: bytes | None, key: bytes | None, host_key: str = "") -> tuple[str | None, str]:
    """解密单条 Cookie 值。返回 (明文, 失败原因)。

    ⚠️ 关键细节：Chromium 130+ / Edge 130+ 在 v10 的**明文前面额外附了 32 字节
    SHA256(host_key)**，用来防止 Cookie 库被复制到别的域复用。如果直接当值用，
    拼出来的请求头会以乱码开头，requests/httpx 直接抛
    `InvalidHeader: Invalid leading whitespace, reserved character(s)`。
    （2026-10-24 实测：UC 浏览器解出的 BDUSS 前面就挂着 32 字节哈希。）
    所以解密后必须校验并剥掉这段前缀。
    """
    if not enc:
        return "", ""
    if enc[:3] in (b"v10", b"v11"):
        if not key:
            return None, "key_missing"
        try:
            import hashlib

            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce, payload = enc[3:15], enc[15:]
            ct, tag = payload[:-16], payload[-16:]
            plain = AESGCM(key).decrypt(nonce, ct + tag, None)
            if host_key and len(plain) >= 32:
                digest = hashlib.sha256(host_key.encode("utf-8")).digest()
                if plain[:32] == digest:
                    plain = plain[32:]
            return plain.decode("utf-8", "replace"), ""
        except Exception:
            # 老版本可能直接 DPAPI 加密（无 AES 结构）
            plain = _dpapi_unprotect(enc[3:])
            if plain is not None:
                try:
                    return plain.decode("utf-8", "replace"), ""
                except Exception:
                    pass
            return None, "decrypt_failed"
    if enc[:3] == b"v20":
        # Edge/Chrome 127+ 应用绑定加密：必须由浏览器自己的 Elevation Service 解
        return None, "abe_v20"
    try:
        return enc.decode("utf-8", "replace"), ""
    except Exception:
        return None, "decode_failed"


# ── 读取 & 统计 ──────────────────────────────────────────────────────
def _domain_match(host_key: str, domains: list[str]) -> bool:
    h = (host_key or "").lstrip(".").lower()
    for d in domains:
        d = d.lower()
        if h == d or h.endswith("." + d):
            return True
    return False


def read_cookies(db: str, kind: str, domains: list[str]) -> tuple[list[dict[str, Any]], str]:
    """读某库中命中目标域名的 Cookie。返回 (行列表, 失败原因)。"""
    con, tmp, locked = _open_db(db)
    if con is None:
        return [], "locked" if locked else "open_failed"
    try:
        if kind == "firefox":
            rows = con.execute(
                "SELECT host, name, value, path, isSecure FROM moz_cookies"
            ).fetchall()
            out = [
                {"host": r[0], "name": r[1], "value": r[2] or "", "path": r[3] or "/",
                 "secure": bool(r[4])}
                for r in rows if _domain_match(r[0], domains) and r[1]
            ]
            return out, ""
        key = master_key(_local_state_for(db))
        rows = con.execute(
            "SELECT host_key, name, encrypted_value, value, path, is_secure FROM cookies"
        ).fetchall()
        out: list[dict[str, Any]] = []
        reason = ""
        for host_key, name, enc, plain, path, secure in rows:
            if not name or not _domain_match(host_key, domains):
                continue
            if plain:  # 少数库有明文 value 列
                out.append({"host": host_key, "name": name, "value": plain,
                            "path": path or "/", "secure": bool(secure)})
                continue
            val, why = decrypt_value(enc, key, host_key)
            if val is None:
                reason = why or reason
                continue
            out.append({"host": host_key, "name": name, "value": val,
                        "path": path or "/", "secure": bool(secure)})
        return out, reason
    except Exception:
        return [], "read_failed"
    finally:
        _close_db(con, tmp)


def count_cookies(db: str, kind: str, domains: list[str]) -> int:
    """只数命中条数（用于挑 profile，不触发逐条解密）。"""
    con, tmp, _ = _open_db(db)
    if con is None:
        return -1  # 打不开（可能被锁）
    try:
        if kind == "firefox":
            rows = con.execute("SELECT host FROM moz_cookies").fetchall()
        else:
            rows = con.execute("SELECT host_key FROM cookies").fetchall()
        return sum(1 for (h,) in rows if _domain_match(h, domains))
    except Exception:
        return -1
    finally:
        _close_db(con, tmp)


def find_source(domains: list[str]) -> dict[str, Any] | None:
    """找出最可能存着目标站登录态的 (浏览器, profile)。

    排序规则（元组比较，越小越优）：
      1. 只考虑**读得出来**的来源。读不到（Windows 上浏览器开着独占锁 Cookies
         库）等于拿不到 Cookie，再「正在使用」也没意义 —— 不能因为用户开着
         Edge 就把一个能读的 QQ 浏览器来源丢掉。
      2. **正在运行的浏览器优先**：用户此刻登录的 profile 才是有效会话，
         装了没开的常是几个月前就过期的 Cookie。
      3. 同档比命中 Cookie 数量（越多越可能是完整登录会话，而非游客态）。

    「开着但读不到」的浏览器由 locked_browsers() 单独报告 —— 那边无法判断
    库里到底有没有目标站 Cookie，所以只用于提示，不能冒充「已检测到登录态」。
    """
    if not domains:
        return None
    best: dict[str, Any] | None = None
    best_key: tuple[int, int] | None = None
    for src in _BROWSER_SOURCES:
        running = is_browser_running(src)
        for profile, db in iter_profiles(src):
            cnt = count_cookies(db, src["kind"], domains)
            if cnt <= 0:
                continue  # 打不开（-1）或确实没有该站 Cookie（0）
            key = (0 if running else 1, -cnt)
            if best_key is not None and key >= best_key:
                continue
            best_key = key
            best = {
                "browser": src["key"],
                "label": src["label"],
                "kind": src["kind"],
                "profile": profile,
                "db": db,
                "running": running,
                "count": cnt,
                "status": "ok",
                "reason": "",
            }
    return best


def locked_browsers() -> list[dict[str, Any]]:
    """正在运行、但 Cookie 库打不开的浏览器。

    用于提示语：读不到 ≠ 没登录。这种情况只能让用户二选一 ——
    关掉浏览器再试，或装扩展直接推送（扩展在浏览器内，不受文件锁影响）。
    """
    out: list[dict[str, Any]] = []
    for src in _BROWSER_SOURCES:
        if not is_browser_running(src):
            continue
        for profile, db in iter_profiles(src):
            con, tmp, locked = _open_db(db)
            _close_db(con, tmp)
            if locked:
                out.append({"browser": src["key"], "label": src["label"], "profile": profile})
                break
    return out


def build_cookie_header(rows: list[dict[str, Any]]) -> str | None:
    """把 Cookie 行拼成请求头字符串（同名后者覆盖前者）。

    只保留可打印 ASCII 的值。各家 Chromium 壳（UC/360/QQ）解密结果是二进制式的
    明文，偶发残留非法字节；带上一个就会让 requests/httpx 抛
    `InvalidHeader`，整条下载链路直接失败 —— 宁可少带一条 Cookie，
    也不能让整份请求头发不出去。
    """
    merged: dict[str, str] = {}
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        val = (r.get("value") or "").strip()
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in val) or not val.isascii():
            continue
        merged[name] = val
    if not merged:
        return None
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def list_installed_browsers() -> list[dict[str, Any]]:
    """列出本机已安装浏览器及运行状态，供前端「登录态来源」面板展示。"""
    out: list[dict[str, Any]] = []
    for src in _BROWSER_SOURCES:
        profiles = iter_profiles(src)
        if not profiles:
            continue
        out.append({
            "browser": src["key"],
            "label": src["label"],
            "running": is_browser_running(src),
            "profiles": len(profiles),
        })
    return out
