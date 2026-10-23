#!/usr/bin/env python3
"""超管 / 管理员权限的**真实 HTTP 端到端**验证（2026-10-22）。

前两个测试文件验的是「谁算超管」和「闸门有没有装上」；本文件拉真正起 App 的
FastAPI 应用，用两个真实账号登录拿 token，打真接口，确认：

  超管   GET 200 / POST 200
  管理员 GET 200（能看后台）  POST 403（不能改数据）
  普通   GET 401

跑法：cd server && ../.build_venv/bin/python tests/test_admin_roles_e2e.py
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

D = tempfile.mkdtemp(prefix="vdl_roles_e2e_")
os.environ["VDL_DATA_DIR"] = D
os.environ["VDL_LOGIN_GATE"] = "0"
os.environ["VDL_PLANS_CLOUD"] = "0"
os.environ["VDL_CLOUD_LINK"] = "0"
os.environ.pop("VDL_ADMIN_IDENTIFIER", None)      # 走「最早注册」兜底，贴近桌面端实况

PASS = 0
FAIL = 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}   {extra}")


import auth_store                                    # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

print("【0】造账号：boss 最早注册（→超管），helper 后被提权（→管理员）")
uid_super = auth_store.create_user("boss@example.test", "pw-123456")
uid_admin = auth_store.create_user("helper@example.test", "pw-123456")
uid_plain = auth_store.create_user("plain@example.test", "pw-123456")
auth_store.set_user_admin(uid_super, True)
auth_store.set_user_admin(uid_admin, True)
importlib.reload(auth_store)      # 清掉模块级缓存，让超管判定读到最新 users.json
ok("boss 被判为 super", auth_store.user_role(uid_super) == "super", auth_store.user_role(uid_super))
ok("helper 被判为 admin", auth_store.user_role(uid_admin) == "admin", auth_store.user_role(uid_admin))
ok("plain 角色为空", auth_store.user_role(uid_plain) == "", auth_store.user_role(uid_plain))

import app                                            # noqa: E402
import routers.admin as adm                           # noqa: E402
importlib.reload(adm)
client = TestClient(app.app)


def login(ident, pw="pw-123456"):
    r = client.post("/api/auth/login", json={"identifier": ident, "password": pw})
    j = r.json()
    return j.get("token"), j


t_super, j_super = login("boss@example.test")
t_admin, j_admin = login("helper@example.test")
t_plain, _ = login("plain@example.test")
ok("登录响应带 role 字段", "role" in j_super, list(j_super)[:8])
ok("超管登录 role=super", j_super.get("role") == "super", j_super.get("role"))
ok("管理员登录 role=admin", j_admin.get("role") == "admin", j_admin.get("role"))

H = lambda t: {"Authorization": "Bearer " + t} if t else {}


def get(path, tok):
    return client.get(path, headers=H(tok))


def post(path, tok, payload=None):
    return client.post(path, headers=H(tok), json=payload or {})


print("\n【1】只读接口：超管和管理员都能看后台")
for who, tok in (("超管", t_super), ("管理员", t_admin)):
    r = get("/api/admin/users", tok)
    ok(f"{who} GET /api/admin/users → 200", r.status_code == 200, r.status_code)
    if r.status_code == 200:
        rows = r.json().get("users") or []
        roles = {str(x.get("identifier")): x.get("role") for x in rows}
        if who == "超管":
            ok("列表里 boss 的 role=super", roles.get("boss@example.test") == "super", roles)
            ok("列表里 helper 的 role=admin", roles.get("helper@example.test") == "admin", roles)

print("\n【2】写接口：管理员一律 403，且不是 401")
r = post(f"/api/admin/users/{uid_plain}/disable", t_admin)
ok("管理员禁用他人 → 403", r.status_code == 403, r.status_code)
ok("403 文案含「只读」", "只读" in json.dumps(r.json(), ensure_ascii=False), r.text[:120])

r = post(f"/api/admin/users/{uid_plain}/set-admin", t_admin, {"is_admin": True})
ok("管理员给他人提权 → 403（原来这里是个越权后门）", r.status_code == 403, r.status_code)

r = post("/api/admin/config/smtp", t_admin, {"accounts": []})
ok("管理员改 SMTP 配置 → 403", r.status_code == 403, r.status_code)

r = post("/api/admin/users/%s/reset-password" % uid_plain, t_admin, {})
ok("管理员重置他人密码 → 403", r.status_code == 403, r.status_code)

r = post("/api/admin/cookies/delete", t_admin, {"id": "x"})
ok("管理员删 Cookie 池条目 → 403", r.status_code == 403, r.status_code)

print("\n【3】普通用户连后台都进不去")
r = get("/api/admin/users", t_plain)
ok("普通用户 GET 后台接口 → 401", r.status_code == 401, r.status_code)
r = post("/api/admin/config/smtp", t_plain, {"accounts": []})
ok("普通用户 POST 后台接口 → 401", r.status_code == 401, r.status_code)

print("\n【4】超管的写权限必须完好（别把能改的人也拦了）")
r = post(f"/api/admin/users/{uid_plain}/set-admin", t_super, {"is_admin": True})
ok("超管给他人的提权请求没被 403", r.status_code != 403, r.status_code)
body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
ok("超管提权业务成功（ok=True）", body.get("ok") is True, str(body)[:160])
importlib.reload(auth_store)
ok("被提权者变成 admin（不是 super）", auth_store.user_role(uid_plain) == "admin",
   auth_store.user_role(uid_plain))

r = post(f"/api/admin/users/{uid_plain}/set-admin", t_super, {"is_admin": False})
ok("超管可以取消管理员（用户明确要求）",
   (r.json() or {}).get("ok") is True, r.text[:160])
importlib.reload(auth_store)
ok("取消后角色归零", auth_store.user_role(uid_plain) == "", auth_store.user_role(uid_plain))

print("\n【5】超管自己不可被取消（否则后台永久锁死）")
r = post(f"/api/admin/users/{uid_super}/set-admin", t_super, {"is_admin": False})
body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
ok("拒绝取消超管", body.get("ok") is not True, str(body)[:160])
ok("提示写明「超级管理员不可被取消」",
   "超级管理员不可被取消" in json.dumps(body, ensure_ascii=False), str(body)[:160])

shutil.rmtree(D, ignore_errors=True)
print(f"\n{'=' * 46}\nPASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
