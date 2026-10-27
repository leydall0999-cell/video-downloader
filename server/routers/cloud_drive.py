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
import os
import re
import urllib.parse

from fastapi import APIRouter
from pydantic import BaseModel

import app
import baidu_cred_store
from cloud_drives import baidu

logger = logging.getLogger(__name__)

router = APIRouter()

# Python 内部错误的特征串 —— 这类文本绝不能原样甩给用户（2026-10-27 线上出现过
# 「百度网盘解析失败: 'str' object has no attribute 'get'」这种英文堆栈碎片）。
_PY_ERR_RE = re.compile(
    r"object has no attribute|not subscriptable|NoneType|positional argument|"
    r"unexpected keyword|Traceback|KeyError|IndexError|TypeError|AttributeError"
)


def _human_err(msg: object) -> str:
    """把后端异常洗成一句用户能看懂的中文。"""
    t = str(msg or "").strip()
    if not t:
        return "百度网盘解析失败，请稍后重试"
    if _PY_ERR_RE.search(t) or not re.search(r"[\u4e00-\u9fff]", t):
        return "百度网盘接口返回异常（可能触发了风控，或分享链接已失效），请稍后重试"
    return t[:120]

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


def _admin_login_cookie() -> str:
    """管理员提供的百度登录态（服务器侧统一一份，所有网页版用户共用）。

    2026-10-25 用户定档：「网盘 cookies 主要由管理员提供，用户自己获取的入口隐藏」。

    来源优先级：
      ① 环境变量 `VDL_BAIDU_BDUSS` —— 管理员/运维直接在服务器上配这一份；
      ② 云端 cookie 池里 baidu.com 的条目 —— 桌面端用户浏览器里登录过百度网盘时
         会自动取到并贡献进来（用户无需手填），管理员手工放进去的也走这里。

    取到 ① 时顺手把它写回池里：这样「管理员那份」始终在共享池中，桌面端也能
    通过共享池拿到同一份账号（两端一份凭据，不用各填各的）。
    """
    env = (os.environ.get("VDL_BAIDU_BDUSS") or "").strip()
    if env:
        header = env if "=" in env else f"BDUSS={env}"
        try:
            from cookie_pool import add_cookie
            add_cookie("baidu.com", header, source="admin")
        except Exception:  # noqa: BLE001 — 入池失败不影响本次使用
            pass
        return header
    try:
        from cookie_pool import get_cookie
        return get_cookie("pan.baidu.com") or get_cookie("baidu.com") or ""
    except Exception:  # noqa: BLE001
        return ""


def _login_cookie(uid: str) -> str:
    """本次要用的百度登录态：管理员那份优先，其次用户自己存的（接口保留，界面已隐藏）。"""
    admin = _admin_login_cookie()
    if admin:
        return admin
    try:
        return baidu_cred_store.load_bduss(uid) or ""
    except Exception:  # noqa: BLE001
        return ""


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
    raw_input = (req.url or "").strip()
    if not raw_input:
        return {"ok": False, "error": "请填写百度网盘分享链接"}
    # 百度网盘「复制链接」给的是整段文案（标题 + 「链接:」 + 「提取码:」），
    # 用户整段粘贴是最自然的操作 ⇒ 先提取真正的 URL 与提取码，不再要求手动摘。
    url, text_pwd = baidu.extract_share_url(raw_input)
    if not url:
        return {"ok": False, "error": "链接格式不对，应是 https://pan.baidu.com/..."}

    cookie = _login_cookie(uid)
    try:
        files = baidu.resolve(url, cookie, (req.pwd or "").strip() or text_pwd)
    except ValueError as e:
        return {"ok": False, "error": str(e), "need_credential": (not cookie)}
    except Exception as e:  # noqa: BLE001
        # exc_info：只打 str(e) 时看不出来是哪个环节炸的（2026-10-27 排查吃过亏）
        logger.warning("[cloud_drive] resolve failed: %s", str(e)[:200], exc_info=True)
        return {"ok": False, "error": f"百度网盘解析失败：{_human_err(e)}", "need_credential": (not cookie)}

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

    cookie = _login_cookie(uid)
    if not cookie:
        raise app.HTTPException(status_code=403, detail="暂无可用的百度登录态，请联系管理员提供")

    name = (request.query_params.get("name") or "").strip() or "baidu-file"

    # —— 每日配额门禁（资源键 cloud_drive，独立计次，不污染普通下载配额）——
    # 免费 3 次/日、会员 200 次/日（见 membership.py）。预检超限抛 402 引导开通会员；
    # 放行即计数（同步流式中转没有「任务完成」回写点，请求即代表一次使用）。
    _cd_store = app.current_member_store(request)
    _cd_q = _cd_store.quota_state("cloud_drive")
    if not (_cd_q.get("unlimited") or _cd_q.get("unknown") or _cd_q.get("allowed", True)):
        if _cd_q.get("tier") == "free":
            _cd_detail = (f"今日免费网盘下载次数已用尽（{_cd_q['limit']}/日），"
                          f"开通VIP会员可解锁 {_cd_q.get('member_limit', 0)} 次/日")
        else:
            _cd_detail = f"今日网盘下载配额已用尽（{_cd_q['limit']}/日）"
        raise app.HTTPException(status_code=402, detail=_cd_detail)
    try:
        _cd_store.use_daily("cloud_drive", 1, title=name, platform="百度网盘")
    except Exception:  # noqa: BLE001 — 计数失败 fail-open，绝不阻断下载
        pass

    # 复用网页版既有的媒体中继 /api/media/proxy（它已具备 SSRF 护栏、Range 透传、
    # Content-Length 回传、防盗链 Referer、浏览器 UA）。这里只额外做两件事：
    #   ① force_range：百度直链裸 GET 一律 403，必须带 Range；
    #   ② cookie：传服务器存的百度登录态（浏览器拿不到，只能由服务器带）。
    # dlink 不出现在 URL 里（令牌 → dlink 在服务端解开），百度 cookie 也不出服务器。
    from routers.core import media_proxy
    return media_proxy(u=dlink, cookie=cookie, dl=name, request=request,
                       force_range="bytes=0-")
