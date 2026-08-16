"""server/routers/fs.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.post("/api/fs/open")
def fs_open(payload: app.OpenPathRequest) -> dict:
    """在系统文件管理器中打开本地路径。

    桌面版用户从浏览器里点「打开下载目录」→ 弹系统通知 / 调起 Finder。
    仅允许打开 DOWNLOAD_DIR 及其子项，不开放任意路径（防误开系统关键目录）。
    """
    if app.sys.platform != "darwin" and app.sys.platform != "win32":
        raise app.HTTPException(status_code=400, detail="该接口仅在桌面端可用")

    raw = (payload.path or "").strip()
    target = app.Path(raw).expanduser().resolve() if raw else app.DOWNLOAD_DIR.resolve()

    # 白名单：必须在 DOWNLOAD_DIR 下（除非用户显式请求 DOWNLOAD_DIR 本身）
    try:
        target.relative_to(app.DOWNLOAD_DIR.resolve())
    except ValueError:
        if target != app.DOWNLOAD_DIR.resolve():
            raise app.HTTPException(status_code=403, detail="只允许打开下载目录及其子路径")

    if not target.exists():
        raise app.HTTPException(status_code=404, detail=f"路径不存在：{target}")

    try:
        if app.sys.platform == "darwin":
            app.subprocess.Popen(["open", str(target)])
        else:
            app.subprocess.Popen(["explorer", str(target)])
    except Exception as e:
        raise app.HTTPException(status_code=500, detail=f"打开失败：{e}")

    return {"opened": str(target), "platform": app.sys.platform}
