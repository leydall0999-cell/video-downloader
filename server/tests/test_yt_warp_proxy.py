#!/usr/bin/env python3
"""YouTube 专属出口代理（VDL_PROXY_YT → Cloudflare WARP）回归测试（2026-10-01）。

背景（香港节点实测定版，用户反复投诉「老是出问题」的根因）
------------------------------------
用户在网页版解析 `youtu.be/fvWZRpzQlCw`（oEmbed 证实视频活着），得到
「YouTube 需要登录 Cookie 才能解析」。矩阵实测（同一视频、同一份带登录态的
公共池 Cookie、同一 yt-dlp 2026.8.19）：

    HK 机房 IP  + 长链 + cookiefile(.youtube.com) + Cookie → BOT（1~2s 即拒）
    HK 机房 IP  + 长链 + 不带 Cookie                      → BOT
    HK 机房 IP  + 4 条对照视频                             → 仅 1 条能过
    同机 WARP 出口（socks5h://127.0.0.1:40000）± Cookie    → 8/8 全过

即：这是**机房 IP 信誉**问题，Cookie 作用域 / player_client / 贴 Cookie 全都
救不了（1~2 秒即拒 = YouTube 在 IP 层直接拒）。唯一实测有效的彻底解法 =
解析与下载出口换成 Cloudflare WARP。

契约（本测试钉住）
  1. `_resolve_proxy` 对 YouTube 系域名（youtube.com / youtu.be / googlevideo.com /
     youtube-nocookie.com / ytimg.com）在设置了 VDL_PROXY_YT 时**必须**返回它；
  2. googlevideo.com 必须一起命中——流地址与解析出口 IP 绑定，下载不走同一
     出口会 403；
  3. 未设置 VDL_PROXY_YT 时回落原规则（VDL_PROXY），行为完全不变；
  4. 国内站（bilibili.com 等）绝不受 VDL_PROXY_YT 影响（仍走 VDL_PROXY_CN/直连）；
  5. VDL_PROXY_YT 空白字符串视同未设置。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import downloader as dl  # noqa: E402


def test_youtube_hosts_pick_warp_proxy():
    os.environ["VDL_PROXY_YT"] = "socks5h://127.0.0.1:40000"
    os.environ.pop("VDL_PROXY", None)
    for host in (
        "www.youtube.com",
        "youtu.be",
        "rr3---sn-npoe7ney.googlevideo.com",
        "www.youtube-nocookie.com",
        "i.ytimg.com",
    ):
        got = dl._resolve_proxy(host)
        assert got == "socks5h://127.0.0.1:40000", f"{host} 应走 VDL_PROXY_YT，实际 {got!r}"
    print("✅ YouTube 系域名（含 googlevideo 流 CDN）命中 VDL_PROXY_YT")


def test_fallback_without_env():
    os.environ.pop("VDL_PROXY_YT", None)
    os.environ["VDL_PROXY"] = "http://fallback:8080"
    got = dl._resolve_proxy("www.youtube.com")
    assert got == "http://fallback:8080", f"未设 VDL_PROXY_YT 应回落 VDL_PROXY，实际 {got!r}"
    # 空白字符串视同未设置
    os.environ["VDL_PROXY_YT"] = "   "
    got = dl._resolve_proxy("youtu.be")
    assert got == "http://fallback:8080", f"空白 VDL_PROXY_YT 视同未设置，实际 {got!r}"
    os.environ.pop("VDL_PROXY", None)
    print("✅ 未设置/空白 VDL_PROXY_YT 时回落 VDL_PROXY（行为不变）")


def test_china_hosts_unaffected():
    os.environ["VDL_PROXY_YT"] = "socks5h://127.0.0.1:40000"
    os.environ["VDL_PROXY_CN"] = ""
    for host in ("www.bilibili.com", "v.douyin.com", "www.iqiyi.com"):
        got = dl._resolve_proxy(host)
        assert got != "socks5h://127.0.0.1:40000", f"国内站 {host} 绝不能走 WARP"
    print("✅ 国内站不受 VDL_PROXY_YT 影响")


def test_stream_host_helper():
    cases = {
        "www.youtube.com": True,
        "youtube.com": True,
        "youtu.be": True,
        "a.b.googlevideo.com": True,
        "googlevideo.com.evil.io": False,   # 后缀伪装必须挡住
        "notyoutube.com": False,            # 子串伪装必须挡住
        "www.bilibili.com": False,
        "": False,
    }
    for host, want in cases.items():
        got = dl._is_youtube_stream_host(host)
        assert got == want, f"_is_youtube_stream_host({host!r}) = {got}，期望 {want}"
    print("✅ 域匹配无后缀/子串伪装漏洞")


if __name__ == "__main__":
    test_youtube_hosts_pick_warp_proxy()
    test_fallback_without_env()
    test_china_hosts_unaffected()
    test_stream_host_helper()
    print("\n✅ VDL_PROXY_YT 出口分流回归测试全部通过")
