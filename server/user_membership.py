"""VDL 会员的 per-user 存储解析（B2：按用户分文件）。

会员引擎 MembershipStore 已支持路径注入，这里按 user_id 把状态文件落到
~/.video-downloader/memberships/{user_id}.json，并缓存单例。

current_member_store(request)：从请求 Authorization 头解析 user_id；
  - 已登录 → 返回该用户的会员 store；
  - 未登录 → 回退到 app.member_store（全局匿名 store，免费档配额共享）。
下游门禁（下载配额墙 / 满速提取 / AI 积分）统一经此函数取 store，实现
「免费用户受限、会员用户按其自身权益放行」。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from auth_store import token_from_header
from membership import MembershipStore

# user_id -> MembershipStore 缓存（按进程，避免每次请求重读磁盘）
_STORE_CACHE: dict[str, MembershipStore] = {}


def _membership_dir() -> Path:
    if __import__("sys").platform == "win32" and getattr(__import__("sys"), "frozen", False):
        from auth_store import _base_dir
        base = _base_dir()
    else:
        base = Path.home() / ".video-downloader"
    d = base / "memberships"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_user_store(user_id: str) -> MembershipStore:
    if user_id not in _STORE_CACHE:
        _STORE_CACHE[user_id] = MembershipStore(path=_membership_dir() / f"{user_id}.json")
    return _STORE_CACHE[user_id]


def get_current_user_id(request) -> Optional[str]:
    auth = None
    try:
        auth = getattr(request, "headers", {}).get("Authorization")
    except Exception:
        auth = None
    if not auth and hasattr(request, "state"):
        # FastAPI Request：从 headers 取
        try:
            auth = request.headers.get("Authorization")
        except Exception:
            auth = None
    return token_from_header(auth)


def require_login_user(request) -> str:
    """功能级登录门禁（2026-09-29 对齐 App「所有功能必须登录才能使用」）。

    执行类端点（转换/拼接/去水印/字幕/解说/订阅/种子/队列/清理）在函数体第一行
    调用本函数；未登录一律 403。detail 必须含「登录」——前端 request() 据
    403+登录 文案置 needLogin 并拉起登录/注册框（双保险的前端兜底）。
    """
    uid = get_current_user_id(request)
    if not uid:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="请先登录账号后使用该功能（免费注册即得）")
    return uid


def current_member_store(request) -> MembershipStore:
    uid = get_current_user_id(request)
    if uid:
        return get_user_store(uid)
    # 匿名回退（延迟 import 避免循环）
    import app
    return app.member_store
