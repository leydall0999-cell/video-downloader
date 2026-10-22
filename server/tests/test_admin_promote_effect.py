#!/usr/bin/env python3
"""后台提权「点了没反应」的根因回归（2026-10-22）。

用户实测现象
------------
1. 桌面 App 后台点「设为管理员」⇒ **界面毫无反应**（既没成功提示也没报错），
   列表里该账号仍是「普通用户」。
2. 该账号随后在 App 里登录 ⇒ 个人中心显示「普通用户」；后台的运维监控 /
   每日对账 / 解析需求 / Cookie 池 全部 401；写操作报「需要管理员权限」。

真因（三层叠加，缺一不可）
--------------------------
① `_resolve_identifier` 解析不出**手机号**账号的标识：
   入参是云端 uid（手机号 "15638319956"），旧实现只认「含 @ 的字符串」，
   其余一律当本机 user_id 去查表 —— 本机 user_id 是 u_xxx ⇒ 查不到 ⇒ 返回空。
② 于是 `_cloud_sync("")` **直接跳过云端**（notice「未找到对应账号标识，本次仅本机
   生效」）⇒ 本机改了、**云端没改**；而面板列表读的是**云端**数据 ⇒ 看起来没反应。
③ `_sync_cloud_is_admin` 把云端 is_admin 当权威回写本机（登录/云端同步时触发）
   ⇒ 云端一直是 False ⇒ 本机每次都被打回 False ⇒ **提权永远不生效**。

本文件钉死
----------
A. `_resolve_identifier` 对手机号/非邮箱标识必须解析出标识（不能返回空）。
B. 提权请求必须真的把 **云端** is_admin 置上（不能只改本机）。
C. 权限文案两级口径：读接口「需要管理员权限」；写接口「需要超级管理员权限」。

跑法：cd server && ../.build_venv/bin/python tests/test_admin_promote_effect.py
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


D = tempfile.mkdtemp(prefix="vdl_promote_")
os.environ["VDL_DATA_DIR"] = D
os.environ.pop("VDL_ADMIN_IDENTIFIER", None)

import auth_store                                    # noqa: E402
importlib.reload(auth_store)
import routers.admin as adm                         # noqa: E402

# 造两个账号：超管是邮箱，普通账号是**手机号**（这正是 2026-10-22 出问题的形态）
uid_super = auth_store.create_user("1812804606@qq.com", "pw-123456")
uid_phone = auth_store.create_user("15638319956", "pw-123456")
auth_store.set_user_admin(uid_super, True)
importlib.reload(auth_store)
ok("超管已建", auth_store.user_is_admin(uid_super))
ok("手机号账号已建且非管理员", auth_store.user_is_admin(uid_phone) is False)

print("\n【A】_resolve_identifier：手机号账号必须解析出标识（不能返回空）")
ok("手机号标识原样返回", adm._resolve_identifier("15638319956") == "15638319956",
   repr(adm._resolve_identifier("15638319956")))
ok("邮箱标识小写返回", adm._resolve_identifier("Boss@Example.COM") == "boss@example.com",
   repr(adm._resolve_identifier("Boss@Example.COM")))
ok("本机 user_id 能反查标识",
   adm._resolve_identifier(uid_phone) == "15638319956",
   repr(adm._resolve_identifier(uid_phone)))
ok("空串返回空", adm._resolve_identifier("") == "")
ok("已删除的本机账号（u_ 前缀查不到）返回空",
   adm._resolve_identifier("u_not_exists_xxxx") == "",
   repr(adm._resolve_identifier("u_not_exists_xxxx")))

print("\n【B】提权必须真的落到云端（不能只改本机）")
# 打桩云端同步，验证「提权请求带着正确的标识发出去」
calls = []


def _fake_cloud_sync(ident, op):
    calls.append(ident)
    return ""


orig_sync = adm._cloud_sync
orig_uid = adm.get_current_user_id
adm._cloud_sync = _fake_cloud_sync
adm.get_current_user_id = lambda _req=None: uid_super   # 以超管身份操作
try:
    # 面板行的 user_id 就是云端 uid（手机号）—— 旧实现在这里会得到空标识
    r = adm.admin_set_admin("15638319956", {"is_admin": True}, request=object())
finally:
    adm._cloud_sync = orig_sync
    adm.get_current_user_id = orig_uid
ok("提权接口返回 ok", (r or {}).get("ok") is True, str(r)[:200])
ok("本机 is_admin 已置上", auth_store.user_is_admin(uid_phone) is True)
ok("云端同步确实被调用", len(calls) == 1, str(calls))
ok("云端收到的是**账号标识**而不是空串",
   calls and calls[0] == "15638319956", str(calls))
ok("云端同步失败不会让本机改动回滚（真源在云端，本机是离线兜底）",
   (r or {}).get("ok") is True, str(r)[:160])

print("\n【C】权限文案两级口径")
admin_src = (ROOT / "routers" / "admin.py").read_text(encoding="utf-8")
ok("读接口闸门说「需要管理员权限」", "未授权：需要管理员权限" in admin_src)
ok("写接口闸门说「需要超级管理员权限」", "未授权：需要超级管理员权限" in admin_src)
ok("旧的「需要超级用户权限」已不存在（两级了，别再混用）",
   "需要超级用户权限" not in admin_src)
ok("管理员写操作是 403（只读）不是 401",
   "仅超级管理员可执行此操作（管理员为只读）" in admin_src)

print("\n【D】运维看板闸门文案：它判的是 is_admin，管理员本就该能看")
core_src = (ROOT / "routers" / "core.py").read_text(encoding="utf-8")
ok("不再写「仅超级管理员账号登录后可用」",
   "仅超级管理员账号登录后可用" not in core_src)
ok("改为「仅管理员账号登录后可用」",
   "仅管理员账号登录后可用" in core_src)

shutil.rmtree(D, ignore_errors=True)
print(f"\n{'=' * 46}\nPASS={PASS}  FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
