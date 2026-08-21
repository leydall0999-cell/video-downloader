"""反向 WebSocket 隧道（Railway 端）。

背景
----
video-downloader 部署在海外（Railway），国内站（B站/抖音/腾讯…）会被地理围栏 403/412。
国内 ECS 上跑 ``cn_proxy`` 正向代理回源，但「海外 Railway 直连 ECS 公网」受跨境入站
限速/丢包影响，yt-dlp 多轮请求下频繁 504（实测：沙箱经同一代理秒回 412，Railway 却
21s 超时——链路而非代理本身的问题）。

本模块让 ECS **主动出站**连 Railway（跨境出站稳定），通过 WebSocket 把本机 ``cn_proxy``
反向暴露给 Railway 本机。Railway 上的 yt-dlp 只需连本机 ``127.0.0.1:18889``（本地隧道
代理），流量经 WebSocket 透明转发到 ECS 的 ``cn_proxy``，再回源国内站。
彻底绕开「海外 → 国内入站」瓶颈。

协议（WebSocket binary 帧）：``[1B type][4B id BE][payload]``
  type 0 = open   payload = yt-dlp 发给代理的第一笔字节（CONNECT / GET 请求头）
  type 1 = data   payload = 透明转发字节（双向）
  type 2 = close  该 id 隧道关闭

本地代理（给 yt-dlp）与 ECS client 之间不解析 HTTP，整条链路按 id 做 raw byte 透传。
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import asyncio
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("cn_tunnel")

router = APIRouter()

# ---- 全局状态（单 ECS client 足够） ----
_TUNNEL: "WebSocket | None" = None
_TUNNEL_READY = asyncio.Event()
_SEND_LOCK = asyncio.Lock()
_PENDING: dict[int, "asyncio.Queue[tuple[str, bytes]]"] = {}
_ID_LOCK = asyncio.Lock()
_NEXT_ID = 1

# 隧道鉴权 token：Railway 与 ECS client 必须一致。可通过环境变量覆盖。
# 注意：隧道端点只把流量转发给本机 cn_proxy，而 cn_proxy 自身还有 Basic 鉴权，
# 所以即使 token 泄露，对方仍需 cn_proxy 密码才能实际出网，敞口有限。
_TUNNEL_TOKEN = (
    os.environ.get("VDL_TUNNEL_TOKEN", "").strip()
    or "vdl-rv-tunnel-7f3a9c2e-4b1d-8c66-2e5f9a0b3c7d"
)
_PROXY_HOST = os.environ.get("VDL_TUNNEL_PROXY_HOST", "127.0.0.1")
_PROXY_PORT = int(os.environ.get("VDL_TUNNEL_PROXY_PORT", "18889") or "18889")


def _frame(typ: int, id_: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BI", typ, id_) + payload


async def _send(ws: "WebSocket", typ: int, id_: int, payload: bytes = b"") -> None:
    async with _SEND_LOCK:
        await ws.send_bytes(_frame(typ, id_, payload))


@router.websocket("/ws/cn-tunnel")
async def cn_tunnel_ws(ws: WebSocket) -> None:
    """ECS client 主动连入的 WebSocket 端点（出站，稳定）。"""
    global _TUNNEL
    auth = ws.headers.get("Authorization", "")
    header_token = auth.removeprefix("Bearer ").strip() if auth else ""
    token = ws.query_params.get("token", "") or header_token
    if _TUNNEL_TOKEN and token != _TUNNEL_TOKEN:
        await ws.close(code=1008, reason="unauthorized")
        return
    await ws.accept()
    _TUNNEL = ws
    _TUNNEL_READY.set()
    logger.info("[cn_tunnel] 反向隧道已建立（ECS client 已连接）")
    try:
        while True:
            # receive 加超时：client 进程崩溃/网络半开（TCP 半开时 receive 永不返回，
            # 旧版在这里卡死导致 _TUNNEL 残留死连接，18889 代理用死连接发 open 帧
            # 立即失败 → yt-dlp 报 Remote end closed）。超时强制走 finally 清理。
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=90)
            except asyncio.TimeoutError:
                logger.warning("[cn_tunnel] WS receive 90s 无消息，视为死连接主动断开")
                break
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if not data or len(data) < 5:
                continue
            typ = data[0]
            id_ = struct.unpack(">I", data[1:5])[0]
            payload = data[5:]
            if typ == 1:  # data：ECS 回的字节，转交给对应本地代理连接
                q = _PENDING.get(id_)
                if q:
                    await q.put(("data", payload))
            elif typ == 2:  # close：该 id 隧道关闭
                q = _PENDING.pop(id_, None)
                if q:
                    await q.put(("close", b""))
    except WebSocketDisconnect:
        logger.warning("[cn_tunnel] ECS client 断开")
    except Exception:
        logger.exception("[cn_tunnel] 隧道接收循环异常")
    finally:
        _TUNNEL = None
        _TUNNEL_READY.clear()
        # 断开时把残留的 pending 代理连接全部关闭，避免它们卡到客户端超时
        for _pid in list(_PENDING.keys()):
            _q = _PENDING.pop(_pid, None)
            if _q:
                try:
                    _q.put_nowait(("close", b""))
                except Exception:
                    pass
        logger.warning("[cn_tunnel] 反向隧道断开（ECS client 失联）")


async def _proxy_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """本地 HTTP 代理连接（yt-dlp 连入）：把流量经隧道发给 ECS。"""
    global _NEXT_ID
    async with _ID_LOCK:
        id_ = _NEXT_ID
        _NEXT_ID += 1
    try:
        # 等隧道就绪；若现有 _TUNNEL 是死连接（WS 断开但 receive 未察觉的残留），
        # 立即清掉并等新的 WS 连接，避免把 open 帧发到死连接上（yt-dlp 报 Remote end closed）。
        if _TUNNEL is None or _TUNNEL.client_state.value != 1:  # 1 = OPEN
            _TUNNEL_READY.clear()
            await asyncio.wait_for(_TUNNEL_READY.wait(), timeout=10)
        ws = _TUNNEL
        if ws is None or ws.client_state.value != 1:
            logger.warning("[cn_tunnel] 代理 id=%s 无可用隧道，关闭", id_)
            return
        # 读第一笔（CONNECT / GET 请求头），作为 open 帧 payload
        first = await reader.read(65536)
        if not first:
            return
        q: "asyncio.Queue[tuple[str, bytes]]" = asyncio.Queue()
        _PENDING[id_] = q
        try:
            await _send(ws, 0, id_, first)
        except Exception as e:
            # open 帧发送失败：隧道刚断。清掉 pending，等重连后由客户端重试
            logger.warning("[cn_tunnel] open 帧发送失败 id=%s: %s", id_, str(e)[:120])
            _PENDING.pop(id_, None)
            _TUNNEL_READY.clear()
            return
        # 把 yt-dlp 后续字节发给 ECS
        writer_task = asyncio.create_task(_tunnel_writer(ws, id_, reader))
        try:
            while True:
                kind, payload = await q.get()
                if kind == "close":
                    break
                writer.write(payload)
                await writer.drain()
        finally:
            writer_task.cancel()
    except asyncio.TimeoutError:
        logger.warning("[cn_tunnel] 隧道未就绪，代理 id=%s 超时关闭", id_)
    except Exception:
        logger.exception("[cn_tunnel] 本地代理连接异常 id=%s", id_)
    finally:
        _PENDING.pop(id_, None)
        try:
            writer.close()
        except Exception:
            pass


async def _tunnel_writer(ws: "WebSocket", id_: int, reader: asyncio.StreamReader) -> None:
    """从 yt-dlp 读后续字节，经隧道发给 ECS。"""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            await _send(ws, 1, id_, data)
    except Exception:
        pass
    finally:
        try:
            await _send(_TUNNEL, 2, id_)
        except Exception:
            pass


@router.get("/api/tunnel-test")
async def tunnel_test(mode: str = "http") -> dict:
    """调试用：经本地隧道代理访问 bilibili，验证反向隧道是否通。

    mode=http：发 GET http://www.bilibili.com/（走 cn_proxy 的 do_GET）。
    mode=connect：发 CONNECT www.bilibili.com:443 后 TLS ClientHello，验证
    yt-dlp 常用的 HTTPS CONNECT 隧道路径（走 cn_proxy 的 do_CONNECT）。
    """
    target = "www.bilibili.com"
    start = time.time()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(_PROXY_HOST, _PROXY_PORT), timeout=10
        )
    except Exception as e:
        return {"ok": False, "stage": "connect_local_proxy", "error": str(e), "elapsed": round(time.time() - start, 2)}

    if mode == "connect":
        request = (
            f"CONNECT {target}:443 HTTP/1.1\r\n"
            f"Host: {target}:443\r\n"
            f"User-Agent: Mozilla/5.0\r\n\r\n"
        ).encode()
    else:
        request = (
            f"GET http://{target}/ HTTP/1.1\r\n"
            f"Host: {target}\r\n"
            f"User-Agent: Mozilla/5.0\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
    writer.write(request)
    await writer.drain()

    try:
        response = await asyncio.wait_for(reader.read(4096), timeout=30)
    except Exception as e:
        writer.close()
        return {"ok": False, "stage": "read_response", "error": str(e), "elapsed": round(time.time() - start, 2)}
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    head = response.split(b"\r\n", 1)[0].decode("latin-1", errors="ignore")
    # ---- 运行时诊断：国内站回源代理的真实生效值（排查「改了没生效」）----
    diag = {}
    try:
        import downloader as _dl
        diag["cn_proxy_url"] = _dl._cn_proxy_url()
        diag["vdl_proxy_cn"] = os.environ.get("VDL_PROXY_CN", "<unset>")
        diag["railway_envs"] = {
            k: bool(os.environ.get(k))
            for k in ("RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_ID", "RAILWAY_PROJECT_ID",
                      "RAILWAY_BRANCH", "RAILWAY_DEPLOYMENT_ID", "RAILWAY_REPLICA_ID")
        }
        diag["is_china_bilibili"] = _dl.is_china_host("bilibili.com")
        # 副本标识：连续请求间变化 => 多副本负载均衡（WS 与 HTTP 落到不同实例）
        diag["replica_id"] = os.environ.get("RAILWAY_REPLICA_ID", "?")
        diag["hostname"] = os.environ.get("RAILWAY_POD_NAME", os.environ.get("HOSTNAME", "?"))
        diag["pid"] = os.getpid()
        # Railway 提供的公网/私有域名（client 直连源站绕 Cloudflare 用）
        diag["railway_public_domain"] = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        diag["railway_private_domain"] = os.environ.get("RAILWAY_PRIVATE_DOMAIN", "")
        diag["railway_app_name"] = os.environ.get("RAILWAY_APP_NAME", "")
        diag["railway_service_name"] = os.environ.get("RAILWAY_SERVICE_NAME", "")
        # yt-dlp 实际 handler 配置：模拟 probe 构建 _YoutubeDL，看 proxy 是否真的进了 handler
        if mode == "handlers":
            try:
                import logging as _logging
                _logging.disable(_logging.INFO)
                opts = _dl._base_options(3, "bilibili.com")
                opts["format"] = None
                ydl = _dl._YoutubeDL(opts)
                rd = ydl._request_director
                diag["ytdlp_proxy_param"] = ydl.params.get("proxy")
                diag["ytdlp_handlers"] = list(rd.handlers.keys())
                diag["ytdlp_handler_proxies"] = {
                    k: str(getattr(h, "proxies", "N/A")) for k, h in rd.handlers.items()
                }
            except Exception as e:
                diag["ytdlp_diag_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        # 运行代码版本指纹：确认容器里跑的是否为最新 downloader.py（防 Docker COPY 缓存/分支错位）
        _dl_path = os.path.abspath(_dl.__file__)
        with open(_dl_path, "rb") as _f:
            _dl_bytes = _f.read()
        diag["dl_path"] = _dl_path
        diag["dl_sha256"] = __import__("hashlib").sha256(_dl_bytes).hexdigest()[:16]
        diag["dl_has_urllib_fix"] = b"forcing urllib handler only" in _dl_bytes
        diag["dl_has_probe_diag"] = b"[diag] host=" in _dl_bytes
        diag["dl_len"] = len(_dl_bytes)
        # probe 诊断日志（effective_proxy 真实生效值 + extract_info 结果）
        _plog = os.path.join(os.environ.get("TMPDIR", "/tmp"), "vdl_probe_debug.log")
        if os.path.exists(_plog):
            _lines = open(_plog, encoding="utf-8", errors="ignore").read().splitlines()
            diag["probe_log"] = _lines[-40:]
        else:
            diag["probe_log"] = ["<no probe log yet>"]
    except Exception as e:
        diag["diag_error"] = str(e)
    extra = {}
    if mode == "connect" and head.startswith("HTTP/1.1 200"):
        # CONNECT 已 200：再发一个 TLS ClientHello 试探，确认双向能透传 TLS 字节
        try:
            writer2, reader2 = await asyncio.wait_for(
                asyncio.open_connection(_PROXY_HOST, _PROXY_PORT), timeout=10
            )
            writer2.write(
                f"CONNECT {target}:443 HTTP/1.1\r\nHost: {target}:443\r\n\r\n".encode()
            )
            await writer2.drain()
            ok = await asyncio.wait_for(reader2.read(64), timeout=15)
            hello = bytes.fromhex(
                "16030100c0010000bc0303" + "00" * 160
            )[:32]  # 最小 TLS ClientHello 前缀（够触发握手响应）
            writer2.write(hello)
            await writer2.drain()
            try:
                tls_resp = await asyncio.wait_for(reader2.read(64), timeout=15)
                extra["tls_bytes"] = len(tls_resp)
                extra["tls_head"] = tls_resp[:8].hex()
            except Exception as e:
                extra["tls_bytes"] = 0
                extra["tls_error"] = str(e)
            writer2.close()
        except Exception as e:
            extra["connect2_error"] = str(e)
    return {
        "ok": True,
        "mode": mode,
        "stage": "success",
        "head": head,
        "body_preview": response[:200].decode("latin-1", errors="ignore"),
        "elapsed": round(time.time() - start, 2),
        "tunnel_ready": _TUNNEL_READY.is_set(),
        **diag,
        **extra,
    }


async def start_cn_tunnel_proxy() -> None:
    """在 Railway 本机启动本地隧道代理（127.0.0.1:18889）。"""
    try:
        server = await asyncio.start_server(_proxy_conn, _PROXY_HOST, _PROXY_PORT)
        logger.info("[cn_tunnel] 本地隧道代理监听 %s:%s", _PROXY_HOST, _PROXY_PORT)
        async with server:
            await server.serve_forever()
    except Exception:
        logger.exception("[cn_tunnel] 本地隧道代理启动失败")
