"""测试 b23.tv 短链展开为 bilibili.com 长链。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from downloader import _expand_b23tv_url, _normalize_share_url


class _FakeResponse:
    def __init__(self, url: str):
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeOpener:
    def __init__(self, url: str):
        self._url = url
        self.calls: list[dict] = []

    def open(self, req, timeout=None):
        self.calls.append({"url": req.full_url, "method": req.method})
        return _FakeResponse(self._url)


def test_expand_b23tv_random_short(monkeypatch):
    """随机短码通过 HEAD 302 展开成长链。"""
    def fake_build_opener(*handlers):
        return _FakeOpener("https://www.bilibili.com/video/BV1gtgE6AEmZ?p=2")

    monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
    url = _expand_b23tv_url("https://b23.tv/RYQqYpV")
    assert url == "https://www.bilibili.com/video/BV1gtgE6AEmZ?p=2"


def test_expand_b23tv_bv_short_no_request(monkeypatch):
    """b23.tv/BVxxx 直接推导，不必发网络请求。"""
    called = {"n": 0}

    def fake_build_opener(*handlers):
        called["n"] += 1
        return _FakeOpener("should_not_happen")

    monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
    url = _expand_b23tv_url("https://b23.tv/BV1Rh411h7Fp?p=3")
    assert url == "https://www.bilibili.com/video/BV1Rh411h7Fp"
    assert called["n"] == 0


def test_expand_b23tv_non_b23_url_unchanged():
    assert (
        _expand_b23tv_url("https://www.bilibili.com/video/BV1Rh411h7Fp")
        == "https://www.bilibili.com/video/BV1Rh411h7Fp"
    )


def test_normalize_share_url_expands_b23tv_random(monkeypatch):
    """_normalize_share_url 对 b23.tv 随机短链：展开→净化→bilibili 长链。"""

    def fake_build_opener(*handlers):
        return _FakeOpener("https://www.bilibili.com/video/BV1gtgE6AEmZ?vd_source=abc&p=2")

    monkeypatch.setattr("urllib.request.build_opener", fake_build_opener)
    url = _normalize_share_url("https://b23.tv/RYQqYpV")
    assert url == "https://www.bilibili.com/video/BV1gtgE6AEmZ?p=2"


if __name__ == "__main__":
    # 简单自测（无 monkeypatch）
    assert _expand_b23tv_url("https://example.com/") == "https://example.com/"
    print("b23tv expand: basic passed (run with pytest for full)")
