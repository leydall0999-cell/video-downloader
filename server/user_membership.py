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

import time

from auth_store import token_from_header
from membership import MembershipStore

# user_id -> MembershipStore 缓存（按进程，避免每次请求重读磁盘）
_STORE_CACHE: dict[str, MembershipStore] = {}

# chosen-store 决议缓存（避免每次请求双读盘）
_RESOLVE_CACHE: dict[str, tuple[float, MembershipStore]] = {}
_RESOLVE_TTL = 10.0


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


def _has_benefit(store: MembershipStore) -> bool:
    try:
        s = store.status()
    except Exception:
        return False
    return (bool(s.get("download_member", {}).get("active"))
            or bool(s.get("ai_member", {}).get("active"))
            or int(s.get("permanent_credits", 0) or 0) > 0)


def current_member_store(request) -> MembershipStore:
    """当前生效的会员 store。

    本机未登录（auth_store）→ 全局匿名 store。
    已登录 → 用户 store；但该用户自己没权益、而**全局 store 有云端买的会员**时
      （账号制把权益落在全局），回退到全局 —— 否则开了会员却因为登了本地账号
      而读不到权益，等于白买。结果按 uid 缓存 10 秒，避免每次请求都读两遍磁盘。
    """
    uid = get_current_user_id(request)
    import app
    if not uid:
        return app.member_store
    now = time.time()
    hit = _RESOLVE_CACHE.get(uid)
    if hit and now - hit[0] < _RESOLVE_TTL:
        return hit[1]
    ustore = get_user_store(uid)
    chosen = app.member_store if (not _has_benefit(ustore)
                                  and _has_benefit(app.member_store)) else ustore
    _RESOLVE_CACHE[uid] = (now, chosen)
    return chosen
