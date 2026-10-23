"""百度网盘解析器（cloud_drives/baidu.py）。

职责：输入一个百度网盘分享链接（+ 提取码）+ 用户百度登录态 Cookie，
产出可交给 VDL 下载器的直链。不负责实际下载 —— 下载由 server/downloader.py
走现有 per-task cookie 透传完成（百度 dlink 的 CDN host 不在 CHINA_DOMAINS，
下载器不会强加 Referer，而登录态 cookie 会注入）。

合规定位：只下载「用户自己的网盘文件 / 别人分享给用户」的文件，不批量抓取、
不破解限速。登录态只落本机（Keychain 或 VDL_DATA_DIR 隔离目录），
不进云端公共 cookie 池。

⚠️ 本模块基于 2026-10-23 对 Chrome 156 时代百度新接口的实测
   （详见 linkswift_research/BAIDU_API_156_FINDINGS.md），四步为：
     ① POST /share/verify        提取码 → 种 BDCLND cookie（即 sekey）
     ② GET  /share/tplconfig     取 sign / timestamp
     ③ GET  /share/list          列文件（顺带拿到 uk / share_id）
     ④ POST /api/sharedownload   换 dlink（**必须 form 编码**，JSON 会 errno=2）
   关键分工：①②③④ 全链路**匿名可过**，但 dlink 的 GET 下载**必须带登录态**
   （匿名 403 / error_code 31326 "user is not authorized"）。
   ⇒ 解析用的 Session 刻意**不带**登录 cookie（实测带全套 cookie 时
     tplconfig 会返回 HTML 而非 JSON）；登录态只塞进 CloudFile.cookie 给下载器。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import requests

_PAN = "pan.baidu.com"
_BASE = f"https://{_PAN}"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
_TIMEOUT = 20

# 百度 errno → 人话（实测汇总，详见 FINDINGS §3）
_ERRNO_MSG = {
    0: "成功",
    2: "参数错误",
    -6: "接口已下线",
    -12: "提取码校验失败",
    116: "分享不存在",
    118: "没有下载权限",
    113: "页面已过期",
    121: "文件数量过多",
    -1: "分享违规",
    31326: "下载需要百度登录态",
    31023: "不支持的预览格式",
}


def _errno_msg(errno, default: str = "百度接口返回错误") -> str:
    try:
        errno = int(errno)
    except (TypeError, ValueError):
        return f"{default}（errno={errno}）"
    msg = _ERRNO_MSG.get(errno)
    return f"{msg}（errno={errno}）" if msg else f"{default}（errno={errno}）"


@dataclass
class CloudFile:
    """一个可下载文件：直链 + 下载它所需的 cookie / 额外头。"""
    name: str
    size: int
    dlink: str
    cookie: str = ""
    headers: dict = field(default_factory=dict)
    need_login: bool = False   # True = 直链已拿到但没有百度登录态，下载会 403

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "size": self.size,
            "dlink": self.dlink,
            "cookie": self.cookie,
            "headers": self.headers,
            "need_login": self.need_login,
        }


# ——————————————————————————————————————————————————————————————
# 底层 HTTP（独立成函数，便于单测 mock）
# ——————————————————————————————————————————————————————————————

def _logid() -> str:
    """百度要求的 logid：base64(32位大写hex + ':FG=1')。"""
    h = binascii.hexlify(os.urandom(16)).decode().upper()
    return urllib.parse.quote(base64.b64encode((h + ":FG=1").encode()).decode())


def common_query() -> str:
    """四个接口共用的查询串（整条链路共用一个 logid，与浏览器行为一致）。"""
    return ("channel=chunlei&web=1&app_id=250528&bdstoken=&"
            f"logid={_logid()}&clienttype=0")


def new_session(referer: str) -> requests.Session:
    """匿名 Session（不注入登录态，见模块文档）。"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        # 必须：部分环境下 gzip 响应解压有坑（实测踩过），强制不压缩
        "Accept-Encoding": "identity",
    })
    return s


def _json(resp) -> dict:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


# ——————————————————————————————————————————————————————————————
# URL 分类
# ——————————————————————————————————————————————————————————————

def parse_baidu_url(url: str) -> dict:
    """把用户输入归类。

    返回 {"kind": "personal"|"share"|"unknown", ...}。
    - share:    {"kind":"share", "surl":"<去前导1>", "raw":"1<surl>", "pwd":...}
                （verify / list 用 surl；tplconfig 用带前导 1 的 raw）
    - personal: {"kind":"personal", "path": "/..."}
    """
    url = (url or "").strip()
    if "pan.baidu.com" not in url and "yun.baidu.com" not in url:
        return {"kind": "unknown"}
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)

    if "/share/" in parsed.path or parsed.path.startswith("/s/") or "surl" in query:
        surl = (query.get("surl") or [""])[0]
        if not surl and parsed.path.startswith("/s/"):
            raw = parsed.path[len("/s/"):].strip("/")
            # /s/1xxxx 中前导 1 是固定前缀，真正的 surl 是去掉它的部分
            surl = raw[1:] if raw.startswith("1") else raw
        if not surl:
            return {"kind": "unknown"}
        return {
            "kind": "share",
            "surl": surl,
            "raw": "1" + surl,
            "pwd": (query.get("pwd") or [""])[0],
        }

    frag = parsed.fragment or ""
    path = ""
    m = re.search(r"path=([^&]+)", parsed.query + "&" + frag)
    if m:
        path = urllib.parse.unquote(m.group(1))
    return {"kind": "personal", "path": path or "/"}


# ——————————————————————————————————————————————————————————————
# 分享页 yunData（兜底用：list 响应没给 uk / share_id 时才抓页面）
# ——————————————————————————————————————————————————————————————

def _extract_yun_data(html: str) -> dict:
    """从分享页 HTML 提取 window.yunData 里的分享元数据。

    兼容百度两种页面形态：
    1) 对象字面量：window.yunData = { ... share_uk:"..." ... }  （当前真实页面）
    2) 点赋值：window.yunData.shareid = '...'; window.yunData.uk = '0'; （老页面）
    真正的分享者 uk 在 share_uk（点赋值形态下页面另有个占位的 uk:'0'），统一映射到 uk。
    """
    data: dict = {}
    for key in ("shareid", "share_uk", "uk", "sign", "timestamp", "bdstoken"):
        val = None
        kpat = r"\b" + key if key == "uk" else key   # uk 需词边界，避免误匹配 share_uk
        # 🔴 对象字面量里 key 是带引号的（"share_uk":"1100294005954"），
        #    冒号前必须允许可选的收尾引号 —— 少了 [\"']? 就永远匹配不到（2026-10-23 实测踩坑）
        m = re.search(kpat + r"""\s*["']?\s*:\s*["']?([^"'\s,}]+)""", html)
        if m:
            val = m.group(1)
        if val is None:
            m = re.search(r"yunData\." + key + r"\s*=\s*['\"]?([^'\"\s;}]+)", html)
            if m:
                val = m.group(1)
        if val is None:
            continue
        if key == "uk" and val in (0, "0", ""):
            continue    # 占位假值 uk:'0'
        if key in ("timestamp", "shareid", "share_uk"):
            try:
                val = int(val)
            except ValueError:
                pass
        data[key] = val
    if "share_uk" in data:
        data["uk"] = data["share_uk"]
    return data


def _yun_data_from_page(s: requests.Session, share_url: str) -> dict:
    try:
        resp = s.get(share_url, timeout=_TIMEOUT)
        return _extract_yun_data(resp.text or "")
    except Exception:  # noqa: BLE001
        return {}


# ——————————————————————————————————————————————————————————————
# 新接口四步
# ——————————————————————————————————————————————————————————————

def share_verify(s: requests.Session, surl: str, pwd: str, q: str) -> str:
    """① 校验提取码。成功返回 sekey（URL-decode 后的 randsk）；无提取码返回 ""。

    副作用：百度会往 Session 里种 BDCLND cookie（= sekey），后续接口靠它认会话。
    """
    if not pwd:
        return ""
    url = f"{_BASE}/share/verify?t={int(time.time() * 1000)}&bioc=1&surl={urllib.parse.quote(surl)}&{q}"
    resp = s.post(url, data={"pwd": pwd, "vcode": "", "vcode_str": ""},
                  timeout=_TIMEOUT)
    j = _json(resp)
    if j.get("errno") != 0:
        raise ValueError(_errno_msg(j.get("errno"), "提取码校验失败"))
    randsk = j.get("randsk") or ""
    return urllib.parse.unquote(randsk)


def share_tplconfig(s: requests.Session, raw: str, q: str) -> tuple[str, str]:
    """② 取 sign / timestamp。"""
    url = (f"{_BASE}/share/tplconfig?surl={urllib.parse.quote(raw)}"
           f"&fields=sign,timestamp&view_mode=1&{q}")
    j = _json(s.get(url, timeout=_TIMEOUT))
    if j.get("errno") != 0:
        raise ValueError(_errno_msg(j.get("errno"), "取分享签名失败"))
    data = j.get("data") or {}
    return data.get("sign") or "", str(data.get("timestamp") or "")


def share_list(s: requests.Session, surl: str, q: str) -> tuple[list, str, str]:
    """③ 列文件。返回 (records, uk, share_id)。"""
    url = (f"{_BASE}/share/list?web=5&app_id=250528&desc=1&showempty=0"
           f"&page=1&num=100&order=time&shorturl={urllib.parse.quote(surl)}"
           f"&root=1&view_mode=1&{q}")
    j = _json(s.get(url, timeout=_TIMEOUT))
    if j.get("errno") != 0:
        raise ValueError(_errno_msg(j.get("errno"), "读取分享内容失败"))
    return (j.get("list") or [],
            str(j.get("uk") or ""),
            str(j.get("share_id") or ""))


def share_dlinks(s: requests.Session, sign: str, ts: str, uk: str, share_id: str,
                 fs_ids: list, sekey: str, q: str) -> dict:
    """④ 换直链。返回 {fs_id: dlink}。

    ⚠️ 必须 form 编码（data=），用 json= 百度会返回 errno=2。改回 JSON 是回归事故。
    """
    if not fs_ids:
        return {}
    url = (f"{_BASE}/api/sharedownload?sign={urllib.parse.quote(sign)}"
           f"&timestamp={ts}&{q}")
    body = {
        "product": "share",
        "encrypt": "0",
        "timestamp": "",
        "sign": "",
        "uk": uk,
        "primaryid": share_id,
        "fid_list": json.dumps([int(f) for f in fs_ids if str(f).isdigit()]),
        "path_list": "[]",
        "isPublic": "false",
        "extra": json.dumps({"sekey": sekey}),
    }
    j = _json(s.post(url, data=body, timeout=_TIMEOUT))
    if j.get("errno") != 0:
        raise ValueError(_errno_msg(j.get("errno"), "获取下载直链失败"))
    out: dict = {}
    for item in j.get("list") or []:
        fid = str(item.get("fs_id") or "")
        if fid and item.get("dlink"):
            out[fid] = item["dlink"]
    return out


# ——————————————————————————————————————————————————————————————
# 对外入口
# ——————————————————————————————————————————————————————————————

def resolve_personal(path: str, cookie: str = "") -> list[CloudFile]:
    """个人网盘路径 —— 阶段 2 再做（新接口 /api/ostp/file/* 已定位未细测）。"""
    raise ValueError("暂不支持个人网盘路径（阶段 2 支持），请先使用分享链接")


def resolve_share(url: str, login_cookie: str = "", pwd: str = "") -> list[CloudFile]:
    """分享链接 → 直链列表。

    login_cookie：用户百度登录态（完整 Cookie 头）。解析过程不需要它，
    但没有它拿到的 dlink 下载会 403，所以会标记 CloudFile.need_login=True。
    """
    p = parse_baidu_url(url)
    if p.get("kind") != "share" or not p.get("surl"):
        raise ValueError("不是有效的百度网盘分享链接")
    surl = p["surl"]
    raw = p.get("raw") or ("1" + surl)
    pwd = pwd or p.get("pwd") or ""
    share_url = f"{_BASE}/s/{raw}"

    q = common_query()
    s = new_session(share_url)

    sekey = share_verify(s, surl, pwd, q)
    if not sekey:
        # 有些分享百度把 sekey 只种在 cookie 里
        sekey = urllib.parse.unquote(s.cookies.get("BDCLND", "") or "")

    sign, ts = share_tplconfig(s, raw, q)
    records, uk, share_id = share_list(s, surl, q)
    if not records:
        raise ValueError("分享内容为空，或链接已失效 / 需要提取码")
    if not (uk and share_id):
        yun = _yun_data_from_page(s, share_url)
        uk = uk or str(yun.get("uk") or "")
        share_id = share_id or str(yun.get("shareid") or "")

    files = [r for r in records if int(r.get("isdir") or 0) == 0]
    if not files:
        raise ValueError("分享里只有文件夹，暂不支持（阶段 2 再上）")

    fs_ids = [r.get("fs_id") for r in files if r.get("fs_id")]
    dlinks = share_dlinks(s, sign, ts, uk, share_id, fs_ids, sekey, q)
    # 批量漏掉的逐个补一次（百度偶发只回一部分）
    missing = [f for f in fs_ids if str(f) not in dlinks]
    for fid in missing:
        try:
            dlinks.update(share_dlinks(s, sign, ts, uk, share_id, [fid], sekey, q))
        except Exception:  # noqa: BLE001
            continue

    results: list[CloudFile] = []
    for r in files:
        fid = str(r.get("fs_id") or "")
        dlink = dlinks.get(fid) or ""
        if not dlink:
            continue
        try:
            size = int(r.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        results.append(CloudFile(
            name=r.get("server_filename") or r.get("filename") or "file",
            size=size,
            dlink=dlink,
            cookie=login_cookie or "",
            headers={"User-Agent": _UA, "Referer": share_url},
            need_login=not bool(login_cookie),
        ))
    if not results:
        raise ValueError("未能从分享链接取到下载直链")
    return results


def resolve(url: str, login_cookie: str = "", pwd: str = "") -> list[CloudFile]:
    """根据链接类型分发。"""
    p = parse_baidu_url(url)
    kind = p.get("kind")
    if kind == "share":
        return resolve_share(url, login_cookie, pwd)
    if kind == "personal":
        return resolve_personal(p.get("path", "/"), login_cookie)
    raise ValueError("无法识别的百度网盘链接")
