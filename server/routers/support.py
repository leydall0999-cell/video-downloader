"""server/routers/support.py — 在线客服 / 工单聊天（/api/support/*）。

存储：~/.video-downloader/support/threads.json（单文件，进程内锁串行读写）。
- 普通登录用户：发消息、看自己的会话、看管理员回复。
- 超级管理员（is_admin）：查看全部用户会话、回复、改状态。
每条消息都记录 role(user/admin)、sender_id、sender_identifier、text、ts，
满足「超管看得到是谁提的、跟谁回」的审计需求。

图片消息（2026-10-03）：「有些场景说不清楚，发截图一目了然」。
- 落盘在 support/media/<uuid>.<ext>（数据目录，**不碰 Downloads/Documents**，
  否则 ad-hoc 重签后 TCC 会重新索要授权、open() 永久阻塞）。
- 消息带 image 字段（文件名），允许「无文字、只有图」。
- 读取走 /api/support/image/{name}：管理员任意；普通用户只能看自己会话里引用过的图。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import atomic_io

from fastapi import APIRouter, Body, Request

router = APIRouter()

_lock = threading.Lock()
_MAX_LEN = 4000

# 图片：白名单格式 + 大小上限（原始字节，不是 base64 长度）
_IMG_MAX_BYTES = 4 * 1024 * 1024
_IMG_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
# 只允许「我们自己生成的文件名」，杜绝 ../ 穿越
_IMG_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp|gif)$")
# 签名 URL 有效期（图片要能反复翻看，给足 30 天；过期后重新拉会话即换新）
_IMG_URL_TTL = 30 * 24 * 3600.0


def _support_dir() -> Path:
    from auth_store import _base_dir

    d = _base_dir() / "support"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _threads_path() -> Path:
    return _support_dir() / "threads.json"


def _read_threads() -> list[dict]:
    p = _threads_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8") or "[]")
    except Exception:
        return []


def _write_threads(threads: list[dict]) -> None:
    atomic_io.atomic_write_json(_threads_path(), threads)


def _require_user(request: Request) -> Optional[str]:
    from user_membership import get_current_user_id

    return get_current_user_id(request)


def _ident(uid: str) -> str:
    from auth_store import user_identifier

    return user_identifier(uid) or uid


def _append_diagnostics(text: str) -> str:
    """错误上报：把启动诊断日志（~/.vdl_launch.log）尾部若干行附到消息后，便于排障。"""
    try:
        log = Path.home() / ".vdl_launch.log"
        if log.exists():
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
            tail = "\n".join(lines[-60:])
            if tail.strip():
                text = text + "\n\n---\n诊断信息（自动附加，来自 ~/.vdl_launch.log）：\n" + tail
    except Exception:
        pass
    return text


def _is_admin(uid: str) -> bool:
    from auth_store import user_is_admin

    return bool(user_is_admin(uid))


def _unread_for(thread: dict, uid: str, admin: bool) -> int:
    """当前查看者视角下，对方发来且晚于自己上次已读时间戳的消息条数。"""
    msgs = thread.get("messages", []) or []
    base = (thread.get("admin_read_ts") if admin else thread.get("user_read_ts")) or 0
    other = "user" if admin else "admin"
    return sum(1 for m in msgs if m.get("role") == other and (m.get("ts") or 0) > base)


def _append(threads: list[dict], thread: dict, msg: dict, status: str = "open") -> None:
    thread["messages"].append(msg)
    thread["updated_at"] = msg["ts"]
    thread["status"] = status
    _write_threads(threads)


# ── 图片（2026-10-03）─────────────────────────────────────────────────────────


def _media_dir() -> Path:
    """附件目录：数据目录下的 support/media（TCC 之外，不受隐私授权影响）。"""
    d = _support_dir() / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _media_path(name: str) -> Optional[Path]:
    """按白名单文件名解析附件路径；非法名（含 ../ 穿越）返回 None。"""
    if not name or not _IMG_NAME_RE.match(str(name)):
        return None
    return _media_dir() / str(name)


def _decode_image_payload(payload: dict[str, Any]) -> tuple[str, bytes, str]:
    """把前端传来的 {mime, data_b64} 解成 (文件名, 原始字节, mime)。

    纯函数（不碰磁盘），便于离线测试。非法输入抛 ValueError。
    """
    mime = str((payload or {}).get("mime") or "").strip().lower()
    ext = _IMG_TYPES.get(mime)
    if not ext:
        raise ValueError("只支持 PNG / JPEG / WebP / GIF 图片")
    raw = str((payload or {}).get("data_b64") or "").strip()
    if not raw:
        raise ValueError("图片内容为空")
    # 前端可能带 data: 前缀
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        data = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("图片数据损坏，请重新选择") from exc
    if not data:
        raise ValueError("图片内容为空")
    if len(data) > _IMG_MAX_BYTES:
        raise ValueError(f"图片过大（≤{_IMG_MAX_BYTES // 1024 // 1024}MB）")
    # 文件头魔数二次校验：后缀按声明的 mime 给，内容对不上就拒（防伪装成图片的任意文件）
    if not _looks_like_image(data, ext):
        raise ValueError("文件内容不是可识别的图片")
    return f"{uuid.uuid4().hex}.{ext}", data, mime


def _looks_like_image(data: bytes, ext: str) -> bool:
    if ext == "png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in ("jpg", "jpeg"):
        return data[:3] == b"\xff\xd8\xff"
    if ext == "gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    if ext == "webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _thread_owns_image(thread: dict, name: str) -> bool:
    return any((m or {}).get("image") == name for m in (thread.get("messages") or []))


def _sign_with_exp(name: str, exp: int) -> str:
    """按「文件名 + 过期时间戳」签出令牌（HMAC-SHA256[:32]，密钥 = 本机 .auth_secret）。"""
    from auth_store import _load_secret

    payload = f"{name}:{int(exp)}".encode("utf-8")
    return hmac.new(_load_secret(), payload, hashlib.sha256).hexdigest()[:32]


def _sign_image(name: str, ttl: float = _IMG_URL_TTL) -> str:
    """给图片签一个带时效的 URL 令牌（`"<exp>.<sig>"`）。

    🔴 为什么需要：`<img src>` / 灯箱是**浏览器直接发请求**，不会带
    Authorization 头 —— 只靠登录态鉴权的话，图片一律 401，界面显示裂图
    （2026-10-03 实测）。所以消息返回时附带签名 URL，凭签名取图；
    密钥复用本机 .auth_secret，泄露不了也跨机通用不了。
    """
    try:
        exp = int(time.time() + ttl)
        return f"{exp}.{_sign_with_exp(name, exp)}"
    except Exception:  # noqa: BLE001 — 签不出来就退回登录态取图
        return ""


def _image_token_ok(name: str, token: str) -> bool:
    """校验图片 URL 令牌：按令牌自带的 exp 重算签名比对，且未过期。"""
    exp_s, _, sig = str(token or "").partition(".")
    try:
        exp = int(exp_s)
    except (TypeError, ValueError):
        return False
    if not sig or exp < int(time.time()):
        return False
    try:
        expected = _sign_with_exp(name, exp)
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(sig, expected)


def _image_url(name: str) -> str:
    if not name:
        return ""
    tok = _sign_image(name)
    return f"/api/support/image/{name}" + (f"?t={tok}" if tok else "")


def _with_image_urls(messages: list[dict]) -> list[dict]:
    """返回给前端前给每条带图消息补 image_url（落盘仍只存文件名）。"""
    out = []
    for m in messages or []:
        if isinstance(m, dict) and m.get("image"):
            m = dict(m)
            m["image_url"] = _image_url(m["image"])
        out.append(m)
    return out


def _image_name_of(payload: dict[str, Any]) -> str:
    """从请求体取出图片文件名并校验格式（空串 = 不带图）。"""
    name = str((payload or {}).get("image") or "").strip()
    if not name:
        return ""
    if not _IMG_NAME_RE.match(name):
        raise ValueError("图片标识非法")
    return name


# ── 历史消息搜索（2026-10-03）────────────────────────────────────────────────


def _search_messages(
    threads: list[dict],
    uid: str,
    is_admin: bool,
    q: str,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """在可见会话的消息正文里搜关键词。纯函数，便于离线测试。

    返回按时间倒序的命中列表：thread_id / user_identifier / role / ts /
    excerpt（含关键词的上下文片段）/ has_image / index（会话内消息下标，供前端定位高亮）。
    """
    kw = (q or "").strip().lower()
    if not kw:
        return []
    out: list[dict[str, Any]] = []
    for t in threads or []:
        if not is_admin and t.get("user_id") != uid:
            continue
        msgs = t.get("messages") or []
        for idx, m in enumerate(msgs):
            text = str((m or {}).get("text") or "")
            if kw not in text.lower():
                continue
            pos = text.lower().find(kw)
            start = max(0, pos - 24)
            end = min(len(text), pos + len(kw) + 40)
            excerpt = ("…" if start > 0 else "") + text[start:end].strip() + ("…" if end < len(text) else "")
            out.append(
                {
                    "thread_id": t.get("id"),
                    "user_identifier": t.get("user_identifier"),
                    "role": (m or {}).get("role"),
                    "ts": (m or {}).get("ts"),
                    "excerpt": excerpt,
                    "has_image": bool((m or {}).get("image")),
                    "index": idx,
                }
            )
    out.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return out[: max(1, int(limit or 60))]


@router.post("/api/support/message")
def support_message(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """用户提交问题（可只发图片）。登录必需。可带 thread_id 续接到已有会话，否则开新会话。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    text = str(payload.get("text") or "").strip()
    try:
        image = _image_name_of(payload)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not text and not image:
        return {"ok": False, "error": "请输入内容或选择一张图片"}
    if len(text) > _MAX_LEN:
        return {"ok": False, "error": f"内容过长（≤{_MAX_LEN}字）"}
    # 错误上报：自动附加启动诊断日志尾部（~/.vdl_launch.log），便于排障。
    # 仅当文案以 [错误上报] 开头且未显式「-不含诊断」时才附加。
    if text.startswith("[错误上报]") and "-不含诊断" not in text:
        text = _append_diagnostics(text)
    tid = str(payload.get("thread_id") or "").strip()
    with _lock:
        threads = _read_threads()
        thread = None
        if tid:
            thread = next(
                (t for t in threads if t.get("id") == tid and t.get("user_id") == uid),
                None,
            )
        if thread is None:
            thread = {
                "id": uuid.uuid4().hex[:12],
                "user_id": uid,
                "user_identifier": _ident(uid),
                "status": "open",
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
                "user_read_ts": int(time.time()),
                "admin_read_ts": 0,
                "messages": [],
            }
            threads.append(thread)
        msg = {
            "role": "user",
            "sender_id": uid,
            "sender_identifier": _ident(uid),
            "text": text,
            "ts": int(time.time()),
        }
        if image:
            msg["image"] = image
        _append(threads, thread, msg)
    return {"ok": True, "thread_id": thread["id"],
            "message": (_with_image_urls([msg])[0] if msg.get("image") else msg)}


@router.get("/api/support/threads")
def support_threads(request: Request) -> dict[str, Any]:
    """会话列表。超管看全部（含提交者账号），普通用户只看自己的。

    每个会话附带 unread（对方发来且自己未读的消息数）；顶层 total_unread_threads
    为含未读的会话数，供前端悬浮气泡红点使用。
    """
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    admin = _is_admin(uid)
    threads = _read_threads()
    items = threads if admin else [t for t in threads if t.get("user_id") == uid]
    items = sorted(items, key=lambda t: t.get("updated_at", 0), reverse=True)
    out = []
    unread_total = 0
    for t in items:
        msgs = t.get("messages", [])
        last = msgs[-1] if msgs else None
        unread = _unread_for(t, uid, admin)
        if unread:
            unread_total += 1
        out.append(
            {
                "id": t["id"],
                "user_identifier": t.get("user_identifier"),
                "status": t.get("status", "open"),
                "created_at": t.get("created_at"),
                "updated_at": t.get("updated_at"),
                "msg_count": len(msgs),
                "last_message": (last or {}).get("text", ""),
                "last_role": (last or {}).get("role"),
                "unread": unread,
                "mine": t.get("user_id") == uid,
            }
        )
    return {"ok": True, "is_admin": admin, "threads": out, "total_unread_threads": unread_total}


@router.get("/api/support/thread/{tid}")
def support_thread(tid: str, request: Request) -> dict[str, Any]:
    """会话详情。本人或超管可访问。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    threads = _read_threads()
    thread = next((t for t in threads if t.get("id") == tid), None)
    if not thread:
        return {"ok": False, "error": "会话不存在"}
    if not _is_admin(uid) and thread.get("user_id") != uid:
        return {"ok": False, "error": "无权访问", "code": "FORBIDDEN"}
    return {
        "ok": True,
        "thread": {
            "id": thread["id"],
            "user_identifier": thread.get("user_identifier"),
            "status": thread.get("status"),
            "messages": _with_image_urls(thread.get("messages", [])),
        },
    }


@router.post("/api/support/thread/{tid}/reply")
def support_reply(tid: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """超管回复某会话（可只发图片）。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    if not _is_admin(uid):
        return {"ok": False, "error": "仅管理员可回复", "code": "FORBIDDEN"}
    text = str(payload.get("text") or "").strip()
    try:
        image = _image_name_of(payload)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not text and not image:
        return {"ok": False, "error": "请输入回复内容或选择一张图片"}
    if len(text) > _MAX_LEN:
        return {"ok": False, "error": f"内容过长（≤{_MAX_LEN}字）"}
    with _lock:
        threads = _read_threads()
        thread = next((t for t in threads if t.get("id") == tid), None)
        if not thread:
            return {"ok": False, "error": "会话不存在"}
        msg = {
            "role": "admin",
            "sender_id": uid,
            "sender_identifier": _ident(uid),
            "text": text,
            "ts": int(time.time()),
        }
        if image:
            msg["image"] = image
        _append(threads, thread, msg)
    return {"ok": True, "message": (_with_image_urls([msg])[0] if msg.get("image") else msg)}


@router.post("/api/support/thread/{tid}/status")
def support_status(tid: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """超管修改会话状态（open / resolved）。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    if not _is_admin(uid):
        return {"ok": False, "error": "仅管理员可操作", "code": "FORBIDDEN"}
    st = str(payload.get("status") or "").strip()
    if st not in ("open", "resolved"):
        return {"ok": False, "error": "状态非法"}
    with _lock:
        threads = _read_threads()
        thread = next((t for t in threads if t.get("id") == tid), None)
        if not thread:
            return {"ok": False, "error": "会话不存在"}
        thread["status"] = st
        thread["updated_at"] = int(time.time())
        _write_threads(threads)
    return {"ok": True, "status": st}


@router.post("/api/support/image")
def support_upload_image(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """上传一张图片（JSON base64，不引 multipart 依赖），返回可引用的文件名。

    登录必需；带 thread_id 时必须是自己（或管理员管辖）的会话。
    """
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    try:
        name, data, _mime = _decode_image_payload(payload)
    except ValueError as e:
        return {"ok": False, "error": str(e), "code": "BAD_IMAGE"}
    tid = str((payload or {}).get("thread_id") or "").strip()
    if tid and not _is_admin(uid):
        with _lock:
            threads = _read_threads()
            own = next((t for t in threads if t.get("id") == tid and t.get("user_id") == uid), None)
        if own is None:
            return {"ok": False, "error": "无权访问", "code": "FORBIDDEN"}
    try:
        (_media_dir() / name).write_bytes(data)
    except OSError as e:
        return {"ok": False, "error": f"图片保存失败：{e}", "code": "IO"}
    return {"ok": True, "image": name, "url": f"/api/support/image/{name}"}


@router.get("/api/support/image/{name}")
def support_get_image(name: str, request: Request, t: str = ""):
    """读取图片。两种身份任一即可：
    1) 已登录（Authorization 头）——管理员任意，普通用户只能自己会话引用过的；
    2) 携带会话消息里下发的签名 URL（`?t=`）——`<img>`/灯箱不会带请求头，只能走这条。
    """
    path = _media_path(name)
    if path is None or not path.exists():
        return {"ok": False, "error": "图片不存在"}

    if t and _image_token_ok(name, t):
        pass  # 签名有效：直接放行（不区分角色，签名本身就是授权）
    else:
        uid = _require_user(request)
        if not uid:
            return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
        if not _is_admin(uid):
            with _lock:
                threads = _read_threads()
            mine = [t2 for t2 in threads if t2.get("user_id") == uid and _thread_owns_image(t2, name)]
            if not mine:
                return {"ok": False, "error": "无权访问", "code": "FORBIDDEN"}

    from fastapi.responses import FileResponse

    media = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
    }
    return FileResponse(
        path,
        media_type=media.get(path.suffix.lstrip("."), "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/api/support/search")
def support_search(request: Request, q: str = "", limit: int = 60) -> dict[str, Any]:
    """搜索历史消息：超管搜全部会话，普通用户搜自己的。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    kw = str(q or "").strip()
    if not kw:
        return {"ok": True, "query": "", "results": []}
    with _lock:
        threads = _read_threads()
    results = _search_messages(threads, uid, _is_admin(uid), kw, limit=limit)
    return {"ok": True, "query": kw, "results": results}


@router.post("/api/support/thread/{tid}/read")
def support_mark_read(tid: str, request: Request) -> dict[str, Any]:
    """标记会话为已读（按当前角色更新 user_read_ts / admin_read_ts）。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    admin = _is_admin(uid)
    with _lock:
        threads = _read_threads()
        thread = next((t for t in threads if t.get("id") == tid), None)
        if not thread:
            return {"ok": False, "error": "会话不存在"}
        if not admin and thread.get("user_id") != uid:
            return {"ok": False, "error": "无权访问", "code": "FORBIDDEN"}
        msgs = thread.get("messages", []) or []
        latest = max((m.get("ts") or 0) for m in msgs) or int(time.time())
        if admin:
            thread["admin_read_ts"] = max(thread.get("admin_read_ts") or 0, latest)
        else:
            thread["user_read_ts"] = max(thread.get("user_read_ts") or 0, latest)
        _write_threads(threads)
    return {"ok": True}
