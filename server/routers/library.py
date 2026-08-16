"""server/routers/library.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/library")
def library_list(q: str = "", platform: str = "", kind: str = "all") -> dict:
    items = app.library_mod.scan_library(app.DOWNLOAD_DIR)
    if q:
        ql = q.lower()
        items = [
            i for i in items
            if ql in (i["title"] or "").lower()
            or ql in (i["name"] or "").lower()
            or ql in (i["uploader"] or "").lower()
        ]
    if platform:
        items = [i for i in items if i["platform"] == platform]
    if kind in ("video", "audio"):
        items = [i for i in items if i["kind"] == kind]
    return {"items": items, "total": len(items)}

@router.get("/api/library/file/{lib_id}")
def library_file(lib_id: str) -> app.FileResponse:
    p = app.library_mod._resolve_safe(app.DOWNLOAD_DIR, lib_id)
    if not p:
        raise app.HTTPException(status_code=404, detail="文件不存在")
    return app.FileResponse(path=p, filename=p.name, media_type="application/octet-stream")

@router.get("/api/library/thumb/{lib_id}")
def library_thumb(lib_id: str) -> app.FileResponse:
    p = app.library_mod.get_thumbnail(app.DOWNLOAD_DIR, lib_id, app.FFMPEG_BIN)
    if not p:
        raise app.HTTPException(status_code=404, detail="无缩略图")
    return app.FileResponse(path=p, media_type="image/jpeg")

@router.delete("/api/library/{lib_id}")
def library_delete(lib_id: str) -> dict:
    if not app.library_mod.delete_item(app.DOWNLOAD_DIR, lib_id):
        raise app.HTTPException(status_code=404, detail="文件不存在")
    return {"deleted": True}

@router.get("/api/library/encfile/{lib_id}")
def library_encfile(lib_id: str) -> app.FileResponse:
    """解密播放：把 .vdlenc 临时解密到 .vault_tmp 并返回（带 Range 支持）。锁定时 423。"""
    app._require_crypto()
    app._require_unlocked()
    src = app.library_mod._resolve_safe(app.DOWNLOAD_DIR, lib_id)
    if not src or src.suffix.lower() != app.library_mod.ENCRYPTED_EXT:
        raise app.HTTPException(status_code=404, detail="加密文件不存在")
    try:
        orig_name, _kind, _ext = app.crypto_mod.read_header(src)
    except Exception:
        raise app.HTTPException(status_code=400, detail="加密文件损坏")
    tmp = app._vault_tmp_for(lib_id)
    try:
        app.crypto_mod.decrypt_file(src, tmp, app.VAULT_KEY)
    except Exception as exc:
        raise app.HTTPException(status_code=400, detail="解密失败：" + type(exc).__name__)
    ext = app.Path(orig_name).suffix.lower()
    media = {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".mov": "video/quicktime",
        ".webm": "video/webm", ".avi": "video/x-msvideo", ".m4v": "video/mp4",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".flac": "audio/flac", ".ogg": "audio/ogg", ".wav": "audio/wav",
        ".opus": "audio/ogg", ".gif": "image/gif", ".webp": "image/webp",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    }.get(ext, "application/octet-stream")
    return app.FileResponse(path=str(tmp), filename=orig_name, media_type=media)
