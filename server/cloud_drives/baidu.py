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
     ④ GET  /api/filemetas?dlink=1  换 dlink（**明文字段 info[].dlink**）
   关键分工：①②③④ 全链路**匿名可过**，但 dlink 的 GET 下载**必须带登录态**
   （匿名 403 / error_code 31326 "user is not authorized"）。
   ⇒ 解析用的 Session 刻意**不带**登录 cookie（实测带全套 cookie 时
     tplconfig 会返回 HTML 而非 JSON）；登录态只塞进 CloudFile.cookie 给下载器。

🔄 2026-10-24 换直链接口变更（重要，别改回去）：
   原 ④ `/api/sharedownload` 已把 `list` 从数组换成 **AES 密文的 base64 字符串**
   （单文件 4184 字符 / 3136 字节，16 字节对齐；带登录态后变 4192 字符但仍是密文）。
   密钥不在客户端可获得范围内（sekey / sign / md5 等候选都试过解不开），
   旧代码 `for item in list` 会直接 AttributeError。
   现有两条实测可用的替代路径：
     · `GET /api/filemetas?dlink=1&fsids=[..]&uk=&shareid=&type=dlink` → 明文 dlink，主路径
     · dlink 下载时 UA 必须**非浏览器**（浏览器 UA 一律 403 31326），见 DLINK_UA
   另注：同一 IP 短时间高频请求会触发 `hitcode:104` 频控（403），下载器需退避重试。
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

_SHARE_URL_RE = re.compile(
    r"https?://(?:pan|yun)\.baidu\.com/[^\s\u4e00-\u9fff\"'<>，。；、）)】]+", re.I)
_SHARE_PWD_RE = re.compile(
    r"(?:提取码|密码|pwd)\s*[:：=]?\s*([0-9A-Za-z]{4})(?![0-9A-Za-z])", re.I)
_URL_TAIL_JUNK = ".,;:!?)）】]」》\"'"


def extract_share_url(text: str) -> tuple[str, str]:
    """从「整段分享文案」里提取 (分享链接, 提取码)。

    用户在百度网盘 App / 网页点「复制链接」，拿到的不是裸 URL，而是多行文案::

        通过百度网盘分享的文件：狂野俄罗斯西伯利亚.mp4
        链接:https://pan.baidu.com/s/1hgu12HSQdxe47CaGb-jx7Q
        提取码:fhh1
        复制这段内容打开「百度网盘APP 即可获取」

    整段粘进输入框是最自然的操作 ⇒ 这里统一做一次规范化，
    前端（web/app.js 的 panExtractShare）保持同样的正则口径。
    """
    raw = (text or "").strip()
    if not raw:
        return "", ""
    m = _SHARE_URL_RE.search(raw)
    if not m:
        return "", ""
    url = m.group(0).rstrip(_URL_TAIL_JUNK)
    pwd = ""
    pm = _SHARE_PWD_RE.search(raw)
    if pm:
        pwd = pm.group(1)
    if not pwd:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            pwd = (q.get("pwd") or [""])[0]
        except Exception:  # noqa: BLE001
            pwd = ""
    return url, pwd


def parse_baidu_url(url: str) -> dict:
    """把用户输入归类。

    返回 {"kind": "personal"|"share"|"unknown", ...}。
    - share:    {"kind":"share", "surl":"<去前导1>", "raw":"1<surl>", "pwd":...}
                （verify / list 用 surl；tplconfig 用带前导 1 的 raw）
    - personal: {"kind":"personal", "path": "/..."}
    """
    url = (url or "").strip()
    # 整段分享文案（标题 + 「链接:」 + 「提取码:」）先规范化，只留真正的 URL；
    # 提取码同时带出来，供后面的 share 分支兜底使用。
    url, _text_pwd = extract_share_url(url)
    if not url:
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
            "pwd": ((query.get("pwd") or [""])[0] or _text_pwd),
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


# 百度 dlink（d.pcs.baidu.com）的 UA 白名单 —— 2026-10-24 实测：
#   带**浏览器 UA** 一律 403 `{"error_code":31326,"error_msg":"user is not authorized"}`；
#   官方客户端 UA / 空 UA / 非浏览器 UA（aria2、requests 默认）都能正常 206。
# 这是百度的防盗链设计（浏览器 UA 不允许直取 PCS 直链），所以下载时必须
# 显式指定客户端 UA，否则用户看到的是「未授权」而不是文件。
DLINK_UA = "netdisk;P2SP;3.0.20.138"

# filemetas 单次 fsids 上限（官方限制 100，超出返回 errno=2）
_FILEMETAS_BATCH = 100


def share_dlinks(s: requests.Session, sign: str, ts: str, uk: str, share_id: str,
                 fs_ids: list, sekey: str, q: str) -> dict:
    """④ 换直链。返回 {fs_id: dlink}。

    2026-10-24 改为双路径，顺序不能反：

    ① **/api/filemetas?dlink=1 —— 主路径（明文）**
       直接返回 `info[].dlink`，一次可批 100 个 fsid。
       发现经过：`/api/sharedownload` 现在对谁都返回密文（见下），
       而同一账号体系的 filemetas 仍给明文直链，且带登录态时更完整。

    ② **/api/sharedownload —— 回退（老路径）**
       实测它已把 `list` 从数组换成了 **AES 密文的 base64 字符串**
       （单文件分享 4184 字符 / 3136 字节，正好 16 字节对齐；
       带上登录态后变 4192 字符 —— 内容变了，但依然是密文）。
       密钥不在客户端可得范围内（sekey / sign / md5 各种候选都试过解不开），
       所以这条路现在基本不可用，只作兜底。
       ⚠️ 它必须 form 编码（data=），用 json= 百度会返回 errno=2。
    """
    ids = [int(f) for f in fs_ids if str(f).isdigit()]
    if not ids:
        return {}

    out: dict = {}
    for i in range(0, len(ids), _FILEMETAS_BATCH):
        batch = ids[i:i + _FILEMETAS_BATCH]
        url = (
            f"{_BASE}/api/filemetas?dlink=1"
            f"&fsids={urllib.parse.quote(json.dumps(batch))}"
            f"&uk={uk}&shareid={share_id}&type=dlink&{q}"
        )
        try:
            j = _json(s.get(url, timeout=_TIMEOUT))
        except Exception:  # noqa: BLE001  网络抖动 → 走回退/下一个批次
            continue
        if j.get("errno") != 0:
            continue
        for item in j.get("info") or []:
            if not isinstance(item, dict):
                continue
            fid = str(item.get("fs_id") or "")
            if fid and item.get("dlink"):
                out[fid] = item["dlink"]
    if out:
        return out

    # ② 回退老接口
    url = (f"{_BASE}/api/sharedownload?sign={urllib.parse.quote(sign)}"
           f"&timestamp={ts}&{q}")
    body = {
        "product": "share",
        "encrypt": "0",
        "timestamp": "",
        "sign": "",
        "uk": uk,
        "primaryid": share_id,
        "fid_list": json.dumps(ids),
        "path_list": "[]",
        "isPublic": "false",
        "extra": json.dumps({"sekey": sekey}),
    }
    j = _json(s.post(url, data=body, timeout=_TIMEOUT))
    if j.get("errno") != 0:
        raise ValueError(_errno_msg(j.get("errno"), "获取下载直链失败"))
    lst = j.get("list")
    if isinstance(lst, str):
        # 走到这里说明 filemetas 也没给直链，只剩密文响应
        raise ValueError(
            "百度这次没返回可用的下载直链（接口返回了加密数据）。"
            "请稍后重试；持续出现说明百度换了接口，需要更新版本"
        )
    if not isinstance(lst, list):
        return {}
    for item in lst:
        if not isinstance(item, dict):   # 防御：偶发夹带非对象条目
            continue
        fid = str(item.get("fs_id") or "")
        if fid and item.get("dlink"):
            out[fid] = item["dlink"]
    return out


# ——————————————————————————————————————————————————————————————
# 对外入口
# ——————————————————————————————————————————————————————————————

def _has_baidu_login(cookie: str) -> bool:
    """判断 Cookie 头里是否含**百度网盘**的有效登录凭证。

    只认 BDUSS。BAIDUID / BIDUPSID / PSTM / H_PS_PSSID 这些是百度搜索侧的
    匿名标识，任何访客都有，跟网盘登录无关。2026-10-24 实测：把一份
    「只有百度搜索匿名 Cookie」的头传给 sharedownload，百度照样返回加密 list
    （`list` 字段是 4 KB 的密文而不是数组），于是只能报「请填 BDUSS」。
    所以判定必须严格到 BDUSS —— 否则会自动读出来的匿名 Cookie 冒充登录态，
    把「缺登录」这条可读提示盖成一句莫名的解密失败。
    """
    return bool(cookie) and "BDUSS=" in cookie


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
    # 🔴🔴 换直链必须用**带登录态的会话**（2026-10-24 实测）：
    # 用匿名会话请求 /api/sharedownload，百度会返回「游客缓存」里的旧直链 ——
    # dstime 停在一小时前、expires=1h，一拿到就过期，下载必报
    # 31360 expire time out。换成带登录态的会话，同一个分享返回的是
    # dstime=now、expires=8h 的活链，下载 206 正常（SVIP 实测 3.6 MB/s）。
    # ⌁ 注意只有**这一步**要登录态：verify / tplconfig / list 仍必须匿名，
    #   带登录态请求 tplconfig 会返回 HTML（踩过）。
    s_dl = s
    login_ok = _has_baidu_login(login_cookie)
    if login_ok:
        s_dl = new_session(share_url)
        s_dl.headers["Cookie"] = login_cookie
    dlinks: dict = {}
    try:
        dlinks = share_dlinks(s_dl, sign, ts, uk, share_id, fs_ids, sekey, q)
    except ValueError:
        if login_ok:
            raise       # 有登录态仍失败 = 真错误（errno 语义 / 风控），照旧抛给上层
        # 匿名申请直链会拿到加密响应（见 share_dlinks 内注释，2026-10-24 实测）。
        # 不当作致命错误：继续往下走，把「文件清单 + need_login」返回给前端，
        # 让用户看到「有什么文件、缺什么才能下」，而不是一个空白面板。
        dlinks = {}
    # 批量漏掉的逐个补一次（百度偶发只回一部分）
    missing = [f for f in fs_ids if str(f) not in dlinks]
    for fid in missing:
        try:
            dlinks.update(share_dlinks(s_dl, sign, ts, uk, share_id, [fid], sekey, q))
        except Exception:  # noqa: BLE001
            continue

    results: list[CloudFile] = []
    for r in files:
        fid = str(r.get("fs_id") or "")
        dlink = dlinks.get(fid) or ""
        if not dlink and login_ok:
            continue     # 有登录态却拿不到直链 = 这条真失败（最后统一报错）
        try:
            size = int(r.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        results.append(CloudFile(
            name=r.get("server_filename") or r.get("filename") or "file",
            size=size,
            dlink=dlink,
            cookie=login_cookie or "",
            headers={"User-Agent": DLINK_UA},
            need_login=not login_ok,
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
