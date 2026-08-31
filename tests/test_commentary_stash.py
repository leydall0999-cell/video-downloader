"""解说视频「接收站」cache-by-hash 单元测试。

直接调用 server/app.py 的底层函数（_stash_path_for/_stash_register/_stash_lookup/
_purge_commentary_stash）+ 一个简化的 FastAPI 测试客户端。

不打完整 server/app（避免 subscriber/retention/iptv 等模块副作用），用一个最小
FastAPI app 把 stash 路由挂上去就行——但 stash 路由本身在 server/routers/
commentary.py 里依赖 _commentary_lock / COMMENTARY_ENABLED / COMMENTARY_STASH_DIR
/ COMMENTARY_LOCAL_OUTPUT 等顶层符号。最稳的做法：在 server/app.py 顶部 import
后，再把这些符号 env 重新设定，然后走 TestClient 全栈。

不打外网：所有 heavy deps (yt-dlp, whisper, ffmpeg-call) 已由 commentary 模块本身的
早绑 stub 覆写；这些测试只触碰 stash 模块，不触发 _commentary_run worker。
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import pytest


@pytest.fixture(scope="module")
def tmp_app(tmp_path_factory):
    """加载 server.app，用临时 DOWNLOAD_DIR + COMMENTARY_OUT。"""
    # 重依赖 stub（必须在 import app 前）
    import types
    fake = types.ModuleType("_fake_ffmpeg")
    fake.run = lambda *a, **kw: None
    sys.modules["_fake_ffmpeg"] = fake

    # 解说 worker stub（切到这个变量真跑时也不会炸）
    commentary_worker_stub = types.ModuleType("commentary_worker_stub")
    sys.modules.setdefault("commentary_worker", commentary_worker_stub)

    # 反指 stash 目录
    stash_root = tmp_path_factory.mktemp("stash-root")
    # mktemp 创建目录 + 同时构造真实的 commentary_out/stash
    os.environ["VDL_COMMENTARY_LOCAL_OUTPUT"] = str(stash_root)
    os.environ["VDL_COMMENTARY_ENABLED"] = "1"
    os.environ.setdefault("VDL_COMMENTARY_STASH_RETENTION_DAYS", "14")

    sys.path.insert(0, str(HERE.parent / "server"))
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as _app  # noqa: E402

    return _app, stash_root / "commentary_out" / "stash"


def _payload(seed: int, n: int = 4096) -> bytes:
    base = b"VIDEOTESTDATA" + bytes([seed % 256]) * 16
    return (base * (n // len(base) + 1))[:n]


def test_stash_path_for_uses_safe_ext(tmp_app):
    _app, _ = tmp_app
    p = _app._stash_path_for("abcdef0123456789", ".mp4")
    assert p.name == "abcdef0123456789.mp4"
    # 危险后缀强制回到 .mp4
    p2 = _app._stash_path_for("abcdef0123456789", ".exe")
    assert p2.name.endswith(".mp4")
    # 危险字符清洗：路径分隔符 / 在 windows 下禁掉，这里 mac 上测试用 .name 检查不含 /
    p3 = _app._stash_path_for("../etc/passwd", ".mp4")
    assert "/" not in p3.name
    assert p3.suffix == ".mp4"


def test_stash_register_lookup_roundtrip(tmp_app):
    _app, _ = tmp_app
    payload = _payload(1)
    sha = "0011223344556677"
    p = _app._stash_path_for(sha, ".mp4")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    sid = _app._stash_register(sha, ".mp4", "movie.mp4")
    assert sid == f"stash:{sha}"
    m = _app._stash_lookup(sha)
    assert m is not None
    assert m["name"] == "movie.mp4"
    assert m["size"] == len(payload)
    # 删文件后 lookup 自动注销
    p.unlink()
    assert _app._stash_lookup(sha) is None


def test_purge_removes_old_files(tmp_app):
    _app, _ = tmp_app
    sha = "ffeeddccbbaa9988"
    p = _app._stash_path_for(sha, ".mp4")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 100)
    # 把 mtime 调到 30 天前
    old = time.time() - 30 * 86400
    os.utime(p, (old, old))
    n = _app._purge_commentary_stash()
    assert n >= 1
    assert not p.exists()


def test_purge_keeps_recent(tmp_app):
    _app, _ = tmp_app
    sha = "1122334455667788"
    p = _app._stash_path_for(sha, ".mp4")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"y" * 100)
    # mtime 仍然新鲜
    n = _app._purge_commentary_stash()
    assert n == 0
    assert p.exists()
    p.unlink()


def test_resolve_source_stash_id_hits_cache(tmp_app):
    _app, _ = tmp_app
    payload = _payload(42)
    sha = "abcd1234abcd1234"
    p = _app._stash_path_for(sha, ".mp4")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    _app._stash_register(sha, ".mp4", "x.mp4")

    obj = _app.CommentaryRequest(file_id=f"stash:{sha}", vertical=False, voice="",
        trim_start=0.0, trim_end=0.0, mode="highlights")
    path = _app._resolve_source(obj)
    assert Path(path).read_bytes() == payload


def test_resolve_source_stash_id_missing_404(tmp_app):
    _app, _ = tmp_app
    obj = _app.CommentaryRequest(file_id="stash:0000000000000000", vertical=False,
        voice="", trim_start=0.0, trim_end=0.0, mode="highlights")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _app._resolve_source(obj)
    assert ei.value.status_code == 404


def test_stash_endpoint_full_flow(tmp_app):
    """走 FastAPI TestClient 真打端点：上传 → 命中 → 重传 from_cache=True。"""
    from fastapi.testclient import TestClient
    _app, _ = tmp_app
    if getattr(_app, "COMMENTARY_ENABLED", False) is False:
        pytest.skip("commentary module 未启用")
    client = TestClient(_app.app, raise_server_exceptions=False)
    payload = _payload(7)
    # 1) 首次
    r1 = client.post("/api/commentary/stash",
                     files={"file": ("a.mp4", io.BytesIO(payload), "video/mp4")})
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    assert d1["from_cache"] is False
    assert d1["id"].startswith("stash:")
    # 2) 再传
    r2 = client.post("/api/commentary/stash",
                     files={"file": ("renamed.mp4", io.BytesIO(payload), "video/mp4")})
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["from_cache"] is True
    assert d2["id"] == d1["id"]


def test_stash_endpoint_rejects_non_video_ext(tmp_app):
    from fastapi.testclient import TestClient
    _app, _ = tmp_app
    if getattr(_app, "COMMENTARY_ENABLED", False) is False:
        pytest.skip("commentary module 未启用")
    client = TestClient(_app.app, raise_server_exceptions=False)
    r = client.post("/api/commentary/stash",
                    files={"file": ("a.txt", io.BytesIO(b"hello"), "text/plain")})
    # .txt 不在 _STASH_VIDEO_EXTS → 应 409
    assert r.status_code == 409
