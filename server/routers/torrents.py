"""server/routers/torrents.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/torrents")
def torrent_list() -> dict:
    app._require_torrent()
    return {"items": app.torrent_manager.list(), "available": True}

@router.post("/api/torrents/add")
def torrent_add(req: app.TorrentAddRequest) -> dict:
    app._require_torrent()
    try:
        return app.torrent_manager.add(
            uri=req.uri, name=req.name or None, paused=req.paused,
            save_path=req.save_path or None,
            file_priorities={int(k): int(v) for k, v in req.file_priorities.items()} or None,
        )
    except ValueError as e:
        raise app.HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise app.HTTPException(status_code=500, detail=str(e))

@router.post("/api/torrents/add-file")
async def torrent_add_file(
    torrent: app.UploadFile | None = app._FastAPIFile(default=None),
    name: str = app.Form(default=""),
    paused: bool = app.Form(default=False),
    save_path: str = app.Form(default=""),
) -> dict:
    app._require_torrent()
    if not torrent:
        raise app.HTTPException(status_code=400, detail="未收到 .torrent 文件")
    data = await torrent.read()
    if not data:
        raise app.HTTPException(status_code=400, detail=".torrent 文件为空")
    try:
        return app.torrent_manager.add(
            torrent_data=data, name=name or None, paused=paused, save_path=save_path or None,
        )
    except ValueError as e:
        raise app.HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise app.HTTPException(status_code=500, detail=str(e))

@router.get("/api/torrents/{tid}")
def torrent_detail(tid: str) -> dict:
    app._require_torrent()
    item = app.torrent_manager.get(tid)
    if not item:
        raise app.HTTPException(status_code=404, detail="种子不存在")
    return item

@router.post("/api/torrents/{tid}/pause")
def torrent_pause(tid: str) -> dict:
    app._require_torrent()
    if not app.torrent_manager.pause(tid):
        raise app.HTTPException(status_code=404, detail="种子不存在")
    return {"paused": True}

@router.post("/api/torrents/{tid}/resume")
def torrent_resume(tid: str) -> dict:
    app._require_torrent()
    if not app.torrent_manager.resume(tid):
        raise app.HTTPException(status_code=404, detail="种子不存在")
    return {"paused": False}

@router.post("/api/torrents/{tid}/remove")
def torrent_remove(tid: str, req: app.TorrentRemoveRequest) -> dict:
    app._require_torrent()
    if not app.torrent_manager.remove(tid, delete_files=req.delete_files):
        raise app.HTTPException(status_code=404, detail="种子不存在")
    return {"removed": True}

@router.post("/api/torrents/{tid}/files")
def torrent_set_files(tid: str, req: app.TorrentFilesRequest) -> dict:
    app._require_torrent()
    if not app.torrent_manager.set_file_priorities(tid, {int(k): int(v) for k, v in req.priorities.items()}):
        raise app.HTTPException(status_code=404, detail="种子不存在或尚无元数据")
    return {"updated": True}
