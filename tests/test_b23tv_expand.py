"""测试 b23.tv 短链展开为 bilibili.com 长链。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from downloader import _expand_b23tv_url, _normalize_share_url


class _FakeRequestsResponse:
    def __init__(self, status_code: int, location: str = "", url: str = "", history=None):
        self.status_code = status_code
        self.headers = {"Location": location} if location else {}
        self.url = url
        self.history = history or []
        self._closed = False

    def close(self):
        self._closed = True


def test_expand_b23tv_random_short(monkeypatch):
    """随机短码通过 HEAD 302 展开成长链。"""
    calls = []

    def fake_head(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        calls.append({"url": url, "proxies": proxies})
        return _FakeRequestsResponse(
            302, "https://www.bilibili.com/video/BV1gtgE6AEmZ?p=2"
        )

    monkeypatch.setattr("requests.head", fake_head)
    url = _expand_b23tv_url("https://b23.tv/RYQqYpV")
    assert url == "https://www.bilibili.com/video/BV1gtgE6AEmZ?p=2"
    assert len(calls) == 1


def test_expand_b23tv_bv_short_no_request(monkeypatch):
    """b23.tv/BVxxx 直接推导成长链，不必发网络请求；保留 p/t 参数。"""
    called = {"n": 0}

    def fake_head(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        called["n"] += 1
        return _FakeRequestsResponse(200)

    monkeypatch.setattr("requests.head", fake_head)
    url = _expand_b23tv_url("https://b23.tv/BV1Rh411h7Fp?p=3")
    assert url == "https://www.bilibili.com/video/BV1Rh411h7Fp?p=3"
    assert called["n"] == 0


def test_expand_b23tv_non_b23_url_unchanged():
    assert (
        _expand_b23tv_url("https://www.bilibili.com/video/BV1Rh411h7Fp")
        == "https://www.bilibili.com/video/BV1Rh411h7Fp"
    )


def test_normalize_share_url_expands_b23tv_random(monkeypatch):
    """_normalize_share_url 对 b23.tv 随机短链：展开→净化→bilibili 长链。"""

    def fake_head(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        return _FakeRequestsResponse(
            302, "https://www.bilibili.com/video/BV1gtgE6AEmZ?vd_source=abc&p=2"
        )

    monkeypatch.setattr("requests.head", fake_head)
    url = _normalize_share_url("https://b23.tv/RYQqYpV")
    assert url == "https://www.bilibili.com/video/BV1gtgE6AEmZ?p=2"


def test_expand_b23tv_head_falls_back_to_get(monkeypatch):
    """HEAD 拿不到 Location 时，回退 GET 跟随重定向取最终 URL。"""
    calls = {"head": 0, "get": 0}

    def fake_head(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None):
        calls["head"] += 1
        # 200 无 Location，逼出 GET 回退
        return _FakeRequestsResponse(200)

    def fake_get(url, *, headers=None, proxies=None, timeout=None, allow_redirects=None, stream=None):
        calls["get"] += 1
        return _FakeRequestsResponse(
            200,
            url="https://www.bilibili.com/video/BV1gtgE6AEmZ",
        )

    monkeypatch.setattr("requests.head", fake_head)
    monkeypatch.setattr("requests.get", fake_get)
    url = _expand_b23tv_url("https://b23.tv/RYQqYpV")
    assert url == "https://www.bilibili.com/video/BV1gtgE6AEmZ"
    assert calls["head"] == 1
    assert calls["get"] == 1


if __name__ == "__main__":
    # 简单自测（无 monkeypatch）
    assert _expand_b23tv_url("https://example.com/") == "https://example.com/"
    print("b23tv expand: basic passed (run with pytest for full)")
