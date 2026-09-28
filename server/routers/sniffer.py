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
    """悬浮球 / 浏览器扩展提交下载项（仅本机/回环场景使用）。

    返回的 send_id 是**回执凭据**：本条只是入队（必然成功），真正的建任务发生在
    桌面端进程里。调用方（扩展）拿 send_id 轮询 /api/sniffer/result，才能知道
    桌面端是「已加入下载」还是失败（未登录 / 不支持 / 超配额），而不是一律显示成功。
    """
    if response is not None:
        _pna(response)
    try:
        item = cdp_sniffer.SNIFFER.add_manual(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "picked": True, "send_id": item["send_id"],
            "url": item["url"][:200]}


@router.get('/api/sniffer/result')
def sniffer_result(send_id: str = "", response: Response = None) -> dict:  # noqa: RUF013
    """扩展查询回执：pending / ok / error(带原因) / unknown。**绝不限流**（扩展 ~0.9s 轮询）。"""
    if response is not None:
        _pna(response)
    return cdp_sniffer.SNIFFER.send_result(send_id)


@router.post('/api/sniffer/send-result')
def sniffer_send_result(payload: dict = Body(...), response: Response = None) -> dict:  # noqa: RUF013
    """桌面端前端写回建任务结果（仅本机/回环场景使用）。"""
    if response is not None:
        _pna(response)
    ok = bool(payload.get("ok"))
    cdp_sniffer.SNIFFER.report_result(
        str(payload.get("send_id") or ""), ok, str(payload.get("message") or ""))
    return {"ok": True}


@router.get('/api/sniffer/picked')
def sniffer_picked(request: Request, response: Response = None) -> dict:  # noqa: RUF013
    # 借这条 3s 一次的既有轮询，把**桌面端自己**的登录态告知服务端：扩展没有桌面端
    # 会话令牌，只有这样才能在点下载之前提示「桌面端未登录」（见 mark_desktop_auth）。
    user_id = None
    try:
        from user_membership import get_current_user_id
        user_id = get_current_user_id(request)
        cdp_sniffer.SNIFFER.mark_desktop_auth(user_id)
    except Exception:  # noqa: BLE001 - 登录态信号是尽力而为，绝不影响出队
        pass
    if response is not None:
        _pna(response)
    # ⚠️ 防偷条目（2026-09-28）：只有带有效令牌的桌面端才允许出队。匿名轮询
    # （Chrome 旧页面仍加载着 desktop-app.js / 普通浏览器）一律返回空、**不得**
    # take_picked()，否则会抢走扩展发来的下载条目，导致「点下载桌面端没反应」。
    # 桌面端桌面端 request() 总会带 Bearer（来自 WKWebView localStorage），与 Chrome
    # 的独立存储天然区分，因此令牌即身份判据。
    if user_id:
        return {"items": cdp_sniffer.SNIFFER.take_picked()}
    return {"items": []}
