"""VDL 后台管理面板数据层（server/admin_store.py）。

职责：
  - 用户管理：列表（含会员态、禁用标记、is_admin 超级用户标记）、禁用/启用、重置密码。
  - 会员管理：列表、按 code 赠送会员、按 delta 调整积分。
  - 使用统计：聚合 stats.json。
  - 系统配置：套餐/成本/SMTP 状态（不暴露任何密码/密钥）。

超级用户（is_admin）的授权名单由 server/auth_store.ensure_superusers 维护
（admin.json 的 admin_identifiers 或环境变量 VDL_ADMIN_IDENTIFIER；无配置时首个
注册账号自动成为超级用户），后台内可经 /api/admin/users/{id}/set-admin 提权/降权。

纯本机、零外部依赖。
"""
from __future__ import annotations

import json
import os
import sys
import time
import atomic_io
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _base_dir() -> Path:
    # 覆盖位：与 server/auth_store._base_dir 保持一致（VDL_DATA_DIR 优先）。
    override = os.environ.get("VDL_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        base = Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    else:
        base = Path.home() / ".video-downloader"
    return base


def _users_path() -> Path:
    return _base_dir() / "users.json"


# --------------------------------------------------------------------------- #
# 用户管理
# --------------------------------------------------------------------------- #
def _load_users() -> dict[str, Any]:
    p = _users_path()
    if not p.exists():
        return {"users": [], "by_identifier": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {"users": [], "by_identifier": {}}
    data.setdefault("users", [])
    data.setdefault("by_identifier", {})
    return data


def _save_users(data: dict[str, Any]) -> None:
    p = _users_path()
    # 原子写（唯一临时名 + os.replace），权限严格 0600。
    # 调用方必须已在 auth_store.users_mutation() 临界区内（并发读改写会互相覆盖丢账号）。
    atomic_io.atomic_write_json(p, data, mode=0o600, indent=2, ensure_ascii=False)


def _license_admin_token() -> str:
    """授权中心管理员令牌：env VDL_LICENSE_ADMIN_TOKEN → {base}/license_admin.json。

    2026-10-16「所有端用户上云端」：后台用户管理改以云端授权中心为数据源，
    管理操作（提权 / 封禁 / 改密）也双写云端 ⇒ 全端生效。未配置时空串，
    调用方退化为仅本机（等同历史行为）。
    """
    tok = (os.environ.get("VDL_LICENSE_ADMIN_TOKEN") or "").strip()
    if tok:
        return tok
    try:
        with open(_base_dir() / "license_admin.json", "r", encoding="utf-8") as fh:
            tok = str((json.load(fh) or {}).get("admin_token") or "").strip()
    except Exception:
        tok = ""
    return tok


def _cloud_users() -> Optional[list[dict[str, Any]]]:
    """拉云端全量账号；不可达 / 未配令牌返回 None（调用方走本机兜底）。"""
    token = _license_admin_token()
    if not token:
        return None
    try:
        import license_client
        r = license_client.users_remote(token)
    except Exception:
        return None
    if not isinstance(r, dict) or not r.get("ok"):
        return None
    users = r.get("users")
    return users if isinstance(users, list) else None


def _cloud_user_row(u: dict[str, Any]) -> dict[str, Any]:
    """云端账号 → 后台行（保持既有字段键名不变，前端无感；新增端维度字段）。"""
    import time as _t
    now = _t.time()
    auth = u.get("authority") or {}
    devs = u.get("devices") or []
    return {
        "user_id": u.get("user_id") or u.get("email"),
        "identifier": u.get("email"),
        "created_at": u.get("created_at"),
        "last_login": u.get("last_login"),
        "disabled": bool(u.get("banned")),
        "is_admin": bool(u.get("is_admin")),
        "source": "cloud",
        "device_count": len(devs),
        "devices": [
            {"name": d.get("name"), "platform": d.get("platform"),
             "app_version": d.get("app_version"),
             "last_seen": d.get("last_seen")}
            for d in devs
        ],
        "membership": {
            "download_active": float(auth.get("member_until_dl") or 0) > now,
            "ai_active": float(auth.get("member_until_ai") or 0) > now,
            "credits_total": int(auth.get("perm_credits") or 0)
            + int(auth.get("ai_credits_left") or 0),
        },
    }


def list_users() -> list[dict[str, Any]]:
    """全端用户列表（2026-10-16：云端授权中心是唯一真源）。

    合并策略：云端账号为主（桌面 App + 网页版 cn/hk 的账号全部在此）；
    本机表中云端没有的账号（离线兜底分叉）以 source=local 附列，不丢人。
    云端不可达时整体退化为本机列表（source=local），后台仍可用。
    """
    from user_membership import get_user_store
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    cloud = _cloud_users()
    if cloud is not None:
        for u in cloud:
            email = str(u.get("email") or "").strip().lower()
            if not email:
                continue
            seen.add(email)
            out.append(_cloud_user_row(u))
    # 本机兜底 / 补充：已删除（deleted_at）账号不出现
    data = _load_users()
    for u in data.get("users", []):
        if u.get("deleted_at"):
            continue
        ident = str(u.get("identifier") or "").strip().lower()
        if ident in seen:
            continue
        uid = u.get("user_id")
        rec: dict[str, Any] = {
            "user_id": uid,
            "identifier": u.get("identifier"),
            "created_at": u.get("created_at"),
            "disabled": bool(u.get("disabled", False)),
            "is_admin": bool(u.get("is_admin", False)),
            "source": "local",
        }
        try:
            st = get_user_store(uid).status()
            rec["membership"] = {
                "download_active": st["download_member"]["active"],
                "ai_active": st["ai_member"]["active"],
                "credits_total": st["credits_total"],
            }
        except Exception:
            rec["membership"] = None
        out.append(rec)
    # 按注册时间倒序（新注册在前）
    out.sort(key=lambda r: (r.get("created_at") or 0), reverse=True)
    return out


def set_user_disabled(user_id: str, disabled: bool) -> dict[str, Any]:
    import auth_store  # 复用账号表的跨进程临界区（与 auth_store 同写 users.json）
    with auth_store.users_mutation():
        data = _load_users()
        user = next((u for u in data["users"] if u["user_id"] == user_id), None)
        if not user:
            return {"ok": False, "error": "用户不存在"}
        user["disabled"] = bool(disabled)
        user["updated_at"] = int(time.time())
        # 安全审计 2026-10-13（F-02）：禁用 / 解禁都撤销该账号已签发 token。
        # 此前 web 端禁用完全不撤 token ⇒ 后台「封禁」对已登录用户无效（实测确认）。
        # ⚠️ 本模块的 `_save_users` 是**直接原子写**、不经 auth_store._save_users，
        #    故必须显式失效鉴权状态缓存，否则 verify_token 的快照最多滞后 1 秒才看到「已禁用」。
        auth_store.bump_token_version(user)
        _save_users(data)
        auth_store._invalidate_status_cache()
    return {"ok": True, "disabled": bool(disabled)}


def delete_user(user_id: str) -> dict[str, Any]:
    """后台删除账号（软删除，2026-10-03）：标记 deleted_at + 禁用。

    与用户自助注销同一落盘形态：该 identifier 此后不可登录、不可再注册；
    记录保留在 users.json 里可审计。超级用户不可删（先降权），
    「不能删自己」由路由层拦。
    """
    import auth_store
    with auth_store.users_mutation():
        data = _load_users()
        user = next((u for u in data["users"] if u["user_id"] == user_id), None)
        if not user:
            return {"ok": False, "error": "用户不存在"}
        if user.get("deleted_at"):
            return {"ok": False, "error": "账号已删除"}
        if user.get("is_admin"):
            return {"ok": False, "error": "超级用户不可删除，请先取消其管理员"}
        user["deleted_at"] = int(time.time())
        user["disabled"] = True
        user["updated_at"] = int(time.time())
        # identifier 索引由 _save_users → _rebuild_index 重建（已跳过 deleted_at）
        _save_users(data)
    return {"ok": True}


def reset_user_password(user_id: str, new_password: str) -> dict[str, Any]:
    if not new_password or len(new_password) < 6:
        return {"ok": False, "error": "新密码至少 6 位"}
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    from auth_store import reset_password
    ok = reset_password(user["identifier"], new_password)
    if not ok:
        return {"ok": False, "error": "重置失败（账号异常）"}
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 会员管理
# --------------------------------------------------------------------------- #
def list_memberships() -> list[dict[str, Any]]:
    """遍历所有 per-user 会员状态文件。"""
    from user_membership import get_user_store
    data = _load_users()
    out = []
    for u in data.get("users", []):
        uid = u.get("user_id")
        try:
            st = get_user_store(uid).status()
        except Exception:
            continue
        out.append({
            "user_id": uid,
            "identifier": u.get("identifier"),
            "download_active": st["download_member"]["active"],
            "download_plan": st["download_member"]["plan"],
            "download_expire_at": st["download_member"]["expire_at"],
            "ai_active": st["ai_member"]["active"],
            "ai_plan": st["ai_member"]["plan"],
            "ai_credits_left": st["ai_member"]["credits_left"],
            "permanent_credits": st["permanent_credits"],
            "credits_total": st["credits_total"],
            "disabled": bool(u.get("disabled", False)),
        })
    out.sort(key=lambda r: (r.get("download_active") or r.get("ai_active") or False), reverse=True)
    return out


def grant_membership(user_id: str, code: str) -> dict[str, Any]:
    from user_membership import get_user_store
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    try:
        res = get_user_store(user_id).activate(code)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"激活失败：{e}"}
    return res


def adjust_credits(user_id: str, delta: int) -> dict[str, Any]:
    from user_membership import get_user_store
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return {"ok": False, "error": "delta 必须为整数"}
    try:
        res = get_user_store(user_id).add_credits(delta)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"调分失败：{e}"}
    return res


# --------------------------------------------------------------------------- #
# 使用统计 / 系统配置
# --------------------------------------------------------------------------- #
def usage_stats() -> dict[str, Any]:
    from stats import get_stats
    return get_stats()


def system_config() -> dict[str, Any]:
    """套餐、成本、SMTP 状态（超级用户专属；含 SMTP 密码以便编辑，仅带 user token 可见）。

    返回 has_superuser 标记是否存在任一 is_admin 账号，供面板提示超级用户引导。
    """
    from membership import MembershipStore, MATTING_CLOUD_CREDIT_COST, load_plan_overrides
    from auth_store import _smtp_accounts, _load_users
    data = _load_users()
    has_superuser = any(u.get("is_admin", False) for u in data.get("users", []))
    cfg: dict[str, Any] = {
        "plans": MembershipStore().plans(),
        "credit_costs": {"matting_cloud": MATTING_CLOUD_CREDIT_COST},
        "smtp": {"configured": False, "accounts": []},
        "has_superuser": bool(has_superuser),
        "has_plan_overrides": False,
    }
    try:
        ov = load_plan_overrides()
        cfg["has_plan_overrides"] = bool(ov)
        if "credit_costs" in ov:
            cfg["credit_costs"] = ov["credit_costs"]
    except Exception:
        pass
    try:
        accounts = _smtp_accounts() or []
        cfg["smtp"]["configured"] = len(accounts) > 0
        # admin 专属：返回完整账户（含 pass）以便前端编辑；仅带 token 可见
        cfg["smtp"]["accounts"] = [
            {"host": a.get("host"), "port": a.get("port"), "user": a.get("user"),
             "pass": a.get("pass"), "from": a.get("from"),
             "from_name": a.get("from_name"), "use_ssl": a.get("use_ssl"),
             "match_domains": a.get("match_domains"), "default": a.get("default")}
            for a in accounts
        ]
    except Exception:
        pass
    return cfg


# --------------------------------------------------------------------------- #
# 配置写回（后台管理面板「系统配置」可编辑）
# --------------------------------------------------------------------------- #
def save_smtp_accounts(accounts: Any) -> dict[str, Any]:
    """保存 SMTP 多账户配置到 ~/.video-downloader/smtp.json（0600）。

    accounts: list[dict]，字段 host/port/user/pass/from/from_name/use_ssl/
    match_domains/default。pass 为空字符串时回退保留同索引已有密码（便于只改
    非密码字段）；否则使用传入值。无账户标记 default 时自动设第一个为默认。
    """
    if not isinstance(accounts, list):
        return {"ok": False, "error": "accounts 必须为数组"}
    from auth_store import _smtp_accounts
    existing = _smtp_accounts() or []
    cleaned: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        if not isinstance(a, dict):
            return {"ok": False, "error": f"第 {i + 1} 个账户格式错误"}
        host = str(a.get("host") or "").strip()
        user = str(a.get("user") or "").strip()
        if not host or not user:
            return {"ok": False, "error": f"第 {i + 1} 个账户缺少 host 或 user"}
        pw = a.get("pass")
        if not pw:
            pw = (existing[i].get("pass") if i < len(existing) else None) or ""
        if not pw:
            return {"ok": False, "error": f"第 {i + 1} 个账户缺少密码"}
        try:
            port = int(a.get("port") or 465)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"第 {i + 1} 个账户端口非法"}
        cleaned.append({
            "host": host,
            "port": port,
            "user": user,
            "pass": str(pw),
            "from": str(a.get("from") or user),
            "from_name": str(a.get("from_name") or "VideoDownloader"),
            "use_ssl": bool(a.get("use_ssl", True)),
            "match_domains": [str(d).strip().lower() for d in (a.get("match_domains") or []) if str(d).strip()],
            "default": bool(a.get("default", False)),
        })
    if cleaned and not any(c.get("default") for c in cleaned):
        cleaned[0]["default"] = True
    p = _base_dir() / "smtp.json"
    try:
        atomic_io.atomic_write_json(p, {"accounts": cleaned})
    except OSError as e:  # noqa: BLE001
        return {"ok": False, "error": f"写入失败：{e}"}
    return {"ok": True, "count": len(cleaned)}


def save_plan_overrides(data: Any) -> dict[str, Any]:
    """写回套餐/成本覆盖到 plans.json（委托 membership.save_plan_overrides）。"""
    from membership import save_plan_overrides as _save
    try:
        result = _save(data or {})
        return {"ok": True, "overrides": result}
    except OSError as e:  # noqa: BLE001
        return {"ok": False, "error": f"写入失败：{e}"}


def reset_stats(path: Optional[Path] = None) -> dict[str, Any]:
    from stats import reset_stats as _reset
    _reset(path)
    return {"ok": True}
