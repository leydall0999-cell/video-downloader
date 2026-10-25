"""服务器端「百度网盘登录态进共享池」离线回归（2026-10-25）。

钉住的契约（任何一条被破坏，网页版网盘就永远没有登录态可用）：

  1. baidu.com 必须在 cookie 池白名单里 —— 否则桌面端把登录态推上来时，
     /api/cookie/sync 会以 400「不支持的域名」拒收，管理员那份也存不进池；
  2. 百度是**仅显式贡献**域：进白名单（可存可取），但绝不被通用自动同步
     通道（_sync_domains_to_cloud）悄悄带走 —— BDUSS = 完整网盘账号权限，
     不能替用户默认决定「共享我的网盘账号」；
  3. 百度登录态必须**真问站点**验真（游客态也带一堆字段，结构校验判不出来）；
  4. 入池后能取回，且取回的就是同一份；
  5. 管理员在服务器上配的那份（VDL_BAIDU_BDUSS）会顺手写回池里，
     于是桌面端也能通过共享池拿到同一份账号（两端一份凭据）。

全程离线：验真走假实现，不打任何外部网络。
运行：cd server && python tests/test_baidu_pool_server.py
"""
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# 隔离数据目录：import cookie_pool 会建池目录，绝不能落到真实池子
os.environ.setdefault("VDL_DATA_DIR", "/tmp/vdl_test_baidu_pool_server")
os.environ.setdefault("VDL_COOKIE_POOL_DIR", "/tmp/vdl_test_baidu_pool_server/pool")
os.environ.setdefault("VDL_CLOUD_LINK", "0")

import cookie_pool as CP  # noqa: E402
# 🔴 导入顺序有讲究：routers.cloud_drive 顶层 `import app`，而 app 又要
# include_router(cloud_drive.router) ⇒ 必须先让 app 起完，否则循环导入报错。
import app as _A  # noqa: E402,F401
import routers.cloud_drive as CD  # noqa: E402

# 一份「像真的」的百度登录态：BDUSS + STOKEN 是网盘登录态的标志字段
_FAKE_HEADER = "BDUSS=fake-bduss-value-0123456789; STOKEN=fake-stoken-abc; BAIDUID=FAKEUID123"


def test_baidu_in_pool_whitelist():
    """baidu.com 必须在白名单里 —— 这是整条链路的入口条件。"""
    assert CP.is_allowed("baidu.com"), "baidu.com 不在白名单：桌面端推云端会被 400 拒收"
    assert CP.is_allowed("pan.baidu.com"), "pan.baidu.com 应命中根域 baidu.com"
    print("✅ baidu.com / pan.baidu.com 都在白名单内")


def test_baidu_is_contrib_only():
    """百度必须是「仅显式贡献」域：可存可取，但不进通用自动同步。"""
    assert CP.is_contrib_only("baidu.com"), "百度应标记为仅贡献域"
    assert CP.is_contrib_only("pan.baidu.com"), "子域也应命中"
    # 不能影响别的站
    assert not CP.is_contrib_only("bilibili.com"), "B 站不应被标成仅贡献域"
    assert not CP.is_contrib_only("douyin.com"), "抖音不应被标成仅贡献域"
    print("✅ 百度标记为仅贡献域，且不影响其它站")


def test_baidu_verify_dispatch():
    """verify_cookie 必须把 baidu 分发到百度专属验真（真问站点）。"""
    src = open(os.path.join(_SERVER_DIR, "cookie_pool.py"), encoding="utf-8").read()
    assert 'if d == "baidu.com":' in src, "verify_cookie 缺百度分发分支"
    assert "return verify_baidu(header)" in src, "百度分发未走 verify_baidu"
    assert "def verify_baidu(" in src, "缺 verify_baidu 实现"
    assert "def baidu_probe(" in src, "缺 baidu_probe 实现"
    assert "xpan/nas" in src, "百度验真应问 xpan/nas uinfo"
    print("✅ 百度验真已接进 verify_cookie 分发")


def test_baidu_verify_result():
    """验真判定口径：errno=0 ⇒ 有效；errno≠0 ⇒ 明确无效；问不出来 ⇒ 放行(None)。"""
    calls: list[str] = []

    def _fake_probe(header):
        calls.append(header)
        if "GOOD" in header:
            return (True, {"errno": 0, "vip_type": 2})
        if "BAD" in header:
            return (False, {"errno": -6})
        return (None, {})

    orig = CP.baidu_probe
    CP.baidu_probe = _fake_probe
    try:
        assert CP.verify_baidu("BDUSS=GOOD") is True, "有效登录态应判 True"
        assert CP.verify_baidu("BDUSS=BAD") is False, "过期 BDUSS 应判 False"
        assert CP.verify_baidu("BDUSS=???") is None, "问不出来应放行(None)"
    finally:
        CP.baidu_probe = orig
    print("✅ 百度验真三种口径正确（True / False / None 放行）")


def test_baidu_pool_roundtrip():
    """入池后能取回同一份 —— 管理员那份写回池、桌面端从池取，靠的就是这个。

    ⚠️ 不能断言「取回的 == 存入的」：池里可能还有别的测试写入的条目，而
    get_cookie 取的是**最新一条** ⇒ 谁最后入池谁说了算。这样写会让本用例
    变成顺序依赖（单独跑绿、跟套件跑红）。正确判据是「取回的那份确实在池里」。
    """
    orig_probe = CP.baidu_probe
    CP.baidu_probe = lambda header: (True, {"errno": 0})
    try:
        assert CP.add_cookie("baidu.com", _FAKE_HEADER, source="admin"), "入池失败"
        got = CP.get_cookie("baidu.com")
        assert got, "池里没取到任何百度登录态"
        assert _FAKE_HEADER in got or got in _FAKE_HEADER, \
            f"取回的不是刚存入的那份：{got!r}"
        # 子域也要能取到（服务器端用 pan.baidu.com 读池）
        got2 = CP.get_cookie("pan.baidu.com")
        assert got2, "按子域 pan.baidu.com 取不到池里的百度登录态"
    finally:
        CP.baidu_probe = orig_probe
    print("✅ 百度登录态入池后可取回（含子域回退）")


def test_admin_env_writes_back_to_pool():
    """管理员在服务器上配的那份（VDL_BAIDU_BDUSS）会顺手写回池里。"""
    orig_probe = CP.baidu_probe
    CP.baidu_probe = lambda header: (True, {"errno": 0})
    old_env = os.environ.get("VDL_BAIDU_BDUSS")
    os.environ["VDL_BAIDU_BDUSS"] = "BDUSS=from-admin-env-value"
    try:
        got = CD._admin_login_cookie()
        assert "from-admin-env-value" in got, "管理员那份没取到"
        pooled = CP.get_cookie("baidu.com") or ""
        assert "from-admin-env-value" in pooled, "管理员那份没写回共享池"
    finally:
        CP.baidu_probe = orig_probe
        if old_env is None:
            os.environ.pop("VDL_BAIDU_BDUSS", None)
        else:
            os.environ["VDL_BAIDU_BDUSS"] = old_env
    print("✅ 管理员那份既能用，也会写回共享池")


if __name__ == "__main__":
    for fn in (
        test_baidu_in_pool_whitelist,
        test_baidu_is_contrib_only,
        test_baidu_verify_dispatch,
        test_baidu_verify_result,
        test_baidu_pool_roundtrip,
        test_admin_env_writes_back_to_pool,
    ):
        fn()
    print("\n🎉 服务器端百度入池离线测试全部通过")
