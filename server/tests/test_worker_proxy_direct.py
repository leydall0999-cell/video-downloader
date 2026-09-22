"""解析 worker 代理路由：直连 / 隧道 两种模式的回归测试。

背景（2026-09-22 实测）：解析入口迁到国内 ECS 后，解析 daemon（127.0.0.1:18731）
与 vdl-web **同机**，而 18889 隧道代理的上游（wss://hanyuxz.top，原 Railway 应用）
已删除。代码却仍然默认把请求塞进隧道 → `/v1/resolve` 秒回
「视频解析服务不可达」（HTTPConnectionPool 127.0.0.1:18889 Max retries exceeded），
而本机 daemon 直连是好的（真实抖音链接 0.4s 解析成功）。整条通道因此长期不可用。

本测试钉住三件事：
  1. `downloader.worker_proxy_url()` 的模式判定（direct/off/none/空 → 直连）；
  2. `_call_vps_worker()` 在直连模式下**不得**带上任何代理（也不得回落环境变量代理）；
  3. 默认（未配置）时保持旧行为 18889，海外 Railway 部署不受影响。

不依赖任何外部网络：伪造 requests Session 捕获出参。
运行：cd server && python tests/test_worker_proxy_direct.py
"""
import os
import sys
import types

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import downloader  # noqa: E402

_ENV_KEYS = ("VDL_WORKER_PROXY", "VDL_COOKIE_PULL_PROXY", "VDL_WORKER_URL",
             "VDL_COOKIE_REFILL_URL", "VDL_COOKIE_SYNC_TOKEN")


def _clear_env():
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


class _FakeResp:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "video_url": "http://cdn/x.mp4", "title": "t"}


class _FakeSession:
    """记录最后一次请求的 endpoint 与 proxies。"""
    last: dict = {}

    def get(self, url, headers=None, proxies=None, timeout=None):
        _FakeSession.last = {"url": url, "proxies": proxies, "timeout": timeout}
        return _FakeResp()


def _patch_requests():
    downloader._requests = types.SimpleNamespace(Session=_FakeSession)
    downloader._worker_http = None


def _call(platform="douyin", url="https://v.douyin.com/abc123/"):
    downloader._RESOLVE_CACHE.clear()
    _FakeSession.last = {}
    downloader._call_vps_worker(platform, url)
    return _FakeSession.last


def test_worker_proxy_url_matrix():
    cases = [
        ({}, "http://127.0.0.1:18889"),                       # 未配置 → 旧默认（海外）
        ({"VDL_WORKER_PROXY": "direct"}, ""),                # 显式直连
        ({"VDL_WORKER_PROXY": "off"}, ""),
        ({"VDL_WORKER_PROXY": "none"}, ""),
        ({"VDL_WORKER_PROXY": ""}, ""),                      # 置空即关闭代理
        ({"VDL_COOKIE_PULL_PROXY": "direct"}, ""),           # 旧变量名同样生效
        ({"VDL_WORKER_PROXY": "direct",
          "VDL_COOKIE_PULL_PROXY": "http://127.0.0.1:18889"}, ""),  # worker 优先
        ({"VDL_WORKER_PROXY": "http://10.0.0.1:3128"}, "http://10.0.0.1:3128"),
    ]
    for env, want in cases:
        _clear_env()
        os.environ.update(env)
        got = downloader.worker_proxy_url()
        assert got == want, f"env={env} 期望 {want!r}，实际 {got!r}"
    print("✅ worker_proxy_url 模式判定正确（direct/off/none/空 → 直连）")


def test_direct_mode_sends_no_proxy():
    _clear_env()
    # 故意注入环境代理：直连模式必须无视它，否则请求会被劫到别处
    os.environ["http_proxy"] = "http://127.0.0.1:9999"
    os.environ["https_proxy"] = "http://127.0.0.1:9999"
    os.environ["VDL_WORKER_PROXY"] = "direct"
    os.environ["VDL_COOKIE_SYNC_TOKEN"] = "tok"
    _patch_requests()
    try:
        req = _call()
        assert req["proxies"] == {"http": "", "https": ""}, \
            f"直连模式不应带代理，实际 {req['proxies']!r}"
        assert "127.0.0.1:18731/v1/resolve" in req["url"], \
            f"直连模式应打本机 daemon，实际 {req['url']!r}"
    finally:
        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)
    print("✅ 直连模式：无代理、直达本机 18731 daemon")


def test_direct_mode_with_remote_base():
    _clear_env()
    os.environ["VDL_WORKER_PROXY"] = "direct"
    os.environ["VDL_WORKER_URL"] = "http://8.138.223.3:8888"
    os.environ["VDL_COOKIE_SYNC_TOKEN"] = "tok"
    _patch_requests()
    req = _call()
    assert req["url"].startswith("http://8.138.223.3:8888/v1/resolve"), \
        f"应打显式 worker 地址，实际 {req['url']!r}"
    assert req["proxies"] == {"http": "", "https": ""}
    print("✅ 显式 worker 地址 + 直连：按配置生效")


def test_default_keeps_tunnel_for_overseas():
    _clear_env()
    os.environ["VDL_COOKIE_SYNC_TOKEN"] = "tok"
    _patch_requests()
    req = _call()
    assert req["proxies"] == {"http": "http://127.0.0.1:18889",
                              "https": "http://127.0.0.1:18889"}, \
        f"未配置时应保持海外默认隧道，实际 {req['proxies']!r}"
    assert "127.0.0.1:18731/v1/resolve" in req["url"]
    print("✅ 未配置：保持 18889 隧道默认（海外 Railway 不受影响）")


def test_misconfigured_cn_proxy_base_still_direct():
    """历史坑：把 cn_proxy(18888) 误填为 worker 目标时，直连模式不得被强塞隧道。"""
    _clear_env()
    os.environ["VDL_WORKER_PROXY"] = "direct"
    os.environ["VDL_WORKER_URL"] = "http://127.0.0.1:18888"
    os.environ["VDL_COOKIE_SYNC_TOKEN"] = "tok"
    _patch_requests()
    req = _call()
    assert "127.0.0.1:18731/v1/resolve" in req["url"], req["url"]
    assert req["proxies"] == {"http": "", "https": ""}, req["proxies"]
    print("✅ 误配 18888 目标 + 直连：纠正到 daemon 且不带代理")


if __name__ == "__main__":
    _clear_env()
    test_worker_proxy_url_matrix()
    test_direct_mode_sends_no_proxy()
    test_direct_mode_with_remote_base()
    test_default_keeps_tunnel_for_overseas()
    test_misconfigured_cn_proxy_base_still_direct()
    _clear_env()
    print("\n✅ 解析 worker 代理路由回归测试全部通过")
