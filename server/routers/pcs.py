"""server/routers/pcs.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/pcs/status")
def pcs_status() -> dict:
    if app.baidu_pcs is None:
        return {"binary_installed": False, "error": "baidu_pcs 模块未加载"}
    try:
        return app.baidu_pcs.status()
    except Exception as e:
        return {"binary_installed": False, "error": str(e)}

@router.get("/api/pcs/build-info")
def pcs_build_info() -> dict:
    """返回构建信息（git 哈希 + 构建时间），用于界面显示版本号防止跑错旧版。"""
    import json, os, sys
    default = {"hash": "unknown", "time": "unknown", "version": "?"}
    # 尝试多个可能路径（PyInstaller 提取目录 / 开发目录 / Resources）
    _candidates = []
    _base = app.os.path.dirname(app.os.path.abspath(__file__))
    for _name in ("build_info.txt", ".build_info.json"):
        _candidates.append(app.os.path.join(_base, _name))
        # PyInstaller onedir: 也尝试 sys._MEIPASS 下的 server/ 子目录
        if getattr(app.sys, 'frozen', False):
            _mei = getattr(app.sys, '_MEIPASS', None)
            if _mei:
                _candidates.append(app.os.path.join(_mei, "server", _name))
                _candidates.append(app.os.path.join(_mei, _name))
    for _p in _candidates:
        try:
            if app.os.path.exists(_p):
                with open(_p, "r") as _f:
                    _data = app.json.load(_f)
                    print(f"[build-info] OK from {_p} -> {_data}")
                    return _data
        except Exception as _e:
            print(f"[build-info] read error {_p}: {_e}")
    # fallback: 尝试从 git 读取
    import subprocess
    try:
        h = app.subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                    cwd=app.os.path.dirname(_base),
                                    stderr=app.subprocess.DEVNULL, timeout=5).decode().strip()
        default["hash"] = h
    except Exception:
        pass
    print(f"[build-info] FALLBACK -> {default}, candidates={_candidates}")
    return default

@router.post("/api/pcs/install")
def pcs_install() -> dict:
    if app.baidu_pcs is None:
        return {"ok": False, "error": "baidu_pcs 模块未加载"}
    def _prog(d):
        with app._pcs_lock:
            app._pcs_tasks["_install"] = {"status": "installing", **d}
    return app.baidu_pcs.ensure_binary(_prog)

@router.post("/api/pcs/login")
def pcs_login(payload: dict) -> dict:
    if app.baidu_pcs is None:
        return {"ok": False, "error": "baidu_pcs 模块未加载"}
    raw = (payload or {}).get("cookies") or (payload or {}).get("bduss") or ""
    try:
        return app.baidu_pcs.login(raw)
    except Exception as e:
        app.logger.exception("pcs_login 异常")
        return {"ok": False, "message": f"登录接口异常：{e}"}

@router.post("/api/pcs/login-password")
def pcs_login_password(payload: dict) -> dict:
    if app.baidu_pcs is None:
        return {"ok": False, "error": "baidu_pcs 模块未加载"}
    username = (payload or {}).get("username") or ""
    password = (payload or {}).get("password") or ""
    try:
        return app.baidu_pcs.login_by_password(username, password)
    except Exception as e:
        app.logger.exception("pcs_login_password 异常")
        return {"ok": False, "message": f"登录接口异常：{e}"}

@router.get("/api/pcs/qr/gen")
def pcs_qr_gen() -> dict:
    if app.baidu_qr is None:
        return {"ok": False, "status": "error", "message": "baidu_qr 模块未加载"}
    try:
        return app.baidu_qr.qr_gen()
    except Exception as e:
        app.logger.exception("pcs_qr_gen 异常")
        return {"ok": False, "status": "error", "message": f"生成二维码失败：{e}"}

@router.get("/api/pcs/qr/poll")
def pcs_qr_poll(sign: str = "") -> dict:
    if app.baidu_qr is None:
        return {"ok": False, "status": "error", "message": "baidu_qr 模块未加载"}
    try:
        return app.baidu_qr.qr_poll(sign)
    except Exception as e:
        app.logger.exception("pcs_qr_poll 异常")
        return {"ok": False, "status": "error", "message": f"轮询异常：{e}"}

@router.get("/api/pcs/who")
def pcs_who() -> dict:
    if app.baidu_pcs is None:
        return {"ok": False, "logged_in": False, "error": "baidu_pcs 模块未加载"}
    return app.baidu_pcs.who()

@router.post("/api/pcs/share/transfer")
def pcs_share_transfer(payload: dict) -> dict:
    if app.baidu_pcs is None:
        return {"ok": False, "error": "baidu_pcs 模块未加载"}
    url = (payload or {}).get("url") or ""
    pwd = (payload or {}).get("pwd") or ""
    return app.baidu_pcs.transfer(url, pwd)

@router.post("/api/pcs/ls")
def pcs_ls(payload: dict) -> dict:
    if app.baidu_pcs is None:
        return {"ok": False, "error": "baidu_pcs 模块未加载"}
    path = (payload or {}).get("path") or "/"
    return app.baidu_pcs.ls(path)

@router.post("/api/pcs/download")
def pcs_download(payload: dict):
    remote = (payload or {}).get("path") or (payload or {}).get("remote_path") or ""
    name = (payload or {}).get("name") or remote.split("/")[-1] or "pcs_download"
    if not remote:
        return {"ok": False, "detail": "缺少网盘路径 path"}
    tid = "pcs_" + str(int(app.time.time() * 1000))[-10:]

    def _worker():
        def _prog(d):
            with app._pcs_lock:
                app._pcs_tasks[tid]["progress"] = d
        with app._pcs_lock:
            app._pcs_tasks[tid] = {"status": "downloading", "name": name, "progress": {"stage": "starting"}}
        try:
            res = app.baidu_pcs.download(remote, progress=_prog)
            with app._pcs_lock:
                app._pcs_tasks[tid].update(status="done" if res["ok"] else "failed", **res)
        except Exception as e:  # noqa: BLE001
            with app._pcs_lock:
                app._pcs_tasks[tid].update(status="failed", message=str(e))

    app.threading.Thread(target=_worker, name=f"vdl-pcs-{tid}", daemon=True).start()
    return {"ok": True, "task_id": tid, "message": "已提交下载"}

@router.get("/api/pcs/task/{tid}")
def pcs_task(tid: str):
    with app._pcs_lock:
        t = app._pcs_tasks.get(tid)
    if not t:
        return {"ok": False, "detail": "任务不存在"}
    return t
