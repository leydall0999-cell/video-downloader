"""百度网盘扫码登录（对接 passport.baidu.com 二维码接口）。

流程：
  1. qr_gen()   调 getqrcode 生成二维码，返回 base64 PNG + sign
  2. qr_poll()  调 channel/unicast 轮询，status: waiting/scanned/confirmed/expired
  3. confirmed  时自动从会话 cookie 提取 BDUSS/STOKEN，调 baidu_pcs.login 完成登录

注意：本模块只在「用户本机」运行（app 的 FastAPI 后端），网络可达 passport.baidu.com。
"""
import base64
import json
import logging
import re
import threading
import time
import uuid

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger("baidu_qr")

# 由 server/app.py 注入，避免循环 import
PCS_LOGIN = None

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://pan.baidu.com/",
}
GET_QR = "https://passport.baidu.com/v2/api/getqrcode"
UNICAST = "https://passport.baidu.com/channel/unicast"
QR_STATUS = "https://passport.baidu.com/v2/api/qrcodestatus"

_lock = threading.Lock()
_STATE = {"sign": None, "session": None, "gid": None, "created": 0.0,
          "confirmed": False, "login_result": None}


def _new_session():
    s = requests.Session()
    s.headers.update(_HEADERS)
    s.mount("https://", HTTPAdapter(max_retries=2))
    return s


def qr_gen() -> dict:
    """生成二维码，返回 {ok, sign, img(base64 data url), expires_in}。"""
    try:
        s = _new_session()
        gid = str(uuid.uuid4())
        tt = str(int(time.time() * 1000))
        params = {"lp": "pc", "qrloginfrom": "pc", "gid": gid, "apiver": "v3", "tt": tt}
        r = s.get(GET_QR, params=params, timeout=20)
        data = r.json()
        if data.get("errno") != 0 or not data.get("sign"):
            return {"ok": False, "message": f"获取二维码失败（{data.get('errno')}）：{data.get('prompt', '')}".strip()}
        sign = data["sign"]
        img_url = data.get("imgurl")
        if img_url and img_url.startswith("passport"):
            img_url = "https://" + img_url
        ir = s.get(img_url, timeout=20)
        if ir.status_code != 200 or not ir.content:
            return {"ok": False, "message": "二维码图片下载失败"}
        img_b64 = base64.b64encode(ir.content).decode("ascii")
        with _lock:
            _STATE.update({"sign": sign, "session": s, "gid": gid,
                           "created": time.time(), "confirmed": False, "login_result": None})
        return {"ok": True, "sign": sign, "img": f"data:image/png;base64,{img_b64}", "expires_in": 120}
    except Exception as e:  # noqa: BLE001
        logger.exception("qr_gen 异常")
        return {"ok": False, "message": f"生成二维码出错：{e}"}


def _parse_unicast(body: str) -> dict:
    """解析 channel/unicast 的 JSONP 响应。"""
    body = (body or "").strip()
    m = re.search(r"\(\s*(\{.*\})\s*\)\s*;?\s*$", body, re.DOTALL)
    if m:
        body = m.group(1)
    try:
        return json.loads(body)
    except Exception:
        return {"errno": -1, "raw": body[:200]}


def qr_poll(sign: str) -> dict:
    """轮询扫码状态。confirmed 时自动完成登录。

    注意：channel/unicast 是百度服务端的**长轮询**接口——服务端会保持
    HTTP 连接打开，直到扫码状态变化（scanned/confirmed）或服务端自身超时。
    因此客户端必须给足 timeout，且把 ReadTimeout 当作「尚未扫码、继续等待」
    处理，而不是上报成错误。
    """
    with _lock:
        if _STATE.get("sign") != sign or _STATE.get("session") is None:
            return {"status": "expired", "message": "二维码已失效，请刷新"}
        s = _STATE["session"]
        gid = _STATE["gid"]
        if _STATE.get("confirmed"):
            return {"status": "confirmed", "login": _STATE.get("login_result")}
    try:
        cb = "bd__cbs__" + str(int(time.time() * 1000))[-8:]
        params = {"channel_id": sign, "tpl": "netdisk_web", "gid": gid,
                  "callback": cb, "tt": str(int(time.time() * 1000))}
        # 长轮询超时：百度服务端可能挂起连接 30s+ 直到状态变化。
        # 60s 覆盖常见服务端长轮询周期；触发 ReadTimeout 视为「尚未扫码」继续等待。
        try:
            r = s.get(UNICAST, params=params, timeout=60)
        except Exception as to:  # ReadTimeout / ConnectTimeout 等网络超时
            if "timeout" in str(type(to)).lower() or "timeout" in str(to).lower():
                return {"status": "waiting", "message": "等待扫码…"}
            raise
        j = _parse_unicast(r.text)
        errno = j.get("errno")
        if errno == 404:
            return {"status": "expired", "message": "二维码已过期，请刷新"}
        data = j.get("data") or {}
        status = str(data.get("status", "0"))
        if status == "0":
            return {"status": "waiting", "message": "等待扫码…"}
        if status == "1":
            return {"status": "scanned", "message": "已扫码，请在手机上确认"}
        if status == "2":
            return _finish_login(sign)
        return {"status": "unknown", "message": f"未知状态：{status}", "raw": j}
    except Exception as e:  # noqa: BLE001
        logger.exception("qr_poll 异常")
        return {"status": "error", "message": f"轮询出错：{e}"}


def _collect_cookies(session) -> dict:
    out = {}
    for c in session.cookies:
        if c.name in ("BDUSS", "STOKEN", "PTOKEN", "BAIDUID", "SAVEID"):
            out[c.name] = c.value
    return out


def _cookie_str(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def _finish_login(sign: str) -> dict:
    """扫码确认后提取 BDUSS/STOKEN 并登录 baiduPCS-Go。"""
    with _lock:
        if _STATE.get("confirmed"):
            return {"status": "confirmed", "login": _STATE.get("login_result")}
        s = _STATE.get("session")
        gid = _STATE.get("gid")
    # 1) 确认后百度通常会经 Set-Cookie 下发 BDUSS/STOKEN（可能跨域到 pan.baidu.com）
    cookies = _collect_cookies(s)
    # 2) 兜底：请求 qrcodestatus 换取凭证（允许重定向以收集跨域 cookie）
    if "BDUSS" not in cookies:
        try:
            tt = str(int(time.time() * 1000))
            params = {"code": sign, "tpl": "netdisk_web", "subpro": "netdisk_web",
                      "apiver": "v3", "gid": gid, "tt": tt, "callback": "bd__cbs__" + tt[-8:]}
            r2 = s.get(QR_STATUS, params=params, timeout=20, allow_redirects=True)
            cookies = _collect_cookies(s)
            # 2b) 再兜底：从响应体解析 bduss 字段（部分版本以 JSON/JSONP 返回）
            if "BDUSS" not in cookies:
                m = re.search(r'"bduss"\s*:\s*"([^"]+)"', r2.text, re.IGNORECASE)
                if m:
                    cookies["BDUSS"] = m.group(1)
        except Exception as e:  # noqa: BLE001
            logger.warning("qrcodestatus 失败: %s", e)
    if "BDUSS" not in cookies:
        res = {"ok": False, "message": "已扫码确认，但未能取到百度登录凭证（BDUSS）。\n请改用「账号密码」方式登录，或重新生成二维码再扫一次。"}
    else:
        cookie_str = _cookie_str(cookies)
        res = PCS_LOGIN.login(cookie_str) if PCS_LOGIN else {"ok": False, "message": "baidu_pcs 未加载"}
    with _lock:
        _STATE["confirmed"] = True
        _STATE["login_result"] = res
    return {"status": "confirmed", "login": res}
