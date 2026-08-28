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
# WS 心跳超时改为可配置：VPS→Railway 跨境链路偶发 10~22s 尖刺，
# 硬编码 ping_timeout=10 会把任何尖刺判死→1011 被踢→重连风暴。
# 放宽到 45s（仍能在真死连接上靠 pong 缺失及时检测），由 VPS systemd 注入。
PING_INTERVAL = int(os.environ.get("VDL_WS_PING_INTERVAL", "30"))
PING_TIMEOUT = int(os.environ.get("VDL_WS_PING_TIMEOUT", "45"))

_WS_URL = RAILWAY_WS + (f"?token={TOKEN}" if TOKEN else "")
# 日志脱敏：token 属真实凭据，绝不打进 journal（防凭据泄露）。连接仍用 _WS_URL。
_WS_URL_LOG = RAILWAY_WS + ("?token=***" if TOKEN else "")


def _frame(typ: int, id_: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BI", typ, id_) + payload


async def heartbeat(ws):
    """周期心跳数据帧：Cloudflare 等反代对 WS 的空闲超时只认【数据帧】，
    ping/pong 控制帧不计入活动，纯 ping 会被反代在 ~100s 后断开 TCP
    （表现为 client 侧 'no close frame received or sent' 反复重连）。
    每 15s 发一帧 type=3 空数据（服务端忽略），把连接保持为"活跃"，
    并压低运营商 NAT/反代空闲超时窗口（深圳移动等 NAT 对空闲连接约 2-5 分钟）。"""
    try:
        while True:
            await asyncio.sleep(15)
            try:
                async with send_lock:
                    await ws.send(_frame(3, 0, b""))
            except Exception:
                return  # 连接已断，主循环会处理重连
    except asyncio.CancelledError:
        pass


def _probe_once(url: str, timeout: int) -> tuple[bool, str]:
    """单次同步探针（在 to_thread 里跑，绝不阻塞 asyncio 事件循环）。

    背景（2026-08-28 根治断连风暴）：旧实现直接在事件循环里同步调用
    urllib.request.urlopen —— 隧道慢窗口时单次探测耗时可达 20~30s，
    期间 client 无法回复服务端 uvicorn 的 keepalive ping（默认 20s 超时），
    被服务端 1011 踢掉 → 每 ~3 分钟断连循环。此函数必须在线程中执行。
    """
    import urllib.request
    import urllib.error
    import ssl
    import json as _json

    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cn_tunnel_client/watchdog"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            data = _json.loads(resp.read().decode("utf-8", errors="ignore"))
            return bool(data.get("tunnel_ready")), ""
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"


async def watchdog(ws):
    """应用层探针：周期性直连 Railway /api/cookie/pull-diag 检查 tunnel_ready。

    背景：cn_tunnel_client 的 WS 连接可能进入"半死"状态——VPS 侧 TCP 层心跳
    send 成功（运营商/proxy 保留 TCP），但 Railway 容器侧 _TUNNEL 已空，帧未
    到达。这种场景主循环的 async for msg in ws 永远不会触发异常，也就无法
    自动重连——必须依赖外部 restart。

    本探针用 pull-diag 作为诊断金标准：tunnel 半死时 Railway /api/cookie/pull-diag
    会报 tunnel_ready=false。连续 N 次失败后主动 ws.close(code=4000) 让主循环
    except 触发重连。探针走 VPS→Railway 公开 HTTPS，**不依赖 tunnel**，因此
    当 tunnel 半死时本探针必然观察到异常。

    环境变量可调：
      VDL_WATCHDOG_URL          探针 URL（默认 Railway pull-diag）
      VDL_WATCHDOG_INTERVAL     探针间隔秒（默认 60）
      VDL_WATCHDOG_TIMEOUT      单次探针超时秒（默认 15）
      VDL_WATCHDOG_MAX_FAIL     连续失败多少次后强制重连（默认 3）
    """
    PROBE_URL = os.environ.get(
        "VDL_WATCHDOG_URL",
        "https://web-production-b9993.up.railway.app/api/cookie/pull-diag",
    )
    INTERVAL = int(os.environ.get("VDL_WATCHDOG_INTERVAL", "60"))
    TIMEOUT = int(os.environ.get("VDL_WATCHDOG_TIMEOUT", "15"))
    MAX_FAIL = int(os.environ.get("VDL_WATCHDOG_MAX_FAIL", "3"))

    fail_count = 0
    while True:
        await asyncio.sleep(INTERVAL)
        ok, err = await asyncio.to_thread(_probe_once, PROBE_URL, TIMEOUT)
        if ok:
            if fail_count:
                print(f"[cn_tunnel_client] watchdog: 隧道恢复（fail_count 清零）", flush=True)
            fail_count = 0
            continue
        fail_count += 1
        print(
            f"[cn_tunnel_client] watchdog: tunnel_ready=false fail_count={fail_count}/{MAX_FAIL}"
            + (f" err={err}" if err else ""),
            flush=True,
        )
        if fail_count >= MAX_FAIL:
            print(
                f"[cn_tunnel_client] watchdog: 连续 {MAX_FAIL} 次失败，主动 close ws 触发主循环重连",
                flush=True,
            )
            try:
                await ws.close(code=4000, reason="watchdog: tunnel half-dead, force reconnect")
            except Exception:
                pass
            return  # 主循环会在 ws.close 后走 except 触发 RECONNECT 重连


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
            # open_timeout=30：Cloudflare→Railway WS 链路偶发握手慢，默认 10s 容易误判
            # 超时触发 websockets 库的 InvalidStateError 竞态崩溃（response.set_exception）。
            async with connect(
                _WS_URL,
                ping_interval=PING_INTERVAL,
                ping_timeout=PING_TIMEOUT,
                open_timeout=30,
                close_timeout=5,
            ) as ws:
                print(f"[cn_tunnel_client] connected {_WS_URL_LOG}", flush=True)
                hb = asyncio.create_task(heartbeat(ws))
                wd = asyncio.create_task(watchdog(ws))
                try:
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
                finally:
                    hb.cancel()
                    wd.cancel()
        except Exception as e:
            print(f"[cn_tunnel_client] error: {e}, reconnect in {RECONNECT}s", flush=True)
            # 清理所有会话
            for id_ in list(sessions.keys()):
                cleanup(id_)
            await asyncio.sleep(RECONNECT)


if __name__ == "__main__":
    asyncio.run(tunnel_client())
