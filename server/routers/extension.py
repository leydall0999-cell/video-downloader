"""浏览器扩展安装包下载 + 零点击自动更新（2026-09-28 / 2026-10-02）。

把「视频工坊 媒体嗅探」Chrome MV3 扩展打包成 zip，通过下载模块对外提供：
    GET  /api/extension/info           → 扩展元信息 + 安装步骤
    GET  /api/extension/package        → 下载 .zip 安装包（Content-Disposition: attachment）
    GET  /api/extension/update-status  → 自动更新状态（目录/磁盘版本/是否待重载）
    POST /api/extension/update-config  → 设置扩展加载目录 + 开关自动更新
    POST /api/extension/update-now     → 立即同步一次

源码优先级（保证开发态最新、冻结包/网页版也能用）：
    1) 仓库根 <repo>/extension/        —— 开发态，永远最新
    2) <this>/assets/extension_src/    —— 冻结包 / 网页版兜底（已随包分发）

**零点击自动更新**为什么这样拆（2026-10-02 用户问「扩展程序更新怎么办」）：
Chrome 不会自动更新解压版扩展——它直接以本地文件夹为源，改了文件也必须到
chrome://extensions 点一次 ↻（官方行为 + 本项目实测：覆盖成 1.0.43 后心跳仍自报
1.0.42，直到用户点 ↻）。但官方文档同时写明「解压版被 reload **视为一次 update**」，
`chrome.runtime.reload()` 同样有效。于是：
    ① App 把新版写进扩展目录（extension_sync，带 manifest.name 校验与只增不删）
    ② 心跳响应里告诉扩展「磁盘上已经是新版 X」→ 扩展自己 `chrome.runtime.reload()`
两条接上，用户就再也不用覆盖目录 / 点 ↻。

**落点 = App 自己维护的目录 `~/视频工坊浏览器扩展`**（`extension_sync.managed_dir()`），
**不是**用户原来那个下载解压出来的目录——后者通常在 `~/Downloads`，属 macOS 隐私保护
（TCC）目录，App 一碰就永久卡死（真机实测 90s 不返回，进程栈停在
`os_scandir → __opendir2 → open$NOCANCEL`）。完整根因与四条安全边界见 extension_sync
的模块 docstring。代价：用户首次要把扩展**从新目录重新加载一次**（仅此一次），
之后所有升级都零点击。

不依赖 app / 不触碰磁盘删除（只往受管目录覆盖写），可在沙盒单测中直接 import。
"""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

import extension_sync

router = APIRouter()

_HERE = Path(__file__).resolve()
_SERVER = _HERE.parent.parent                      # server/routers/extension.py -> server

_PKG_NAME = "视频工坊媒体嗅探"


def _candidate_dirs():
    """扩展源码可能出现的多处位置（开发态 / 冻结包 / PyInstaller _MEIPASS），逐个试，命中 manifest.json 即用。

    冻结后 __file__ 解析出的 _SERVER 不一定等于 Contents/Resources/server，故不能只依赖单条相对路径。
    """
    dirs = []
    server = _SERVER
    dirs.append(server.parent / "extension")                  # 开发态：<repo>/extension
    dirs.append(server / "assets" / "extension_src")          # 冻结/网页版兜底
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        mp = Path(mp)
        dirs.append(mp / "extension")                         # --add-data "$REPO/extension:extension" 目标
        dirs.append(mp / "server" / "assets" / "extension_src")
        try:  # 兜底：在 _MEIPASS 下任意位置找带 manifest.json 的 extension 目录
            for hit in mp.glob("**/extension/manifest.json"):
                dirs.append(hit.parent)
                break
        except Exception:
            pass
    try:  # 开发态若 server 在 _MEIPASS 之外，也扫一层
        for hit in server.parent.glob("extension/manifest.json"):
            dirs.append(hit.parent)
            break
    except Exception:
        pass
    seen = set()
    out = []
    for d in dirs:
        d = Path(d)
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _source_dir() -> Path:
    tried = []
    for d in _candidate_dirs():
        tried.append(str(d))
        if d.is_dir() and (d / "manifest.json").is_file():
            return d
    raise FileNotFoundError("找不到扩展源码目录（已尝试：" + " ; ".join(tried) + "）")


def _manifest() -> dict:
    return json.loads((_source_dir() / "manifest.json").read_text(encoding="utf-8"))


def _build_zip(src: Path) -> bytes:
    """把扩展目录打成 zip（排除测试与 .DS_Store），返回内存字节。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(src)
            if rel.parts[0] == "tests":
                continue
            if p.name == ".DS_Store":
                continue
            z.writestr(str(rel), p.read_bytes())
    return buf.getvalue()


@router.get("/api/extension/info")
def extension_info() -> dict:
    m = _manifest()
    return {
        "name": m.get("name", _PKG_NAME),
        "version": m.get("version", "1.0.0"),
        "manifest_version": m.get("manifest_version", 3),
        "download_url": "/api/extension/package",
        "min_browser": "Chrome / Edge / Brave（支持 Manifest V3）",
        "install_steps": [
            "下载并解压扩展安装包",
            "浏览器地址栏打开 chrome://extensions（Edge 为 edge://extensions）",
            "右上角打开「开发者模式」开关",
            "点击「加载已解压的扩展程序」，选择解压出来的文件夹",
            "点击工具栏扩展图标，即可嗅探网页媒体并一键发回桌面端下载",
        ],
    }


@router.get("/api/extension/package")
def extension_package() -> Response:
    src = _source_dir()
    data = _build_zip(src)
    m = _manifest()
    name = m.get("name", _PKG_NAME)
    version = m.get("version", "1.0.0")
    filename = f"{name}-{version}.zip"
    ascii_name = f"vdl-sniffer-extension-{version}.zip"
    disp = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": disp},
    )


# ---------------------------------------------------------------------------
# 扩展零点击自动更新（2026-10-02）
#
# 分工：本文件负责「判断该不该更新 + 提供接口」，真正的落盘在 extension_sync；
# 触发时机是**扩展自己的心跳**（/api/sniffer/ext-ping → maybe_sync），所以用户
# 什么都不用做，浏览器一连上就会把磁盘上的扩展刷成新版。
# ---------------------------------------------------------------------------

def _expected_name() -> str:
    """我们这个扩展的 manifest.name —— 也是「目标目录是不是本扩展」的判据。"""
    try:
        return str(_manifest().get("name") or _PKG_NAME)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _PKG_NAME


def _installed_version() -> str:
    """浏览器里**正在跑**的扩展版本（心跳自报）。拿不到 → 空串（不猜）。"""
    try:
        import cdp_sniffer  # 延迟 import：本模块要能在沙盒单测里独立 import
        return str(cdp_sniffer.SNIFFER.status().get("ext_version") or "")
    except Exception:  # noqa: BLE001 - 状态只是参考，取不到就按「未知」处理
        return ""


# 心跳是每分钟一次（SW 唤醒还会额外触发）→ 同一版本 20 秒内只算一次，
# 免得每次心跳都去 stat/读 manifest。
_sync_throttle: dict = {"key": "", "ts": 0.0, "result": None}


def _effective_load_dir(cfg: dict | None = None) -> str:
    """本次要写哪个目录：显式配置优先，否则用**受管目录**（`~/视频工坊浏览器扩展`）。

    受管目录是默认落点——App 自己维护、不在 TCC 保护范围，所以心跳触发的自动同步
    不会弹系统授权框、也不会阻塞。
    """
    cfg = cfg if isinstance(cfg, dict) else extension_sync.get_config()
    explicit = str(cfg.get("load_dir") or "").strip()
    return explicit or str(extension_sync.managed_dir())


def maybe_sync(installed_version: str) -> dict:
    """心跳触发的自动同步。幂等、节流、**绝不抛**（心跳不能因此 500）。

    返回 {"auto": bool, "reload_to": str, "synced": bool, "error": str}。
    reload_to 仅当「磁盘上确实是新版」且「新版严格新于扩展自报版本」时非空
    —— 扩展据此调 chrome.runtime.reload()；其余情况为空串（扩展什么都不做）。
    """
    try:
        cfg = extension_sync.get_config()
    except Exception as exc:  # noqa: BLE001
        return {"auto": False, "reload_to": "", "synced": False, "error": str(exc)}
    if not cfg.get("auto"):
        return {"auto": False, "reload_to": "", "synced": False, "error": ""}

    now = time.time()
    if (installed_version and _sync_throttle["key"] == installed_version
            and now - float(_sync_throttle["ts"]) < 20):
        cached = _sync_throttle["result"]
        return dict(cached) if isinstance(cached, dict) else {"auto": True, "reload_to": "", "synced": False, "error": ""}

    try:
        src_ver = str(_manifest().get("version") or "")
        load_dir = _effective_load_dir(cfg)
        disk_ver = extension_sync.read_version(load_dir)
        synced = False
        error = ""
        if src_ver and disk_ver != src_ver:
            res = extension_sync.sync_to(load_dir, _source_dir(), _expected_name(),
                                         allow_empty=(load_dir == str(extension_sync.managed_dir())))
            synced = bool(res.get("ok"))
            error = str(res.get("error") or "")
            disk_ver = str(res.get("version") or extension_sync.read_version(load_dir))
        # 只有磁盘上**确实**是 src_ver（同步成功或早就同步过）才敢让扩展重载，
        # 否则会把用户重载到一个更旧的版本上。
        reload_to = ""
        if src_ver and disk_ver == src_ver and extension_sync.is_newer(src_ver, installed_version):
            reload_to = src_ver
        out = {"auto": True, "reload_to": reload_to, "synced": synced, "error": error}
    except Exception as exc:  # noqa: BLE001
        out = {"auto": True, "reload_to": "", "synced": False, "error": str(exc)}

    _sync_throttle.update(key=installed_version, ts=now, result=out)
    return dict(out)


def _resolve_load_dir(load_dir) -> str:
    """把入参解析成「确定的扩展目录」；无法确定/不允许时抛 400（由 UI 引导用户）。

    "auto" → 受管目录 `~/视频工坊浏览器扩展`（必要时就地初始化）
    其他   → 必须通过 manifest.name 校验，且不得落在 macOS 隐私保护目录内
    """
    if not isinstance(load_dir, str) or not load_dir.strip():
        raise HTTPException(status_code=400, detail="缺少扩展目录")
    spec = load_dir.strip()
    if spec == "auto":
        res = extension_sync.ensure_managed(_source_dir(), _expected_name())
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=str(res.get("error") or "无法准备扩展目录"))
        return str(res.get("dir") or extension_sync.managed_dir())
    p = Path(spec).expanduser()
    if extension_sync.is_tcc_protected(p):
        # 写这类目录会让 App 卡在内核里（见 extension_sync 模块 docstring），事前拒绝
        raise HTTPException(
            status_code=400,
            detail="不能选「桌面 / 文稿 / 下载」里的目录：macOS 会拦住 App 的访问并弹授权框，"
                   f"写到那里会让程序卡住。请改用 App 维护的目录：{extension_sync.managed_dir()}",
        )
    if not extension_sync.is_our_extension(p, _expected_name()):
        raise HTTPException(
            status_code=400,
            detail=f"该目录里没有「{_expected_name()}」的 manifest.json，已拒绝（防止写坏别的扩展）",
        )
    return str(p)


def _status_payload() -> dict:
    cfg = extension_sync.get_config()
    installed = _installed_version()
    managed = str(extension_sync.managed_dir())
    out = {
        "auto": bool(cfg.get("auto")),
        "load_dir": _effective_load_dir(cfg),
        "explicit_dir": str(cfg.get("load_dir") or ""),
        "managed_dir": managed,
        "using_managed": not str(cfg.get("load_dir") or "").strip(),
        "installed_version": installed,
        "source_version": "",
        "on_disk_version": "",
        "needs_setup": False,
        "pending": False,
        "error": "",
    }
    try:
        out["source_version"] = str(_manifest().get("version") or "")
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        out["error"] = f"内置扩展源不可用：{exc}"
        return out

    # 受管目录：顺手补齐（幂等且廉价——版本一致时只是一次只读比对）。
    # 这是唯一「GET 也写」的地方，刻意为之：用户打开面板就能看到目录已就绪、可直接复制路径。
    ld = out["load_dir"]
    if out["using_managed"]:
        res = extension_sync.ensure_managed(_source_dir(), _expected_name())
        if not res.get("ok") and not out["error"]:
            out["error"] = str(res.get("error") or "")
    out["on_disk_version"] = extension_sync.read_version(ld)
    # 「还没从受管目录装过」→ 面板给出一次性的加载引导
    out["needs_setup"] = not (
        installed and out["source_version"] and installed == out["source_version"]
    )
    out["pending"] = bool(
        out["on_disk_version"] and installed
        and extension_sync.is_newer(out["on_disk_version"], installed)
    )
    return out


@router.get("/api/extension/update-status")
def extension_update_status() -> dict:
    """自动更新状态：开关、目录、源码版本、磁盘版本、是否待扩展重载。"""
    return _status_payload()


@router.post("/api/extension/update-config")
def extension_update_config(payload: dict = Body(default={})) -> dict:
    """设置/关闭扩展自动更新。

    body:
        load_dir: "auto"（用 App 维护的扩展目录）| 具体路径（须通过 manifest.name 校验）| ""（关闭并清空）
        auto:     bool（可选）是否开启自动更新
    开启后会**立即同步一次**，并返回同步结果 —— 用户点一下就能看到「已写入 v X」。
    """
    data = payload if isinstance(payload, dict) else {}
    raw_dir = data.get("load_dir")
    if raw_dir is not None and not isinstance(raw_dir, str):
        raise HTTPException(status_code=400, detail="load_dir 必须是字符串")

    if isinstance(raw_dir, str) and not raw_dir.strip():
        # 空串＝关闭并清空目录（显式动作，避免误清）
        extension_sync.save_config(load_dir="", auto=False)
        return {"ok": True, "closed": True, "status": _status_payload()}

    if isinstance(raw_dir, str):
        # "auto" 归一化成空串存起来（= 用受管目录），路径本身不落库，避免日后受管目录改名后失效
        resolved = _resolve_load_dir(raw_dir)
        extension_sync.save_config(
            load_dir="" if resolved == str(extension_sync.managed_dir()) else resolved
        )

    want_auto = data.get("auto")
    if want_auto is not None:
        extension_sync.save_config(auto=bool(want_auto))

    cfg = extension_sync.get_config()
    sync = None
    if cfg.get("auto"):
        _sync_throttle.update(key="", ts=0.0, result=None)   # 手动操作不吃节流
        sync = maybe_sync(_installed_version())
    return {"ok": True, "sync": sync, "status": _status_payload()}


@router.post("/api/extension/update-now")
def extension_update_now() -> dict:
    """立即把内置新版同步进扩展目录（不改开关；未配置则用受管目录）。"""
    cfg = extension_sync.get_config()
    load_dir = _effective_load_dir(cfg)
    try:
        if load_dir == str(extension_sync.managed_dir()):
            res = extension_sync.ensure_managed(_source_dir(), _expected_name())
        else:
            res = extension_sync.sync_to(load_dir, _source_dir(), _expected_name())
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _sync_throttle.update(key="", ts=0.0, result=None)
    return {"ok": bool(res.get("ok")), "result": res, "status": _status_payload()}
