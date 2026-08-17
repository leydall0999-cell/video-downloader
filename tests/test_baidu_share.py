"""百度网盘分享链接下载（share_list / share_transfer / download_share / 令牌持久化）单测 + 路由集成。

不联网：用 monkeypatch 替换 requests / provider 方法，验证解析、转存、下载流程与错误分支。
"""
import json
import sys
import time
from pathlib import Path

import pytest

_SERVER = str(Path(__file__).resolve().parent.parent / "server")
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

import clouddrive as cd  # noqa: E402
from clouddrive import BaiduProvider, CloudError  # noqa: E402


class _Resp:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data or {}
        self.status_code = status_code

    def json(self):
        return self._json


class _FakeSession:
    """替代 requests.Session()：拦截 verify/list/transfer 请求，避免真实联网。

    transfer POST 的 data 会被记录到 captured_data，供断言转存目标路径使用。
    """

    def __init__(self, transfer_resp=None, verify_resp=None):
        self._transfer_resp = transfer_resp or _Resp({
            "errno": 0,
            "extra": {"list": [{"fs_id": 1, "path": "/x"}]},
        })
        self._verify_resp = verify_resp or _Resp({"errno": 0, "randsk": "R"})
        self.captured_data = None

    def post(self, url, *a, **k):
        if "transfer" in str(url):
            self.captured_data = k.get("data")
            return self._transfer_resp
        return self._verify_resp

    def get(self, *a, **k):
        return _Resp({})


def _fake_meta(items, sekey="SEK", share_id=123, uk=456, session=None):
    """构造 _share_meta 的桩返回值（绕过真实的 verify/list 请求）。"""
    return {"sekey": sekey, "share_id": share_id, "uk": uk, "items": items, "session": session}


# ── _parse_share_surl ───────────────────────────────────────────────────
def test_parse_surl_short():
    assert BaiduProvider()._parse_share_surl("https://pan.baidu.com/s/1AbCdEf") == "AbCdEf"  # 类型前缀 1 已剥离


def test_parse_surl_query():
    assert BaiduProvider()._parse_share_surl("https://pan.baidu.com/share/init?surl=XyZ") == "XyZ"


def test_parse_surl_share_path():
    assert BaiduProvider()._parse_share_surl("https://pan.baidu.com/share/1QwEr") == "QwEr"  # 类型前缀 1 已剥离


def test_parse_surl_invalid():
    with pytest.raises(CloudError):
        BaiduProvider()._parse_share_surl("not a url")


# ── share_list ──────────────────────────────────────────────────────────
def test_share_list_parses(monkeypatch):
    prov = BaiduProvider()
    # 桩掉 _share_meta，避免真实 verify/list 请求；直接喂 list 数据验证归一化与排序
    monkeypatch.setattr(prov, "_share_meta", lambda *a, **k: _fake_meta([
        {"fs_id": 11, "path": "/movie.mp4", "server_filename": "movie.mp4", "size": 100, "isdir": 0},
        {"fs_id": 12, "path": "/sub", "server_filename": "sub", "size": 0, "isdir": 1},
    ]))
    res = prov.share_list("https://pan.baidu.com/s/1AbC", "pwd1")
    assert res["surl"] == "AbC"  # 短链类型前缀 1 已被剥离
    assert len(res["list"]) == 2
    assert res["list"][0]["isdir"] is True  # 文件夹在前


def test_share_list_bad_pwd(monkeypatch):
    prov = BaiduProvider()
    monkeypatch.setattr(cd.requests, "get", lambda *a, **k: _Resp({"errno": -12, "errmsg": "wrong pwd"}))
    with pytest.raises(CloudError):
        prov.share_list("https://pan.baidu.com/s/1AbC", "x")


# ── share_transfer ──────────────────────────────────────────────────────
def test_share_transfer_ok(monkeypatch):
    prov = BaiduProvider()
    sess = _FakeSession(transfer_resp=_Resp({
        "errno": 0,
        "extra": {"list": [{"fs_id": 999, "path": "/VideoDownloader_Share/movie.mp4"}]},
    }))
    # 桩掉 _share_meta（verify）与 _ensure_dest_dir 的真实联网
    monkeypatch.setattr(prov, "_share_meta", lambda *a, **k: _fake_meta(
        [{"fs_id": 11, "path": "/movie.mp4", "server_filename": "movie.mp4", "isdir": 0}],
        session=sess,
    ))
    monkeypatch.setattr(cd.requests, "post", lambda *a, **k: _Resp({}))
    out = prov.share_transfer("https://pan.baidu.com/s/1AbC", "pwd1", ["/movie.mp4"], "/VideoDownloader_Share", "TOK")
    assert out == [{"fs_id": 999, "path": "/VideoDownloader_Share/movie.mp4"}]


def test_share_transfer_requires_token(monkeypatch):
    prov = BaiduProvider()
    with pytest.raises(CloudError):
        prov.share_transfer("https://pan.baidu.com/s/1AbC", "", ["/movie.mp4"], "/d", "")


def test_share_transfer_expired(monkeypatch):
    prov = BaiduProvider()
    monkeypatch.setattr(cd.requests, "post", lambda *a, **k: _Resp({"errno": -9, "errmsg": "not found"}))
    with pytest.raises(CloudError):
        prov.share_transfer("https://pan.baidu.com/s/1AbC", "", ["/movie.mp4"], "/d", "TOK")


# ── download_share ──────────────────────────────────────────────────────
def test_download_share_transfer_then_download(tmp_path, monkeypatch):
    prov = BaiduProvider()
    captured = {}

    def fake_transfer(url, pwd, paths, dest, token, **k):
        return [{"fs_id": 999, "path": "/VideoDownloader_Share/movie.mp4"}]

    def fake_download(token, fs_id, path, local_path, **k):
        captured["fs_id"] = fs_id
        captured["path"] = path
        return 123

    # 桩掉 _share_meta，否则 download_share 会真实 verify
    monkeypatch.setattr(prov, "_share_meta", lambda *a, **k: _fake_meta([
        {"fs_id": 999, "path": "/movie.mp4", "server_filename": "movie.mp4", "isdir": 0},
    ]))
    monkeypatch.setattr(prov, "share_transfer", fake_transfer)
    monkeypatch.setattr(prov, "download", fake_download)
    dest = tmp_path / "movie.mp4"
    n = prov.download_share("https://pan.baidu.com/s/1AbC", "pwd", "/movie.mp4", dest, "TOK")
    assert n == 123
    assert captured["fs_id"] == 999
    assert captured["path"] == "/VideoDownloader_Share/movie.mp4"


# ── 令牌持久化 ──────────────────────────────────────────────────────────
def test_token_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "_baidu_token_path", lambda: tmp_path / "baidu_token.json")
    cd.clear_baidu_token()
    assert cd.load_baidu_token() == {}
    cd.save_baidu_token({"access_token": "ABC", "expires_in": 30, "scope": "basic netdisk"})
    assert cd.load_baidu_token()["access_token"] == "ABC"
    cd.clear_baidu_token()
    assert cd.load_baidu_token() == {}


def test_token_store_ignores_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cd, "_baidu_token_path", lambda: tmp_path / "baidu_token.json")
    cd.save_baidu_token({})  # 无 access_token 不应写入
    assert not (tmp_path / "baidu_token.json").exists()


# ── 路由集成 ────────────────────────────────────────────────────────────
def test_share_list_route(monkeypatch):
    import app as m
    monkeypatch.setattr(m, "BAIDU_ENABLED", True)
    monkeypatch.setattr(m._baidu_provider, "share_list", lambda url, pwd="", _dir="": {
        "surl": "AbC",
        "list": [{"fs_id": 1, "path": "/f.mp4", "name": "f.mp4", "size": 10, "isdir": 0}],
    })
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    r = c.post("/api/cloud/baidu/share/list", json={"url": "https://pan.baidu.com/s/1AbC", "pwd": ""})
    assert r.status_code == 200
    assert r.json()["list"][0]["name"] == "f.mp4"


def test_share_list_route_requires_url(monkeypatch):
    import app as m
    monkeypatch.setattr(m, "BAIDU_ENABLED", True)
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    r = c.post("/api/cloud/baidu/share/list", json={"url": ""})
    assert r.status_code == 400


def test_share_download_route_requires_auth(tmp_path, monkeypatch):
    import app as m
    monkeypatch.setattr(m, "BAIDU_ENABLED", True)
    monkeypatch.setattr(cd, "_baidu_token_path", lambda: tmp_path / "tok.json")
    cd.clear_baidu_token()  # 确保本机无令牌 → 路由应要求授权
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    r = c.post("/api/cloud/baidu/share/download", json={"url": "u", "path": "/f.mp4"})
    assert r.status_code == 400


def test_share_download_route_ok(tmp_path, monkeypatch):
    import app as m
    monkeypatch.setattr(m, "BAIDU_ENABLED", True)
    monkeypatch.setattr(m, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(cd, "_baidu_token_path", lambda: tmp_path / "tok.json")
    cd.save_baidu_token({"access_token": "TOK"})  # 真实持久化令牌，供路由回退读取

    def fake_ds(url, pwd, share_path, local_path, token, progress=None, backend="auto", dest="/VideoDownloader_Share", **kwargs):
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"data")
        if progress:
            progress(4, 4)
        return 4

    monkeypatch.setattr(m._baidu_provider, "download_share", fake_ds)
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    r = c.post("/api/cloud/baidu/share/download", json={"url": "u", "path": "/f.mp4", "name": "f.mp4"})
    assert r.status_code == 200
    tid = r.json()["task_id"]
    for _ in range(50):
        t = c.get(f"/api/cloud/baidu/task/{tid}").json()
        if t["status"] not in ("pending", "transferring"):
            break
        time.sleep(0.05)
    assert t["status"] == "completed"
    assert t["filepath"]


def test_token_routes(tmp_path, monkeypatch):
    import app as m
    monkeypatch.setattr(m, "BAIDU_ENABLED", True)
    monkeypatch.setattr(cd, "_baidu_token_path", lambda: tmp_path / "tok.json")
    cd.clear_baidu_token()
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    assert c.get("/api/cloud/baidu/token").json()["logged_in"] is False
    assert c.post("/api/cloud/baidu/token", json={"access_token": "TOK", "expires_in": 30}).status_code == 200
    g2 = c.get("/api/cloud/baidu/token")
    assert g2.json()["logged_in"] is True
    assert g2.json()["access_token"] == "TOK"
    assert c.delete("/api/cloud/baidu/token").status_code == 200
    assert c.get("/api/cloud/baidu/token").json()["logged_in"] is False


# ── 2026 新政：/apps/{appname}/ 路径兼容 ────────────────────────────────
def test_baidu_app_name_from_env(monkeypatch):
    monkeypatch.setattr(cd.os.environ, "get", lambda k, d="": {"VDL_BAIDU_APP_NAME": "MyApp"}.get(k, d))
    assert cd._baidu_app_name() == "MyApp"


def test_baidu_app_name_empty(monkeypatch):
    monkeypatch.setattr(cd.os.environ, "get", lambda k, d="": "")
    assert cd._baidu_app_name() is None


def test_share_dest_with_app_name(monkeypatch):
    monkeypatch.setattr(cd.os.environ, "get", lambda k, d="": {"VDL_BAIDU_APP_NAME": "VDL"}.get(k, d))
    assert cd._baidu_share_dest("Share") == "/apps/VDL/Share"


def test_share_dest_without_app_name(monkeypatch):
    monkeypatch.setattr(cd.os.environ, "get", lambda k, d="": "")
    assert cd._baidu_share_dest("Share") == "/Share"


def test_share_transfer_uses_apps_path(monkeypatch):
    """配置 APP_NAME 后，share_transfer 的 dest 自动走 /apps/{name}/ 前缀（不传 dest 时）。"""
    prov = BaiduProvider()
    monkeypatch.setattr(cd.os.environ, "get", lambda k, d="": {"VDL_BAIDU_APP_NAME": "TestApp"}.get(k, d))
    sess = _FakeSession(transfer_resp=_Resp({
        "errno": 0,
        "extra": {"list": [{"fs_id": 1, "path": "/apps/TestApp/VideoDownloader_Share/f.mp4"}]},
    }))
    # 桩掉 _share_meta（verify）与 _ensure_dest_dir；用假 session 捕获 transfer POST 的 data
    monkeypatch.setattr(prov, "_share_meta", lambda *a, **k: _fake_meta(
        [{"fs_id": 1, "path": "/f.mp4", "server_filename": "f.mp4", "isdir": 0}],
        session=sess,
    ))
    monkeypatch.setattr(cd.requests, "post", lambda *a, **k: _Resp({}))
    prov.share_transfer("https://pan.baidu.com/s/1AbC", "pwd", ["/f.mp4"], "", "TOK")
    # 验证 transfer POST 的 data.path 是 /apps/{appname}/VideoDownloader_Share
    assert sess.captured_data["path"] == "/apps/TestApp/VideoDownloader_Share"
