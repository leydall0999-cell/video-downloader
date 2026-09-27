"""CDP 浏览器嗅探路由（2026-09-27，对标 DataTool 悬浮球）。

- connect/disconnect/status/items：工坊 UI 的嗅探面板用
- send：页面悬浮球 outbox 通道失败时的兜底回传入口（正常路径走 CDP
  outbox 轮询，不经过页面 fetch —— https 页面往回环发 fetch 受
  Private Network Access 约束，响应须带 Allow-Private-Network 头）
- picked：前端轮询取走悬浮球里用户点了「下载」的项目
CORS 预检由 app.py 的全局 CORSMiddleware（默认 "*"）处理，这里不重复。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request, Response

import cdp_sniffer  # noqa: E402 - 与 app.py 同级（server 目录在 sys.path），路由包内不能用相对导入

router = APIRouter()

_PNA_HEADERS = {
    "Access-Control-Allow-Private-Network": "true",
    "Access-Control-Allow-Origin": "*",
}


def _pna(resp: Response) -> Response:
    resp.headers.update(_PNA_HEADERS)
    return resp


@router.post('/api/sniffer/connect')
def sniffer_connect(payload: dict = Body(default={})) -> dict:
    port = int(payload.get("port") or 9222)
    if not (1024 <= port <= 65535):
        raise HTTPException(status_code=400, detail="端口范围 1024-65535")
    launch = bool(payload.get("launch"))
    try:
        return cdp_sniffer.SNIFFER.start(port=port, launch=launch)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post('/api/sniffer/disconnect')
def sniffer_disconnect() -> dict:
    return cdp_sniffer.SNIFFER.stop()


@router.get('/api/sniffer/status')
def sniffer_status() -> dict:
    return cdp_sniffer.SNIFFER.status()


@router.get('/api/sniffer/items')
def sniffer_items(limit: int = 100) -> dict:
    return {"items": cdp_sniffer.SNIFFER.items(limit=max(1, min(limit, 200)))}


@router.post('/api/sniffer/send')
def sniffer_send(payload: dict = Body(...), response: Response = None) -> dict:  # noqa: RUF013
    """悬浮球兜底回传（仅本机/回环场景使用）。"""
    if response is not None:
        _pna(response)
    try:
        item = cdp_sniffer.SNIFFER.add_manual(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "picked": True, "url": item["url"][:200]}


@router.get('/api/sniffer/picked')
def sniffer_picked(response: Response = None) -> dict:  # noqa: RUF013
    if response is not None:
        _pna(response)
    return {"items": cdp_sniffer.SNIFFER.take_picked()}
