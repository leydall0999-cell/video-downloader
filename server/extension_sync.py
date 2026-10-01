"""扩展「零点击自动更新」：把内置新版写进一个**由 App 自己维护**的扩展目录（2026-10-02）。

为什么需要这个模块
------------------
解压版（Load unpacked）扩展 Chrome **不会自动更新**：它以本地文件夹为源，改了文件
也必须到 chrome://extensions 点一次 ↻。本项目实测一致——把加载目录覆盖成 1.0.43 后，
扩展心跳仍自报 1.0.42，直到用户点 ↻ 才变。

但官方文档同时写明：解压版被 reload **视为一次 update**，`chrome.runtime.reload()`
同样有效。于是把「更新」拆成两半就有了零点击：

    ① App（本模块）把新版文件**写进**扩展目录；
    ② 扩展收到「磁盘上已有新版」的信号后**自己重载自己**（见 extension/background.js）。

**落点为什么必须是「App 自己维护的目录」（2026-10-02 真机实测，重要）**
------------------------------------------------------------------------
第一版把落点定在「用户原来那个加载目录」，而它通常在 `~/Downloads`（用户下载 zip
后解压的位置）——于是 App 一碰就**永久卡死**：`/api/extension/update-status` 90 秒不
返回，进程栈全是 `os_scandir → __opendir2 → open$NOCANCEL` 一动不动；把同一个路径换成
`/tmp` 则 13 毫秒返回。

根因是 macOS 的隐私保护（TCC）：`~/Desktop` / `~/Documents` / `~/Downloads` 属受保护
目录，App 首次访问时系统弹「访问下载文件夹」授权框；本项目是 **ad-hoc 签名**，每次重建
cdhash 都变 → 每次都要重新弹框，而弹框出现在后台线程里没人点 → `open()` 在内核里
**无限期阻塞**。这不是新问题：同一根因本项目已被咬过 3 次（见 app.py::_tcc_safe_config_path
的注释，当时是把配置文件搬出 Downloads 才修好）。

所以本模块的落点是 `~/视频工坊浏览器扩展`（`managed_dir()`）：
  - **不在 TCC 保护范围**（主目录根，不是 Desktop/Documents/Downloads）→ App 可自由
    读写，不需要任何系统授权、绝不阻塞；
  - 目录名里**不带版本号** → 路径恒定 → Chrome 按路径派生的扩展 ID 稳定（换目录＝换
    ID＝用户设置与网站授权全丢，所以绝不能把版本号放进目录名，也绝不能改名/搬家）；
  - 在 Finder 主目录里可见，用户「加载已解压的扩展程序」时好找。

本模块守住四条安全边界（改动前请先读这四条）：

  - **目标目录必须是我们这个扩展**：读它的 manifest.json 校验 name，不符一律拒写
    —— 用户可能选错目录，写坏别人的扩展不可逆。（受管目录首次为空时允许初始化。）
  - **拒绝写 macOS 隐私保护目录**（桌面/文稿/下载）：写了就会把 App 卡死（见上）。
  - **只覆盖/新增，绝不删除**目标目录里的任何文件：那是用户主目录下的普通文件夹，
    误删不可逆；源里删掉的文件需要用户自己清理。
  - **路径只来自「受管目录」或「用户显式选择」**，不猜别的路径（早期版本会扫
    Downloads/Desktop/Documents 找候选，那正是卡死的来源，已彻底移除）。

配置存 <数据目录>/extension_sync.json（数据目录见 auth_store._base_dir()，支持
VDL_DATA_DIR 隔离），形如 {"load_dir": "", "auto": true}：
  - load_dir 空串 = 用受管目录（推荐，默认）
  - load_dir 具体路径 = 用户显式指定的目录（须通过 name 校验、且不在 TCC 保护目录内）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import atomic_io

# 与 routers/extension.py::_build_zip 保持一致：这两类不进扩展包
_EXCLUDE_TOP_DIRS = {"tests"}
_EXCLUDE_NAMES = {".DS_Store"}

# App 自己维护的扩展目录名（**不带版本号**：路径必须恒定，Chrome 的扩展 ID 由路径派生）
MANAGED_DIR_NAME = "视频工坊浏览器扩展"

# macOS 隐私保护（TCC）目录：访问会触发授权弹框；本项目 ad-hoc 签名每次重建都让授权
# 失效，未应答的弹框会让 open() 在内核里无限期阻塞 → 一律不碰（详见模块 docstring）。
_TCC_PROTECTED = ("Desktop", "Documents", "Downloads")

# 绝不写入的路径特征：写自己的 App 包会让签名失效
_FORBIDDEN_SUBSTR = ("/Applications/", ".app/")


# ---------- 配置 ----------

def _config_path() -> Path:
    # 延迟 import：auth_store 只在这里用到，避免模块级耦合
    import auth_store
    return auth_store._base_dir() / "extension_sync.json"


def get_config() -> dict:
    """读取自动更新配置。缺省 = 未开启、用受管目录（**安全默认**：只动自己的目录）。"""
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


# ---------- 受管目录 ----------

def managed_dir() -> Path:
    """App 自己维护的扩展目录：`~/视频工坊浏览器扩展`。

    选在主目录根（不在 Desktop/Documents/Downloads 之内）＝不在 TCC 保护范围，
    App 可自由读写、不需要任何系统授权、不会阻塞。路径**恒定**（不带版本号），
    所以 Chrome 派生的扩展 ID 在后续所有升级中都保持不变。

    可用 VDL_EXTENSION_DIR 覆盖：给单测做隔离（否则测试会往用户真实主目录里建目录），
    也给需要把它放到别的盘的用户一条路。
    """
    override = (os.environ.get("VDL_EXTENSION_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / MANAGED_DIR_NAME


def is_tcc_protected(path) -> bool:
    """路径是否落在 macOS 隐私保护目录内（桌面/文稿/下载）。

    非 macOS 一律 False —— 这套限制是 macOS 特有的。用于**事前拒绝**写入，
    避免把 App 卡在内核里（症状见模块 docstring）。
    """
    if sys.platform != "darwin":
        return False
    try:
        p = Path(path).expanduser().resolve()
        home = Path.home()
    except (OSError, RuntimeError):
        return False
    for name in _TCC_PROTECTED:
        try:
            p.relative_to(home / name)
            return True
        except ValueError:
            continue
    return False


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


# ---------- 同步 ----------

def sync_to(load_dir, src_dir, expected_name: str, allow_empty: bool = False) -> dict:
    """把内置扩展源覆盖写进 load_dir。返回 {ok, version, written, unchanged, error}。

    只增不删；目标目录必须已是我们这个扩展（防止写错目录）——唯一例外是
    `allow_empty=True` 且目标目录**里没有 manifest.json**（受管目录的首次初始化）。
    任何异常都返回 ok=False + error，不往外抛 —— 调用方是扩展心跳，绝不能因此 500。
    """
    try:
        target = Path(load_dir).expanduser().resolve()
        src = Path(src_dir).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return {"ok": False, "error": f"路径解析失败：{exc}", "version": "", "written": 0, "unchanged": 0}

    if any(s in str(target) for s in _FORBIDDEN_SUBSTR):
        return {"ok": False, "error": "拒绝写入应用包内路径（会破坏签名）",
                "version": "", "written": 0, "unchanged": 0}
    if is_tcc_protected(target):
        # 写了就会触发系统授权弹框；ad-hoc 签名下弹框无人应答 → open() 永久阻塞 → App 假死。
        # 放在「目录是否存在」之前：危险路径要**先**拒掉，与它当前在不在无关。
        return {"ok": False,
                "error": "拒绝写入 macOS 隐私保护目录（桌面/文稿/下载）：App 访问会被系统阻塞。"
                         f"请改用 App 维护的扩展目录：{managed_dir()}",
                "version": "", "written": 0, "unchanged": 0}
    if not src.is_dir() or not (src / "manifest.json").is_file():
        return {"ok": False, "error": "内置扩展源不可用（找不到 manifest.json）",
                "version": "", "written": 0, "unchanged": 0}
    if not target.is_dir():
        return {"ok": False, "error": f"扩展目录不存在：{target}", "version": "", "written": 0, "unchanged": 0}
    if not is_our_extension(target, expected_name):
        got = read_name(target)
        if got or not allow_empty:
            got = got or "（无 manifest.json）"
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


def ensure_managed(src_dir, expected_name: str) -> dict:
    """确保受管目录存在且是本扩展，然后把源写进去。

    幂等：版本/内容一致时只做只读比对（written=0）。返回 sync_to 的结果 + dir。
    这是「零点击」的落点，也是唯一被自动（心跳）触发的写路径。
    """
    d = managed_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"无法创建扩展目录：{exc}", "dir": str(d),
                "version": "", "written": 0, "unchanged": 0}
    res = sync_to(d, src_dir, expected_name, allow_empty=True)
    res["dir"] = str(d)
    return res
