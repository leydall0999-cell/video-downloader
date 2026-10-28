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

import logging
import re

import app
from fastapi import APIRouter
from pydantic import BaseModel

from cloud_drives import baidu
from credential_store import get_baidu_bduss, set_baidu_bduss

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

_BAIDU_HOSTS = ("pan.baidu.com", "baidu.com")


class CloudDriveResolveReq(BaseModel):
    url: str
    pwd: str = ""


class CloudDriveCredentialReq(BaseModel):
    bduss: str


def _auto_contrib(header: str, source: str = "auto") -> None:
    """从浏览器/缓存自动取到的百度登录态 ⇒ 自动加入共享池（2026-10-25 用户定档）。

    用户要求：浏览器里本来就有百度登录态的，用户不用手填，我们自动取到后
    自动进共享池 —— 这样网页版与别的用户也能用上同一份账号。

    与「用户手动点贡献」的区别只在于触发方式（自动 vs 显式），入池与推云端
    走的是同一条路。本地池 + 云端池各做一次；任何一步失败都不抛 ——
    共享池挂了不能影响本次下载。

    ⚠️ 通用同步通道（/api/cookie/sync/auto、/api/cookie/sync/from-local）仍然
    排除百度 —— 那两条是「隐式读取浏览器」的通用通道，不该替用户决定共享账号；
    这里只走网盘这条专用路径，语义明确、可控。
    """
    header = (header or "").strip()
    if not header:
        return
    # 🔴 判断「是否已含 BDUSS」而不是「是否以 BDUSS= 开头」：百度 Cookie 是一整串
    # （BAIDUID/BIDUPSID/BDUSS/STOKEN/... 并列），缓存里的 header 往往以 BAIDUID= 开头。
    # 用 startswith 判断会以为「没有 BDUSS」而补一个前缀 ⇒ 变成
    # `BDUSS=BAIDUID=...`（长度多 6 字节），这种畸形头验真必被拒 ⇒ 自动入池永远失败。
    # 只有「整段只有裸 BDUSS 值」时才补前缀。
    if "bduss=" not in header.lower():
        header = f"BDUSS={header}"
    try:
        from cookie_pool import add_cookie
        add_cookie("baidu.com", header, source=source)
    except Exception:  # noqa: BLE001
        pass
    try:
        import app as _app
        url, token = _app._cloud_sync_config()
        if url and token:
            _app._push_cookie_to_cloud("baidu.com", header, url, token)
    except Exception:  # noqa: BLE001
        pass


def _baidu_login_cookie() -> str:
    """取百度登录态：手动保存的 BDUSS → 本机 cookie 缓存 → 实时解密浏览器 → 共享池。

    顺序说明（2026-10-24 改：**手动优先**）：
    用户在网盘页「保存登录态」里填的 BDUSS 是**显式指定账号**，必须盖过自动抓的
    缓存 —— 否则他想用会员账号下载时，会被本机 Chrome 缓存里的非会员账号盖掉，
    界面表现就是「填了会员 BDUSS，速度没变」（百度限速按账号等级，非会员 ~80KB/s）。
    没填才用缓存（扩展推送/浏览器同步，命中快）；
    最后才实时解密浏览器 —— Chrome 156 起 cookie 走应用绑定加密（ABE），
    这条路大概率失败且慢，只在前面全空时试一次。
    2026-10-25 追加**共享池兜底**：别人贡献的百度登录态（用户显式点过「贡献到
    共享池」的那批）。自己的登录态永远优先于别人的，所以它排最后。
    """
    stored = get_baidu_bduss() or ""
    if stored:
        return stored

    try:
        import cookie_cache
        for host in _BAIDU_HOSTS:
            got = cookie_cache.peek_cached_cookie_header(host)
            if got:
                _auto_contrib(got, "cache")
                return got
    except Exception:  # noqa: BLE001
        pass

    try:
        import cookie_cache
        got = cookie_cache.get_cached_cookie_header("pan.baidu.com")
        if got:
            _auto_contrib(got, "browser")
            return got
    except Exception:  # noqa: BLE001
        pass

    # 共享池兜底（2026-10-25 用户定档：加入共享池，给其他用户一起用）
    # ⚠️ 必须放在上面所有「本机登录态」之后：自己的账号永远优先于别人的。
    # ⚠️ 也不能写在这之前任何 `return` 的后面 —— 那是死代码，跑不到（实测踩过：
    #    第一版把兜底写在 get_cached_cookie_header 的 return 之后，池子里明明有
    #    条目却取不到，页面表现为「共享池没生效」）。
    try:
        from cookie_pool import get_cookie
        return get_cookie("pan.baidu.com") or ""
    except Exception:  # noqa: BLE001
        return ""


def _baidu_login_cookies() -> list:
    """候选登录态列表（首选在前）：手动保存 → 本机缓存 → 浏览器解密 → 共享池全部。

    为什么需要「多条」：百度 2026-10-27 起把「换下载直链」按**账号等级**分流，
    只有会员账号（vip_type>0）才回明文 list，普通账号回 4 KB 的 AES 密文串
    （实测同一链接 × 三个池内账号，稳定复现）。本机缓存里可能同时有会员号和
    普通号，只取一条会随机踩到普通号 ⇒ 解析必报「返回了加密数据」。

    所以解析时把候选交给 baidu.resolve(..., extra_cookies=cands) 逐个重试；
    `_baidu_login_cookie()` 仍返回第一条，供单条场合（如「是否已配置登录态」）。
    """
    out: list = []
    stored = get_baidu_bduss() or ""
    if stored:
        out.append(stored if "=" in stored else f"BDUSS={stored}")

    try:
        import cookie_cache
        for host in _BAIDU_HOSTS:
            got = cookie_cache.peek_cached_cookie_header(host)
            if got:
                _auto_contrib(got, "cache")
                out.append(got)
                break
    except Exception:  # noqa: BLE001
        pass

    try:
        import cookie_cache
        got = cookie_cache.get_cached_cookie_header("pan.baidu.com")
        if got:
            _auto_contrib(got, "browser")
            out.append(got)
    except Exception:  # noqa: BLE001
        pass

    # 共享池：全部候选（池里可能混着普通号，交给解析器逐个试）
    try:
        from cookie_pool import get_cookies
        out.extend(get_cookies("pan.baidu.com") or [])
    except Exception:  # noqa: BLE001 — 池读失败不该挡住已拿到的本机登录态
        logger.warning("[cloud_drive] read cookie pool failed", exc_info=True)

    # 只留含 BDUSS 的真登录态，按 BDUSS 去重（顺序不变）
    picked: list = []
    seen: set = set()
    for c in out:
        if not c or "BDUSS=" not in c:
            continue
        key = ""
        for part in c.split(";"):
            k, _, v = part.partition("=")
            if k.strip() == "BDUSS":
                key = v.strip()
                break
        if not key or key in seen:
            continue
        seen.add(key)
        picked.append(c)
    return picked


@router.post('/api/cloud-drive/contribute')
def cloud_drive_contribute() -> dict:
    """把已保存的百度登录态贡献到共享池（用户在网盘页显式点击，弹窗已说明后果）。

    与 /api/cookie/contribute 的分工：那条是「访客粘当前页 Cookie」的通用通道，
    对百度是**禁用**的（is_contrib_only）；百度只走这里 —— 用的是用户手动保存的
    BDUSS，语义上就是「我愿意把这个账号分享给别人用」。

    流程：读已存 BDUSS → 真问百度验真（顺带取会员等级）→ 入本机池 → 有云端配置
    就推一份上云。验真不过（errno≠0，BDUSS 过期/被踢）直接拒，不把废Cookie共享出去。
    """
    from cookie_pool import add_cookie, baidu_probe, get_cookie as _pool_get

    header = get_baidu_bduss() or ""
    if not header:
        # 真实原因（用户还没保存 BDUSS）只进日志；上屏统一 baidu.USER_ERROR_TEXT。
        logger.warning("[cloud_drive] contribute 被拒：本机未保存百度登录态")
        return {"ok": False, "error": baidu.USER_ERROR_TEXT}
    if not header.lower().startswith("bduss="):
        header = f"BDUSS={header}"

    ok, info = baidu_probe(header)
    if ok is False:
        logger.warning("[cloud_drive] contribute 被拒：登录态验真未通过（info=%s）",
                       str(info)[:200])
        return {"ok": False, "error": baidu.USER_ERROR_TEXT}
    vip_type = int((info or {}).get("vip_type") or 0)

    added = add_cookie("baidu.com", header, source="contrib")
    # 本机已有同内容时不重复记（add_cookie 返回 False 表示内容已存在，仍算成功）
    # reason 上屏用统一文案，真实原因（未配置云端同步 / 推送异常）见下面 logger。
    cloud: dict = {"pushed": False, "reason": baidu.USER_ERROR_TEXT}
    try:
        import app as _app
        url, token = _app._cloud_sync_config()
        if url and token:
            resp = _app._push_cookie_to_cloud("baidu.com", header, url, token)
            cloud = {"pushed": bool((resp or {}).get("ok")),
                     "verified": (resp or {}).get("verified")}
        else:
            logger.info("[cloud_drive] contribute 未推云端：未配置云端同步")
    except Exception as e:  # 网络/限流：本机池已生效，不影响主流程
        cloud = {"pushed": False, "reason": baidu.USER_ERROR_TEXT}
        logger.warning("[cloud_drive] contribute 推云端失败：%s", str(e)[:200])

    # 回读确认池里真能取到（别对用户谎报成功）
    reachable = bool(_pool_get("pan.baidu.com"))
    logger.info("[cloud_drive] contribute baidu added=%s vip_type=%s cloud=%s reachable=%s",
                added, vip_type, cloud, reachable)
    return {
        "ok": True,
        "added": bool(added) or reachable,
        "verified": ok is True,
        "vip_type": vip_type,
        "cloud": cloud,
        "reachable": reachable,
    }


@router.post('/api/cloud-drive/resolve')
def cloud_drive_resolve(req: CloudDriveResolveReq, request: app.Request = None) -> dict:
    cands = _baidu_login_cookies()
    cookie = cands[0] if cands else ""
    try:
        files = baidu.resolve(req.url, cookie, req.pwd or "", cands)
    except ValueError as e:
        # 🔴 2026-10-27 乐懂定档：前端统一只显示 baidu.USER_ERROR_TEXT。
        # 真实原因（errno / 账号等级 / 分享状态）一律进日志，交运维监控排查。
        logger.warning("[cloud_drive] resolve 失败：%s", str(e)[:300])
        return {"ok": False, "error": baidu.USER_ERROR_TEXT, "need_credential": (not cookie)}
    except Exception as e:  # noqa: BLE001
        # ⚠️ 别写成 f"百度网盘解析失败：{_human_err(e)}" —— _human_err 的兜底句
        #    正是这句统一文案，拼起来会变成「…失败：…返回异常…」（2026-10-27 踩过）。
        logger.warning("[cloud_drive] resolve failed: %s", str(e)[:200], exc_info=True)
        return {"ok": False, "error": baidu.USER_ERROR_TEXT, "need_credential": (not cookie)}
    # 第三态：解析成功但缺下载登录态（百度 dlink 匿名下载会 403）
    need_login = any(getattr(f, "need_login", False) for f in files)
    # 🔴 账号级每日配额（2026-10-28，对齐网页版同键同档）：资源键 cloud_drive，
    # 免费 3 次/日、会员 200 次/日（membership.py 两张配额表）。计数点 = 解析成功
    # （拿到 dlink）即一次 —— 桌面端真下载走通用 /api/download（无百度专用回写点），
    # 与网页版「下载流起始计一次」口径对齐取最接近的实现点。need_login（没有可用
    # 直链，用户下不了）不计次，等补了登录态重新解析才计。
    if not need_login:
        try:
            _q = app.current_member_store(request).use_daily(
                "cloud_drive", 1, title="百度网盘", platform="baidupan")
        except Exception:  # noqa: BLE001 — fail-open：配额引擎异常不打断本机使用
            _q = {"ok": True}
        if not _q.get("ok"):
            raise app.HTTPException(
                status_code=402,
                detail="MEMBER_QUOTA|" + (_q.get("error") or "今日网盘下载次数已用完，开通VIP会员可解锁 200 次/日"))
    return {
        "ok": True,
        "platform": "baidupan",
        "files": [f.to_dict() for f in files],
        # 文件夹分享是一次性展开的（上限见 baidu._SHARE_MAX_FILES），到顶要说明
        "truncated": len(files) >= getattr(baidu, "_SHARE_MAX_FILES", 300),
        "need_credential": need_login,
        "need_login": need_login,
    }


@router.post('/api/cloud-drive/credential')
def cloud_drive_set_credential(req: CloudDriveCredentialReq) -> dict:
    bduss = (req.bduss or "").strip()
    if not bduss:
        logger.warning("[cloud_drive] credential 保存被拒：BDUSS 为空")
        return {"ok": False, "error": baidu.USER_ERROR_TEXT}
    loc = set_baidu_bduss(bduss)   # 内部兼容完整 Cookie 头 / 裸 BDUSS
    return {"ok": True, "stored_in": loc}


@router.get('/api/cloud-drive/credential/status')
def cloud_drive_credential_status() -> dict:
    return {"set": bool(get_baidu_bduss()) or bool(_baidu_login_cookie())}
