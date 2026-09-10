"""server/routers/system.py — 关于 / 版本 / 自动更新（/api/system/*）。

更新源：阿里云 VPS 静态目录（latest.json + VideoDownloader.app.zip），
由 build_mac.sh 在 VDL_PUBLISH_UPDATE=1 时 scp 上传。

- GET  /api/system/info   当前运行实例的版本（冻结于导入时）+ 平台 + 是否可更新
- GET  /api/system/latest 代理 VPS latest.json，并比出版本判断是否有更新
- POST /api/system/update 下载目标版本 zip → 校验 sha256 → 派生「脱离父进程」的
                          更新助手 → 返回 ok，由前端调用 quit_app() 退出；
                          助手等待主进程退出后 ditto 覆盖 /Applications 并重新 open。

macOS 自更新难点：运行中的 .app 不能覆盖自身。解法是用 start_new_session
派生的独立助手进程，主程序退出后由助手完成替换与重启。
"""
from __future__ import annotations

import os
import sys
import time
import uuid
import json
import hashlib
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

import urllib.request
from fastapi import APIRouter, Body, Request

router = APIRouter()

# ---- 配置（可用环境变量覆盖，便于测试/换源） ----
# VPS 静态目录 /opt/vdl-update 由 http.server 暴露在 8765，文件直挂根路径。
UPDATE_BASE_URL = os.environ.get("VDL_UPDATE_BASE_URL", "http://8.138.223.3:8765").rstrip("/")
UPDATE_GUARD_SECONDS = int(os.environ.get("VDL_UPDATE_GUARD_SECONDS", "120"))
_DOWNLOAD_TIMEOUT = int(os.environ.get("VDL_UPDATE_DL_TIMEOUT", "1800"))

_LAUNCH_LOG = Path.home() / ".vdl_launch.log"

# 更新任务状态机（异步）：前端通过 pywebview 原生 bridge 触发，后端在后台线程执行
# 下载/套用/派生助手，前端轮询 /api/system/update/status 获取进度。
# 这样「触发更新」完全不经过 WKWebView 的 fetch，从原理上规避其偶发 "Load failed" 拦截。
_UPDATE_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 版本读取（冻结于导入时，避免部署后旧进程报假版本）
# --------------------------------------------------------------------------- #
def _candidates_version_txt() -> list[Path]:
    cands: list[Path] = []
    exe = getattr(sys, "executable", "")
    if exe:
        cands.append(Path(exe).resolve().parent.parent / "Resources" / "version.txt")
    cands.append(Path(__file__).resolve().parent.parent / "version.txt")
    return cands


def _candidates_build_txt() -> list[Path]:
    cands: list[Path] = []
    exe = getattr(sys, "executable", "")
    if exe:
        cands.append(Path(exe).resolve().parent.parent / "Resources" / "build_version.txt")
    cands.append(Path(__file__).resolve().parent.parent / "build_version.txt")
    return cands


def _read_first(cands: list[Path]) -> str:
    for c in cands:
        try:
            if c.exists():
                return c.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


VERSION = _read_first(_candidates_version_txt()) or "0.0.0"
BUILD = _read_first(_candidates_build_txt()) or "dev"
IS_BUNDLED = getattr(sys, "frozen", False) and ".app" in str(getattr(sys, "executable", ""))


def _bundle_path() -> Optional[Path]:
    """当前运行中的 .app 路径（仅打包态有效）。"""
    if not IS_BUNDLED:
        return None
    exe = Path(sys.executable).resolve()
    # .../VideoDownloader.app/Contents/MacOS/VideoDownloader → up 3
    if "Contents/MacOS" in str(exe):
        return exe.parents[2]
    return None


def _parse_ver(v: str) -> tuple[int, ...]:
    nums = []
    for part in v.split("."):
        digs = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digs) if digs else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


# --------------------------------------------------------------------------- #
# 辅助：HTTP 拉取 / 下载
# --------------------------------------------------------------------------- #
def _http_get_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "VDL-Update/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_download(url: str, dest: Path, timeout: int = _DOWNLOAD_TIMEOUT) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "VDL-Update/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length", "0") or "0")
        done = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    # 进度留作扩展（前端目前靠「下载中…」占位）
                    _ = int(done * 100 / total)


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# 增量更新：文件级 delta（服务端生成「变更文件包」，客户端复制已装 app 到
# staging 后套用 delta；仅变更文件下传，故下载是几 MB 级而非 312MB 全量）
# --------------------------------------------------------------------------- #
def _apply_delta(staging_app: Path, pkg_zip: Path) -> bool:
    """把文件级增量包套用到 staging_app（已含旧版完整副本）上，得到新版。"""
    import shutil
    import zipfile

    tmp = staging_app.parent / "_deltatmp"
    try:
        with zipfile.ZipFile(str(pkg_zip)) as z:
            manifest = json.loads(z.read("manifest.json").decode("utf-8"))
            # 记号：zip 条目里记录了 Unix 权限（external_attr >> 16），用于 new 文件复原可执行位
            zip_modes = {}
            for info in z.infolist():
                if info.filename == "manifest.json":
                    continue
                m = (info.external_attr >> 16) & 0o777
                if m:
                    zip_modes[info.filename] = m
            for n in z.namelist():
                if n == "manifest.json":
                    continue
                z.extract(n, str(tmp))  # 解出 <idx> 字节块到临时目录
        for entry in manifest:
            op = entry.get("op")
            rel = entry.get("path")
            dst = staging_app / rel
            if op == "del":
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                continue
            if op == "link":
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(entry.get("target", ""), str(dst))
                continue
            idx = str(entry.get("idx"))
            blob = tmp / idx
            dst.parent.mkdir(parents=True, exist_ok=True)
            if op == "bsdiff":
                oldf = dst  # staging 里已是旧文件副本
                newf = dst.with_name(dst.name + ".new")
                subprocess.run(
                    ["/usr/bin/bspatch", str(oldf), str(newf), str(blob)],
                    check=True,
                )
                # ⚠️ 关键：bspatch 生成的临时文件是 0644，若直接 replace 会让可执行文件
                # （如 Contents/MacOS/VideoDownloader）丢失 +x，导致更新后应用无法启动。
                # 必须沿用被替换文件的原始权限。
                try:
                    os.chmod(str(newf), os.stat(str(oldf)).st_mode)
                except OSError:
                    pass
                newf.replace(dst)
            else:  # new
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                blob.replace(dst)
                # 新增文件同样需要复原权限（清单里的 mode 优先，其次 zip 条目自带权限）
                try:
                    mode = int(entry.get("mode") or 0) or zip_modes.get(idx, 0)
                    if mode:
                        os.chmod(str(dst), mode)
                except (OSError, ValueError, TypeError):
                    pass
        # 兜底保险：套用后确认主可执行文件带执行位。历史上 bsdiff 替换会让 0755 变 0644，
        # 导致更新后 macOS 直接拒绝启动（Launch failed）。此处再校验一次并自愈。
        try:
            import plistlib
            exe = "VideoDownloader"
            pl = staging_app / "Contents" / "Info.plist"
            if pl.exists():
                with open(str(pl), "rb") as f:
                    exe = plistlib.load(f).get("CFBundleExecutable") or exe
            main_bin = staging_app / "Contents" / "MacOS" / exe
            if main_bin.exists() and not (os.stat(str(main_bin)).st_mode & 0o111):
                os.chmod(str(main_bin), 0o755)
        except Exception:
            pass
        return True
    except Exception:
        return False
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def _verify_app(app: Path, target_ver: str) -> bool:
    """校验还原出的 app：ad-hoc 签名完整性 + 版本号匹配。"""
    r = subprocess.run(
        ["codesign", "--verify", "--verbose=2", str(app)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return False
    vt = app / "Contents" / "Resources" / "version.txt"
    try:
        return vt.read_text(encoding="utf-8").strip() == target_ver
    except Exception:
        return False


def _prepare_update(data: dict, work: Path, bundle: Path, target_ver: str) -> Optional[Path]:
    """准备好新 .app 的 staging 路径。优先增量补丁，失败自动回退全量。"""
    staging = work / "VideoDownloader.app"

    # —— 增量分支：已装版本 == from_version 且补丁可用 ——
    from_ver = str(data.get("from_version") or "")
    patch_url = data.get("patch_url") or ""
    patch_sha = (data.get("patch_sha256") or "").lower().strip()
    if from_ver and patch_url and VERSION == from_ver:
        try:
            patch_path = work / "patch.delta"
            _http_download(patch_url, patch_path, timeout=_DOWNLOAD_TIMEOUT)
            if patch_sha and _sha256_of(patch_path).lower() != patch_sha:
                raise RuntimeError("补丁 sha256 不匹配")
            # 复制已装 app 到 staging（本地 I/O，避免触碰运行中的 /Applications）
            subprocess.run(["ditto", str(bundle), str(staging)], check=True)
            if not _apply_delta(staging, patch_path):
                raise RuntimeError("delta 套用失败")
            # 套用后文件已变更，原 ad-hoc 签名失效，重新 ad-hoc 签名（macOS 自带 codesign）
            subprocess.run(
                ["codesign", "--force", "--deep", "--sign", "-", str(staging)],
                check=True,
                capture_output=True,
            )
            if _verify_app(staging, target_ver):
                return staging
        except Exception:
            pass  # 增量失败 -> 回退全量

    # —— 全量分支（兜底）——
    url = data.get("url") or ""
    expect_sha = (data.get("sha256") or "").lower().strip()
    if not url:
        return None
    zip_path = work / "VideoDownloader.app.zip"
    try:
        _http_download(url, zip_path, timeout=_DOWNLOAD_TIMEOUT)
        if expect_sha and _sha256_of(zip_path).lower() != expect_sha:
            return None
        # 解压到独立临时目录：发布包由 `ditto -c -k <app>` 生成，zip 根部直接是
        # bundle 内容（Contents/…）而非外层 VideoDownloader.app/ 目录，故先解开再
        # 把内容整体搬进 staging/VideoDownloader.app，兼容两种打包形态。
        extract = work / "_extract"
        shutil.rmtree(str(extract), ignore_errors=True)
        extract.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ditto", "-x", "-k", str(zip_path), str(extract)], check=True)
        nested = extract / "VideoDownloader.app"
        src = nested if nested.is_dir() else extract
        shutil.rmtree(str(staging), ignore_errors=True)
        subprocess.run(["ditto", str(src), str(staging)], check=True)
        shutil.rmtree(str(extract), ignore_errors=True)
    except Exception:
        return None
    return staging if staging.exists() else None


# --------------------------------------------------------------------------- #
# 更新助手（独立进程，脱离父进程存活）
# --------------------------------------------------------------------------- #
_ASSISTANT_SRC = r'''#!/bin/bash
# VDL update assistant — 脱离父进程运行：等待主程序退出后替换 .app 并重新打开。
STAGING="$1"
TARGET="$2"
MAIN_PID="$3"
PORT="$4"
GUARD="$5"
LOG="$6"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG" 2>/dev/null; }

wait_main() {
  local waited=0
  while [ "$waited" -lt "$GUARD" ]; do
    alive=0
    if [ -n "$MAIN_PID" ] && kill -0 "$MAIN_PID" 2>/dev/null; then alive=1; fi
    if lsof -ti:"$PORT" >/dev/null 2>&1; then alive=1; fi
    if [ "$alive" -eq 0 ]; then log "assistant: main gone after ${waited}s"; return 0; fi
    sleep 1
    waited=$((waited+1))
  done
  log "assistant: guard(${GUARD}s) timeout, proceeding anyway"
  return 0
}

install_app() {
  local dst_parent
  dst_parent="$(dirname "$TARGET")"
  mkdir -p "$dst_parent" 2>/dev/null
  if [ -w "$dst_parent" ]; then
    log "assistant: ditto (direct) $STAGING -> $TARGET"
    ditto "$STAGING" "$TARGET"
  else
    local script="ditto $(printf '%q' "$STAGING") $(printf '%q' "$TARGET")"
    log "assistant: ditto (admin) $script"
    osascript -e "do shell script \"$script\" with administrator privileges"
  fi
}

log "assistant start (staging=$STAGING target=$TARGET pid=$MAIN_PID port=$PORT guard=${GUARD}s)"
wait_main
install_app
log "assistant: open $TARGET"
open "$TARGET"
# 清理临时工作目录（含 old.tar / new.tar / patch / staging）
rm -rf "$(dirname "$STAGING")" 2>/dev/null
log "assistant done"
'''


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@router.get("/api/system/info")
def system_info() -> dict[str, Any]:
    return {
        "ok": True,
        "version": VERSION,
        "build": BUILD,
        "platform": "macos",
        "bundled": IS_BUNDLED,
        "updatable": IS_BUNDLED,
        "update_base_url": UPDATE_BASE_URL,
    }


@router.get("/api/system/latest")
def system_latest() -> dict[str, Any]:
    try:
        data = _http_get_json(UPDATE_BASE_URL.rstrip("/") + "/latest.json", timeout=15)
    except Exception as e:
        return {"ok": False, "error": "无法连接更新服务器（%s）" % e, "current": VERSION}
    latest_ver = str(data.get("version") or "")
    update_available = bool(latest_ver) and _parse_ver(latest_ver) > _parse_ver(VERSION)
    return {
        "ok": True,
        "current": VERSION,
        "latest": {
            "version": latest_ver,
            "notes": data.get("notes") or "",
            "published_at": data.get("published_at") or "",
            "url": data.get("url") or "",
            "size": data.get("size") or 0,
            "sha256": data.get("sha256") or "",
            "from_version": data.get("from_version") or "",
            "patch_url": data.get("patch_url") or "",
            "patch_size": data.get("patch_size") or 0,
            "patch_sha256": data.get("patch_sha256") or "",
        },
        "update_available": update_available,
    }


@router.post("/api/system/update")
def system_update(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    # 更新涉及磁盘写入与进程派生，限定已登录用户触发
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    if not IS_BUNDLED:
        return {"ok": False, "error": "开发模式下不支持一键更新，请下载安装包", "code": "NOT_BUNDLED"}

    target_ver = str(payload.get("version") or "").strip()
    if not target_ver:
        return {"ok": False, "error": "缺少目标版本"}

    # 取最新信息（含下载地址与校验和）
    try:
        data = _http_get_json(UPDATE_BASE_URL.rstrip("/") + "/latest.json", timeout=15)
    except Exception as e:
        return {"ok": False, "error": "无法连接更新服务器：%s" % e}
    if str(data.get("version")) != target_ver:
        return {"ok": False, "error": "目标版本与服务器不一致，请重试"}

    bundle = _bundle_path()
    if not bundle:
        return {"ok": False, "error": "无法确定当前 App 路径"}
    target_app = bundle.parent / "VideoDownloader.app"

    # 改为异步：立即创建 job 并启动后台线程执行「下载/套用/派生助手」，
    # 前端（pywebview 原生 bridge）轮询 /api/system/update/status 获取进度。
    # 这样「触发更新」完全不经过 WKWebView 的 fetch，从原理上规避其偶发拦截。
    job_id = uuid.uuid4().hex[:12]
    work = Path(tempfile.gettempdir()) / ("vdl_update_" + job_id)
    try:
        work.mkdir(parents=True, exist_ok=True)
    except Exception:
        return {"ok": False, "error": "无法创建临时工作目录"}
    with _JOBS_LOCK:
        _UPDATE_JOBS[job_id] = {
            "status": "queued", "progress": 0, "error": "",
            "target_ver": target_ver, "created_at": time.time(),
        }
    t = threading.Thread(
        target=_run_update,
        args=(job_id, data, work, bundle, target_app, target_ver),
        daemon=True,
    )
    t.start()
    return {"ok": True, "job_id": job_id, "async": True,
            "message": "更新任务已启动，正在后台下载并准备安装"}


def _run_update(job_id: str, data: dict, work: Path, bundle: Path,
                target_app: Path, target_ver: str) -> None:
    """后台线程：准备新 .app（增量优先，失败回退全量）+ 派生更新助手。"""
    def _set(status: str, progress: int = 0, error: str = "") -> None:
        with _JOBS_LOCK:
            j = _UPDATE_JOBS.get(job_id)
            if j:
                j["status"] = status
                j["progress"] = progress
                if error:
                    j["error"] = error

    try:
        _set("downloading", 10)
        staging_app = _prepare_update(data, work, bundle, target_ver)
        if not staging_app:
            _set("error", 0, "更新准备失败，请稍后重试或手动下载安装包")
            return

        _set("applying", 80)
        # 派生脱离父进程的更新助手（纯 bash：等待主程序退出后 ditto 覆盖并重新 open）
        assistant_sh = work / "assistant.sh"
        assistant_sh.write_text("#!/bin/bash\n" + _ASSISTANT_SRC, encoding="utf-8")
        try:
            os.chmod(assistant_sh, 0o755)
        except Exception:
            pass
        log_path = Path.home() / ".vdl_update.log"
        try:
            log_path.write_text("", encoding="utf-8")
        except Exception:
            pass
        subprocess.Popen(
            ["/bin/bash", str(assistant_sh),
             str(staging_app), str(target_app), str(os.getpid()),
             "8321", str(UPDATE_GUARD_SECONDS), str(log_path)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _set("ready", 100)
    except Exception as e:
        _set("error", 0, "更新失败：%s" % e)


@router.get("/api/system/update/status")
def system_update_status(job: str = "") -> dict[str, Any]:
    with _JOBS_LOCK:
        j = _UPDATE_JOBS.get(job)
        if not j:
            return {"ok": False, "error": "任务不存在或已过期"}
        return {"ok": True, **j}


def _require_user(request: Request) -> Optional[str]:
    from user_membership import get_current_user_id
    return get_current_user_id(request)
