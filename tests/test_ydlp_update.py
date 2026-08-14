"""ydlp_update 单元测试：bootstrap 路径插入 / Zip Slip 防护 / sha256 校验。"""
import hashlib
import importlib
import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import ydlp_update


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    importlib.reload(ydlp_update)
    return ydlp_update


def _make_wheel(members: dict) -> bytes:
    buf = io.BytesIO()
    zf = zipfile.ZipFile(buf, "w")
    for name, content in members.items():
        zf.writestr(name, content)
    zf.close()
    return buf.getvalue()


class _FakeResp:
    def __init__(self, content):
        self._c = content

    def raise_for_status(self):
        pass

    @property
    def content(self):
        return self._c


def _mount(fresh, monkeypatch, wheel_bytes, sha=None, version="9999.0"):
    meta = {
        "info": {"version": version},
        "urls": [
            {"filename": "y.whl", "url": "https://x/y.whl", "digests": {"sha256": sha} if sha else {}},
        ],
    }

    class FakeMeta:
        def raise_for_status(self):
            pass

        def json(self):
            return meta

    def fake_get(url, *a, **k):
        if url.endswith("/json"):
            return FakeMeta()
        return _FakeResp(wheel_bytes)

    monkeypatch.setattr(fresh.requests, "get", fake_get)


def test_bootstrap_inserts_sys_path(fresh, tmp_path):
    pkg = tmp_path / ".videodownloader" / "yt_dlp" / "yt_dlp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    sys.path[:] = [p for p in sys.path if "yt_dlp" not in p]
    fresh.bootstrap()
    assert sys.path[0] == str(tmp_path / ".videodownloader" / "yt_dlp")


def test_update_rejects_zip_slip(fresh, tmp_path, monkeypatch):
    wb = _make_wheel({"yt_dlp/__init__.py": "# ok", "../sibling_evil.py": "pwn"})
    _mount(fresh, monkeypatch, wb, sha=hashlib.sha256(wb).hexdigest())
    res = fresh.update()
    assert res["ok"] is False
    assert not (tmp_path / ".videodownloader" / "sibling_evil.py").exists()


def test_update_success_when_valid(fresh, tmp_path, monkeypatch):
    wb = _make_wheel({"yt_dlp/__init__.py": "# ok", "yt_dlp/__main__.py": "x"})
    _mount(fresh, monkeypatch, wb, sha=hashlib.sha256(wb).hexdigest())
    res = fresh.update()
    assert res["ok"] is True and res["updated"] is True
    assert (tmp_path / ".videodownloader" / "yt_dlp" / "yt_dlp" / "__init__.py").exists()


def test_update_rejects_bad_sha(fresh, tmp_path, monkeypatch):
    wb = _make_wheel({"yt_dlp/__init__.py": "# ok"})
    _mount(fresh, monkeypatch, wb, sha="0" * 64)
    res = fresh.update()
    assert res["ok"] is False
    assert "校验和" in res["error"]
