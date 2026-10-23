"""server/routers/cloud_drive.py — 网盘文件源路由（阶段 0：百度网盘）。

提供：
- POST /api/cloud-drive/resolve    解析网盘链接 → 返回可下载文件列表（直链 + 所需 cookie）
- POST /api/cloud-drive/credential 保存百度网盘登录态（完整 Cookie 头 或 裸 BDUSS）
- GET  /api/cloud-drive/credential/status  查询是否已保存

设计：本路由只做「解析 + 凭据管理」，真正的下载仍走现有 POST /api/download
（把返回的 dlink 作为 url、cookie 作为 cookie 字段即可，下载器会带登录态且不加 Referer）。
不侵入视频解析主链路（core.py），风险面最小。

登录态契约（百度特有）：解析四步匿名可过，但 **dlink 下载必须带登录态**。
所以存在第三态：解析成功但没登录态 ⇒ ok=True + need_credential=True，
前端要提示「已读到文件，但下载需要百度登录态」。
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from cloud_drives import baidu
from credential_store import get_baidu_bduss, set_baidu_bduss

router = APIRouter()

_BAIDU_HOSTS = ("pan.baidu.com", "baidu.com")


class CloudDriveResolveReq(BaseModel):
    url: str
    pwd: str = ""


class CloudDriveCredentialReq(BaseModel):
    bduss: str


def _baidu_login_cookie() -> str:
    """取百度登录态：本机 cookie 缓存 → 手动保存的 BDUSS → 实时解密浏览器。

    顺序说明：扩展推送/浏览器同步的缓存命中最快也最新，放第一；
    手动粘贴的 BDUSS 是没装扩展用户的兜底；
    最后才实时解密浏览器 —— Chrome 156 起 cookie 走应用绑定加密（ABE），
    这条路大概率失败且慢，只在前面全空时试一次。
    """
    try:
        import cookie_cache
        for host in _BAIDU_HOSTS:
            got = cookie_cache.peek_cached_cookie_header(host)
            if got:
                return got
    except Exception:  # noqa: BLE001
        pass

    stored = get_baidu_bduss() or ""
    if stored:
        return stored

    try:
        import cookie_cache
        return cookie_cache.get_cached_cookie_header("pan.baidu.com") or ""
    except Exception:  # noqa: BLE001
        return ""


@router.post('/api/cloud-drive/resolve')
def cloud_drive_resolve(req: CloudDriveResolveReq) -> dict:
    cookie = _baidu_login_cookie()
    try:
        files = baidu.resolve(req.url, cookie, req.pwd or "")
    except ValueError as e:
        # 链接无法识别 / 解析失败：返回语义清晰的错误，便于前端提示
        return {"ok": False, "error": str(e), "need_credential": (not cookie)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"百度网盘解析失败：{e}", "need_credential": (not cookie)}
    # 第三态：解析成功但缺下载登录态（百度 dlink 匿名下载会 403）
    need_login = any(getattr(f, "need_login", False) for f in files)
    return {
        "ok": True,
        "platform": "baidupan",
        "files": [f.to_dict() for f in files],
        "need_credential": need_login,
        "need_login": need_login,
    }


@router.post('/api/cloud-drive/credential')
def cloud_drive_set_credential(req: CloudDriveCredentialReq) -> dict:
    bduss = (req.bduss or "").strip()
    if not bduss:
        return {"ok": False, "error": "登录态不能为空"}
    loc = set_baidu_bduss(bduss)   # 内部兼容完整 Cookie 头 / 裸 BDUSS
    return {"ok": True, "stored_in": loc}


@router.get('/api/cloud-drive/credential/status')
def cloud_drive_credential_status() -> dict:
    return {"set": bool(get_baidu_bduss()) or bool(_baidu_login_cookie())}
