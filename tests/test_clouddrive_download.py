"""百度网盘下载（list / download_url / download）单元测试 + 路由集成测试。

不联网：用 monkeypatch 替换 requests / provider 方法，验证解析、换链、流式落盘与错误分支。
"""
import os
import sys
import json
from pathlib import Path

import pytest

_SERVER = str(Path(__file__).resolve().parent.parent / "server")
if _SERVER not in sys.path:
    sys.path.insert(0, _SERVER)

import clouddrive as cd  # noqa: E402
from clouddrive import BaiduProvider, CloudError  # noqa: E402


class _Resp:
    def __init__(self, json_data=None, status_code=200, chunks=None, raw=None):
        self._json = json_data or {}
        self.status_code = status_code
        self._chunks = chunks if chunks is not None else [b""]
        self._raw = raw

    def json(self):
        return self._json

    def iter_content(self, chunk_size=1024):
        for c in self._chunks:
            yield c


def test_list_files_returns_parsed(monkeypatch):
    prov = BaiduProvider()
    fake = _Resp({"errno": 0, "list": [
        {"fs_id": 2, "path": "/a/file.mp4", "server_filename": "file.mp4", "size": 100, "isdir": 0},
        {"fs_id": 1, "path": "/a/sub", "server_filename": "sub", "size": 0, "isdir": 1},
    ]})
    monkeypatch.setattr(cd.requests, "get", lambda *a, **k: fake)
    data = prov.list_files("TOK", "/a")
    assert data["list"][0]["fs_id"] == 2


def test_list_files_errno_raises(monkeypatch):
    prov = BaiduProvider()
    monkeypatch.setattr(cd.requests, "get", lambda *a, **k: _Resp({"errno": -6, "errmsg": "no such dir"}))
    with pytest.raises(CloudError):
        prov.list_files("TOK", "/nope")


def test_list_files_missing_token_raises(monkeypatch):
    prov = BaiduProvider()
    with pytest.raises(CloudError):
        prov.list_files("", "/")


def test_download_url_ok(monkeypatch):
    prov = BaiduProvider()
    monkeypatch.setattr(cd.requests, "get", lambda *a, **k: _Resp(
        {"errno": 0, "dlink": "https://d.pcs.baidu.com/x", "size": 123}))
    info = prov.download_url("TOK", 99, "/a/file.mp4")
    assert info["dlink"].startswith("https://")
    assert info["size"] == 123


def test_download_url_errno_raises(monkeypatch):
    prov = BaiduProvider()
    monkeypatch.setattr(cd.requests, "get", lambda *a, **k: _Resp({"errno": -20, "error_msg": "need vip"}))
    with pytest.raises(CloudError):
        prov.download_url("TOK", 99, "/a/big.iso")


def test_download_url_missing_token_raises(monkeypatch):
    prov = BaiduProvider()
    with pytest.raises(CloudError):
        prov.download_url("", 99, "/a")


def test_download_streams_to_file(tmp_path, monkeypatch):
    prov = BaiduProvider()
    # 跳过换链，直接给 dlink 信息
    monkeypatch.setattr(prov, "download_url", lambda *a, **k: {"dlink": "https://d.pcs.baidu.com/x", "size": 10})
    captured = {}

    def fake_get(url, **k):
        # 校验下载请求带了正确 UA（否则百度会 403/死循环）
        assert k.get("headers", {}).get("User-Agent") == "pan.baidu.com", "必须带 pan.baidu.com UA"
        captured["url"] = url
        return _Resp(status_code=200, chunks=[b"hello", b"wor", b"ld"])

    monkeypatch.setattr(cd.requests, "get", fake_get)
    dest = tmp_path / "out.bin"
    prog = []
    written = prov.download("TOK", 99, "/a/file.txt", dest, progress=lambda d, t: prog.append((d, t)))
    assert written == 10
    assert dest.read_bytes() == b"helloworld"
    assert prog[-1][0] == 10  # 进度落到总字节


def test_download_non_200_raises(tmp_path, monkeypatch):
    prov = BaiduProvider()
    monkeypatch.setattr(prov, "download_url", lambda *a, **k: {"dlink": "https://d.pcs.baidu.com/x", "size": 8})

    def fake_get(url, **k):
        return _Resp(status_code=403, chunks=[b""])
    monkeypatch.setattr(cd.requests, "get", fake_get)
    with pytest.raises(CloudError):
        prov.download("TOK", 99, "/a/file.txt", tmp_path / "x.bin")


# ── 路由集成（启用百度时）───────────────────────────────────────────────
def test_baidu_list_route(monkeypatch):
    import app as m
    monkeypatch.setattr(m, "BAIDU_ENABLED", True)
    monkeypatch.setattr(m._baidu_provider, "list_files", lambda tok, p, page=1, limit=200: {
        "errno": 0,
        "list": [
            {"fs_id": 1, "path": "/v", "server_filename": "v", "size": 0, "isdir": 1},
            {"fs_id": 2, "path": "/f.mp4", "server_filename": "f.mp4", "size": 10, "isdir": 0},
        ],
    })
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    r = c.get("/api/cloud/baidu/list?path=%2F&token=TOK")
    assert r.status_code == 200
    items = r.json()["list"]
    assert items[0]["isdir"] is True and items[1]["isdir"] is False  # 文件夹在前
    assert items[1]["name"] == "f.mp4"


def test_baidu_list_route_requires_token(monkeypatch):
    import app as m
    monkeypatch.setattr(m, "BAIDU_ENABLED", True)
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    r = c.get("/api/cloud/baidu/list?path=%2F")  # 无 token
    assert r.status_code == 400


# ── aria2c 并发下载后端 ────────────────────────────────────────────────
def test_download_uses_aria2c_when_available(tmp_path, monkeypatch):
    import types
    prov = BaiduProvider()
    monkeypatch.setattr(prov, "download_url", lambda *a, **k: {"dlink": "https://d.pcs.baidu.com/x", "size": 10})
    monkeypatch.setattr(cd, "_aria2c_path", lambda: "/usr/bin/aria2c")

    dest = tmp_path / "out.bin"
    captured = {}

    def fake_run(cmd, **kwargs):
        # 校验并发参数与输出文件名确实传给 aria2c
        assert "-x" in cmd and "8" in cmd
        assert "--out" in cmd and dest.name in cmd
        assert "pan.baidu.com" in cmd
        captured["cmd"] = cmd
        # 模拟 aria2c 已把文件下完
        dest.write_bytes(b"helloworld")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    prog = []
    written = prov.download("TOK", 99, "/a/file.txt", dest, progress=lambda d, t: prog.append((d, t)))
    assert written == 10
    assert dest.read_bytes() == b"helloworld"
    assert captured["cmd"][0].endswith("aria2c")  # 用的是 aria2c 而不是 requests
    assert prog[-1] == (10, 10)


def test_download_auto_falls_back_to_requests(tmp_path, monkeypatch):
    """backend=auto 但本机无 aria2c 时，应回退 requests 流式下载（功能不中断）。"""
    prov = BaiduProvider()
    monkeypatch.setattr(prov, "download_url", lambda *a, **k: {"dlink": "https://d.pcs.baidu.com/x", "size": 10})
    monkeypatch.setattr(cd, "_aria2c_path", lambda: None)  # 模拟无 aria2c

    def fake_get(url, **k):
        assert k.get("headers", {}).get("User-Agent") == "pan.baidu.com"
        return _Resp(status_code=200, chunks=[b"hello", b"wor", b"ld"])

    monkeypatch.setattr(cd.requests, "get", fake_get)
    dest = tmp_path / "out.bin"
    written = prov.download("TOK", 99, "/a/file.txt", dest, backend="auto")
    assert written == 10
    assert dest.read_bytes() == b"helloworld"


def test_download_aria2c_forced_raises_without_binary(tmp_path, monkeypatch):
    """backend='aria2c' 但本机无 aria2c 时，应直接报错（不静默回退）。"""
    prov = BaiduProvider()
    monkeypatch.setattr(prov, "download_url", lambda *a, **k: {"dlink": "https://d.pcs.baidu.com/x", "size": 10})
    monkeypatch.setattr(cd, "_aria2c_path", lambda: None)
    with pytest.raises(CloudError):
        prov.download("TOK", 99, "/a/file.txt", tmp_path / "x.bin", backend="aria2c")


def test_aria2c_download_failure_raises(tmp_path, monkeypatch):
    import types
    monkeypatch.setattr(cd, "_aria2c_path", lambda: "/usr/bin/aria2c")
    dest = tmp_path / "out.bin"

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(returncode=2, stdout="", stderr="some aria2c error")

    monkeypatch.setattr(cd.subprocess, "run", fake_run)
    with pytest.raises(CloudError):
        cd._aria2c_download("https://d.pcs.baidu.com/x", dest, 10, progress=None)
