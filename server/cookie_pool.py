"""公共 Cookie 池（与『仅本机个人缓存』严格隔离）。

设计边界：
- cookie_cache.py 管的是「本机浏览器自动解密的个人缓存」，原则『仅本机、不外传、不跨用户』。
- 本模块管的是「用户经 /api/cookie/sync 自愿上报、知情同意的指定站点登录态」，
  用于网页版公共服务复用，让访客无需手动粘贴 Cookie。两者存储目录与用途完全分离。

安全 / 合规：
- 仅允许白名单域（目前 chrqj 系列），绝不收其他网站 Cookie。
- 存储加密（VDL_COOKIE_ENC_KEY；未配则降级为 600 权限明文），不落明文日志。
- 入池前用该 Cookie 试一次目标站验真（确认有效才收，垃圾 / 失效直接拒）。
- 后台定时探测（verify_and_prune）剔除失效项，避免公共池静默失效。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from cryptography.fernet import Fernet
except Exception:  # pragma: no cover
    Fernet = None


_POOL_DIR = Path.home() / ".videodownloader" / "cookie_pool"
_TTL = 30 * 24 * 3600  # 30 天，超时视为失效
_LOCK = threading.Lock()

# 白名单基础集（根域）。实际允许范围由 _root_domains() 动态计算：
#   = 基础集 + downloader._COOKIE_HARDENED_DOMAINS 派生 + env VDL_COOKIE_POOL_DOMAINS 扩展。
# 这样「哪些站允许上报登录态」不再写死 chrqj，加站只需改清单/配 env。
_BASE_DOMAINS = {"chrqj.com"}

# chrqj 验真用的签名参数（与 yt_dlp_plugins/extractor/chrqj.py 保持一致）
_CHRQJ_API = "https://www.chrqj.com/mw-movie/anonymous/v2/video/episode/url"
_CHRQJ_SIGN_KEY = "cb808529bae6b6be45ecfab29a4889bc"
_CHRQJ_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_CHRQJ_TEST = ("116537", "877419")  # 与 chrqj.py _TESTS 样例一致


def _cipher():
    if Fernet is None:
        return None
    k = os.environ.get("VDL_COOKIE_ENC_KEY")
    if not k:
        return None
    try:
        return Fernet(k.encode() if isinstance(k, str) else k)
    except Exception:
        return None


def _norm_domain(domain: str) -> str:
    domain = (domain or "").strip().lower()
    for p in ("https://", "http://", "//"):
        if domain.startswith(p):
            domain = domain[len(p):]
    return domain.split("/")[0].split(":")[0]


def _strip_sub(domain: str) -> str:
    """去常见子域前缀（www./m.），得到根域；其余子域（如 CDN 域）保留。"""
    d = _norm_domain(domain)
    for sub in ("www.", "m."):
        if d.startswith(sub):
            d = d[len(sub):]
    return d


def _root_domains() -> set:
    """白名单根域集合 = 基础集 + hardened 清单派生 + env 扩展。"""
    ds: set = {_strip_sub(d) for d in _BASE_DOMAINS if _strip_sub(d)}
    # env 扩展：逗号/分号/空格分隔的域名列表
    extra = os.environ.get("VDL_COOKIE_POOL_DOMAINS", "")
    for d in extra.replace(";", ",").replace(" ", ",").split(","):
        d = _strip_sub(d)
        if d:
            ds.add(d)
    # 从下载层强反爬清单派生（惰性，避免拉重依赖）
    try:
        import downloader  # noqa: F401
        for d in getattr(downloader, "_COOKIE_HARDENED_DOMAINS", ()):
            d = _strip_sub(d)
            if d:
                ds.add(d)
    except Exception:
        pass
    return ds


def is_allowed(domain: str) -> bool:
    """判断域名是否在公共池白名单内（精确根域 或 其子域）。"""
    d = _strip_sub(domain)
    if not d:
        return False
    return any(d == r or d.endswith("." + r) for r in _root_domains())


def _pool_file(domain: str) -> Path:
    safe = domain.replace("/", "_").replace("\\", "_").replace(":", "_")
    return _POOL_DIR / f"{safe}.json"


def _candidates(domain: str):
    """归一化后的候选 host（带 / 不带 www）。"""
    d = _norm_domain(domain)
    if not d:
        return []
    if d.startswith("www."):
        return [d, d[4:]]
    return [d, "www." + d]


def _decrypt_item(c: dict) -> str:
    header = c.get("header")
    if header:
        return header
    enc = c.get("header_enc")
    cipher = _cipher()
    if enc and cipher:
        try:
            return cipher.decrypt(enc.encode()).decode()
        except Exception:
            return ""
    return ""


def _save(domain: str, cookies: list) -> None:
    _POOL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_POOL_DIR, 0o700)
    except Exception:
        pass
    cipher = _cipher()
    payload = []
    for c in cookies:
        item = {"ts": c.get("ts", int(time.time())), "source": c.get("source", "sync")}
        header = c.get("header") or ""
        if cipher and header:
            item["header_enc"] = cipher.encrypt(header.encode()).decode()
        else:
            item["header"] = header
        payload.append(item)
    f = _pool_file(domain)
    f.write_text(json.dumps({"cookies": payload}, ensure_ascii=False))
    try:
        os.chmod(f, 0o600)
    except Exception:
        pass


def get_cookie(domain: str) -> str | None:
    """返回该域最新有效的明文 Cookie（最近上报优先）；无则返回 None。"""
    for d in _candidates(domain):
        f = _pool_file(d)
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
            for c in reversed(data.get("cookies", [])):  # 最新优先
                if time.time() - c.get("ts", 0) > _TTL:
                    continue
                header = _decrypt_item(c)
                if header:
                    return header
        except Exception:
            continue
    return None


def add_cookie(domain: str, header: str, source: str = "sync") -> bool:
    """入池。返回 True=新增 / 更新，False=重复或非法域。"""
    domain = _norm_domain(domain)
    if not is_allowed(domain):
        return False
    header = (header or "").strip()
    if not header:
        return False
    with _LOCK:
        f = _pool_file(domain)
        cookies = []
        if f.exists():
            try:
                cookies = json.loads(f.read_text()).get("cookies", [])
            except Exception:
                cookies = []
        for c in cookies:  # 去重：同明文已存在则仅刷新 ts
            if _decrypt_item(c) == header:
                c["ts"] = int(time.time())
                c["source"] = source
                _save(domain, cookies)
                return True
        cookies.append({"header": header, "ts": int(time.time()), "source": source})
        _save(domain, cookies)
        return True


def verify_chrqj(header: str) -> bool | None:
    """用 Cookie 试一次 chrqj 签名接口。True=有效, False=明确无效, None=无法判定(网络不可达)。"""
    if requests is None:
        return None
    try:
        vid, nid = _CHRQJ_TEST
        params = {"clientType": "1", "id": vid, "nid": nid}
        t = str(int(time.time() * 1000))
        g = "&".join("%s=%s" % (k, params[k]) for k in sorted(params))
        h = "%s&key=%s&t=%s" % (g, _CHRQJ_SIGN_KEY, t)
        sign = hashlib.sha1(hashlib.md5(h.encode()).hexdigest().encode()).hexdigest()
        headers = {
            "User-Agent": _CHRQJ_UA,
            "Referer": "https://www.chrqj.com/",
            "sign": sign,
            "t": t,
            "deviceId": str(uuid.uuid4()),
            "authorization": "",
            "Cookie": header,
        }
        r = requests.get(_CHRQJ_API, params=params, headers=headers, timeout=10)
        if r.status_code != 200:
            return False
        d = r.json()
        if d.get("code") == 200 and (d.get("data") or {}).get("list"):
            return True
        return False
    except Exception:
        return None


# 站点通用验真用的测试 URL（domain 根域 -> 该站任意一个可解析的视频页）。
# 只有「接口签名特殊、需专属验真」的站才写专属 verify；其余站用 _verify_generic
# 拿这里的 URL 试解析判定 Cookie 有效性。env VDL_COOKIE_POOL_TEST_URLS 可追加：
#   "domain=url;domain2=url2"
_TEST_URLS = {
    "chrqj.com": "https://www.chrqj.com/vod/play/116537/1/877419",
}


def _test_url(domain: str) -> str | None:
    d = _strip_sub(domain)
    if d in _TEST_URLS:
        return _TEST_URLS[d]
    extra = os.environ.get("VDL_COOKIE_POOL_TEST_URLS", "")
    for part in extra.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            if _strip_sub(k) == d:
                return v.strip()
    return None


def _verify_generic(domain: str, header: str) -> bool | None:
    """yt-dlp 通用验真兜底：带 Cookie 试解析该站测试 URL，成功拿到 info 即视为有效。

    适用于一切有 yt-dlp 提取器的平台（douyin/快手/bilibili/v.qq 等），
    无需为每站单独写签名验真。无测试 URL 时返回 None（无法判定，放行入池）。
    """
    url = _test_url(domain)
    if not url:
        return None
    try:
        from yt_dlp import YoutubeDL
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 1,
            "http_headers": {"Cookie": header, "User-Agent": _CHRQJ_UA},
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return bool(info and info.get("id"))
    except Exception:
        return False


def verify_cookie(domain: str, header: str) -> bool | None:
    """按域分发验真：chrqj 走专属签名验真，其余走 yt-dlp 通用验真。"""
    d = _strip_sub(domain)
    if d == "chrqj.com":
        return verify_chrqj(header)
    return _verify_generic(d, header)


def _prune_one(domain: str) -> int:
    """对单个规范化域文件做剔除，返回剩余有效数。"""
    with _LOCK:
        f = _pool_file(domain)
        if not f.exists():
            return 0
        try:
            cookies = json.loads(f.read_text()).get("cookies", [])
        except Exception:
            return 0
        kept = []
        for c in cookies:
            header = _decrypt_item(c)
            if not header:
                continue
            if time.time() - c.get("ts", 0) > _TTL:
                continue
            ok = verify_cookie(domain, header)
            if ok is False:  # 明确无效才剔除；None(网络不可达)保留
                continue
            kept.append(c)
        _save(domain, kept)
        return len(kept)


def verify_and_prune(domain: str) -> int:
    """后台探测：对域及其 www 变体剔除过期/失效项，返回剩余有效总数。"""
    domain = _norm_domain(domain)
    if not is_allowed(domain):
        return 0
    total = 0
    for d in _candidates(domain):
        total += _prune_one(d)
    return total


def all_domains() -> list:
    return sorted(_root_domains())
