#!/usr/bin/env python3
"""零依赖的国内正向代理（HTTP + HTTPS/CONNECT 隧道）。

用途：video-downloader 部署在海外（Railway 等）时，国内站（B站/抖音/腾讯…）
会被地理围栏拦截。把本代理跑在**物理位于国内**的机器上，再用 ngrok/cloudflared
把它的端口暴露成公网 URL，最后在 Railway 设置：

    VDL_PROXY_CN=http://<公网URL>

yt-dlp 解析/下载国内站就会经此代理回源，绕开地域封锁。

仅用 Python 标准库，无需 pip 安装任何东西。

用法：
    python3 cn_proxy.py [端口]      # 默认 8899，监听 0.0.0.0
"""
from __future__ import annotations

import select
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899

# 跳过的逐跳头，避免代理把自身连接头转发给上游
_HOP_BY_HOP = {
    "proxy-connection", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VDL-CN-Proxy/1.0"

    # ---- HTTPS：客户端发 CONNECT host:port，代理建立 TCP 隧道双向转发 ----
    _counter = 0

    def do_CONNECT(self) -> None:
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
        client.setblocking(False)
        upstream.setblocking(False)
        conns = [client, upstream]
        try:
            while True:
                readable, _, _ = select.select(conns, [], [], 120)
                if not readable:
                    return
                for src in readable:
                    dst = upstream if src is client else client
                    try:
                        data = src.recv(65536)
                    except (BlockingIOError, OSError):
                        return
                    if not data:
                        return
                    try:
                        dst.sendall(data)
                    except OSError:
                        return
        finally:
            for s in conns:
                try:
                    s.close()
                except OSError:
                    pass

    def log_message(self, *args) -> None:  # 静默
        pass


def main() -> None:
    httpd = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"[cn_proxy] listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
