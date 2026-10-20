#!/usr/bin/env python3
"""YouTube 解析入口的「本机 socks5 出口预检」回归测试（2026-10-21）。

背景（香港节点实测定版，网页版「请求超时（超过 120s）」真根因）
--------------------------------------------------
warp-svc 进程活着但 IPC 卡死 → 127.0.0.1:40000 无监听，而 vdl-web 的
VDL_PROXY_YT=socks5h://127.0.0.1:40000 仍指向它。yt-dlp 把每个请求都打到
死端口，按 socket_timeout + 重试链逐次撞墙，实测累计 120s+ 才吐错，用户
只看到一句无信息红字「请求超时，请重试或检查网络」，白等两分钟。
同一时刻普通视频解析正常，因此极易误判成「这条视频有问题」。

三层防线（本测试钉住第二层）：
  1. 入口预检（本文件）：socks5 不可达 → 0.6s 内明确报「线路维护中」，
     category=network（前端引导「可重试」），绝不发起任何网络请求；
  2. 运维自愈：vdl-warp-watchdog.timer 每分钟探测 generate_204，
     不可用则 warp-cli connect → systemctl restart warp-svc；
  3. 出口切换：VDL_PROXY_YT 指向别的 socks 即换出口，代码不用改。

契约：
  1. `_local_socks_alive` 只对**本机** socks5 端口做判定，非 socks 协议、
     远端 socks、空串、畸形串一律返回 True（探测无意义，不能误拦）；
  2. socks4/socks5/socks5h、带 user:pw@、大写、localhost 形态都要识别；
  3. 死端口返回 False，活端口返回 True；
  4. `_resolve_youtube` 入口遇到不可达的本机 socks 必须抛 ResolveError
     且 category=network，且在抛错前**不能**去取 visitor_data（证明没打网络）。
"""

import os
import socket
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import downloader as dl  # noqa: E402


# 起一个真正 accept 的本机监听。注意 listen backlog 要足够大：
# 只 bind+listen 不 accept 时，backlog 被未决连接占满会让第二次 connect
# 失败，从而把「活端口」误判成死端口（第一版自测就这么假失败过）。
def _spawn_listener():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    port = srv.getsockname()[1]

    def _accept_loop():
        while True:
            try:
                conn, _ = srv.accept()
                conn.close()
            except OSError:
                return

    threading.Thread(target=_accept_loop, daemon=True).start()
    return port


def _dead_port():
    """拿一个此刻一定没有服务在听的端口（bind 端口 0 后立即关闭）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_local_socks_alive_forms():
    live = _spawn_listener()
    dead = _dead_port()
    cases = [
        (f"socks5h://127.0.0.1:{live}", True, "socks5h 活端口"),
        (f"socks5h://user:pw@127.0.0.1:{live}", True, "带账号的 socks5h"),
        (f"socks5h://localhost:{live}", True, "localhost 形态"),
        (f"socks4://127.0.0.1:{live}", True, "socks4 形态"),
        (f"SOCKS5H://127.0.0.1:{live}", True, "大写协议名"),
        (f"socks5h://127.0.0.1:{dead}", False, "本机死端口必须判死"),
        ("http://vdl:x@47.82.101.79:8080", True, "http 代理不拦"),
        ("socks5h://1.2.3.4:1080", True, "远端 socks 不拦（本机探不到）"),
        ("", True, "空串不拦"),
        ("socks5h://nodport", True, "畸形串不拦"),
        ("   ", True, "空白串不拦"),
    ]
    for proxy, want, label in cases:
        got = dl._local_socks_alive(proxy)
        assert got == want, f"{label}：_local_socks_alive({proxy!r}) = {got}，期望 {want}"
    print("✅ _local_socks_alive 形态判定（活/死端口、协议变体、非本机不拦）")


def test_resolve_entry_blocks_dead_socks_without_network():
    """死端口 socks 必须在入口就抛错，且一次网络请求都不发。"""
    dead = _dead_port()
    visited = []
    original = dl._fetch_youtube_visitor_data

    def _spy(_proxy):
        visited.append(_proxy)
        return ""

    dl._fetch_youtube_visitor_data = _spy
    try:
        dl._resolve_youtube(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            proxy=f"socks5h://127.0.0.1:{dead}",
        )
        raise AssertionError("本机 socks5 不可达时应直接抛错，不该继续解析")
    except dl.ResolveError as exc:
        assert exc.category == "network", f"category 应为 network，实际 {exc.category!r}"
        assert "维护" in str(exc), f"文案应说明线路维护中，实际 {exc!r}"
        assert not visited, "预检失败时绝不该去取 visitor_data（等于打了网络）"
    finally:
        dl._fetch_youtube_visitor_data = original
    print("✅ 死端口 socks：入口 0.6s 内抛 network 错，零网络请求")


def test_resolve_entry_not_blocked_when_proxy_ok():
    """socks 可达时入口绝不误拦（继续走正常解析流程）。"""
    live = _spawn_listener()
    visited = []
    original = dl._fetch_youtube_visitor_data

    def _spy(_proxy):
        visited.append(_proxy)
        # 抛一个可识别的哨兵异常，证明「已经越过预检、进入了正常流程」
        raise dl.ResolveError("__PASSED_PREFLIGHT__", category="sentinel")

    dl._fetch_youtube_visitor_data = _spy
    try:
        dl._resolve_youtube(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            proxy=f"socks5h://127.0.0.1:{live}",
        )
        raise AssertionError("哨兵异常应穿透出来")
    except dl.ResolveError as exc:
        assert str(exc) == "__PASSED_PREFLIGHT__", f"应越过预检，实际 {exc!r}"
        assert visited, "socks 可达时应继续取 visitor_data"
    finally:
        dl._fetch_youtube_visitor_data = original
    print("✅ socks 可达时入口不误拦，正常流程继续")


def test_resolve_entry_no_proxy_not_blocked():
    """无代理（国内直连场景）同样不拦。"""
    visited = []
    original = dl._fetch_youtube_visitor_data

    def _spy(_proxy):
        visited.append(_proxy)
        raise dl.ResolveError("__PASSED_PREFLIGHT__", category="sentinel")

    dl._fetch_youtube_visitor_data = _spy
    saved = {k: os.environ.pop(k, None) for k in ("VDL_PROXY_YT", "VDL_PROXY")}
    try:
        dl._resolve_youtube("https://www.youtube.com/watch?v=dQw4w9WgXcQ", proxy="")
        raise AssertionError("哨兵异常应穿透出来")
    except dl.ResolveError as exc:
        assert str(exc) == "__PASSED_PREFLIGHT__", f"无代理时不该被拦，实际 {exc!r}"
    finally:
        dl._fetch_youtube_visitor_data = original
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    print("✅ 无代理场景不拦（国内直连/未配置出口时行为不变）")


if __name__ == "__main__":
    test_local_socks_alive_forms()
    test_resolve_entry_blocks_dead_socks_without_network()
    test_resolve_entry_not_blocked_when_proxy_ok()
    test_resolve_entry_no_proxy_not_blocked()
    print("\n✅ test_yt_socks_preflight 全部通过")
