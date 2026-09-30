#!/usr/bin/env python3
"""网页版分享凭据端点回归测试（受限版分享，2026-09-30）。

背景：网页版新增「生成二维码 / 生成网页」两视图，上传走同源 /api/upload
（nginx 反代分享节点 8901），分享凭据由本端点下发。红线：
  ① 未登录 → ok=False + code=NO_AUTH（绝不把分享 token 发给匿名访客，
     否则等于开放公网文件托管，滥用/合规风险）；
  ② 节点无 token 文件 → SHARE_UNAVAILABLE（HK 节点没有分享服务，前端据此降级）；
  ③ 每日限次：取满 VDL_WEB_SHARE_DAILY 次后 DAILY_LIMIT；
  ④ /api/share/consume 计数 +1；
  ⑤ app.py 必须 include 本 router（铁律：新模块与 include 同提交，否则全新 clone 起不来）。
全程离线：token 文件用临时路径，计数走 VDL_DATA_DIR 隔离目录。
运行：cd server && python tests/test_share_token.py
"""
import os
import sys
import tempfile

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_sharetok_"))
os.environ.setdefault("VDL_CLOUD_LINK", "0")
# token 文件指到临时目录（必须在 import app / 首次调用前设置）
_TOK_FILE = os.path.join(tempfile.mkdtemp(prefix="vdl_sharetok_file_"), "share_token")
os.environ["VDL_SHARE_TOKEN_FILE"] = _TOK_FILE
os.environ["VDL_WEB_SHARE_DAILY"] = "3"          # 限次调小，方便测满

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from auth_store import create_user, issue_token  # noqa: E402

c = TestClient(server_app.app)
_H = {}


def _login():
    if not _H:
        uid = create_user(f"sharetok_{os.getpid()}@self.test", "pw123456")
        assert uid, "本地造账号失败"
        _H["Authorization"] = f"Bearer {issue_token(uid)}"
    return _H


def _write_token(tok="tok-share-test-123"):
    with open(_TOK_FILE, "w", encoding="utf-8") as f:
        f.write(tok)


def _del_token():
    try:
        os.remove(_TOK_FILE)
    except OSError:
        pass


def test_anonymous_gets_no_token():
    _write_token()
    r = c.get("/api/share/token")
    body = r.json()
    assert body.get("ok") is False and body.get("code") == "NO_AUTH", \
        f"匿名必须拿不到分享 token，得到 {body}"
    assert "token" not in body, "NO_AUTH 响应里绝不能带 token 字段"
    print("✅ 匿名拿不到分享 token（NO_AUTH，且不泄露 token 字段）")


def test_unavailable_without_token_file():
    _del_token()
    r = c.get("/api/share/token", headers=_login()).json()
    assert r.get("ok") is False and r.get("code") == "SHARE_UNAVAILABLE", \
        f"无 token 文件应 SHARE_UNAVAILABLE（HK 节点场景），得到 {r}"
    print("✅ 无分享节点时 SHARE_UNAVAILABLE（前端据此降级）")


def test_limits_readonly_no_token_leak():
    """/api/share/limits 只读：不计数、绝不返回 token 字段。"""
    _write_token()
    r = c.get("/api/share/limits", headers=_login()).json()
    assert r.get("ok") is True and "token" not in r, f"limits 端点不得泄露 token：{r}"
    lim = r.get("limits") or {}
    assert lim.get("max_file_mb") == 95, f"单文件上限应 95MB（CF 100MB 约束）：{lim}"
    assert lim.get("pagetool_max_total_mb") == 60, f"生成网页合计应 60MB：{lim}"
    assert lim.get("default_expire_days") == 7, f"默认 7 天过期：{lim}"
    assert 0 not in (lim.get("expire_days") or []), "受限版不提供永久（0 天）档"
    r_anon = c.get("/api/share/limits").json()
    assert r_anon.get("code") == "NO_AUTH", "limits 匿名也应拒绝（保持一致门禁）"
    print("✅ /api/share/limits 只读 + 不泄露 token + 匿名拒绝")


def test_daily_cap_counts_on_fetch():
    """取 token 即计数：VDL_WEB_SHARE_DAILY=3，第 4 次必须 DAILY_LIMIT。"""
    _write_token()
    for i in range(3):
        r2 = c.get("/api/share/token", headers=_login()).json()
        assert r2.get("ok") is True, f"第 {i + 1} 次取 token 不应被拦：{r2}"
        assert r2.get("limits", {}).get("used_today") == i + 1, f"used_today 应随取递增：{r2}"
    r3 = c.get("/api/share/token", headers=_login()).json()
    assert r3.get("ok") is False and r3.get("code") == "DAILY_LIMIT", \
        f"超出每日限次必须 DAILY_LIMIT：{r3}"
    print("✅ 每日限次（取 token 即计数，3 次后 DAILY_LIMIT）")


def test_consume_counts():
    """计数器底层：_incr_used 递增、_read_used 一致。"""
    st = __import__("routers.share_token", fromlist=["share_token"])
    used1 = st._incr_used("u_counter_x")
    used2 = st._incr_used("u_counter_x")
    assert used2 == used1 + 1, f"计数应递增：{used1} -> {used2}"
    assert st._read_used("u_counter_x") == used2, "_read_used 应与计数一致"
    print("✅ 计数器递增")


def test_app_includes_router():
    """铁律 21：新模块必须与 app.py include 同提交（否则全新 clone ImportError）。
    注意：本环境的 include_router 会包成 _IncludedRouter（无 .path），枚举 app.routes
    看不到子路由 —— 必须用真实请求验证挂载。"""
    src = open(os.path.join(_SERVER_DIR, "app.py"), encoding="utf-8").read()
    assert "from routers import share_token" in src and "include_router(_share_token_rtr.router)" in src, \
        "app.py 未挂载 share_token router"
    r = c.get("/api/share/limits").json()
    assert "ok" in r, f"/api/share/limits 未挂载（真实请求失败）：{r}"
    print("✅ app.py 已挂载 share_token（真实请求 /api/share/limits 可达）")


if __name__ == "__main__":
    test_anonymous_gets_no_token()
    test_unavailable_without_token_file()
    test_limits_readonly_no_token_leak()
    test_daily_cap_counts_on_fetch()
    test_consume_counts()
    test_app_includes_router()
    print("\n✅ 分享凭据端点回归测试全部通过")
