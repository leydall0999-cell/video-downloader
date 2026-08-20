#!/usr/bin/env python3
"""反向 WebSocket 隧道 ECS 端 client。

Railway 端协议帧：[1B type][4B id BE][payload]
  type 0 = open   payload = yt-dlp 发给本地代理的第一笔数据
  type 1 = data   payload = 透明转发数据
  type 2 = close  该 id 隧道关闭

ECS client 采用与 Railway 端对称的架构：
- 单主循环从 WS 读消息
- 每个 id 对应一条到本机 cn_proxy 的 TCP 连接和一个上行队列
- 主循环按 id 把 type=1/type=2 消息分发给对应队列
- 每连接有两个 task： pump_down（proxy -> WS）、pump_up（queue -> proxy）
"""
import asyncio
import os
import struct
from websockets.asyncio.client import connect

RAILWAY_WS = os.environ.get("VDL_TUNNEL_URL", "wss://hanyuxz.top/ws/cn-tunnel")
TOKEN = os.environ.get("VDL_TUNNEL_TOKEN", "")
LOCAL_PROXY = os.environ.get("VDL_LOCAL_PROXY", "http://127.0.0.1:18888")
RECONNECT = 5

_WS_URL = RAILWAY_WS + (f"?token={TOKEN}" if TOKEN else "")


def _frame(typ: int, id_: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BI", typ, id_) + payload


async def tunnel_client():
    send_lock = asyncio.Lock()
    # id -> (reader, writer, up_queue, up_task)
    sessions: dict[int, tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.Queue, asyncio.Task]] = {}

    def cleanup(id_: int):
        sess = sessions.pop(id_, None)
        if not sess:
            return
        _reader, writer, _q, up_task = sess
        if up_task and not up_task.done():
            up_task.cancel()
        try:
            writer.close()
        except Exception:
            pass

    async def pump_down(id_: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """从本机 proxy 读 -> WS type=1；连接关闭时发 type=2。"""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                async with send_lock:
                    await ws.send(_frame(1, id_, data))
        except Exception:
            pass
        finally:
            try:
                async with send_lock:
                    await ws.send(_frame(2, id_))
            except Exception:
                pass
            cleanup(id_)

    async def pump_up(id_: int, writer: asyncio.StreamWriter, q: asyncio.Queue):
        """从队列读 -> 本机 proxy。"""
        try:
            while True:
                kind, payload = await q.get()
                if kind == "close":
                    break
                writer.write(payload)
                await writer.drain()
        except Exception:
            pass
        finally:
            cleanup(id_)

    async def handle_open(id_: int, initial: bytes):
        """收到 open 帧：新建到本机 cn_proxy 的 TCP 连接。"""
        parsed = LOCAL_PROXY.replace("http://", "").replace("https://", "")
        host, port_s = parsed.rsplit(":", 1)
        port = int(port_s)
        ssl = LOCAL_PROXY.startswith("https://")
        try:
            reader, writer = await asyncio.open_connection(host, port, ssl=ssl)
            writer.write(initial)
            await writer.drain()
        except Exception as e:
            print(f"[cn_tunnel_client] id={id_} connect local proxy failed: {e}", flush=True)
            try:
                async with send_lock:
                    await ws.send(_frame(2, id_))
            except Exception:
                pass
            return
        q: asyncio.Queue = asyncio.Queue()
        up_task = asyncio.create_task(pump_up(id_, writer, q))
        sessions[id_] = (reader, writer, q, up_task)
        asyncio.create_task(pump_down(id_, reader, writer))

    while True:
        try:
            async with connect(_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                print(f"[cn_tunnel_client] connected {_WS_URL}", flush=True)
                async for msg in ws:
                    if isinstance(msg, str):
                        continue
                    if len(msg) < 5:
                        continue
                    typ = msg[0]
                    id_ = struct.unpack(">I", msg[1:5])[0]
                    payload = msg[5:]
                    if typ == 0:  # open
                        if id_ in sessions:
                            cleanup(id_)
                        asyncio.create_task(handle_open(id_, payload))
                    elif typ == 1:  # data
                        sess = sessions.get(id_)
                        if sess:
                            try:
                                sess[2].put_nowait(("data", payload))
                            except Exception:
                                pass
                    elif typ == 2:  # close
                        sess = sessions.pop(id_, None)
                        if sess:
                            try:
                                sess[2].put_nowait(("close", b""))
                            except Exception:
                                pass
                            try:
                                sess[1].close()
                            except Exception:
                                pass
        except Exception as e:
            print(f"[cn_tunnel_client] error: {e}, reconnect in {RECONNECT}s", flush=True)
            # 清理所有会话
            for id_ in list(sessions.keys()):
                cleanup(id_)
            await asyncio.sleep(RECONNECT)


if __name__ == "__main__":
    asyncio.run(tunnel_client())
