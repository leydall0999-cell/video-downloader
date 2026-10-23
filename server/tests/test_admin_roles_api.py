#!/usr/bin/env python3
"""后台写接口的超管闸门 —— 路由层回归（2026-10-22）。

test_admin_roles.py 验的是「谁算超管」；本文件验「闸门有没有真的装上」：
  · 管理员（admin）调写接口 → 403，且**不是** 401（身份没问题，是权限不够）
  · 超管（super）→ 放行
  · 非 admin → 401（连后台都不该进）
  · 静态扫描：每一个 @router.post/delete 都必须用 require_super，
    **不允许**残留 require_admin —— 漏一个就等于给管理员开了个后门。

跑法：python3 server/tests/test_admin_roles_api.py
"""
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def ok(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {extra}")


def make_env(users, admin_json=None):
    d = tempfile.mkdtemp(prefix="vdl_rolesapi_")
    (Path(d) / "users.json").write_text(
        json.dumps({"users": users}, ensure_ascii=False), encoding="utf-8")
    if admin_json is not None:
        (Path(d) / "admin.json").write_text(
            json.dumps(admin_json, ensure_ascii=False), encoding="utf-8")
    os.environ["VDL_DATA_DIR"] = d
    os.environ.pop("VDL_ADMIN_IDENTIFIER", None)
    return d


def u(uid, ident, created, is_admin):
    return {"user_id": uid, "identifier": ident, "created_at": created, "is_admin": is_admin}


D = make_env([
    u("u_super", "1812804606@qq.com", 1788916708, True),
    u("u_admin", "15638319956", 1791102569, True),
    u("u_plain", "15014313254", 1788918967, False),
])
import auth_store                                    # noqa: E402
importlib.reload(auth_store)
import routers.admin as adm                          # noqa: E402
importlib.reload(auth_store)          # admin 内部是延迟 import，这里再保证一次新鲜

from fastapi import HTTPException                    # noqa: E402


def _call_guard(who: str):
    """把「当前登录者」打桩成 who，再跑一次 require_super。"""
    orig = adm.get_current_user_id
    adm.get_current_user_id = lambda _req=None: who
    try:
        adm.require_super(object())
        return None
    except HTTPException as e:
        return e
    finally:
        adm.get_current_user_id = orig


print("【1】require_super 的三种身份")
e = _call_guard("u_super")
ok("超管放行（无异常）", e is None, repr(e))
e = _call_guard("u_admin")
ok("管理员被拦", e is not None and e.status_code == 403, getattr(e, "status_code", None))
ok("管理员收到的是 403 而不是 401（不能把已登录的人踢回登录页）",
   getattr(e, "status_code", None) == 403)
ok("403 文案说清了「只读」", "只读" in str(getattr(e, "detail", "")), getattr(e, "detail", ""))
e = _call_guard("u_plain")
ok("普通用户 401", getattr(e, "status_code", None) == 401, getattr(e, "status_code", None))
e = _call_guard("")
ok("未登录 401", getattr(e, "status_code", None) == 401, getattr(e, "status_code", None))

print("\n【2】静态扫描：写接口一个都不能漏（admin.py + admin_cookies.py）")
WRITE = re.compile(r"@router\.(post|put|patch|delete)\(")


# 机器对机器令牌鉴权的接口不算用户权限接口：/api/cookie/pool-admin 用
# VDL_COOKIE_SYNC_TOKEN（供桌面 App 后台代理云端池），本就没有用户身份。
_TOKEN_AUTH_ROUTES = ("/api/cookie/pool-admin",)


def scan(fname: str):
    """返回 (写接口数, 只读接口数, 漏闸门的写路由行号, 误用 super 的读路由行号)。

    ⚠️ 关键：**没找到任何闸门**的路由也必须记 bad —— 否则「删掉 require_super
    那一行」这种回退会被静默放过，等于给管理员开后门还测不出来。
    """
    lines = (ROOT / "routers" / fname).read_text(encoding="utf-8").split("\n")
    w = r = 0
    bad_w, bad_r = [], []
    pending = None      # (kind, 行号, 路由原文)

    def close(kind, line_no):
        if pending is None:
            return
        if pending[0] == "write":
            bad_w.append(pending[1])
        else:
            bad_r.append(pending[1])

    for i, line in enumerate(lines, 1):
        is_write = bool(WRITE.match(line))
        is_read = bool(re.match(r"@router\.get\(", line))
        if is_write or is_read:
            # 上一个路由已结束但从未见到闸门 ⇒ 漏了
            if pending is not None:
                close(pending[0], pending[1])
            route = line
            if any(t in route for t in _TOKEN_AUTH_ROUTES):
                pending = None          # 令牌鉴权接口，跳过
            else:
                pending = ("write", i, route) if is_write else ("read", i, route)
            if is_write:
                w += 1
            else:
                r += 1
            continue
        if pending and "require_super(request)" in line:
            if pending[0] != "write":
                bad_r.append(pending[1])
            pending = None
            continue
        if pending and "require_admin(request)" in line:
            if pending[0] == "write":
                bad_w.append(pending[1])
            pending = None
            continue
    if pending is not None:             # 文件末尾未闭合
        close(pending[0], pending[1])
    return w, r, bad_w, bad_r


w1, r1, bw1, br1 = scan("admin.py")
ok(f"admin.py：写接口 {w1} 个全部用 require_super", not bw1, str(bw1))
ok(f"admin.py：只读接口 {r1} 个仍用 require_admin（管理员能看）", not br1, str(br1))
ok("admin.py 写接口数与改造基线一致（新增写接口请同步本基线）", w1 == 16, w1)

w2, r2, bw2, br2 = scan("admin_cookies.py")
ok(f"admin_cookies.py：写接口 {w2} 个全部有闸门（3 个超管 + 1 个令牌鉴权）",
   not bw2 and w2 == 4, f"w={w2} bad={bw2}")
ok("admin_cookies.py：只读接口仍用 require_admin（管理员能看池概况）", not br2, str(br2))

print("\n【3】单一真源：超管判定只允许在 auth_store 里实现一次")
# 用「装饰器后第一处闸门」判定，而不是全文 grep —— 否则 GET 的 require_admin 会误报
raw = (ROOT / "routers" / "admin.py").read_text(encoding="utf-8")
ok("admin.py 复用 auth_store.is_super_identifier，不自己实现一套名单判定",
   "is_super_identifier" in raw, "未找到对 is_super_identifier 的引用")
ok("admin.py 不自己实现名单读取（名单只有 auth_store._admin_identifiers 一处）",
   "def _admin_identifiers" not in raw and "_admin_identifiers(" not in raw,
   "发现第二处名单逻辑")

print("\n【4】set-admin 的自我保护")
setadmin = raw.split("def admin_set_admin")[1].split("\n@router.")[0] if "def admin_set_admin" in raw else ""
ok("拒绝降级超管（否则全库无超管 ⇒ 后台永久锁死）",
   "is_super_identifier" in setadmin, setadmin[:120])
ok("拒绝取消自己", "不能取消当前登录账号的管理员权限" in setadmin)
ok("已改用 require_super", "require_super(request)" in setadmin)

shutil.rmtree(D, ignore_errors=True)
print(f"\n{'=' * 46}\nPASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
