#!/usr/bin/env python3
"""后台权限拆两级（超管 / 管理员）的离线回归 —— 2026-10-22。

需求原文：
  「当前账号改为超级管理员，赋予其他账号的是管理员，管理员权限可以被超级管理员取消」
  「被设为管理员这里要同步为管理员，超管永远只有一个，管理员能看到后台，但是不能改数据」

三件事必须钉死，任何一件回退用户都能立刻感知：
  1. **超管唯一**：名单配了多个也只认第一个；名单为空时兜底「最早注册的 is_admin」。
  2. **标签正确**：其余 is_admin 全部是 admin（管理员），不再一律显示「超管」。
  3. **管理员不吃到超管权限**：role != super 的人调写接口必须 403（本文件验函数层，
     路由层的 require_super 由 test_admin_roles_api.py 覆盖）。

跑法：python3 server/tests/test_admin_roles.py
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


def make_env(users, admin_json=None, env_idents=None):
    """造一个隔离的数据目录并重新加载 auth_store（模块级缓存必须一并清掉）。"""
    d = tempfile.mkdtemp(prefix="vdl_roles_")
    (Path(d) / "users.json").write_text(
        json.dumps({"users": users}, ensure_ascii=False), encoding="utf-8")
    if admin_json is not None:
        (Path(d) / "admin.json").write_text(
            json.dumps(admin_json, ensure_ascii=False), encoding="utf-8")
    os.environ["VDL_DATA_DIR"] = d
    os.environ.pop("VDL_ADMIN_IDENTIFIER", None)
    if env_idents:
        os.environ["VDL_ADMIN_IDENTIFIER"] = env_idents
    import auth_store
    importlib.reload(auth_store)
    return auth_store, d


def user(uid, ident, created, is_admin):
    return {"user_id": uid, "identifier": ident, "created_at": created, "is_admin": is_admin}


print("【1】无任何名单：兜底取「最早注册的 is_admin」当超管（桌面端真实场景）")
# 与用户本机数据同构：1812804606 最早注册 → 超管；15638319956 后来被提权 → 管理员
au, d1 = make_env([
    user("u_early", "1812804606@qq.com", 1788916708, True),
    user("u_late", "15638319956", 1791102569, True),
    user("u_none", "15014313254", 1788918967, False),
])
ok("名单为空时也必须有超管（否则后台全变只读、谁也改不了）",
   au.super_admin_identifier() == "1812804606@qq.com", au.super_admin_identifier())
ok("最早注册的 is_admin 是 super", au.user_role("u_early") == "super", au.user_role("u_early"))
ok("较晚被提权的 is_admin 是 admin（不再显示成超管）",
   au.user_role("u_late") == "admin", au.user_role("u_late"))
ok("未提权账号角色为空", au.user_role("u_none") == "", au.user_role("u_none"))
ok("is_super_identifier 严格相等（大小写无关）",
   au.is_super_identifier("1812804606@QQ.COM") is True)
ok("空标识不算超管", au.is_super_identifier("") is False)
ok("首尾空格容错", au.is_super_identifier("  1812804606@qq.com ") is True)
shutil.rmtree(d1, ignore_errors=True)

print("\n【2】名单只有一个：只认名单，且与「谁最早注册」无关")
au, d2 = make_env(
    [user("u_a", "boss@x.com", 1700000000, True),
     user("u_b", "helper@x.com", 1600000000, True)],     # 注意：u_b 注册更早
    admin_json={"admin_identifiers": ["boss@x.com"]})
ok("名单内账号是 super（即使注册更晚）", au.user_role("u_a") == "super", au.user_role("u_a"))
ok("名单外 is_admin 是 admin", au.user_role("u_b") == "admin", au.user_role("u_b"))
shutil.rmtree(d2, ignore_errors=True)

print("\n【3】名单配了多个：只认第一个 —— 这是「超管永远只有一个」的硬保证")
au, d3 = make_env(
    [user("u_a", "first@x.com", 1600000000, True),
     user("u_b", "second@x.com", 1610000000, True),
     user("u_c", "third@x.com", 1620000000, True)],
    admin_json={"admin_identifiers": ["first@x.com", "second@x.com", "third@x.com"]})
ok("名单第一项是 super", au.user_role("u_a") == "super", au.user_role("u_a"))
ok("名单第二项降级为 admin（不允许多超管）", au.user_role("u_b") == "admin", au.user_role("u_b"))
ok("名单第三项降级为 admin", au.user_role("u_c") == "admin", au.user_role("u_c"))
ok("全库恰好 1 个 super", sum(au.user_role(u) == "super" for u in ("u_a", "u_b", "u_c")) == 1)
shutil.rmtree(d3, ignore_errors=True)

print("\n【4】环境变量名单优先于 admin.json（部署侧可强行指定）")
au, d4 = make_env(
    [user("u_a", "env@x.com", 1600000000, True),
     user("u_b", "file@x.com", 1610000000, True)],
    admin_json={"admin_identifiers": ["file@x.com"]},
    env_idents="env@x.com")
ok("VDL_ADMIN_IDENTIFIER 里的账号是 super", au.user_role("u_a") == "super", au.user_role("u_a"))
ok("admin.json 里那条降级为 admin", au.user_role("u_b") == "admin", au.user_role("u_b"))
shutil.rmtree(d4, ignore_errors=True)

print("\n【5】边界：全库没有 is_admin 时不崩（不能抛异常把后台打挂）")
au, d5 = make_env([user("u_a", "nobody@x.com", 1600000000, False)])
ok("无超管时返回空串", au.super_admin_identifier() == "", repr(au.super_admin_identifier()))
ok("无人是 super", au.user_role("u_a") == "", au.user_role("u_a"))
ok("未知 user_id 不抛异常", au.user_role("u_ghost") == "", repr(au.user_role("u_ghost")))
shutil.rmtree(d5, ignore_errors=True)

print(f"\n{'=' * 46}\nPASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
