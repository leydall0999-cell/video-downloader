#!/usr/bin/env python3
"""反向 WebSocket 隧道 client（国内 ECS 端）。

主动出站连 Railway 的 /ws/cn-tunnel（跨境出站稳定），把本机 cn_proxy（127.0.0.1:18888）
经隧道反向暴露给 Railway 本机。收到 Railway 的 open 帧后，连本机 cn_proxy 并双向透传。

与 Railway 端 cn_tunnel.py 配套：整条链路按连接 id 做 raw byte 透传，不解析 HTTP。

依赖：
    pip install websockets

部署（systemd，见 cn_tunnel_client.service）：
    - 放本文件到 /opt/vdl-tunnel/cn_tunnel_client.py
    - 设环境变量 CN_TUNNEL_WS / VDL_TUNNEL_TOKEN / CN_PROXY_LOCAL
    - systemctl enable --now cn_tunnel_client
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [cn_tunnel_client] %(levelname)s %(message)s",
)
log = logging.getLogger()

WS_URL = os.environ.get("CN_TUNNEL_WS", "wss://hanyuxz.top/ws/cn-tunnel").rstrip("/")
TOKEN = os.environ.get(
    "VDL_TUNNEL_TOKEN", "vdl-rv-tunnel-7f3a9c2e-4b1d-8c66-2e5f9a0b3c7d"
)
_CN = os.environ.get("CN_PROXY_LOCAL", "127.0.0.1:18888")
CN_HOST, CN_PORT = _CN.split(":")
CN_PORT = int(CN_PORT)


def _frame(typ: int, id_: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BI", typ, id_) + payload


async def _reader_to_ws(ws, id_: int, reader: asyncio.StreamReader) -> None:
    """把本机 cn_proxy 的响应经隧道发回 Railway。"""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            await ws.send(_frame(1, id_, data))
    except Exception:
        pass
    finally:
        try:
            await ws.send(_frame(2, id_))
        except Exception:
            pass


async def client_loop() -> None:
    url = f"{WS_URL}?token={TOKEN}"
    while True:
        try:
            async with websockets.connect(
                url, ping_interval=20, ping_timeout=30, max_size=None
            ) as ws:
                log.info("隧道已连 Railway: %s", WS_URL)
                conns: dict[int, asyncio.StreamWriter] = {}
                async for raw in ws:
                    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 5:
                        continue
                    typ = raw[0]
                    id_ = struct.unpack(">I", raw[1:5])[0]
                    payload = raw[5:]
                    if typ == 0:  # open：连本机 cn_proxy
                        reader, writer = await asyncio.open_connection(CN_HOST, CN_PORT)
                        writer.write(payload)
                        await writer.drain()
                        conns[id_] = writer
                        asyncio.create_task(_reader_to_ws(ws, id_, reader))
                    elif typ == 1:  # data：写本机 cn_proxy
                        w = conns.get(id_)
                        if w:
                            w.write(payload)
                            await w.drain()
                    elif typ == 2:  # close
                        w = conns.pop(id_, None)
                        if w:
                            try:
                                w.close()
                            except Exception:
                                pass
        except Exception as e:  # noqa: BLE001
            log.warning("隧道断开: %s；5s 后重连", e)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(client_loop())
