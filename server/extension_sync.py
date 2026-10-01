"""扩展「零点击自动更新」：记住用户的扩展加载目录，并把内置新版写进去（2026-10-02）。

为什么需要这个模块
------------------
解压版（Load unpacked）扩展 Chrome **不会自动更新**：它以本地文件夹为源，改了文件
也必须到 chrome://extensions 点一次 ↻。本项目实测一致——把加载目录覆盖成 1.0.43 后，
扩展心跳仍自报 1.0.42，直到用户点 ↻ 才变。

但官方文档同时写明：解压版被 reload **视为一次 update**，`chrome.runtime.reload()`
同样有效。于是把「更新」拆成两半就有了零点击：

    ① App（本模块）把新版文件**写进**用户的扩展加载目录；
    ② 扩展收到「磁盘上已有新版」的信号后**自己重载自己**（见 extension/background.js）。

本模块负责 ①，并守住三条安全边界（改动前请先读这三条）：

  - **目标目录必须是我们这个扩展**：读它的 manifest.json 校验 name，不符一律拒写
    —— 用户可能选错目录，写坏别人的扩展不可逆。
  - **只覆盖/新增，绝不删除**目标目录里的任何文件：那是用户主目录下的普通文件夹，
    误删不可逆；源里删掉的文件需要用户自己清理。
  - **路径只来自「用户显式选择」或 detect_load_dir() 的识别结果**，不猜别的路径。

配置存 <数据目录>/extension_sync.json（数据目录见 auth_store._base_dir()，支持
VDL_DATA_DIR 隔离），形如 {"load_dir": "...", "auto": true}。
"""
from __future__ import annotations

import json
from pathlib import Path

import atomic_io

# 与 routers/extension.py::_build_zip 保持一致：这两类不进扩展包
_EXCLUDE_TOP_DIRS = {"tests"}
_EXCLUDE_NAMES = {".DS_Store"}

# detect_load_dir 的扫描范围（用户最可能解压到的位置）与规模上限
_SCAN_ROOTS = ("Downloads", "Desktop", "Documents")
_SCAN_MAX_DIRS = 800
_SCAN_MAX_DEPTH = 2

# 绝不写入的路径特征：写自己的 App 包会让签名失效
_FORBIDDEN_SUBSTR = ("/Applications/", ".app/")


# ---------- 配置 ----------

def _config_path() -> Path:
    # 延迟 import：auth_store 只在这里用到，避免模块级耦合
    import auth_store
    return auth_store._base_dir() / "extension_sync.json"


def get_config() -> dict:
    """读取自动更新配置。缺省 = 未开启、无目录（**安全默认**：不碰用户任何文件夹）。"""
    cfg = {"load_dir": "", "auto": False}
    cp = _config_path()
    if cp.is_file():
        try:
            saved = json.loads(cp.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                if isinstance(saved.get("load_dir"), str):
                    cfg["load_dir"] = saved["load_dir"].strip()
                cfg["auto"] = bool(saved.get("auto"))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def save_config(load_dir: str | None = None, auto: bool | None = None) -> dict:
    """写入配置（未传的字段保持原值）。返回写入后的完整配置。"""
    cfg = get_config()
    if load_dir is not None:
        cfg["load_dir"] = str(load_dir).strip()
    if auto is not None:
        cfg["auto"] = bool(auto)
    atomic_io.atomic_write_json(_config_path(), cfg)
    return cfg


# ---------- 版本比较 ----------

def _ver_tuple(v) -> tuple | None:
    """'1.0.44' → (1,0,44)；非法/含非数字段 → None（调用方一律当「不比」处理）。"""
    s = str(v or "").strip()
    if not s:
        return None
    out = []
    for part in s.split("."):
        if not part.isdigit():
            return None
        out.append(int(part))
    return tuple(out)


def is_newer(a, b) -> bool:
    """a 是否严格新于 b。任一侧非法一律 False（宁可不动，也不要把旧版写回去）。"""
    ta, tb = _ver_tuple(a), _ver_tuple(b)
    if ta is None or tb is None:
        return False
    return ta > tb


# ---------- 目录识别 ----------

def read_version(path) -> str:
    """读某目录 manifest.json 的 version；不存在/解析失败返回空串。"""
    try:
        m = json.loads((Path(path) / "manifest.json").read_text(encoding="utf-8"))
        return str(m.get("version") or "")
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def read_name(path) -> str:
    try:
        m = json.loads((Path(path) / "manifest.json").read_text(encoding="utf-8"))
        return str(m.get("name") or "")
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def is_our_extension(path, expected_name: str) -> bool:
    """该目录是不是我们这个扩展（按 manifest.name 判定）。expected_name 为空则不认。"""
    if not expected_name:
        return False
    p = Path(path)
    if not p.is_dir() or not (p / "manifest.json").is_file():
        return False
    return read_name(p) == expected_name


def _iter_candidate_dirs():
    """扫描常见位置（深度 ≤ _SCAN_MAX_DEPTH），产出「像扩展目录」的路径。

    只做只读探测：不读文件内容以外的东西，也不修改任何目标。
    """
    home = Path.home()
    seen = 0
    for root_name in _SCAN_ROOTS:
        root = home / root_name
        if not root.is_dir():
            continue
        stack = [(root, 0)]
        while stack:
            d, depth = stack.pop()
            try:
                children = sorted(d.iterdir())
            except OSError:
                continue
            for child in children:
                if not child.is_dir():
                    continue
                seen += 1
                if seen > _SCAN_MAX_DIRS:
                    return
                if (child / "manifest.json").is_file():
                    yield child
                    continue  # 命中即不再往里钻（扩展目录不会嵌套扩展目录）
                if depth + 1 < _SCAN_MAX_DEPTH and not child.name.startswith("."):
                    stack.append((child, depth + 1))


def detect_load_dir(installed_version: str, expected_name: str) -> dict:
    """自动识别扩展加载目录（零配置路径）。

    判据：目录里 manifest.json 的 name == 我们的扩展名（**强判据**，不会认错人）。
    优选顺序：
      1) 版本与「浏览器里正在跑的扩展版本」一致的（几乎只可能是真正被加载的那个）
      2) 唯一的候选
    多个同版本候选时取最近修改的（每次升级都会重写该目录）。
    仍无法唯一确定 → 返回空 dir + ambiguous，由 UI 让用户手选（不瞎猜）。
    """
    hits = []
    for d in _iter_candidate_dirs():
        if is_our_extension(d, expected_name):
            try:
                mtime = d.stat().st_mtime
            except OSError:
                mtime = 0.0
            hits.append({"dir": str(d), "version": read_version(d), "mtime": mtime})

    if not hits:
        return {"dir": "", "candidates": [], "reason": "not_found"}

    same = [h for h in hits if installed_version and h["version"] == installed_version]
    if same:
        same.sort(key=lambda h: h["mtime"], reverse=True)
        reason = "version_match" if len(same) == 1 else "version_match_multi"
        return {"dir": same[0]["dir"], "candidates": hits, "reason": reason}

    # 没有版本吻合的：多个候选不猜（用户后来可能又解压了别的副本）
    if len(hits) == 1:
        return {"dir": hits[0]["dir"], "candidates": hits, "reason": "single"}
    return {"dir": "", "candidates": hits, "reason": "ambiguous"}


# ---------- 同步 ----------

def sync_to(load_dir, src_dir, expected_name: str) -> dict:
    """把内置扩展源覆盖写进 load_dir。返回 {ok, version, written, unchanged, error}。

    只增不删；目标目录必须已是我们这个扩展（防止写错目录）。任何异常都返回
    ok=False + error，不往外抛 —— 调用方是扩展心跳，绝不能因此让心跳 500。
    """
    try:
        target = Path(load_dir).expanduser().resolve()
        src = Path(src_dir).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return {"ok": False, "error": f"路径解析失败：{exc}", "version": "", "written": 0, "unchanged": 0}

    if not src.is_dir() or not (src / "manifest.json").is_file():
        return {"ok": False, "error": "内置扩展源不可用（找不到 manifest.json）",
                "version": "", "written": 0, "unchanged": 0}
    if not target.is_dir():
        return {"ok": False, "error": f"扩展目录不存在：{target}", "version": "", "written": 0, "unchanged": 0}
    if any(s in str(target) for s in _FORBIDDEN_SUBSTR):
        return {"ok": False, "error": "拒绝写入应用包内路径（会破坏签名）",
                "version": "", "written": 0, "unchanged": 0}
    if not is_our_extension(target, expected_name):
        got = read_name(target) or "（无 manifest.json）"
        return {"ok": False, "error": f"目标目录不是本扩展（name={got}），已拒绝写入",
                "version": "", "written": 0, "unchanged": 0}

    written = unchanged = 0
    try:
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(src)
            if rel.parts[0] in _EXCLUDE_TOP_DIRS or p.name in _EXCLUDE_NAMES:
                continue
            dst = target / rel
            data = p.read_bytes()
            if dst.is_file():
                try:
                    if dst.read_bytes() == data:
                        unchanged += 1
                        continue
                except OSError:
                    pass
            dst.parent.mkdir(parents=True, exist_ok=True)
            atomic_io.atomic_write_bytes(dst, data)
            written += 1
    except OSError as exc:
        return {"ok": False, "error": f"写入失败：{exc}", "version": read_version(target),
                "written": written, "unchanged": unchanged}

    return {"ok": True, "error": "", "version": read_version(target),
            "written": written, "unchanged": unchanged}
