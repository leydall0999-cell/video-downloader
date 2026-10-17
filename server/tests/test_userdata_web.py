"""网页版「用户信息类数据云备份」通道契约回归（2026-10-17）。

背景：桌面端 1.0.72 起已把「设置与偏好 + 平台登录态」随账号备份到授权中心；
网页版（hanyuxz.top）此前**没有接入**。2026-10-17 补上，关键设计：
  · 与桌面端**同一个账号资料包通道**，但用 `scope="web"` **分文件**存
    （服务端 userdata/<账号sha>.web.json）⇒ 两端各存各的，互不覆盖
    （桌面端的资料包是「整体替换 parts」组装的，共用一份会互相抹掉）；
  · 加解密全在**浏览器**做（WebCrypto：PBKDF2-SHA256 20 万轮 + AES-256-GCM），
    服务端只转发密文 ⇒ 本服务与授权中心都读不到任何 Cookie 内容。

本测试钉住五件事：
  1. 未登录 / 无云端身份 → 明确拒绝，不静默假装成功；
  2. 已登录 → status/pull/push 都带上 scope="web"（写错分区会串到桌面端那份）；
  3. push 的 parts 为空 → 本层就拒（服务端也会拒，但早拒早报错）；
  4. license_client 三个函数确实把 scope 放进了请求体（打桩 opener，不联网）；
  5. 路由已在 server/app.py 注册、前端两处登录成功分支都接了自动同步
     （防手滑删掉接线而测试仍绿）。

不联网（license_client 打桩）、不碰真实凭据。
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_REPO = _SERVER.parent
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))
os.environ.setdefault("VDL_DATA_DIR", tempfile.mkdtemp(prefix="vdl_udweb_"))
os.environ.setdefault("VDL_CLOUD_LINK", "0")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import license_client  # noqa: E402

# ── 假 app 模块：路由里 `import app` 取的就是它（避免在测试里把整个 FastAPI 装配起来）──
_fake_app = types.ModuleType("app")
_fake_app.state_uid = ""
_fake_app.state_token = ""


class _FakeStore:
    def __init__(self, token: str):
        self._t = token

    def cloud_session(self):
        return {"email": "t@example.com", "token": self._t}


_fake_app.get_current_user_id = lambda request: _fake_app.state_uid
_fake_app.current_member_store = lambda request: _FakeStore(_fake_app.state_token)
sys.modules["app"] = _fake_app

from routers import userdata as ud  # noqa: E402

_CALLS: list[dict] = []


def _install_fakes(meta=None, payload=None, ok=True):
    """把 license_client 的三个转发函数换成内存假实现，记录收到的 scope。"""
    _CALLS.clear()

    def _meta(token, scope="", base_url=None, timeout=0.0, opener=None):
        _CALLS.append({"fn": "meta", "token": token, "scope": scope})
        return {"ok": ok, "meta": meta}

    def _pull(token, scope="", base_url=None, timeout=0.0, opener=None):
        _CALLS.append({"fn": "pull", "token": token, "scope": scope})
        return {"ok": ok, "payload": payload, "meta": meta}

    def _push(token, blob, scope="", base_url=None, timeout=0.0, opener=None):
        _CALLS.append({"fn": "push", "token": token, "scope": scope,
                       "parts": sorted((blob or {}).get("parts", {}).keys())})
        return {"ok": ok, "updated_at": 1700000000.0, "meta": meta}

    license_client.userdata_meta_remote = _meta
    license_client.userdata_pull_remote = _pull
    license_client.userdata_push_remote = _push


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ud.router)
    return TestClient(app)


def _login(uid: str = "u1", token: str = "cloud-tok"):
    _fake_app.state_uid = uid
    _fake_app.state_token = token


def _logout():
    _fake_app.state_uid = ""
    _fake_app.state_token = ""


# ── ① 认人 ────────────────────────────────────────────────────────────────────
def test_status_requires_login():
    _logout()
    r = _client().get("/api/userdata/status")
    assert r.status_code == 200, r.status_code
    d = r.json()
    assert d.get("ok") is False and d.get("code") == "NO_LOGIN", d


def test_status_without_cloud_token():
    """纯本机账号（离线注册）：明确说明「未接入云端」，且不误报已备份。"""
    _install_fakes()
    _login(uid="u1", token="")
    d = _client().get("/api/userdata/status").json()
    assert d.get("ok") is True, d
    assert d.get("cloud_linked") is False, d
    assert d.get("cloud") is None, d
    assert "未接入云端" in str(d.get("cloud_error") or ""), d
    assert not _CALLS, f"未接入云端时不该打授权中心，实际 {_CALLS}"


def test_status_returns_cloud_meta():
    _install_fakes(meta={"parts": ["cookies", "settings"], "settings": {"count": 2},
                         "cookies": {"count": 1}})
    _login()
    d = _client().get("/api/userdata/status").json()
    assert d.get("ok") is True and d.get("cloud_linked") is True, d
    assert (d.get("cloud") or {}).get("settings", {}).get("count") == 2, d
    assert _CALLS and _CALLS[0]["fn"] == "meta", _CALLS
    assert _CALLS[0]["scope"] == "web", f"必须落在网页版分区，实际 {_CALLS[0]!r}"


# ── ② 转发必须带 scope=web ────────────────────────────────────────────────────
def test_pull_forwards_with_web_scope():
    _install_fakes(payload={"version": 1, "parts": {"settings": {"vdl_use": "personal"}}})
    _login()
    d = _client().post("/api/userdata/pull", json={}).json()
    assert d.get("ok") is True, d
    assert _CALLS == [{"fn": "pull", "token": "cloud-tok", "scope": "web"}], _CALLS


def test_push_rejects_empty_parts():
    _install_fakes()
    _login()
    c = _client()
    for bad in ({}, {"payload": {}}, {"payload": {"parts": {}}}, {"payload": {"parts": None}}):
        d = c.post("/api/userdata/push", json=bad).json()
        assert d.get("ok") is False, f"parts 空/缺应被拒，实际 {d}"
        assert "parts" in str(d.get("error") or ""), d
    assert not _CALLS, f"被拒的请求不该转发到授权中心，实际 {_CALLS}"


def test_push_forwards_with_web_scope():
    _install_fakes()
    _login()
    body = {"payload": {"version": 1, "updated_at": 1.0, "device": "web-x",
                        "parts": {"settings": {"vdl_use": "personal"},
                                  "cookies": {"ct": "AAAA"}}}}
    d = _client().post("/api/userdata/push", json=body).json()
    assert d.get("ok") is True, d
    assert len(_CALLS) == 1, _CALLS
    assert _CALLS[0]["fn"] == "push" and _CALLS[0]["scope"] == "web", _CALLS[0]
    assert _CALLS[0]["parts"] == ["cookies", "settings"], _CALLS[0]


def test_pull_and_push_require_cloud_token():
    _install_fakes()
    _login(uid="u1", token="")
    c = _client()
    for p in ("/api/userdata/pull", "/api/userdata/push"):
        d = c.post(p, json={"payload": {"parts": {"settings": {}}}}).json()
        assert d.get("ok") is False and d.get("code") == "NO_CLOUD", (p, d)
    assert not _CALLS, _CALLS


# ── ③ license_client 真的把 scope 放进了请求体（打桩 opener，不联网）─────────
class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_license_client_sends_scope():
    seen: list[dict] = []

    def _opener(req, timeout=0):
        seen.append({"path": req.full_url, "body": json.loads(req.data.decode())})
        return _Resp({"ok": True, "meta": None, "payload": None, "updated_at": 1.0})

    license_client.userdata_meta_remote("tok", scope="web", base_url="https://x", opener=_opener)
    license_client.userdata_pull_remote("tok", scope="web", base_url="https://x", opener=_opener)
    license_client.userdata_push_remote("tok", {"parts": {"settings": {}}}, scope="web",
                                        base_url="https://x", opener=_opener)
    # 桌面端那份（scope 留空）不得带上 scope 字段，保持与 1.0.72 线上契约一致
    license_client.userdata_meta_remote("tok", base_url="https://x", opener=_opener)

    paths = [s["path"] for s in seen]
    assert paths == ["https://x/api/license/data-meta", "https://x/api/license/data-pull",
                     "https://x/api/license/data-push", "https://x/api/license/data-meta"], paths
    assert all(s["body"].get("scope") == "web" for s in seen[:3]), seen
    assert "scope" not in seen[3]["body"], f"默认（桌面端）不得带 scope：{seen[3]!r}"


# ── ④ 接线没被删（源码级回归）────────────────────────────────────────────────
def test_app_registers_router():
    src = (_SERVER / "app.py").read_text(encoding="utf-8")
    assert "from routers import userdata as _userdata_rtr" in src, "app.py 未引入网页版云备份路由"
    assert "app.include_router(_userdata_rtr.router)" in src, "app.py 未注册网页版云备份路由"
    assert (_SERVER / "routers" / "userdata.py").exists(), "routers/userdata.py 丢了"


def test_frontend_wiring():
    js = (_REPO / "web" / "app.js").read_text(encoding="utf-8")
    html = (_REPO / "web" / "index.html").read_text(encoding="utf-8")
    assert js.count("void udAfterLogin(pw);") == 2, "两条登录成功路径（个人中心 / 登录弹窗）都要接自动同步"
    assert "udRender()" in js, "个人中心加载后要渲染云端资料卡片"
    assert "/api/userdata/pull" in js and "/api/userdata/push" in js, "前端同步引擎缺端点调用"
    assert "crypto.subtle" in js, "平台登录态必须由浏览器本机加密（WebCrypto）"
    assert 'id="cloudDataCard"' in html and 'id="cloudDataSyncBtn"' in html, "个人中心缺云端资料卡片"
    assert "PBKDF2" in js and "AES-GCM" in js, "加密算法必须与桌面端同族（PBKDF2 + AES-GCM）"


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            traceback.print_exc()
    print("FAILED" if failed else "ALL PASS", failed)
    sys.exit(1 if failed else 0)
