"""server/routers/baidu_dlink.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.post("/api/baidu_dlink")
def add_baidu_dlink(payload: app.BaiduDlinkRequest, request: app.Request) -> dict:
    # SSRF 护栏：仅允许百度域名直链
    if "pan.baidu.com" not in payload.dlink and "baidu.com" not in payload.dlink:
        raise app.HTTPException(status_code=400, detail="仅支持百度网盘下载直链")
    # 文件名安全化
    raw = (payload.filename or "baidu_download").strip()
    fname = app.re.sub(r'[^\w\-\.\(\)\u4e00-\u9fff ]', '_', raw) or "baidu_download"
    if not fname.lower().endswith((".apk", ".zip", ".rar", ".mp4", ".pdf", ".7z", ".tar", ".gz", ".exe", ".dmg", ".iso", ".txt", ".json")):
        fname += ".bin"
    dest_dir = app.DOWNLOAD_DIR / "baidu"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / fname
    # 防重名
    if dest.exists():
        base, ext = app.os.path.splitext(fname)
        i = 1
        while dest.exists():
            dest = dest_dir / f"{base}_{i}{ext}"
            i += 1
    task_id = "bd_" + str(int(app.time.time() * 1000))[-10:]

    def _run():
        try:
            clouddrive._aria2c_download(dlink=payload.dlink, dest=dest, total=0, concurrency=8)
            app.logger.info("百度直链下载完成: %s", dest)
        except Exception as e:
            app.logger.error("百度直链下载失败: %s", e)

    _t2 = app.threading.Thread(target=_run, name="vdl-baidu-dlink", daemon=True)
    _t2.start()
    return {"ok": True, "task_id": task_id, "dest": str(dest), "message": "已提交 aria2c 下载"}
