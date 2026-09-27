"""YouTube 解析提速：player_skip=configs（来自竞品 DataTool 的 yt_dlp_bridge.py）。

回归背景（2026-09-27）：香港节点实测 yt-dlp 2026.8.19（桌面端走默认客户端链），
开启 player_skip=["configs"] 后，同一视频两次解析：
  格式数 49→49、协议分布 mhtml×4 + m3u8_native×17 + https×28 完全不变
  耗时 5.54s→4.18s / 4.11s→4.43s（噪声内，不劣化）
⇒ 省掉一次 ytcfg 请求，且不裁掉 HLS/DASH 格式。

本用例守住两件事：
  1. YouTube 的 opts 里必须带上 player_skip=["configs"]
  2. VDL_YT_PLAYER_SKIP=0 时能整段关掉（线上紧急回滚，不用发版）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

import pytest  # noqa: E402

import downloader as dl  # noqa: E402


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """_base_options 会走代理探测/浏览器 cookie 探测，测试里一律短路。"""
    monkeypatch.setattr(dl, "_resolve_proxy", lambda *a, **kw: "")
    monkeypatch.setattr(dl, "_cn_proxy_url", lambda *a, **kw: "")


def _yt_args(**kw):
    opts = dl._base_options(host="youtube.com", **kw)
    return (opts.get("extractor_args") or {}).get("youtube") or {}


def test_youtube_opts_carry_player_skip_configs(monkeypatch):
    monkeypatch.delenv("VDL_YT_PLAYER_SKIP", raising=False)
    assert _yt_args().get("player_skip") == ["configs"]


def test_youtube_player_skip_can_be_disabled_by_env(monkeypatch):
    """VDL_YT_PLAYER_SKIP=0：整段关掉（线上紧急回滚通道）。"""
    monkeypatch.setenv("VDL_YT_PLAYER_SKIP", "0")
    assert "player_skip" not in _yt_args()


@pytest.mark.parametrize("flag", ["false", "no", "off", "FALSE"])
def test_youtube_player_skip_off_variants(monkeypatch, flag):
    monkeypatch.setenv("VDL_YT_PLAYER_SKIP", flag)
    assert "player_skip" not in _yt_args()


def test_youtu_be_short_host_also_covered(monkeypatch):
    monkeypatch.delenv("VDL_YT_PLAYER_SKIP", raising=False)
    opts = dl._base_options(host="youtu.be")
    ya = (opts.get("extractor_args") or {}).get("youtube") or {}
    assert ya.get("player_skip") == ["configs"]


def test_non_youtube_host_untouched(monkeypatch):
    """非 YouTube 站不得被塞入 youtube extractor args。"""
    monkeypatch.delenv("VDL_YT_PLAYER_SKIP", raising=False)
    opts = dl._base_options(host="bilibili.com", cookie="SESSDATA=x")
    assert (opts.get("extractor_args") or {}).get("youtube") is None
