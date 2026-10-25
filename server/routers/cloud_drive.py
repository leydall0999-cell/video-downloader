"""server/routers/cloud_drive.py — 网页版百度网盘下载（服务器中转）。

用户 2026-10-25 拍板：网页版也做百度网盘，登录态存服务器（填一次以后不用再填）。

与桌面端（app-dev 那套）的三点差别 —— 这也是网页版必须另写的原因：
  1. 桌面端把 dlink 交给**本机**下载器去下；网页版的浏览器拿不到百度 cookie，
     所以由服务器带登录态去取文件，再**流式中转**给浏览器。
  2. 因此**绝不能**把 dlink / 百度 cookie 下发给浏览器：dlink 用服务端加密的
     不透明令牌承载（bduss_cred_store.seal_token），百度 cookie 一次都不出服务器。
  3. 登录态按用户存（bduss_cred_store），每人一份、互不可见。

下载的三个硬规矩（来自桌面端 2026-10-24 实测，别改）：
  · 直链域白名单：只认 *.baidupcs.com / *.pcs.baidu.com（否则等于让服务器
    变成任意 URL 的代理，SSRF）。
  · UA 用**浏览器 UA**，不能用 P2SP 客户端 UA —— P2SP UA + Range 会 403
    （错误码 31326「无权限」，极具误导性）。
  · **必须带 Range**（bytes=0-）：裸 GET 一律 403，错误码会伪装成 31326
    「无权限」或 31360「直链过期」。

限速说明：百度按**账号**总带宽限速，且多连接是负优化（实测 1 连最快），
所以这里固定单连接。实际速度 = min(百度给该账号的带宽, 服务器出口带宽)。
"""
from __future__ import annotations

import logging
import re
import urllib.parse

from fastapi import APIRouter
from pydantic import BaseModel

import app
import baidu_cred_store
from cloud_drives import baidu

logger = logging.getLogger(__name__)

router = APIRouter()

# 百度网盘直链域（与桌面端 downloader._NETDISK_HOST_RE 同一口径）
_NETDISK_HOST_RE = re.compile(r"(?:^|\.)(?:baidupcs\.com|pcs\.baidu\.com)$")


class CloudDriveResolveReq(BaseModel):
    url: str
    pwd: str = ""


class CloudDriveCredentialReq(BaseModel):
    bduss: str


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _is_netdisk_direct(url: str) -> bool:
    """是否百度网盘直链域（白名单，防 SSRF）。"""
    h = _host_of(url)
    return bool(h) and bool(_NETDISK_HOST_RE.search(h))


def _uid(request) -> str:
    """取当前登录用户（未登录一律 403，且文案含「登录」以触发前端登录框）。"""
    from user_membership import require_login_user
    return require_login_user(request)


# ——————————————————————————————————————————————————————————————
# 解析
# ——————————————————————————————————————————————————————————————

@router.post('/api/cloud-drive/resolve')
def cloud_drive_resolve(req: CloudDriveResolveReq, request: app.Request) -> dict:
    """解析百度网盘分享链接 → 可下载文件列表。

    返回里**没有** dlink、**没有**百度 cookie：只有服务端签的不透明令牌 id、
    文件名、大小。真正的下载走 /api/cloud-drive/download。
    """
    uid = _uid(request)
    url = (req.url or "").strip()
    if not url:
        return {"ok": False, "error": "请填写百度网盘分享链接"}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"ok": False, "error": "链接格式不对，应是 https://pan.baidu.com/... "}

    cookie = baidu_cred_store.load_bduss(uid)
    try:
        files = baidu.resolve(url, cookie, (req.pwd or "").strip())
    except ValueError as e:
        return {"ok": False, "error": str(e), "need_credential": (not cookie)}
    except Exception as e:  # noqa: BLE001
        logger.warning("[cloud_drive] resolve failed: %s", str(e)[:200])
        return {"ok": False, "error": f"百度网盘解析失败：{str(e)[:120]}", "need_credential": (not cookie)}

    out = []
    for f in files:
        out.append({
            "id": baidu_cred_store.seal_token(uid, f.dlink),
            "name": f.name,
            "size": int(f.size or 0),
            "need_login": bool(getattr(f, "need_login", False)),
        })
    need_login = any(x["need_login"] for x in out) or (not cookie)
    return {
        "ok": True,
        "platform": "baidupan",
        "files": out,
        "need_credential": need_login,
        "need_login": need_login,
        "has_credential": bool(cookie),
    }


# ——————————————————————————————————————————————————————————————
# 登录态（存服务器，按用户）
# ——————————————————————————————————————————————————————————————

@router.get('/api/cloud-drive/credential/status')
def cloud_drive_credential_status(request: app.Request) -> dict:
    uid = _uid(request)
    return {
        "set": baidu_cred_store.has_bduss(uid),
        "configured": baidu_cred_store.configured(),
    }


@router.post('/api/cloud-drive/credential')
def cloud_drive_set_credential(req: CloudDriveCredentialReq, request: app.Request) -> dict:
    """保存百度登录态到服务器（加密落盘，权限 600，每用户一份）。"""
    uid = _uid(request)
    try:
        stored = baidu_cred_store.save_bduss(uid, (req.bduss or "").strip())
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except baidu_cred_store.CredentialError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "stored_in": "server", "preview": stored[:24]}


@router.delete('/api/cloud-drive/credential')
def cloud_drive_clear_credential(request: app.Request) -> dict:
    uid = _uid(request)
    return {"ok": True, "cleared": baidu_cred_store.clear_bduss(uid)}


# ——————————————————————————————————————————————————————————————
# 下载（服务器中转）
# ——————————————————————————————————————————————————————————————

@router.get('/api/cloud-drive/download')
def cloud_drive_download(request: app.Request):
    """按令牌取文件：服务器带登录态去百度取，再流式转发给浏览器。

    浏览器这边只看到本服务的普通文件下载，碰不到百度 cookie 和 dlink。
    """
    uid = _uid(request)
    token = (request.query_params.get("id") or "").strip()
    if not token:
        raise app.HTTPException(status_code=400, detail="缺少下载令牌")
    dlink = baidu_cred_store.open_token(uid, token)
    if not dlink:
        raise app.HTTPException(status_code=400, detail="下载令牌无效或已过期，请重新解析")
    if not _is_netdisk_direct(dlink):
        raise app.HTTPException(status_code=400, detail="下载地址不是百度网盘直链，已拒绝")

    cookie = baidu_cred_store.load_bduss(uid)
    if not cookie:
        raise app.HTTPException(status_code=403, detail="还没有百度登录态，请先保存 BDUSS 再下载")

    name = (request.query_params.get("name") or "").strip() or "baidu-file"
    # 复用网页版既有的媒体中继 /api/media/proxy（它已具备 SSRF 护栏、Range 透传、
    # Content-Length 回传、防盗链 Referer、浏览器 UA）。这里只额外做两件事：
    #   ① force_range：百度直链裸 GET 一律 403，必须带 Range；
    #   ② cookie：传服务器存的百度登录态（浏览器拿不到，只能由服务器带）。
    # dlink 不出现在 URL 里（令牌 → dlink 在服务端解开），百度 cookie 也不出服务器。
    from routers.core import media_proxy
    return media_proxy(u=dlink, cookie=cookie, dl=name, request=request,
                       force_range="bytes=0-")
