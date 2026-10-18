"""会员中心公告（member notice）离线守卫。

覆盖：
  1. 云端关闭（VDL_PLANS_CLOUD=0）/ 未配置地址 ⇒ **空公告**，绝不凭空造文案；
  2. 云端返回公告的解析：正常 / enabled=False / 空文本 / 前后空白 / 脏缓存；
  3. plans() 出口必带 notice 键 —— 会员中心只读一个接口就能拿到公告；
  4. 下发函数在无地址 / 无令牌时返回 ok=False 且**不抛**（云端不可达不能拖垮后台保存）；
  5. 源码棘轮：授权中心 member_notice_set + plans_payload.notice；两端 plans() 带
     notice；前端用 textContent 渲染（公告文案不当 HTML 解释）。

运行：python tests/test_member_notice.py   （无需网络，不写真实家目录）
"""

import os
import sys
import tempfile
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
REPO = SERVER.parent
sys.path.insert(0, str(SERVER))

# 隔离：所有落盘都在临时目录；且默认关闭云端（避免测试触网）
_TMP = tempfile.mkdtemp(prefix="vdl_notice_test_")
os.environ["VDL_DATA_DIR"] = _TMP
os.environ["VDL_PLANS_CLOUD"] = "0"
os.environ.pop("VDL_LICENSE_BASE", None)

import membership as M  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


print("[1] 云端关闭 / 未配置 ⇒ 空公告")
n = M.cloud_member_notice()
check("VDL_PLANS_CLOUD=0 返回空公告", n == {"enabled": False, "text": ""}, n)
check("默认值不硬编码任何文案", "下方价格" not in str(n))

print("[2] 云端返回公告的解析（桩掉网络）")
_orig_api = M._license_api
_orig_cpo = M.cloud_plan_overrides
M._license_api = lambda path: "http://stub.invalid/api/license/plans"
M.cloud_plan_overrides = lambda *a, **k: {}
os.environ["VDL_PLANS_CLOUD"] = "1"

M._CLOUD_PLANS_CACHE["notice"] = {"enabled": True, "text": "下方价格为测试期间优惠价格"}
n = M.cloud_member_notice()
check("正常公告解析正确",
      n == {"enabled": True, "text": "下方价格为测试期间优惠价格"}, n)

M._CLOUD_PLANS_CACHE["notice"] = {"enabled": True, "text": "  两侧空白要裁掉  "}
check("文本前后空白被裁掉", M.cloud_member_notice()["text"] == "两侧空白要裁掉",
      M.cloud_member_notice())

M._CLOUD_PLANS_CACHE["notice"] = {"enabled": False, "text": "有文本但开关关着"}
n = M.cloud_member_notice()
check("enabled=False 时不展示", n["enabled"] is False and n["text"] == "有文本但开关关着", n)

M._CLOUD_PLANS_CACHE["notice"] = {"enabled": True, "text": "   "}
n = M.cloud_member_notice()
check("空文本 ⇒ enabled=False（不留空公告条）", n == {"enabled": False, "text": ""}, n)

M._CLOUD_PLANS_CACHE["notice"] = "脏数据"
check("脏缓存不炸且返回空公告", M.cloud_member_notice() == {"enabled": False, "text": ""})

print("[3] plans() 出口带 notice")
M._CLOUD_PLANS_CACHE["notice"] = {"enabled": True, "text": "下方价格为测试期间优惠价格"}
st = M.MembershipStore(path=Path(_TMP) / "membership.json")
pl = st.plans()
check("plans() 含 notice 键", "notice" in pl)
check("plans().notice 内容正确",
      pl.get("notice") == {"enabled": True, "text": "下方价格为测试期间优惠价格"},
      pl.get("notice"))
check("plans() 原有字段未受影响",
      "download_member" in pl and "ai_member" in pl and "credit_packs" in pl)

print("[4] 下发函数：失败不抛、原因可读")
M._license_api = lambda path: ""
out = M.push_member_notice_to_cloud(True, "x")
check("无授权中心地址 ⇒ no_license_base",
      out.get("ok") is False and out.get("reason") == "no_license_base", out)

M._license_api = lambda path: "http://stub.invalid/api/license/member_notice_set"
import admin_store  # noqa: E402
_orig_tok = admin_store._license_admin_token
admin_store._license_admin_token = lambda: ""
out = M.push_member_notice_to_cloud(True, "x")
check("无管理员令牌 ⇒ no_admin_token",
      out.get("ok") is False and out.get("reason") == "no_admin_token", out)

M._license_api = lambda path: "http://127.0.0.1:1/api/license/member_notice_set"
admin_store._license_admin_token = lambda: "fake-token"
out = M.push_member_notice_to_cloud(True, "x")
check("云端不可达 ⇒ 返回 ok=False（不抛异常）", out.get("ok") is False, out)
check("失败原因以 error: 开头，后台可展示",
      str(out.get("reason") or "").startswith("error:"), out)

# 还原桩
admin_store._license_admin_token = _orig_tok
M._license_api = _orig_api
M.cloud_plan_overrides = _orig_cpo
os.environ["VDL_PLANS_CLOUD"] = "0"

print("[5] 源码棘轮")
mem_src = (SERVER / "membership.py").read_text(encoding="utf-8")
check("membership 有 cloud_member_notice()", "def cloud_member_notice(" in mem_src)
check("membership 有 push_member_notice_to_cloud()",
      "def push_member_notice_to_cloud(" in mem_src)
check("plans() 出口调用 cloud_member_notice()", '"notice": cloud_member_notice(),' in mem_src)
check("云端拉取时缓存 notice", '_CLOUD_PLANS_CACHE["notice"]' in mem_src)

app_js = (REPO / "web" / "app.js").read_text(encoding="utf-8")
check("前端渲染公告用 textContent（不解释 HTML）",
      "txt.textContent = text;" in app_js)
check("前端不再对公告用 innerHTML",
      "memberNoticeText'].innerHTML" not in app_js)
check("套餐渲染处调用公告渲染",
      "_renderMemberNotice(p.notice)" in app_js or "_renderMemNotice(_memPlans.notice)" in app_js)

idx = (REPO / "web" / "index.html").read_text(encoding="utf-8")
check("会员中心有公告容器", 'id="memberNotice"' in idx or 'id="memNotice"' in idx)

lic = REPO / "deploy" / "license_server.py"
if lic.exists():
    lic_src = lic.read_text(encoding="utf-8")
    check("授权中心 plans_payload 下发 notice", '"notice": _member_notice(st)' in lic_src)
    check("授权中心有 member_notice_set 动作", '"member_notice_set"' in lic_src)
    check("授权中心空文案启用时拒绝（BAD_NOTICE）", "BAD_NOTICE" in lic_src)
else:
    print("  （本仓无 deploy/license_server.py，跳过授权中心源码守卫）")

ops = REPO / "web" / "ops" / "index.html"
if ops.exists() and "loadResolveDemand" in ops.read_text(encoding="utf-8"):
    ops_src = ops.read_text(encoding="utf-8")
    check("运维控制台有公告编辑入口", "/api/admin/member-notice" in ops_src)
    check("运维控制台公告输入框存在", 'id="noticeText"' in ops_src)
else:
    # 桌面仓里的 web/ops/index.html 是早期旧副本（不含解析需求榜那批面板），
    # 真正在跑的是 web-dev 的运维控制台；旧副本不参与产品，跳过不判缺陷。
    print("  （本仓无在线运维控制台，跳过该守卫）")

print()
if FAILS:
    print(f"❌ 失败 {len(FAILS)} 项：" + "；".join(FAILS))
    sys.exit(1)
print("✅ 会员中心公告守卫全部通过")
