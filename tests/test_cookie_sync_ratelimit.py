"""公共 Cookie 池同步限流（_sync_rate_ok）的域级豁免回归测试。

背景
----
`/api/cookie/sync` 原为「单 IP 30 秒至多一次」限流。但桌面端「同步 Cookie 到
云端」是**按域批量推送**：本机浏览器登录了十来个站，就顺序推十几次。按 IP 限流
导致除第一个域以外全部 `429`，其中排在末尾的 `youtube.com` / `youku.com` 必然
失败 —— YouTube 登录态自愈链路（桌面端自动推送 → 云端 → 海外对端）因此断掉。

修法：`_sync_rate_ok(ip, scope)` 传 scope 时按 **(IP, scope)** 计数；令牌鉴权的
`/api/cookie/sync` 传 domain 作 scope，网页访客的 `/api/cookie/contribute` 不传、
仍按单 IP。

本组测试锁定：① 同域仍受限；② 同 IP 多域突发全部放行；③ 不传 scope 仍是单 IP；
④ 环回豁免。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import app as vdl_app

_IP = "203.0.113.9"  # TEST-NET-3，绝不会是真实客户端


@pytest.fixture(autouse=True)
def _clean_buckets():
    vdl_app._SYNC_RL["ts"].clear()
    yield
    vdl_app._SYNC_RL["ts"].clear()


def test_same_scope_throttled():
    """同一 (IP, 域) 30 秒内第二次必须被拒。"""
    assert vdl_app._sync_rate_ok(_IP, "youtube.com") is True
    assert vdl_app._sync_rate_ok(_IP, "youtube.com") is False


def test_burst_of_distinct_domains_allowed():
    """桌面端一次批量推送的十来个不同域，必须全部放行（本次回归的核心）。"""
    domains = [
        "bilibili.com", "chenzhongtech.com", "douyin.com", "iesdouyin.com",
        "kuaishou.com", "qq.com", "tiktok.com", "xiaohongshu.com",
        "youku.com", "youtube.com",
    ]
    assert all(vdl_app._sync_rate_ok(_IP, d) is True for d in domains)


def test_no_scope_still_per_ip():
    """不传 scope（访客 contribute）保持单 IP 限流，且与带 scope 的桶互不干扰。"""
    assert vdl_app._sync_rate_ok(_IP) is True
    assert vdl_app._sync_rate_ok(_IP) is False
    assert vdl_app._sync_rate_ok(_IP, "youtube.com") is True


def test_loopback_exempt():
    """环回客户端（桌面 App 自连本机后端）不限流。"""
    for _ in range(5):
        assert vdl_app._sync_rate_ok("127.0.0.1", "youtube.com") is True
        assert vdl_app._sync_rate_ok("::1") is True


def test_different_ips_are_independent():
    """不同 IP 各自成桶。"""
    assert vdl_app._sync_rate_ok("198.51.100.7", "youtube.com") is True
    assert vdl_app._sync_rate_ok("198.51.100.8", "youtube.com") is True
