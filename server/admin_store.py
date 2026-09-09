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
from pathlib import Path
from typing import Any, Callable, Optional

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _base_dir() -> Path:
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
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError:
        pass


def list_users() -> list[dict[str, Any]]:
    """返回用户列表，含会员态与禁用标记。"""
    from user_membership import get_user_store
    data = _load_users()
    out = []
    for u in data.get("users", []):
        uid = u.get("user_id")
        rec: dict[str, Any] = {
            "user_id": uid,
            "identifier": u.get("identifier"),
            "created_at": u.get("created_at"),
            "disabled": bool(u.get("disabled", False)),
            "is_admin": bool(u.get("is_admin", False)),
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
    data = _load_users()
    user = next((u for u in data["users"] if u["user_id"] == user_id), None)
    if not user:
        return {"ok": False, "error": "用户不存在"}
    user["disabled"] = bool(disabled)
    user["updated_at"] = int(time.time())
    _save_users(data)
    return {"ok": True, "disabled": bool(disabled)}


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
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"accounts": cleaned}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
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
