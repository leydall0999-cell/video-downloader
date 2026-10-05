# -*- coding: utf-8 -*-
"""跨端共享「首次免费」名额集成测试：网页版 store 与桌面端 store 共用同一份
内存版授权中心，证明「同一账号在任一端领过一次后，另一端（哪怕点不同功能）也领不到」。

不碰真实 ECS、不起真实 HTTP；用 license_server.trial_claim_impl 作共享后端，
两端 membership 模块的 trial_consume 都打到它，最贴近真实跨端链路。
"""
import sys, os, types, importlib.util, tempfile, time

WEB_DIR = "/Users/suixindelang/WorkBuddy/video-downloader/server"
APP_DIR = "/Users/suixindelang/WorkBuddy/video-downloader-app/server"
LS_FILE = "/tmp/vdl_license/license_server.py"

# ── 共享内存版授权中心（真实 impl）──
os.environ["VDL_LICENSE_SECRET"] = "testsecret"
_ls_spec = importlib.util.spec_from_file_location("ls_server_mod", LS_FILE)
ls = importlib.util.module_from_spec(_ls_spec)
_ls_spec.loader.exec_module(ls)
SECRET = ls.SECRET
_SHARED_STATE = {"users": {}}

class LicenseCloudError(Exception):
    pass

def _claim_real(token, op, mode="once", base_url=None, timeout=8.0, opener=None):
    try:
        return ls.trial_claim_impl(_SHARED_STATE, token, op, mode, time.time(), SECRET)
    except Exception as e:
        raise LicenseCloudError(str(e))

def _claim_offline(token, op, mode="once", base_url=None, timeout=8.0, opener=None):
    raise LicenseCloudError("offline")

# 两端共用同一个 fake license_client
fake_lc = types.ModuleType("license_client")
fake_lc.trial_claim_remote = _claim_real
fake_lc.license_base = lambda: "http://x"
fake_lc.LicenseCloudError = LicenseCloudError
sys.modules["license_client"] = fake_lc

# 网页端用的 cloud_link（link_enabled 必须 True 才会去打授权中心）
fake_cl = types.ModuleType("cloud_link")
fake_cl.link_enabled = lambda: True
sys.modules["cloud_link"] = fake_cl

# 桌面端 membership 顶层 import atomic_io
sys.path.insert(0, APP_DIR)

def load_mm(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m

web_m = load_mm("web_mm", os.path.join(WEB_DIR, "membership.py"))
app_m = load_mm("app_mm", os.path.join(APP_DIR, "membership.py"))

def new_account(email):
    r = ls.register_impl(_SHARED_STATE, email, "pw12345678", time.time(), SECRET,
                         {"fp": "fp", "name": "d"})
    return r["token"]

def make_stores(token, email):
    ws = web_m.MembershipStore(path=__import__("pathlib").Path(tempfile.mkdtemp()) / "m.json")
    as_ = app_m.MembershipStore(path=__import__("pathlib").Path(tempfile.mkdtemp()) / "m.json")
    ws._ensure_loaded(); as_._ensure_loaded()
    ws.cloud_session = lambda: {"token": token}          # 网页端 token 来源
    as_._state.setdefault("meta", {})["account"] = {"token": token, "email": email}  # 桌面端 token 来源
    return ws, as_

FAILS = []
def check(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ((" — " + extra) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)

# ═══════ 1) 网页端先领 → 桌面端（不同功能）应被拒（once=账号级）═══════
print("\n[1] 网页端 commentary_llm 先领，桌面端 dewatermark_ai 应被拒")
tok = new_account("xacct@example.com")
ws, as_ = make_stores(tok, "xacct@example.com")
r1 = web_m.gate_message(ws, "commentary_llm")
check("网页端首次领取放行", r1 is None, f"got {r1!r}")
check("网页端本地标记已用", ws.trial_used("commentary_llm", web_m.free_trial_policy()))
r2 = app_m.gate_message(as_, "dewatermark_ai")
check("桌面端跨端被拒(不同功能/once)", isinstance(r2, str) and "已用过一次免费体验" in r2,
      f"got {r2!r}")
check("桌面端本地同步标记已用", as_.trial_used("dewatermark_ai", app_m.free_trial_policy()))

# ═══════ 2) 反向：桌面端先领 → 网页端应被拒 ═══════
print("\n[2] 桌面端 subtitle_asr 先领，网页端 matting_cloud 应被拒")
tok2 = new_account("yacct@example.com")
ws2, as2 = make_stores(tok2, "yacct@example.com")
r3 = app_m.gate_message(as2, "subtitle_asr")
check("桌面端首次领取放行", r3 is None, f"got {r3!r}")
r4 = web_m.gate_message(ws2, "matting_cloud")
check("网页端跨端被拒(不同功能/once)", isinstance(r4, str) and "已用过一次免费体验" in r4,
      f"got {r4!r}")

# ═══════ 3) 离线兜底：授权中心不可达 → 两端都 fail-open 放行（不惩罚免费用户）═══════
print("\n[3] 授权中心离线 → 离线兜底放行")
fake_lc.trial_claim_remote = _claim_offline
tok3 = new_account("zacct@example.com")
ws3, as3 = make_stores(tok3, "zacct@example.com")
r5 = web_m.gate_message(ws3, "commentary_llm")
check("离线·网页端兜底放行", r5 is None, f"got {r5!r}")
check("离线·网页端本地标记已用", ws3.trial_used("commentary_llm", web_m.free_trial_policy()))
r6 = app_m.gate_message(as3, "dewatermark_ai")
check("离线·桌面端兜底放行", r6 is None, f"got {r6!r}")
check("离线·桌面端本地标记已用", as3.trial_used("dewatermark_ai", app_m.free_trial_policy()))
fake_lc.trial_claim_remote = _claim_real

# ═══════ 4) 变异：若 trial_consume 没真的去授权中心领，[1] 的跨端拒绝就会失效 ═══════
print("\n[4] 变异校验：断掉跨端领取后，[1] 的拒绝必须消失（证明链路真实生效）")
fake_lc.trial_claim_remote = lambda *a, **k: {"ok": True, "claimed": True, "already": False}
tok4 = new_account("wacct@example.com")
ws4, as4 = make_stores(tok4, "wacct@example.com")
web_m.gate_message(ws4, "commentary_llm")
r4b = app_m.gate_message(as4, "dewatermark_ai")
check("变异：断领后桌面端不再被拒（反向证明[1]有效）", r4b is None, f"got {r4b!r}")
fake_lc.trial_claim_remote = _claim_real

print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项未通过：")
    for f in FAILS:
        print("   ·", f)
    sys.exit(1)
print("✅ 跨端共享首次免费 · 全部通过")
