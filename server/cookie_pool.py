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
import logging
import os
import threading
import time
import urllib.parse
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


def _resolve_pool_dir() -> Path:
    """解析公共池存储目录。优先级：
    1. VDL_COOKIE_POOL_DIR —— 显式指定（已是完整目录，直接采用）
    2. RAILWAY_VOLUME_MOUNT_PATH —— Railway 持久卷自动注入，拼 /cookie_pool
    3. 兜底 —— ~/.videodownloader/cookie_pool（本地 / 无卷环境，向后兼容）
    这样挂了 Railway 持久卷后，池文件自动落到卷上、跨容器重启存活。
    """
    custom = os.environ.get("VDL_COOKIE_POOL_DIR")
    if custom:
        return Path(custom)
    mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if mount:
        return Path(mount) / "cookie_pool"
    return Path.home() / ".videodownloader" / "cookie_pool"


_POOL_DIR = _resolve_pool_dir()
_TTL = 30 * 24 * 3600  # 30 天，超时视为失效
_LOCK = threading.Lock()

# 白名单基础集（根域）。实际允许范围由 _root_domains() 动态计算：
#   = 基础集 + downloader._COOKIE_HARDENED_DOMAINS 派生 + env VDL_COOKIE_POOL_DOMAINS 扩展。
# 这样「哪些站允许上报登录态」不再写死 chrqj，加站只需改清单/配 env。
_BASE_DOMAINS = {"chrqj.com", "bilibili.com", "youku.com"}

logger = logging.getLogger(__name__)

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
    """提取根域（近似 eTLD+1）：去掉任意子域前缀，保留最后两段。"""
    d = _norm_domain(domain)
    if not d:
        return ""
    parts = d.split(".")
    if len(parts) <= 2:
        return d
    return ".".join(parts[-2:])


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
    """归一化后的候选 host（带 / 不带 www，并回退到根域）。

    例如 v.douyin.com 的视频请求要能匹配到存在 douyin.com.json 里的公共池 Cookie。
    """
    d = _norm_domain(domain)
    if not d:
        return []
    root = _strip_sub(d)
    seen: set[str] = set()
    out: list[str] = []
    for cand in (d, root):
        if not cand or cand in seen:
            continue
        seen.add(cand)
        out.append(cand)
        if cand.startswith("www."):
            no_www = cand[4:]
            if no_www not in seen:
                seen.add(no_www)
                out.append(no_www)
        else:
            with_www = "www." + cand
            if with_www not in seen:
                seen.add(with_www)
                out.append(with_www)
    return out


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


def _save(domain: str, cookies: list) -> bool:
    try:
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
            enc = c.get("header_enc") or ""
            if not header and not enc:
                # 双空条目（历史写坏/旧 bug 产物）直接丢弃，避免池文件无限膨胀
                continue
            if cipher:
                # 有明文 → 加密存储；只有密文 → 原样保留
                # （修复：旧实现用 c.get("header") 取明文，加密记录取到空串后
                #  走 else 把已有密文重写成空明文条目，导致每次 _save 丢失历史记录）
                item["header_enc"] = cipher.encrypt(header.encode()).decode() if header else enc
            else:
                if header:
                    item["header"] = header
                elif enc:
                    # 无 cipher 但有密文：尝试解密回明文，失败则保留密文
                    plain = _decrypt_item(c)
                    if plain:
                        item["header"] = plain
                    else:
                        item["header_enc"] = enc
            payload.append(item)
        f = _pool_file(domain)
        text = json.dumps({"cookies": payload}, ensure_ascii=False)
        # 原子写：先写同目录临时文件再 os.replace，避免跨进程并发（VPS 推送 / Railway prune / 读取）
        # 读到「写一半」的撕裂文件（之前诡异 65 字节空条目的根因之一）。
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, f)
        try:
            os.chmod(f, 0o600)
        except Exception:
            pass
        logger.info("[cookie_pool] _save ok domain=%s file=%s bytes=%s", domain, f, len(text))
        return True
    except Exception as e:
        logger.error("[cookie_pool] _save failed domain=%s dir=%s err=%s", domain, _POOL_DIR, e)
        return False


def get_cookie(domain: str) -> str | None:
    """返回该域最新有效的明文 Cookie（最近上报优先）；无则返回 None。"""
    candidates = _candidates(domain)
    logger.info("[cookie_pool] get domain=%s candidates=%s", domain, candidates)
    for d in candidates:
        f = _pool_file(d)
        logger.info("[cookie_pool] get file=%s exists=%s", f, f.exists())
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
            for c in reversed(data.get("cookies", [])):  # 最新优先
                if time.time() - c.get("ts", 0) > _TTL:
                    continue
                header = _decrypt_item(c)
                if header:
                    logger.info("[cookie_pool] get hit domain=%s candidate=%s len=%s", domain, d, len(header))
                    return header
        except Exception as e:
            logger.warning("[cookie_pool] get read error domain=%s file=%s: %s", domain, f, e)
            continue
    logger.info("[cookie_pool] get miss domain=%s", domain)
    return None


def add_cookie(domain: str, header: str, source: str = "sync") -> bool:
    """入池。返回 True=新增 / 更新，False=重复或非法域。"""
    domain = _norm_domain(domain)
    allowed = is_allowed(domain)
    logger.info("[cookie_pool] add domain=%s allowed=%s source=%s len=%s", domain, allowed, source, len(header or ""))
    if not allowed:
        return False
    header = (header or "").strip()
    if not header:
        return False
    with _LOCK:
        f = _pool_file(domain)
        logger.info("[cookie_pool] add file=%s exists=%s", f, f.exists())
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
                return _save(domain, cookies)
        cookies.append({"header": header, "ts": int(time.time()), "source": source})
        ok = _save(domain, cookies)
        logger.info("[cookie_pool] add saved domain=%s total=%s ok=%s", domain, len(cookies), ok)
        return ok


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


# 结构校验白名单：对 yt-dlp 试解析不稳定（常误报 False）的站，改为只检查
# Cookie 是否含该站关键登录态字段。命中即视为「结构有效」放行入池，不再
# 依赖 yt-dlp 试解析（优酷会员/受限内容常被 -3007 误判为无效）。
# 字段名集合取自各站公开登录态 cookie 命名（仅校验键名存在，不读取值）。
_STRUCTURAL_FIELDS: dict[str, set[str]] = {
    "youku.com": {"P__yk__uck", "yktk", "cna", "x5sec", "sess_vkey", "unb"},
    "v.qq.com": {"vus_session", "video_platform", "uid_tt", "login_ecookie"},
    "qq.com": {"vus_session", "video_platform", "uid_tt", "login_ecookie"},
}


def _structural_check(domain: str, header: str) -> bool | None:
    """轻量结构校验：命中白名单站关键登录态字段即返回 True（结构有效）。

    返回 True  = 含关键字段，结构有效，放行入池（无需 yt-dlp 试解析）。
    返回 None  = 该域无结构校验表（交给 _verify_generic 判定）。
    不返回 False（结构校验只做「存在即放行」，不做「缺失即否决」——
    缺失关键字段的站仍走通用验真，避免误杀仅用部分字段即可生效的 Cookie）。
    """
    d = _strip_sub(domain)
    fields = _STRUCTURAL_FIELDS.get(d)
    if not fields:
        return None
    # 归一化 cookie 头为键名集合（兼容 "k=v; k2=v2" 与 "k=v; " 等写法）
    keys: set[str] = set()
    for part in (header or "").split(";"):
        k = part.split("=", 1)[0].strip()
        if k:
            keys.add(k)
    if keys & fields:
        logger.info("[cookie_pool] structural_check hit domain=%s", d)
        return True
    logger.info("[cookie_pool] structural_check miss domain=%s（缺关键登录态字段）", d)
    return None


def add_ckey(domain: str, ckey: str, source: str = "contrib") -> bool:
    """存优酷 ckey 播放签名（与 Cookie 同域存储，独立时效）。

    ckey 是优酷 UPS 接口必需的播放签名参数（YoukuIE 不会生成，缺则 -3007）。
    它有时效（数小时~1天），过期需用户重新 Copy as cURL 贡献。
    """
    domain = _norm_domain(domain)
    if not is_allowed(domain):
        logger.warning("[cookie_pool] add_ckey 非白名单域=%s 拒绝", domain)
        return False
    ckey = (ckey or "").strip()
    if not ckey:
        return False
    with _LOCK:
        f = _pool_file(domain)
        data = {}
        if f.exists():
            try:
                data = json.loads(f.read_text())
            except Exception:
                data = {}
        data["ckey"] = {"ckey": ckey, "ts": int(time.time()), "source": source}
        try:
            _POOL_DIR.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(f.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False))
            os.replace(tmp, f)
            try:
                os.chmod(f, 0o600)
            except Exception:
                pass
            logger.info("[cookie_pool] add_ckey ok domain=%s len=%s", domain, len(ckey))
            return True
        except Exception as e:
            logger.error("[cookie_pool] add_ckey failed domain=%s err=%s", domain, e)
            return False


def get_ckey(domain: str) -> str | None:
    """读取该域最新有效的 ckey（最近贡献优先）；无/过期则返回 None。"""
    for d in _candidates(domain):
        f = _pool_file(d)
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
            item = data.get("ckey")
            if not item:
                continue
            if time.time() - item.get("ts", 0) > _TTL:
                continue
            ck = item.get("ckey")
            if ck:
                logger.info("[cookie_pool] get_ckey hit domain=%s candidate=%s len=%s", domain, d, len(ck))
                return ck
        except Exception as e:
            logger.warning("[cookie_pool] get_ckey read error domain=%s: %s", d, e)
    logger.info("[cookie_pool] get_ckey miss domain=%s", domain)
    return None


def verify_cookie(domain: str, header: str) -> bool | None:
    """按域分发验真：chrqj 走专属签名验真，其余优先结构校验、再 yt-dlp 通用验真。

    口径统一为「仅明确无效(False)才拒，无法判定(None)放行」，与
    verify_and_prune 对 None 保留的语义一致。这样 yt-dlp 试解析不稳定的站
    （如优酷 -3007）不会因误判 False 而被拒之池外。
    """
    d = _strip_sub(domain)
    if d == "chrqj.com":
        return verify_chrqj(header)
    # 优先结构校验：命中白名单站关键字段即放行
    s = _structural_check(d, header)
    if s is True:
        return True
    # 未命中结构字段表 / 未含关键字段：降级走 yt-dlp 通用试解析
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


# ----------------------------------------------------------------------------
# 按需补推（Railway -> VPS 守护进程）
# 场景：Railway 容器重建/重启后持久卷被清空，或下载时公共池 miss。
# 主动召唤 VPS 守护进程的 /v1/push 立即推送一次 Cookie，使池在 ~30s 内补满，
# 不再依赖人工补推。只在配置了 VDL_COOKIE_REFILL_URL + VDL_COOKIE_REFILL_TOKEN 时生效。
# ----------------------------------------------------------------------------
_REFILL_COOLDOWN: dict[str, float] = {}


def request_refill(domain: str, blocking: bool = False) -> bool:
    """请求 VPS 守护进程立即补推一次指定域 Cookie。

    - 带每域冷却（默认 60s），避免重启后请求洪峰反复触发。
    - blocking=False 时后台线程执行（不阻塞当前请求）；True 时同步等待结果。
    返回是否成功发起/完成。
    """
    domain = _norm_domain(domain)
    if not is_allowed(domain):
        return False
    base = os.environ.get("VDL_COOKIE_REFILL_URL")
    token = os.environ.get("VDL_COOKIE_REFILL_TOKEN")
    if not base or not token:
        logger.info("[cookie_pool] request_refill skipped (未配置 REFILL_URL/TOKEN) domain=%s", domain)
        return False
    now = time.time()
    last = _REFILL_COOLDOWN.get(domain, 0.0)
    if now - last < 60:
        return False
    _REFILL_COOLDOWN[domain] = now

    def _do():
        try:
            import urllib.request

            url = (base.rstrip("/") + "/v1/push?token="
                   + urllib.parse.quote(token, safe=""))
            req = urllib.request.Request(
                url, method="POST",
                headers={"User-Agent": "vdl-cookie-refill"},
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                data = r.read().decode() or "{}"
            ok = bool(json.loads(data).get("ok"))
            logger.info("[cookie_pool] request_refill done domain=%s ok=%s", domain, ok)
        except Exception as e:
            logger.warning("[cookie_pool] request_refill failed domain=%s: %s", domain, e)

    if blocking:
        _do()
        return True
    t = threading.Thread(target=_do, daemon=True)
    t.start()
    return True

