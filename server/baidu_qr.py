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
# 轮询计数器：用于在 unicast 长轮询无响应时穿插 qrcodestatus 兜底检测
_poll_count = 0


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


def _safe_preview(body: str, limit: int = 200) -> str:
    """日志安全预览：遇到二进制/不可打印内容（如被重定向到的 PNG 图片）降级为摘要，
    避免把图片字节当文本打印造成乱码污染日志。"""
    if not body:
        return ""
    # 图片魔数快速识别（PNG / JPEG / GIF）
    head = body[:8].encode("utf-8", "replace") if isinstance(body, str) else body[:8]
    if head[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"GIF8"):
        return f"[binary image len={len(body)}]"
    sample = body[:limit]
    if any((ord(c) < 32 or ord(c) > 126) and c not in "\r\n\t" for c in sample):
        return f"[binary len={len(body)}]"
    return sample


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

    混合策略（解决 unicast 长轮询在部分网络环境下不推送状态的问题）：
      - 奇数次：unicast 长轮询（25s 超时），等待百度服务端主动推送
      - 偶数次：qrcodestatus 短查询（10s 超时），主动拉取当前状态
    两种方式任一检测到状态变更即返回。
    """
    global _poll_count
    with _lock:
        if _STATE.get("sign") != sign or _STATE.get("session") is None:
            return {"status": "expired", "message": "二维码已失效，请刷新"}
        s = _STATE["session"]
        gid = _STATE["gid"]
        if _STATE.get("confirmed"):
            return {"status": "confirmed", "login": _STATE.get("login_result")}

    _poll_count += 1
    use_unicast = (_poll_count % 2 == 1)  # 奇数用长轮询，偶数用短查询

    try:
        if use_unicast:
            result = _poll_unicast(s, sign, gid)
        else:
            result = _poll_qrcodestatus(s, sign, gid)

        # 无论哪种方式，status=2 都走统一登录流程
        if result.get("status") == "2":
            return _finish_login(sign)
        return result
    except Exception as e:  # noqa: BLE001
        err_type = type(e).__name__
        if any(k in err_type.lower() for k in ("timeout", "connection", "socket")):
            logger.debug("[qr_poll] %s 超时（视为等待扫码）: %s",
                         "unicast" if use_unicast else "qrcodestatus", e)
            return {"status": "waiting", "message": "等待扫码…"}
        logger.exception("[qr_poll] 异常")
        return {"status": "error", "message": f"轮询出错：{e}"}


def _poll_unicast(session, sign: str, gid: str) -> dict:
    """unicast 长轮询：等待百度服务端推送状态变更。"""
    cb = "bd__cbs__" + str(int(time.time() * 1000))[-8:]
    params = {"channel_id": sign, "tpl": "netdisk_web", "gid": gid,
              "callback": cb, "tt": str(int(time.time() * 1000))}
    r = session.get(UNICAST, params=params, timeout=(8, 25))
    # 防御：若响应为图片（异常重定向）则降级为等待，不解析
    ct = r.headers.get("Content-Type", "")
    if ct.startswith("image/") or r.content[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0"):
        return {"status": "waiting", "message": "等待扫码…"}
    body_text = r.text
    j = _parse_unicast(body_text)
    logger.info("[qr_poll] unicast响应: errno=%s data.status=%s raw=%s",
                j.get("errno"), (j.get("data") or {}).get("status"), _safe_preview(body_text))
    errno_val = j.get("errno")
    if errno_val == 404:
        return {"status": "expired", "message": "二维码已过期，请刷新"}
    data = j.get("data") or {}
    status = str(data.get("status", "0"))
    if status == "0":
        return {"status": "waiting", "message": "等待扫码…"}
    if status == "1":
        return {"status": "scanned", "message": "已扫码，请在手机上确认"}
    if status == "2":
        return {"status": "2"}  # 由调用方处理登录
    return {"status": "unknown", "message": f"未知状态：{status}", "raw": j}


def _poll_qrcodestatus(session, sign: str, gid: str) -> dict:
    """qrcodestatus 短查询：主动拉取当前扫码状态（unicast 兜底）。"""
    tt = str(int(time.time() * 1000))
    cb = "bd__cbs__" + tt[-8:]
    params = {"code": sign, "tpl": "netdisk_web", "subpro": "netdisk_web",
              "apiver": "v3", "gid": gid, "tt": tt, "callback": cb}
    # 不自动跟随重定向：百度在二维码未确认时常 302 到 PNG 图片，跟随后
    # 响应体为图片二进制，r.text 强解 UTF-8 会乱码、JSONP 解析失败 errno=-1。
    r = session.get(QR_STATUS, params=params, timeout=(5, 10), allow_redirects=False)
    # 3xx 重定向到二维码图片（未登录态常见）→ 视为等待，不解析
    if 300 <= r.status_code < 400:
        return {"status": "waiting", "message": "等待扫码…"}
    # 响应体为图片（被重定向到 PNG/JPEG）时也降级为等待
    ct = r.headers.get("Content-Type", "")
    if ct.startswith("image/") or r.content[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0"):
        return {"status": "waiting", "message": "等待扫码…"}
    body_text = r.text
    j = _parse_unicast(body_text)
    logger.info("[qr_poll] qrcodestatus响应: errno=%s status=%s raw=%s",
                j.get("errno"), j.get("status"), _safe_preview(body_text))
    errno_val = j.get("errno")
    if errno_val == 404 or errno_val == 400400:
        return {"status": "expired", "message": "二维码已过期，请刷新"}
    # qrcodestatus 返回格式可能不同，尝试多种字段名
    status = str(j.get("status") or j.get("errno") or "0")
    if status in ("0", "0'"):
        return {"status": "waiting", "message": "等待扫码…"}
    if status in ("1", "1'"):
        return {"status": "scanned", "message": "已扫码，请在手机上确认"}
    if status in ("2", "2'"):
        return {"status": "2"}  # 由调用方处理登录
    # 如果 qrcodestatus 返回了 BDUSS cookie（确认后直接下发的场景）
    bduss_match = re.search(r'"bduss"\s*:\s*"([^"]+)"', body_text, re.IGNORECASE)
    if bduss_match and bduss_match.group(1):
        return {"status": "2"}  # 有 BDUSS 说明已确认
    return {"status": "waiting", "message": "等待扫码…"}


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
        # ★ 2026-08-27 续26：把 BDUSS 同步写到 ~/.vdl/baidu_bduss.txt，
        #   下次 baidu_login / get_baidu_dlink 可直接读此文件复用，
        #   避免每次都要扫码 / 弹登录窗。
        try:
            import os
            _bduss_dir = os.path.expanduser("~/.vdl")
            os.makedirs(_bduss_dir, exist_ok=True)
            _bduss_path = os.path.join(_bduss_dir, "baidu_bduss.txt")
            # 权限 0o600 防读取（与其他 token 文件一致）
            tmp_path = _bduss_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as _f:
                _f.write(cookies["BDUSS"].strip())
            try:
                os.chmod(tmp_path, 0o600)
            except Exception:
                pass
            os.replace(tmp_path, _bduss_path)
            logger.info("BDUSS 已持久化到 %s，长度 %d", _bduss_path, len(cookies["BDUSS"]))
        except Exception as _e_bduss:
            logger.warning("BDUSS 持久化失败（不影响下载）: %s", _e_bduss)
        res = PCS_LOGIN.login(cookie_str) if PCS_LOGIN else {"ok": False, "message": "baidu_pcs 未加载"}
    with _lock:
        _STATE["confirmed"] = True
        _STATE["login_result"] = res
    return {"status": "confirmed", "login": res}
