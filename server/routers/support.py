"""server/routers/support.py — 在线客服 / 工单聊天（/api/support/*）。

存储：~/.video-downloader/support/threads.json（单文件，进程内锁串行读写）。
- 普通登录用户：发消息、看自己的会话、看管理员回复。
- 超级管理员（is_admin）：查看全部用户会话、回复、改状态。
每条消息都记录 role(user/admin)、sender_id、sender_identifier、text、ts，
满足「超管看得到是谁提的、跟谁回」的审计需求。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

router = APIRouter()

_lock = threading.Lock()
_MAX_LEN = 4000


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
    p = _threads_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(threads, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def _require_user(request: Request) -> Optional[str]:
    from user_membership import get_current_user_id

    return get_current_user_id(request)


def _ident(uid: str) -> str:
    from auth_store import user_identifier

    return user_identifier(uid) or uid


def _is_admin(uid: str) -> bool:
    from auth_store import user_is_admin

    return bool(user_is_admin(uid))


def _append(threads: list[dict], thread: dict, msg: dict, status: str = "open") -> None:
    thread["messages"].append(msg)
    thread["updated_at"] = msg["ts"]
    thread["status"] = status
    _write_threads(threads)


@router.post("/api/support/message")
def support_message(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """用户提交问题。登录必需。可带 thread_id 续接到已有会话，否则开新会话。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "请输入内容"}
    if len(text) > _MAX_LEN:
        return {"ok": False, "error": f"内容过长（≤{_MAX_LEN}字）"}
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
        _append(threads, thread, msg)
    return {"ok": True, "thread_id": thread["id"], "message": msg}


@router.get("/api/support/threads")
def support_threads(request: Request) -> dict[str, Any]:
    """会话列表。超管看全部（含提交者账号），普通用户只看自己的。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    admin = _is_admin(uid)
    threads = _read_threads()
    items = threads if admin else [t for t in threads if t.get("user_id") == uid]
    items = sorted(items, key=lambda t: t.get("updated_at", 0), reverse=True)
    out = []
    for t in items:
        msgs = t.get("messages", [])
        last = msgs[-1] if msgs else None
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
                "mine": t.get("user_id") == uid,
            }
        )
    return {"ok": True, "is_admin": admin, "threads": out}


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
            "messages": thread.get("messages", []),
        },
    }


@router.post("/api/support/thread/{tid}/reply")
def support_reply(tid: str, request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """超管回复某会话。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    if not _is_admin(uid):
        return {"ok": False, "error": "仅管理员可回复", "code": "FORBIDDEN"}
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "请输入回复内容"}
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
        _append(threads, thread, msg)
    return {"ok": True, "message": msg}


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
