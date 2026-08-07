#!/usr/bin/env python3
"""零依赖的国内正向代理（HTTP + HTTPS/CONNECT 隧道），支持可选 Basic 鉴权。

用途：video-downloader 部署在海外（Railway 等）时，国内站（B站/抖音/腾讯…）
会被地理围栏拦截。把本代理跑在**物理位于国内**的机器上，再用公网 IP / 隧道
暴露它的端口，最后在 Railway 设置：

    VDL_PROXY_CN=http://user:pass@<公网地址>:端口

yt-dlp 解析/下载国内站就会经此代理回源，绕开地域封锁。

仅用 Python 标准库，无需 pip 安装任何东西。

用法：
    python3 cn_proxy.py [端口]                # 默认 8899，监听 0.0.0.0
    CN_PROXY_AUTH=user:pass python3 cn_proxy.py 18888   # 启用 Basic 鉴权（上公网必开）

鉴权：
    - 设置 CN_PROXY_AUTH=user:pass 后，所有请求（含 HTTPS 的 CONNECT）必须带
      Proxy-Authorization: Basic <base64(user:pass)>，否则返回 407。
    - 不设置则行为不变（本地 / 已限来源 IP 场景用）。
"""
from __future__ import annotations

import base64
import os
import select
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899

# 可选 Basic 鉴权：设置 CN_PROXY_AUTH=user:pass 后，所有请求（含 CONNECT）必须带
# Proxy-Authorization: Basic base64(user:pass)，否则返回 407。
_AUTH = os.environ.get("CN_PROXY_AUTH", "").strip()
_EXPECTED = ("Basic " + base64.b64encode(_AUTH.encode("utf-8")).decode("ascii")) if _AUTH else None

# 跳过的逐跳头，避免代理把自身连接头转发给上游
_HOP_BY_HOP = {
    "proxy-connection", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VDL-CN-Proxy/1.1"

    # ---- 鉴权：所有请求入口先过这一关 ----
    _counter = 0

    def _authenticate(self) -> bool:
        if not _EXPECTED:
            return True
        if self.headers.get("Proxy-Authorization", "") == _EXPECTED:
            return True
        self.send_response(407, "Proxy Authentication Required")
        self.send_header("Proxy-Authenticate", 'Basic realm="VDL-CN-Proxy"')
        self.send_header("Connection", "close")
        self.end_headers()
        return False

    # ---- HTTPS：客户端发 CONNECT host:port，代理建立 TCP 隧道双向转发 ----
    def do_CONNECT(self) -> None:
        if not self._authenticate():
            return
        host, _, port = self.path.partition(":")
        port = int(port) or 443
        Handler._counter += 1
        print(f"[cn_proxy] #{Handler._counter} CONNECT {host}:{port}", flush=True)
        try:
            upstream = socket.create_connection((host, port), timeout=20)
        except OSError as e:
            self.send_error(502, f"Bad gateway: {e}")
            return
        self.send_response(200, "Connection Established")
        self.send_header("Connection", "close")
        self.end_headers()
        self._tunnel(self.connection, upstream)

    # ---- HTTP：客户端发绝对 URI（GET http://...），代理代为请求 ----
    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_HEAD(self) -> None:
        self._proxy()

    def _proxy(self) -> None:
        if not self._authenticate():
            return
        target = self.path
        Handler._counter += 1
        print(f"[cn_proxy] #{Handler._counter} {self.command} {target[:120]}", flush=True)
        if not target.startswith("http://"):
            self.send_error(400, "Only absolute-form HTTP proxy requests supported")
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else None
            req = urllib.request.Request(target, data=body, method=self.command)
            for k, v in self.headers.items():
                if k.lower() in _HOP_BY_HOP:
                    continue
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() in _HOP_BY_HOP:
                        continue
                    self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as e:  # noqa: BLE001
            try:
                self.send_error(502, f"Bad gateway: {e}")
            except Exception:
                pass

    def _tunnel(self, client: socket.socket, upstream: socket.socket) -> None:
        # 非阻塞双向转发 + 发送缓冲。
        # 重要：sendall 在非阻塞 socket 上遇到背压会抛 BlockingIOError，绝不能当致命错误
        # 关掉整条隧道——否则下载大文件时只要对端接收稍慢就会被掐断（IncompleteRead）。
        client.setblocking(False)
        upstream.setblocking(False)
        socks = [client, upstream]
        peer = {client: upstream, upstream: client}
        buf = {client: b"", upstream: b""}  # 待发给各端的数据（背压时堆积）
        try:
            while True:
                rlist, wlist, _ = select.select(
                    socks,                       # 谁可读
                    [s for s in socks if buf[s]],# 谁还有积压数据待发
                    [], 120,
                )
                if not rlist and not wlist:
                    return  # 120s 无活动，超时关闭
                for src in rlist:
                    try:
                        data = src.recv(65536)
                    except (BlockingIOError, OSError):
                        continue  # 临时错误，下一轮再试
                    if not data:
                        # 该方向 EOF：不再读它，并通知对端"写端关闭"
                        socks.remove(src)
                        try:
                            peer[src].shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        continue
                    buf[peer[src]] += data
                for dst in wlist:
                    if not buf[dst]:
                        continue
                    try:
                        sent = dst.send(buf[dst])
                        buf[dst] = buf[dst][sent:]
                    except (BlockingIOError, OSError):
                        continue  # 发送缓冲满，下一轮再发
                # 双方都已 EOF 且积压清空 → 干净退出
                if not socks and not any(buf.values()):
                    return
        finally:
            for s in (client, upstream):
                try:
                    s.close()
                except OSError:
                    pass

    def log_message(self, *args) -> None:  # 静默
        pass


def main() -> None:
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    if _AUTH:
        note = f" (auth=on, user={_AUTH.split(':', 1)[0]})"
    else:
        note = " (auth=OFF — 仅限已限制来源 IP / 随机隧道 URL 场景)"
    print(f"[cn_proxy] listening on {LISTEN_HOST}:{LISTEN_PORT}{note}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
