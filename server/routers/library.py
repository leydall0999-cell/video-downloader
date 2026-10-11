"""server/routers/library.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

# 扩展名 → 播放用 MIME。🔴 2026-09-18 实测：`/api/library/file/{id}` 原先一律回
# `application/octet-stream`（配合 filename= 让浏览器下载），但 URL 里没有扩展名、
# MIME 也不是视频 ⇒ WKWebView（App 的 webview）直接拒绝解码，`<video>` 报
# MEDIA_ERR_SRC_NOT_SUPPORTED、videoWidth 恒 0 ⇒「下载历史库」选来的片子在预览里
# **永远是黑屏**（媒体库弹窗里的视频预览同样）。带 ?play=1 走这张表即可正常播放；
# 不带参数保持原样（下载语义不变）。
_MEDIA_BY_EXT = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".flv": "video/x-flv", ".ts": "video/mp2t", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

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
def library_file(lib_id: str, play: int = 0) -> app.FileResponse:
    p = app.library_mod._resolve_safe(app.DOWNLOAD_DIR, lib_id)
    if not p:
        raise app.HTTPException(status_code=404, detail="文件不存在")
    if play:
        # 内联播放：按扩展名给正确 MIME，且**不带 filename=**（避免 attachment 语义）
        return app.FileResponse(path=p, media_type=_MEDIA_BY_EXT.get(p.suffix.lower(), "application/octet-stream"))
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

def _purge_vault_tmp(keep: "app.Path | None" = None) -> None:
    """清理保险箱解密临时目录里的历史残留明文（`<lib_id>.dec`）。

    安全审计 2026-10-11（P1-7）：旧实现把 .vdlenc 解密到
    `DOWNLOAD_DIR/.vault_tmp/<id>.dec` 后长期留存。现在解密播放已改为
    **内存流式解密、明文不落盘**（见 `library_encfile`），本函数只用于
    清掉升级前遗留的 `.dec`，以及用户手动触发的清理。
    """
    try:
        for f in app.VAULT_TMP.glob("*.dec"):
            if keep is not None and f == keep:
                continue
            try:
                f.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 —— 清理失败不得影响播放
        pass


# 单次响应最多回多少明文：避免播放器请求 `Range: bytes=0-`（整段）时
# 把整部影片解密进内存。客户端拿到 206 + Content-Range 后会继续请求后续区间。
_MAX_ENC_WINDOW = 8 << 20
# 无 Range 头且文件大于此值时，也只回首个窗口（宁可少给也不 OOM）
_MAX_ENC_WHOLE = 64 << 20


def _parse_range(header: str, total: int) -> "tuple[int, int] | None":
    """解析单段 Range（`bytes=a-b` / `bytes=a-` / `bytes=-n`）。

    返回 (start, end) 闭区间；**无法满足**（语法合法但越界）返回 None。
    完全不带 Range 头由调用方另行处理。
    """
    h = (header or "").strip().lower()
    if not h.startswith("bytes="):
        return None
    spec = h[len("bytes="):].split(",")[0].strip()
    if "-" not in spec:
        return None
    a, _, b = spec.partition("-")
    try:
        if not a:                                  # bytes=-n → 末尾 n 字节
            n = int(b)
            if n <= 0:
                return None
            return max(0, total - n), total - 1
        start = int(a)
        end = int(b) if b else total - 1
    except ValueError:
        return None
    if start < 0 or start > end or start >= total:
        return None
    return start, min(end, total - 1)


def _safe_disposition_name(name: str) -> str:
    """把原名净化成可安全放进 `Content-Disposition` 的 ASCII 串。

    `.vdlenc` 文件头里的原名是**我们自己写进去的**，但仍按不可信处理：
    含引号/换行时直接拼进响应头会造成头注入（CRLF 注入）。这里只保留可打印
    ASCII 且剔除 `"` 与 `\\`，为空时回落 "vault"。
    """
    ascii_name = (name or "").encode("ascii", "ignore").decode()
    cleaned = "".join(ch for ch in ascii_name if ch.isprintable() and ch not in '"\\')
    return cleaned.strip() or "vault"


@router.get("/api/library/encfile/{lib_id}")
def library_encfile(lib_id: str, request: app.Request) -> app.Response:
    """解密播放：**内存流式解密 + 支持 Range**，明文绝不落盘。

    安全审计 2026-10-11（P1-7）：旧实现把 .vdlenc 解密到
    `DOWNLOAD_DIR/.vault_tmp/<id>.dec` 再交给 FileResponse —— 用户以为「已加锁」，
    实际明文长期留在磁盘上（且落在下载目录里，可被当作普通媒体文件取走）。
    现在直接按请求区间解密所需分块并回传：磁盘上**只有密文**。

    分块 GCM 的 nonce 只由 (nonce_base, 块序号) 决定，块间无链式依赖，
    故可按需只解密覆盖区间的那几块 —— 播放器拖动进度也只需解密对应分块。
    """
    app._require_crypto()
    app._require_unlocked()
    # 顺手清掉升级前遗留的 .dec 明文（现已不再产生任何解密产物）
    _purge_vault_tmp()
    src = app.library_mod._resolve_safe(app.DOWNLOAD_DIR, lib_id)
    if not src or src.suffix.lower() != app.library_mod.ENCRYPTED_EXT:
        raise app.HTTPException(status_code=404, detail="加密文件不存在")
    try:
        orig_name, _kind, ext = app.crypto_mod.read_header(src)
        total = app.crypto_mod.plaintext_size(src)
    except Exception:
        raise app.HTTPException(status_code=400, detail="加密文件损坏")
    media = {
        ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".mov": "video/quicktime",
        ".webm": "video/webm", ".avi": "video/x-msvideo", ".m4v": "video/mp4",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
        ".flac": "audio/flac", ".ogg": "audio/ogg", ".wav": "audio/wav",
        ".opus": "audio/ogg", ".gif": "image/gif", ".webp": "image/webp",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    }.get(ext, "application/octet-stream")

    raw_range = (request.headers.get("range") or "").strip()
    partial = False
    if raw_range:
        rng = _parse_range(raw_range, total)
        if rng is None:
            # 语法合法但越界 → 416（RFC 7233）
            return app.Response(status_code=416, headers={
                "Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"})
        start, end = rng
        partial = True
    else:
        start, end = 0, max(0, total - 1)
        if total > _MAX_ENC_WHOLE:
            end = _MAX_ENC_WINDOW - 1
            partial = True
    if end - start + 1 > _MAX_ENC_WINDOW:
        end = start + _MAX_ENC_WINDOW - 1
        partial = True

    try:
        data, _n, _k = app.crypto_mod.decrypt_range(src, app.VAULT_KEY, start, end)
    except Exception as exc:  # noqa: BLE001
        raise app.HTTPException(status_code=400, detail="解密失败：" + type(exc).__name__)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        # ⚠️ 不要手写 Content-Length：Starlette 会按 body 长度自动补一条，
        #    两边都写会出现**重复的 Content-Length**（协议违规，部分客户端拒收）。
        "Content-Disposition": f'inline; filename="{_safe_disposition_name(orig_name)}"',
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{start + len(data) - 1}/{total}"
        return app.Response(content=data, status_code=206, media_type=media, headers=headers)
    return app.Response(content=data, media_type=media, headers=headers)
