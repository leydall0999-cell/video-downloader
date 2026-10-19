"""YouTube 解析提速：player_skip=configs（来自竞品 DataTool 的 yt_dlp_bridge.py）。

回归背景（2026-09-27）：香港节点实测 yt-dlp 2026.8.19 + player_client=web_safari，
开启 player_skip=["configs"] 后，同一视频两次解析耗时 5.63s→4.52s、4.83s→4.49s，
而格式数（11）与协议分布（mhtml×4 + m3u8_native×6 + https×1）完全不变，
即「省掉一次 ytcfg 请求」且不裁掉 HLS/DASH 格式。

本用例守住两件事：
  1. YouTube 的 opts 里必须带上 player_skip=["configs"]
  2. VDL_YT_PLAYER_SKIP=0 时能整段关掉（线上紧急回滚，不用发版）

🔴 2026-10-19 变更：不再强制 player_client=web_safari，改走 yt-dlp 默认客户端链。
香港节点矩阵实测（CuK9hrKXb8U，visitor_data 已注入、fetch_pot=never）：
    (default 链)  → 9.5s / 50 格式（m3u8_native×16 最高 1440p + https×30）
    web_safari    → FAIL "Requested format is not available"（js 开/关都败）
    android       → 仅 5 格式（无 m3u8）；tv/ios/mweb/web/web_embedded/tv_simply 全 FAIL
web_safari 是 2026-09-21 按「与 bgutil PO Token 同 client」选的，但那只在
**POT 真可用**时成立；POT 不可用时它连格式列表都拿不到 ⇒ 网页版解析全坏。
player_client 是「合并」模式，传任何单客户端都会覆盖默认链，故一个都不传。
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
    """YouTube：必须带 player_skip=configs，且不得强制 player_client。"""
    monkeypatch.delenv("VDL_YT_PLAYER_SKIP", raising=False)
    monkeypatch.delenv("VDL_YT_HLS_CLIENT", raising=False)
    ya = _yt_args()
    assert "player_client" not in ya, (
        f"默认客户端链才能拿到完整格式列表，不得强制 player_client（HK 矩阵："
        f"web_safari 在 POT 不可用时直接 'format is not available'）：{ya}")
    assert ya.get("player_skip") == ["configs"], f"应带 player_skip=configs：{ya}"


def test_youtube_player_skip_can_be_disabled_by_env(monkeypatch):
    """VDL_YT_PLAYER_SKIP=0：整段关掉（线上紧急回滚通道）。"""
    monkeypatch.setenv("VDL_YT_PLAYER_SKIP", "0")
    monkeypatch.delenv("VDL_YT_HLS_CLIENT", raising=False)
    ya = _yt_args()
    assert "player_skip" not in ya, f"关掉后不应有 player_skip：{ya}"
    assert "player_client" not in ya


@pytest.mark.parametrize("flag", ["false", "no", "off", "FALSE"])
def test_youtube_player_skip_off_variants(monkeypatch, flag):
    monkeypatch.setenv("VDL_YT_PLAYER_SKIP", flag)
    assert "player_skip" not in _yt_args()


def test_youtu_be_short_host_also_covered(monkeypatch):
    """youtu.be 短链同样要走 YouTube 提速参数。"""
    monkeypatch.delenv("VDL_YT_PLAYER_SKIP", raising=False)
    opts = dl._base_options(host="youtu.be")
    ya = (opts.get("extractor_args") or {}).get("youtube") or {}
    assert ya.get("player_skip") == ["configs"]


def test_non_youtube_host_untouched(monkeypatch):
    """非 YouTube 站不得被塞入 youtube extractor args。"""
    monkeypatch.delenv("VDL_YT_PLAYER_SKIP", raising=False)
    opts = dl._base_options(host="bilibili.com", cookie="SESSDATA=x")
    assert (opts.get("extractor_args") or {}).get("youtube") is None
