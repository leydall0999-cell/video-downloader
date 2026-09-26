"""web 版账号接授权中心（「打通 web 与 App 用户数据」）回归测试 —— 全程离线。

被钉住的四条硬要求（2026-09-26）：
  1. 注册/登录**先打云端**（账号权威在授权中心），成功后本机落同号镜像；
  2. **云端有、本机没有**的账号（在 App 注册的）能在网页版登录 —— 这就是「打通」的判据；
  3. 会员/积分以云端 `authority` 快照为准（覆盖本地：本地手改数字一同步就被回滚）；
  4. 改密 / 积分扣减会同步云端（否则两端密码分叉、网页版花掉的积分被「涨回来」）。

不碰网络：`license_client` 的 register / login / heartbeat / password / spend 全部
替换为内存假实现（真模块的属性替换，与 cloud_link 的调用点一致）。

运行：cd server && python tests/test_cloud_link.py
"""
import os
import sys
import tempfile
import time

# ⚠️ 必须在 import app / auth_store 之前把 HOME 指到临时目录：
#    ~/.video-downloader 下的账号库、会员库都按 Path.home() 现算，避免污染真机数据。
_TMP_HOME = tempfile.mkdtemp(prefix="vdl-cloudlink-home-")
os.environ["HOME"] = _TMP_HOME
os.environ["VDL_CLOUD_LINK"] = "1"
os.environ.pop("VDL_OFFLINE_TESTS", None)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import license_client  # noqa: E402
import cloud_link  # noqa: E402
import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

NOW = time.time()


def _acct(email, fp="", name="", **auth_over):
    auth = {"v": 1, "member_until_dl": 0.0, "member_until_ai": 0.0,
            "perm_credits": 0, "ai_credits_left": 0, "banned": False}
    auth.update(auth_over)
    return {"user_id": email, "email": email, "max_devices": 2,
            "devices": [{"fp": fp, "name": name, "current": True}],
            "purchases": [], "authority": auth}


class FakeCloud:
    """内存版授权中心：只实现 cloud_link 会用到的那几个动作。"""

    def __init__(self):
        self.users = {}          # email -> password
        self.authority = {}      # email -> authority dict
        self.tokens = {}         # token -> email
        self.calls = []          # [(action, ...)]
        self.down = False        # True = 模拟云端不可达

    # ---- 安装/还原 ----
    def install(self):
        license_client.register_remote = self.register_remote
        license_client.login_remote = self.login_remote
        license_client.heartbeat_remote = self.heartbeat_remote
        license_client.set_password_remote = self.set_password_remote
        license_client.spend_remote = self.spend_remote

    # ---- 假实现 ----
    def _check(self):
        if self.down:
            raise license_client.LicenseCloudError("无法连接授权中心: 假离线")

    def seed(self, email, password, **auth_over):
        self.users[email] = password
        self.authority[email] = _acct(email, **auth_over)["authority"]

    def register_remote(self, email, password, fp, name="", **kw):
        self._check()
        self.calls.append(("register", email, fp))
        email = email.strip().lower()
        if email in self.users:
            return {"ok": False, "code": "EXISTS", "error": "该账号已存在，请直接登录"}
        self.seed(email, password)
        return self.login_remote(email, password, fp, name)

    def login_remote(self, email, password, fp, name="", **kw):
        self._check()
        self.calls.append(("login", email, fp))
        email = email.strip().lower()
        if email not in self.users:
            return {"ok": False, "code": "NO_ACCOUNT", "error": "账号不存在，请先注册"}
        if self.users[email] != password:
            return {"ok": False, "code": "BAD_PASSWORD", "error": "密码不正确"}
        token = f"cloud-{email}"
        self.tokens[token] = email
        acct = _acct(email, fp, name or "网页版")
        acct["authority"] = dict(self.authority.get(email) or acct["authority"])
        return {"ok": True, "token": token, "account": acct}

    def heartbeat_remote(self, token, fp, **kw):
        self._check()
        self.calls.append(("heartbeat", token, fp))
        email = self.tokens.get(token)
        if not email:
            return {"ok": False, "code": "DEVICE_EVICTED", "error": "已被其他设备挤出"}
        acct = _acct(email, fp, "网页版")
        acct["authority"] = dict(self.authority.get(email) or acct["authority"])
        return {"ok": True, "account": acct}

    def set_password_remote(self, email, new_password, old_password="", token="", **kw):
        self._check()
        self.calls.append(("password", email))
        email = email.strip().lower()
        if email not in self.users:
            return {"ok": True, "synced": False, "reason": "cloud_no_account"}
        if old_password and self.users[email] == old_password:
            self.users[email] = new_password
            return {"ok": True, "synced": True}
        if token and self.tokens.get(token) == email:
            self.users[email] = new_password
            return {"ok": True, "synced": True}
        return {"ok": False, "code": "BAD_CREDENTIALS", "error": "云端校验未通过"}

    def spend_remote(self, token, items, **kw):
        self._check()
        self.calls.append(("spend", tuple(sorted(i.get("id", "") for i in items))))
        email = self.tokens.get(token)
        if not email:
            return {"ok": False, "code": "BAD_TOKEN"}
        auth = self.authority.setdefault(email, {})
        for it in items or []:
            cost = int(it.get("cost") or 0)
            if it.get("pool") == "ai":
                auth["ai_credits_left"] = max(0, int(auth.get("ai_credits_left", 0)) - cost)
            else:
                auth["perm_credits"] = max(0, int(auth.get("perm_credits", 0)) - cost)
        return {"ok": True, "applied": len(items or []), "authority": auth}


def _client(fake: FakeCloud) -> TestClient:
    fake.install()
    return TestClient(server_app.app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_register_hits_cloud_and_mirrors_locally():
    fake = FakeCloud()
    c = _client(fake)
    r = c.post("/api/auth/register", json={"identifier": "new@self.test", "password": "pw123456"})
    j = r.json()
    assert j.get("ok"), j
    assert j.get("cloud_synced") is True, j
    assert "new@self.test" in fake.users, "云端必须已有该账号（权威在授权中心）"
    assert any(a == "register" for a, *_ in fake.calls), fake.calls
    # 本机镜像：本机账号表也必须能认出这个号（功能门禁/bearer 靠它）
    from auth_store import authenticate
    assert authenticate("new@self.test", "pw123456"), "本机镜像账号缺失"
    # 浏览器设备号 cookie（= 授权中心的 device.fp）
    assert cloud_link.COOKIE_NAME in r.cookies, "未签发设备号 cookie"
    print("✅ 注册：先打云端 + 本机落镜像 + 签发设备号 cookie")


def test_account_registered_in_app_can_login_on_web():
    """打通的核心判据：只在 App 注册过的账号（云端有、本机没有）能在网页版登录。"""
    fake = FakeCloud()
    fake.seed("app-user@self.test", "apppw123", member_until_ai=NOW + 86400,
              ai_credits_left=50)
    from auth_store import user_exists
    assert not user_exists("app-user@self.test"), "前置条件：本机不该有该账号"
    c = _client(fake)
    j = c.post("/api/auth/login", json={"identifier": "app-user@self.test",
                                       "password": "apppw123"}).json()
    assert j.get("ok"), j
    assert j.get("cloud_synced") is True, j
    assert user_exists("app-user@self.test"), "云端认了但本机没落镜像"
    me = c.get("/api/auth/me", headers=_auth(j["token"])).json()
    assert me.get("ok") and me.get("identifier") == "app-user@self.test", me
    assert me.get("account", {}).get("logged_in") is True, me
    print("✅ App 注册的账号能在网页版登录（云端权威 + 本机镜像）")


def test_wrong_password_rejected_and_public_error_is_vague():
    fake = FakeCloud()
    fake.seed("app-user@self.test", "apppw123")
    c = _client(fake)
    j = c.post("/api/auth/login", json={"identifier": "app-user@self.test",
                                       "password": "wrong-pw"}).json()
    assert not j.get("ok"), j
    assert "账号或密码错误" in str(j.get("error")), f"公网文案必须模糊（防账号枚举）: {j}"
    j2 = c.post("/api/auth/login", json={"identifier": "nobody@self.test",
                                         "password": "whatever"}).json()
    assert not j2.get("ok") and "账号或密码错误" in str(j2.get("error")), j2
    print("✅ 密码错/账号不存在：公网统一模糊文案（防枚举）")


def test_member_status_uses_cloud_authority_and_rolls_back_tamper():
    fake = FakeCloud()
    fake.seed("paid@self.test", "paidpw123", member_until_dl=NOW + 86400,
              member_until_ai=NOW + 86400, ai_credits_left=30, perm_credits=20)
    c = _client(fake)
    j = c.post("/api/auth/login", json={"identifier": "paid@self.test",
                                       "password": "paidpw123"}).json()
    assert j.get("ok"), j
    st = c.get("/api/member/status", headers=_auth(j["token"])).json()
    assert st["ai_member"]["active"] is True, st
    assert st["credits_total"] == 50, st
    # 本地手改（模拟篡改）→ 下一次状态查询必须被云端真值覆盖回来
    import user_membership
    store = user_membership.get_user_store(j["user_id"])
    store._ensure_loaded()
    store._state["permanent_credits"]["total"] = 999999
    store._persist()
    cloud_link._LAST_REFRESH.clear()          # 绕过 60s 节流，强制再同步一次
    st2 = c.get("/api/member/status", headers=_auth(j["token"])).json()
    assert st2["credits_total"] == 50, f"篡改未被云端覆盖: {st2['credits_total']}"
    assert st2["cloud"]["ok"] is True, st2.get("cloud")
    print("✅ 会员/积分以云端权威为准（本地篡改一同步即回滚）")


def test_change_password_syncs_to_cloud():
    fake = FakeCloud()
    fake.seed("pw@self.test", "oldpw123")
    c = _client(fake)
    j = c.post("/api/auth/login", json={"identifier": "pw@self.test",
                                       "password": "oldpw123"}).json()
    assert j.get("ok"), j
    r = c.post("/api/auth/change-password", headers=_auth(j["token"]),
               json={"current_password": "oldpw123", "new_password": "newpw456"}).json()
    assert r.get("ok") and r.get("cloud_synced") is True, r
    assert fake.users["pw@self.test"] == "newpw456", "云端密码没跟上（两端会分叉）"
    print("✅ 改密：本机改了，云端同步跟上")


def test_spend_reports_to_cloud():
    fake = FakeCloud()
    fake.seed("spender@self.test", "spendpw1", member_until_ai=NOW + 86400,
              ai_credits_left=40, perm_credits=10)
    c = _client(fake)
    j = c.post("/api/auth/login", json={"identifier": "spender@self.test",
                                       "password": "spendpw1"}).json()
    assert j.get("ok"), j
    # 先让云端权威落地本机（否则本地没积分可花）
    st = c.get("/api/member/status", headers=_auth(j["token"])).json()
    assert st["credits_total"] == 50, st
    import user_membership
    store = user_membership.get_user_store(j["user_id"])
    res = store.spend_credits(15, reason="offline_test")
    assert res.get("ok"), res
    for _ in range(150):                       # 上报是后台线程，轮询等结果
        if any(x[0] == "spend" for x in fake.calls):
            break
        time.sleep(0.02)
    assert any(x[0] == "spend" for x in fake.calls), f"扣减没上报云端: {fake.calls}"
    assert fake.authority["spender@self.test"]["ai_credits_left"] == 25, fake.authority
    print("✅ 积分扣减上报云端（否则花掉的积分会被同步涨回来）")


def test_cloud_down_falls_back_to_local():
    """云端不可达不能让用户登不上：退回纯本机注册/登录，并如实带 cloud_notice。"""
    fake = FakeCloud()
    fake.down = True
    c = _client(fake)
    r = c.post("/api/auth/register", json={"identifier": "solo@self.test",
                                          "password": "solopw123"}).json()
    assert r.get("ok"), r
    assert r.get("cloud_synced") is False, r
    assert r.get("cloud_notice"), "云端没同步上却没告诉用户"
    j = c.post("/api/auth/login", json={"identifier": "solo@self.test",
                                       "password": "solopw123"}).json()
    assert j.get("ok") and j.get("cloud_synced") is False, j
    st = c.get("/api/member/status", headers=_auth(j["token"])).json()
    assert st.get("cloud", {}).get("ok") is False, st.get("cloud")
    print("✅ 云端不可达：本机兜底可登，如实提示未同步")


def main() -> int:
    tests = [
        test_register_hits_cloud_and_mirrors_locally,
        test_account_registered_in_app_can_login_on_web,
        test_wrong_password_rejected_and_public_error_is_vague,
        test_member_status_uses_cloud_authority_and_rolls_back_tamper,
        test_change_password_syncs_to_cloud,
        test_spend_reports_to_cloud,
        test_cloud_down_falls_back_to_local,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"❌ {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"❌ {fn.__name__} 异常: {type(e).__name__}: {e}")
    if failed:
        print(f"\n❌ 云端账号打通测试失败 {failed}/{len(tests)}")
        return 1
    print("\n🎉 web 版账号与 App 打通（云端权威 + 本机镜像 + 权益同步）全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
