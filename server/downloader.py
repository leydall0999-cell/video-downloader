"""yt-dlp 封装层：解析视频信息 + 执行下载并回报进度。"""

from __future__ import annotations

import logging
import os
import re
import glob
import sqlite3
import subprocess
import sys
import json
import base64
import time
import threading
from pathlib import Path
from typing import Any

# 在 import yt_dlp 之前加载本地自定义提取器插件（如 chrqj.com）。
# yt-dlp 会在自身导入时扫描 sys.path 上的 yt_dlp_plugins 包并自动注册其中的 IE。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
try:
    import yt_dlp_plugins  # noqa: F401  (触发插件自动注册)
except ImportError:
    pass

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError, UnsupportedError

from platforms import LinkError, is_china_host, SUPPORTED_PLATFORMS
from tasks import DownloadTask, TaskStore
import socket
import urllib.request
import tempfile

try:
    import requests as _requests
except Exception:  # pragma: no cover
    _requests = None  # type: ignore[assignment]

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    _CRYPTO_OK = True
except Exception:  # pragma: no cover
    Cipher = algorithms = modes = PKCS7 = None  # type: ignore[assignment]
    _CRYPTO_OK = False


def _cookie_diag(key: str, value: str = "") -> None:
    """写 Cookie 诊断日志到临时文件（打包后可读，不影响正常运行）。"""
    try:
        path = os.path.join(tempfile.gettempdir(), "vdl_cookie_diag.txt")
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        with open(path, "a") as f:
            f.write(f"[{ts}] {key}={value}\n")
    except Exception:
        pass
from urllib.parse import urlparse, parse_qsl, urlencode

logger = logging.getLogger(__name__)

SOCKET_TIMEOUT = 30  # 国内 CDN 偶发慢响应，30 秒更稳
PROBE_RETRIES = 1
# 下载健壮性：防止站点/CDN 假死导致任务永久挂起、占满并发槽拖垮后续任务
# 1) 下载阶段：已开始下分片但 N 秒无字节增量 → 判定停滞，自动终止
# 2) 整体硬上限：解析+下载任意阶段超过此秒数 → 强制结束（兜底；腾讯等限速站常需更久）
DOWNLOAD_STALL_TIMEOUT = int(os.environ.get("VDL_DOWNLOAD_STALL_TIMEOUT", "180"))
# 腾讯等站按 IP/单连接限速（实测 ~16KB/s），1800s 仅够下 29MB，故放宽到 7200s 兜底
DOWNLOAD_HARD_TIMEOUT = int(os.environ.get("VDL_DOWNLOAD_HARD_TIMEOUT", "7200"))
WATCHDOG_POLL = int(os.environ.get("VDL_WATCHDOG_POLL", "5"))  # 看门狗轮询间隔（秒）


def _macos_system_proxy() -> str:
    """读取 macOS 系统代理设置（系统偏好里开启的那个），返回 yt-dlp 可用的代理 URL。

    优先顺序：HTTPS 代理 > HTTP 代理 > SOCKS 代理。macOS 里这些通常都是一个
    HTTP CONNECT 代理，所以用 http:// 形式返回。
    """
    try:
        out = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    values: dict[str, str] = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        values[key.strip()] = val.strip()
    host = values.get("HTTPSProxy") or values.get("HTTPProxy")
    port = values.get("HTTPSPort") or values.get("HTTPPort")
    if host and port:
        return f"http://{host}:{port}"
    socks_host = values.get("SOCKSProxy")
    socks_port = values.get("SOCKSPort")
    if socks_host and socks_port:
        return f"socks5://{socks_host}:{socks_port}"
    return ""


# 常见本地代理端口（仅 macOS 兜底扫描用）：GUI 应用读不到 shell 代理时启用
_PROXY_PORTS = (
    (7890, "http"), (7891, "socks"), (7892, "http"), (7893, "socks"),
    (10808, "http"), (10809, "socks"), (6152, "http"), (6153, "socks"),
    (1079, "http"), (1080, "socks"), (1081, "socks"), (8888, "http"),
)
_PROXY_PROBE_CACHE: str | None = None  # None=未探测, ""=无命中, str=代理串


def _probe_local_proxy_ports() -> str:
    """扫描 127.0.0.1 上的常见代理端口，命中监听的第一个即返回 yt-dlp 代理串。

    仅作兜底：当 scutil 未配置系统代理、但本机确在跑 Clash/V2Ray/Surge 等时启用。
    双击 .app 是 GUI 进程、不继承终端 http_proxy，靠此兜底避免 YouTube 直连 403。
    结果缓存到模块级变量，避免每次 YouTube 解析都重扫（约 3s 开销）。
    """
    global _PROXY_PROBE_CACHE
    if _PROXY_PROBE_CACHE is not None:
        return _PROXY_PROBE_CACHE
    for port, kind in _PROXY_PORTS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                _PROXY_PROBE_CACHE = (
                    f"socks5://127.0.0.1:{port}" if kind == "socks"
                    else f"http://127.0.0.1:{port}"
                )
                return _PROXY_PROBE_CACHE
        except OSError:
            pass
        finally:
            s.close()
    _PROXY_PROBE_CACHE = ""
    return ""


def _host_of(url: str) -> str:
    """从链接里取出主机名（去掉 www./m. 前缀），解析失败返回空串。"""
    try:
        host = (urlparse(url).hostname or "").lower()
        return host.removeprefix("www.").removeprefix("m.")
    except ValueError:
        return ""


# B站 URL 归一化：统一转为 www.bilibili.com/video/BVxxx 长链。
#
# 历史策略曾把长链转 b23.tv 短链，因为无 Cookie 时长链被 B站 412 风控；但网页端
# (Railway) 必须使用登录态 Cookie，而 yt-dlp 的 BiliBili 提取器只识别 bilibili.com
# 长链，对 b23.tv 随机短码会退化为 [generic] 提取器，无法注入 Cookie/Referer，
# 导致代理 IP 下仍 412。因此新策略：所有 B站 链接统一归一化为 bilibili.com 长链，
# 由 BiliBili 提取器处理；对 b23.tv 短码先用 HEAD 请求展开真实长链。
_BILIBILI_LONG_URL_RE = re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/video/(BV[0-9A-Za-z]+)")
_BILIBILI_SHORT_BV_RE = re.compile(r"https?://(?:www\.|m\.)?b23\.tv/(BV[0-9A-Za-z]+)")
# 根路径 BV（无 /video/ 前缀，B站 在某些分享场景下会给出此形态）：
# yt-dlp 原生正则只认 /video/ 前缀，此形态会退化成 generic 提取器导致 403；
# 必须归一化为标准 /video/BVxxx 长链，API 兜底正则也依赖 /video/ 形态。
_BILIBILI_ROOT_BV_RE = re.compile(r"https?://(?:www\.)?bilibili\.com/(BV[0-9A-Za-z]+)")


def _expand_b23tv_url(url: str, proxy: str = "") -> str:
    """把 b23.tv 随机短码短链展开为真实 bilibili.com 长链。

    yt-dlp 对 b23.tv/xxxxx 随机短码会使用 [generic] 提取器，无法按 B站 逻辑注入
    Cookie/Referer/UA；在代理 IP 风控较严时会被 412 拦截。b23.tv 会返回 302
    Location，指向真实 www.bilibili.com/video/BVxxx 长链，后续即可交给
    BiliBili 提取器处理。

    实现策略：
    1) 优先 HEAD 取 Location（不下载 body，最省带宽）；
    2) HEAD 失败/无有效 Location 时回退 GET + allow_redirects=True，取最终 URL；
    3) 兼容相对 Location；
    4) 全链路记录日志，便于线上排查。
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("b23.tv", "www.b23.tv", "m.b23.tv"):
        return url
    # 已经是 /BVxxx 的短链可以直接推导成长链，不必发请求；保留 p/t 参数
    m = _BILIBILI_SHORT_BV_RE.match(url)
    if m:
        kept = [(k, v) for k, v in parse_qsl(parsed.query) if k in ("p", "t")]
        query = urlencode(kept)
        return f"https://www.bilibili.com/video/{m.group(1)}" + (f"?{query}" if query else "")

    try:
        import requests
    except Exception as e:
        logger.warning("[b23.tv expand] requests not available: %s", e)
        return url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://b23.tv/",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    timeout = int(os.environ.get("VDL_B23TV_EXPAND_TIMEOUT", "15"))

    def _is_bili_location(location: str) -> bool:
        return bool(location) and "bilibili.com" in location

    def _abs_location(resp) -> str:
        loc = resp.headers.get("Location") or ""
        if loc and not loc.startswith(("http://", "https://")):
            # 相对 Location：按 RFC 3986 拼成绝对 URL
            from urllib.parse import urljoin
            loc = urljoin(resp.url, loc)
        return loc

    # 1) 先尝试 HEAD（轻量）
    try:
        resp = requests.head(
            url,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=False,
        )
        expanded = _abs_location(resp)
        if resp.status_code in (301, 302, 307, 308) and _is_bili_location(expanded):
            logger.info("[b23.tv expand] HEAD %s -> %s", url, expanded)
            return expanded
        logger.info(
            "[b23.tv expand] HEAD %s status=%s location=%s",
            url,
            resp.status_code,
            expanded[:200],
        )
    except Exception as e:
        logger.info("[b23.tv expand] HEAD %s failed: %s", url, str(e)[:200])

    # 2) 回退 GET + 自动跟随重定向（某些环境 HEAD 被 CDN 丢弃/不返回 Location）
    try:
        resp = requests.get(
            url,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        final_url = resp.url
        # b23.tv 随机短码有时会返回 200 HTML 页面（前端 JS 跳转），
        # 此时 resp.url 仍是 b23.tv 自身，需从 body 解析真实 bilibili.com 链接兜底。
        if not _is_bili_location(final_url):
            try:
                body = resp.text
                m = re.search(r"https?://(?:www\.|m\.)?bilibili\.com/[^\"'\\s<>]+", body)
                if m:
                    cand = m.group(0)
                    if _is_bili_location(cand):
                        logger.info("[b23.tv expand] body-parse %s -> %s", url, cand)
                        resp.close()
                        return cand
            except Exception as be:
                logger.info("[b23.tv expand] body-parse %s failed: %s", url, str(be)[:120])
        resp.close()
        if _is_bili_location(final_url):
            logger.info("[b23.tv expand] GET %s -> %s", url, final_url)
            return final_url
        logger.info(
            "[b23.tv expand] GET %s final=%s history=%s",
            url,
            final_url,
            [r.status_code for r in resp.history],
        )
    except Exception as e:
        logger.info("[b23.tv expand] GET %s failed: %s", url, str(e)[:200])

    return url


def _expand_iqiyi_short_url(url: str, proxy: str = "") -> str:
    """把爱奇艺短链（iqy.net / qy.net 官方 302 跳转短链服务）展开为 iqiyi.com 真实视频页。

    yt-dlp 的 IqiyiIE 仅识别 iqiyi.com / iq.com 域；对 iqy.net/i/<id> 会落 [generic]
    提取器，而 iqy.net 页面本身不是 HTML 视频页，generic 拿不到视频流 → 「无法从该链接中
    找到视频」。把短链展开成长链后即可走 IqiyiIE 正常拿到 info。

    实现策略（沿用 _expand_b23tv_url 模式）：
    1) 优先 HEAD 取 Location（不下载 body，最省带宽）；
    2) HEAD 失败/无有效 Location 时回退 GET + allow_redirects=True，取最终 URL；
    3) Location 仅在指向 iqiyi.com / iq.com 时才接受，避免被跳到无关域。
    4) 展开失败保留原 URL —— 让下游 yt-dlp 走 generic 给出更明确的「找不到视频」错误。
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("iqy.net", "www.iqy.net", "m.iqy.net",
                    "qy.net", "www.qy.net", "m.qy.net"):
        return url

    try:
        import requests
    except Exception as e:
        logger.warning("[iqy.net expand] requests not available: %s", e)
        return url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://iqiyi.com/",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    timeout = int(os.environ.get("VDL_B23TV_EXPAND_TIMEOUT", "15"))

    def _is_iqiyi_location(location: str) -> bool:
        return bool(location) and ("iqiyi.com" in location or "iq.com" in location)

    def _abs_location(resp) -> str:
        loc = resp.headers.get("Location") or ""
        if loc and not loc.startswith(("http://", "https://")):
            from urllib.parse import urljoin
            loc = urljoin(resp.url, loc)
        return loc

    # 1) HEAD（轻量，跟随重定向拿到最终 iqiyi.com 链接）
    try:
        resp = requests.head(
            url, headers=headers, proxies=proxies, timeout=timeout, allow_redirects=True,
        )
        expanded = resp.url
        if _is_iqiyi_location(expanded):
            logger.info("[iqiyi short expand] HEAD %s -> %s", url, expanded)
            return expanded
        logger.info(
            "[iqiyi short expand] HEAD %s status=%s final=%s",
            url, resp.status_code, expanded[:200],
        )
    except Exception as e:
        logger.info("[iqiyi short expand] HEAD %s failed: %s", url, str(e)[:200])

    # 2) GET + 自动跟随重定向（HEAD 被 CDN 丢弃/不返回 Location 时回退）
    try:
        resp = requests.get(
            url, headers=headers, proxies=proxies, timeout=timeout,
            allow_redirects=True, stream=True,
        )
        final_url = resp.url
        resp.close()
        if _is_iqiyi_location(final_url):
            logger.info("[iqy.net expand] GET %s -> %s", url, final_url)
            return final_url
        logger.info(
            "[iqy.net expand] GET %s final=%s history=%s",
            url, final_url, [r.status_code for r in resp.history],
        )
    except Exception as e:
        logger.info("[iqy.net expand] GET %s failed: %s", url, str(e)[:200])

    # 展开失败：保留原 URL，让 yt-dlp 走 generic 给出「找不到视频」的明确错误
    return url


def _expand_generic_302(url: str, proxy: str = "", allowed_hosts: tuple[str, ...] = (),
                        referer: str = "") -> str:
    """通用短链 302 展开（如 shturl.cc → inke.cn、xhslink.cn → xiaohongshu.com），带目标域名白名单校验。

    与 _expand_iqiyi_short_url 同思路：yt-dlp 的 InkeIE 只认 inke.cn 域，
    shturl.cc 短链会落 [generic] 失败，需先 HEAD/GET 跟随 302 展开。

    安全：展开后目标 host 必须在 allowed_hosts 白名单内，否则视为展开失败
    返回原 URL（防恶意短链把用户带到无关站点）。
    """
    import requests as _requests

    host = _host_of(url) or ""
    # 原始 URL 已在目标域内（非短链域）→ 无需展开，直接返回
    if any(h in host for h in allowed_hosts):
        return url
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Referer": referer or ("https://www." + (allowed_hosts[0] if allowed_hosts else "") + "/"),
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = _requests.get(url, headers=headers, proxies=proxies,
                          allow_redirects=True, timeout=15)
        final = r.url or url
        final_host = _host_of(final) or ""
        if any(h in final_host for h in allowed_hosts):
            return final
    except Exception as e:  # noqa: BLE001
        logger.info("[short expand] %s failed: %s", url, str(e)[:120])
    return url


def _normalize_bilibili_url(url: str) -> str:
    """把 B站 长链/b23.tv BV 短链统一归一化为 www.bilibili.com/video/BVxxx 长链。

    保留分P（p）和时间戳（t）参数；去掉 vd_source/spm_id_from 等追踪参数。
    非 B站 链接原样返回。
    """
    # 先尝试长链（/video/BVxxx）
    m = _BILIBILI_LONG_URL_RE.match(url)
    if not m:
        # 再尝试 b23.tv/BVxxx 短链
        m = _BILIBILI_SHORT_BV_RE.match(url)
    if not m:
        # 根路径 BV（无 /video/ 前缀，B站 部分分享场景给出此形态）
        m = _BILIBILI_ROOT_BV_RE.match(url)
    if not m:
        return url
    bvid = m.group(1)
    parsed = urlparse(url)
    params: dict[str, str] = {}
    if parsed.query:
        for k, v in parse_qsl(parsed.query):
            if k in ("p", "t"):
                params[k] = v
    query = urlencode(params)
    return f"https://www.bilibili.com/video/{bvid}" + (f"?{query}" if query else "")


# --------------------------------------------------------------------------- #
# 通用链接归一化：平台无关净化 + B站 归一为 bilibili.com 长链
# --------------------------------------------------------------------------- #
# 通用追踪/推广参数（地址栏常带，对解析无益；部分平台会据此触发更严的防盗链/风控）
_URL_TRACKING_PARAMS = frozenset({
    "vd_source", "spm_id_from", "from", "from_source",     "share_source",
    "share_medium", "share_from", "share_id", "shareid", "share_channel", "timestamp",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "is_search", "scene", "sid", "campaign", "ame_from", "monitor",
    "xhsshare", "appuid", "wxfcache", "m_source",
})


def _strip_tracking_params(url: str, keep: frozenset[str] | set[str] | None = None) -> str:
    """通用：剥掉 vd_source/spm_id_from/from/share_* 等追踪参数，其余保留。

    keep: 指定需要保留的参数名集合（大小写敏感，按需使用）。用于爱奇艺
    playShare.html 等场景，其中 shareId / positiveId 是视频标识而非追踪参数。
    """
    parsed = urlparse(url)
    if not parsed.query:
        return url
    keep_set = keep or set()
    kept: list[tuple[str, str]] = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        if k in keep_set:
            kept.append((k, v))
            continue
        if k.lower() in _URL_TRACKING_PARAMS:
            continue
        kept.append((k, v))
    if not kept:
        return parsed._replace(query="").geturl()
    return parsed._replace(query=urlencode(kept)).geturl()


def _normalize_share_url(url: str, proxy: str = "") -> str:
    """链接归一化入口（平台无关）。

    1. B站：所有链接统一归一化为 www.bilibili.com/video/BVxxx 长链。
       b23.tv 随机短码会先用 HEAD 请求 302 展开，确保 yt-dlp 使用 BiliBili
       提取器（能正确注入 Cookie/Referer/UA）。桌面端 IP 风控较松时短链也
       能走 generic 通过；网页端(Railway 代理 IP)必须走 bilibili 提取器。
    2. 爱奇艺：iqy.net / qy.net 短链先 HEAD/GET 302 展开为 iqiyi.com 长链，
       否则 yt-dlp IqiyiIE 不认 iqy.net 域会落 [generic] 返回空。
    3. 其他平台：剥离追踪参数，降低防盗链/风控识别概率。
       不做伪短链转换——抖音/快手/小红书短链是服务端随机 token，
       无法从长链静态推导，强行构造会破坏解析（需调分享 API，不在本范围）。
    """
    # B站 特例优先：先把短链展开成长链，再统一规范化为 bilibili.com 长链
    if "bilibili.com" in url or "b23.tv" in url:
        expanded = _expand_b23tv_url(url, proxy=proxy)
        normalized = _strip_tracking_params(_normalize_bilibili_url(expanded))
        logger.info("[normalize] %s -> %s", url, normalized)
        return normalized

    # 爱奇艺 短链 iqy.net / qy.net 展开为 iqiyi.com 长链（yt-dlp IqiyiIE 仅识别 iqiyi.com）
    if "iqy.net" in url or "qy.net" in url:
        expanded = _expand_iqiyi_short_url(url, proxy=proxy)
        if expanded != url:
            # playShare 分享页的 shareId / positiveId 是视频标识，必须保留
            keep = {"shareId", "positiveId"} if "playShare" in expanded else None
            normalized = _strip_tracking_params(expanded, keep=keep)
            logger.info("[normalize] %s -> %s", url, normalized)
            return normalized
        # 展开失败：保留原 URL，让 yt-dlp 走 generic 给「找不到视频」更明确的错误
        return _strip_tracking_params(url)

    # 花椒直播短链 shturl.cc/xxx：302 展开为 inke.cn 真实直播/回放页
    # （yt-dlp InkeIE 只认 inke.cn 域，shturl.cc 会落 generic 失败）
    if "shturl.cc" in url:
        expanded = _expand_generic_302(url, proxy=proxy, allowed_hosts=("inke.cn",))
        if expanded != url:
            logger.info("[normalize] %s -> %s", url, expanded)
            return expanded
        return _strip_tracking_params(url)

    # 小红书短链 xhslink.cn / xhslink.com/o/xxx：302 展开为 xiaohongshu.com 详情页
    # （yt-dlp XiaohongshuIE 只认 xiaohongshu.com 域，短链会落 generic 失败；
    #  展开后的 xsec_token / share_id 等参数由 yt-dlp 自行处理，保留不剥离）
    if "xhslink.cn" in url or "xhslink.com" in url:
        expanded = _expand_generic_302(url, proxy=proxy,
                                       allowed_hosts=("xiaohongshu.com",),
                                       referer="https://www.xiaohongshu.com/")
        if expanded != url:
            logger.info("[normalize] %s -> %s", url, expanded)
            return expanded
        return _strip_tracking_params(url)

    # 爱奇艺 直接 playShare 分享页（www.iqiyi.com/playShare.html?shareId=...）：
    # shareId / positiveId 是视频标识，必须保留，否则 bare playShare.html 会被
    # 爱奇艺跳到 error.html?errortype=2（"内容暂时无法观看"），worker 也抓不到 m3u8。
    if "iqiyi.com" in url and "playShare" in url:
        normalized = _strip_tracking_params(url, keep={"shareId", "positiveId"})
        logger.info("[normalize] %s -> %s", url, normalized)
        return normalized

    # 抖音：分享短链 v.douyin.com 展开后常为 iesdouyin.com/xg/video/ID，
    # 该域名 Playwright 解析拿不到视频流；归一化为 douyin.com/video/ID 即可正常解析。
    m = re.search(r"iesdouyin\.com/xg/video/(\d{15,})", url)
    if m:
        normalized = f"https://www.douyin.com/video/{m.group(1)}"
        logger.info("[normalize] %s -> %s", url, normalized)
        return normalized

    # 西瓜视频（字节跳动同系，视频ID互通）：ixigua.com/<ID> 直接链接
    # 归一化为 douyin.com/video/<ID>，复用抖音解析通道即可正常解析下载。
    m = re.search(r"ixigua\.com/(?:video/|i)?(\d{15,})", url)
    if m:
        normalized = f"https://www.douyin.com/video/{m.group(1)}"
        logger.info("[normalize] %s -> %s", url, normalized)
        return normalized

    # 其余平台：仅做通用追踪参数净化，零风险
    return _strip_tracking_params(url)


def _to_int(v):
    """安全转 int：B站 view API 的 duration/stat 字段常是字符串。"""
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def _bilibili_api_extract(url: str, proxy: str = "", cookie: str = "") -> dict[str, Any] | None:
    """B站 专用兜底解析器：当 yt-dlp 网络栈反复 IncompleteRead 时，直接用 requests 调 B站 API。

    这是「绕过 yt-dlp 网络层」的最后手段。只处理最常见的 www.bilibili.com/video/BVxxx
    单 P 视频，构造一个足够前端展示 + 后续下载的 info dict。若 API 返回需 WBI 签名或
    其他复杂形态，返回 None 让外层继续抛原异常。
    """
    m = re.search(r"bilibili\.com/(?:video/)?(BV[0-9A-Za-z]+)", url)
    if not m:
        return None
    bvid = m.group(1)
    parsed = urlparse(url)
    page = 1
    try:
        page = int(parse_qs(parsed.query).get("p", ["1"])[0])
    except Exception:
        pass

    try:
        import requests
    except Exception as e:
        logger.warning("[bilibili api fallback] requests not available: %s", e)
        return None

    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    if cookie:
        headers["Cookie"] = _clean_header_value(cookie)

    # 1) 取视频元数据
    view_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    try:
        r = requests.get(view_url, headers=headers, proxies=proxies, timeout=30)
        r.raise_for_status()
        view_data = r.json()
        if view_data.get("code") != 0:
            logger.warning("[bilibili api fallback] view API code=%s msg=%s", view_data.get("code"), view_data.get("message"))
            return None
        video_info = view_data["data"] or {}
        pages = video_info.get("pages") or []
        page_info = pages[page - 1] if 1 <= page <= len(pages) else (pages[0] if pages else {})
        cid = page_info.get("cid")
        if not cid:
            logger.warning("[bilibili api fallback] no cid found")
            return None
    except Exception as e:
        logger.warning("[bilibili api fallback] view request failed: %s", str(e)[:200])
        return None

    # 2) 取播放地址（优先 DASH）
    play_url = (
        f"https://api.bilibili.com/x/player/playurl"
        f"?bvid={bvid}&cid={cid}&qn=127&fnver=0&fnval=4048&fourk=1"
    )
    try:
        r = requests.get(play_url, headers=headers, proxies=proxies, timeout=30)
        r.raise_for_status()
        play_data = r.json()
        if play_data.get("code") != 0:
            logger.warning("[bilibili api fallback] playurl API code=%s msg=%s", play_data.get("code"), play_data.get("message"))
            return None
        play_info = play_data.get("data") or {}
    except Exception as e:
        logger.warning("[bilibili api fallback] playurl request failed: %s", str(e)[:200])
        return None

    # 3) 构造 formats
    formats: list[dict[str, Any]] = []
    fmt_names = {
        q.get("quality"): q.get("new_description") or q.get("display_desc")
        for q in play_info.get("support_formats") or []
    }

    def _mime_ext(mime: str) -> str:
        return {"video/mp4": "m4s", "audio/mp4": "m4s"}.get(mime, "mp4")

    dash = play_info.get("dash") or {}

    # DASH 视频
    for v in dash.get("video") or []:
        url0 = v.get("baseUrl") or v.get("base_url") or v.get("url")
        if not url0:
            continue
        fid_match = re.search(r"-([0-9]+)\.m4s\\?", url0)
        fmt_id = str(v.get("id")) if not fid_match else fid_match.group(1)
        formats.append({
            "format_id": fmt_id,
            "url": url0,
            "ext": _mime_ext(v.get("mimeType") or v.get("mime_type") or ""),
            "vcodec": (v.get("codecs") or "").split(".")[0] or "avc",
            "acodec": "none",
            "width": v.get("width"),
            "height": v.get("height"),
            "fps": _to_int(v.get("frameRate")) or _to_int(v.get("frame_rate")),
            "tbr": (_to_int(v.get("bandwidth")) or 0) / 1000.0,
            "filesize": _to_int(v.get("size")),
            "quality": v.get("id"),
            "format": fmt_names.get(v.get("id")),
            "protocol": "https",
            "http_headers": {"Referer": "https://www.bilibili.com/"},
        })

    # DASH 音频
    for a in dash.get("audio") or []:
        url0 = a.get("baseUrl") or a.get("base_url") or a.get("url")
        if not url0:
            continue
        formats.append({
            "format_id": f'a-{a.get("id", "audio")}',
            "url": url0,
            "ext": _mime_ext(a.get("mimeType") or a.get("mime_type") or ""),
            "vcodec": "none",
            "acodec": (a.get("codecs") or "").split(".")[0] or "aac",
            "tbr": (_to_int(a.get("bandwidth")) or 0) / 1000.0,
            "filesize": _to_int(a.get("size")),
            "format": "音频",
            "protocol": "https",
            "http_headers": {"Referer": "https://www.bilibili.com/"},
        })

    # 兼容旧版 durl（FLV/MP4 直链）
    for d in play_info.get("durl") or []:
        url0 = d.get("url")
        if not url0:
            continue
        formats.append({
            "format_id": f'durl-{d.get("order", 0)}',
            "url": url0,
            "ext": "mp4",
            "filesize": _to_int(d.get("size")),
            "protocol": "https",
            "http_headers": {"Referer": "https://www.bilibili.com/"},
        })

    if not formats:
        logger.warning("[bilibili api fallback] no formats extracted from play_info")
        return None

    owner = video_info.get("owner") or {}
    stat = video_info.get("stat") or {}
    info = {
        "id": f'{bvid}{f"_p{page}" if page > 1 else ""}',
        "title": video_info.get("title", bvid),
        "description": video_info.get("desc"),
        "thumbnail": video_info.get("pic"),
        "uploader": owner.get("name"),
        "uploader_id": str(owner.get("mid") or ""),
        "duration": _to_int(video_info.get("duration")),
        "view_count": _to_int(stat.get("view")),
        "like_count": _to_int(stat.get("like")),
        "comment_count": _to_int(stat.get("reply")),
        "timestamp": _to_int(video_info.get("pubdate")),
        "webpage_url": url,
        "extractor": "bilibili",
        "extractor_key": "BiliBili",
        "ext": "mp4",
        "formats": formats,
        "http_headers": {"Referer": "https://www.bilibili.com/"},
    }
    # 模拟"已选流"：yt-dlp process_info 直接吃 info（不经 process_ie_result），
    # 无 requested_formats 会走单文件分支 → dl() 因无顶层 url 抛 No video formats。
    # 手动挑 best 视频轨 + 音频轨构造 requested_formats（同抖音修复经验，
    # 不设顶层 url 避免 HttpFD 合并分支丢音频；format 级已带 protocol=https）。
    _vids = [f for f in formats if f.get("vcodec") and f["vcodec"] != "none"]
    _auds = [f for f in formats if f.get("acodec") and f["acodec"] != "none"]
    if _vids and _auds:
        _vids.sort(key=lambda f: (f.get("height") or 0), reverse=True)
        info["requested_formats"] = [_vids[0], _auds[0]]
    logger.info("[bilibili api fallback] extracted %s formats for %s", len(formats), bvid)
    return info


def _rebuild_requested_formats(info: dict[str, Any], quality_key: str) -> None:
    """按用户选择的清晰度重建 requested_formats（B站 API 兜底 info 固定选了 best）。

    yt-dlp process_info 直接吃 info.requested_formats 下载，不会再用 format
    selector 过滤。若不重建，用户选 480P/360P 也会下成兜底固定的 720P。
    仅音频（audio/m4a）→ 单文件音频轨；视频 → <= 目标高度的最高 avc1 轨 + 音频轨。
    """
    fmts = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    vids = [f for f in fmts if f.get("vcodec") and f["vcodec"] != "none"]
    auds = [f for f in fmts if f.get("acodec") and f["acodec"] != "none"]
    if not vids:
        return
    if quality_key in (AUDIO_KEY, M4A_KEY):
        if auds:
            auds.sort(key=lambda f: f.get("abr") or 0, reverse=True)
            info["requested_formats"] = [auds[0]]
            info["url"] = auds[0]["url"]
            info["protocol"] = "https"
            info["ext"] = "m4a"
        return
    target = 9999
    try:
        target = int(quality_key)
    except (TypeError, ValueError):
        pass
    cand = [f for f in vids if (f.get("height") or 9999) <= target] or vids
    avc = [f for f in cand if str(f.get("vcodec") or "").startswith("avc")]
    pool = avc or cand
    pool.sort(key=lambda f: (f.get("height") or 0), reverse=True)
    v = pool[0]
    if auds:
        auds.sort(key=lambda f: f.get("abr") or 0, reverse=True)
        info["requested_formats"] = [v, auds[0]]
    else:
        info["requested_formats"] = [v]
        info["url"] = v["url"]
        info["protocol"] = "https"
    info["ext"] = "mp4"


def _clean_header_value(value: str) -> str:
    """过滤 HTTP header 值，只保留 latin-1 安全字符。

    浏览器复制出的 Cookie 串可能混入中文/特殊字符，requests/urllib3 在把 header
    编码为 latin-1 发送时会抛 `UnicodeEncodeError: 'latin-1' ... ordinal not in range(256)`。
    这里按字节清理：保留能 encode 成 latin-1 且解码后不变的字符，其余丢弃。
    """
    if not value:
        return value
    try:
        encoded = value.encode("latin-1")
        return encoded.decode("latin-1")
    except (UnicodeEncodeError, UnicodeDecodeError):
        cleaned = []
        for ch in value:
            try:
                ch.encode("latin-1")
                cleaned.append(ch)
            except UnicodeEncodeError:
                pass
        return "".join(cleaned)


def _patch_bilibili_webpage_download(proxy: str = "", cookie: str = "", ua: str = "") -> None:
    """为 BiliBiliIE 打补丁：视频页 HTML 用 requests 预下载，绕过 yt-dlp urllib 经代理 IncompleteRead。

    仅对 bilibili.com/video/BVxxx 的第一次 _download_webpage_handle 生效。
    如果 requests 也失败，回退原 yt-dlp 行为。
    """
    try:
        from yt_dlp.extractor.bilibili import BiliBiliIE
    except Exception:
        return

    attr = "_vdl_webpage_patched"
    if getattr(BiliBiliIE._download_webpage_handle, attr, False):
        return

    _orig = BiliBiliIE._download_webpage_handle

    def _patched(self, url_or_request, video_id, *args, **kwargs):
        url = ""
        try:
            if isinstance(url_or_request, str):
                url = url_or_request
            else:
                url = getattr(url_or_request, "url", "") or str(url_or_request)
        except Exception:
            url = str(url_or_request)

        if url and "/video/BV" in url and "bilibili.com" in url:
            logger.info("[bilibili patch] try requests for %s", url)
            try:
                import email.message
                import io
                import requests
                from urllib.response import addinfourl

                headers = {
                    "User-Agent": ua or (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://www.bilibili.com/",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                }
                if cookie:
                    headers["Cookie"] = _clean_header_value(cookie)
                proxies = {"http": proxy, "https": proxy} if proxy else None
                resp = requests.get(
                    url,
                    headers=headers,
                    proxies=proxies,
                    timeout=30,
                    allow_redirects=True,
                )
                resp.raise_for_status()
                content = resp.content

                # B站 响应头里可能含非 latin-1 字符，直接塞给 yt-dlp 会触发
                # 'latin-1' codec can't encode characters... 回退到原 urllib 路径。
                # 这里只保留 latin-1 安全的头，并用标准库 addinfourl 包装。
                safe_headers = email.message.Message()
                for k, v in resp.headers.items():
                    try:
                        v.encode("latin-1")
                        safe_headers[k] = v
                    except UnicodeEncodeError:
                        logger.debug("[bilibili patch] drop non-latin1 header %s", k)
                fp = io.BytesIO(content)
                urlh = addinfourl(fp, safe_headers, resp.url, code=resp.status_code)

                logger.info("[bilibili patch] requests OK %s bytes=%s", resp.url, len(content))
                return resp.text, urlh
            except Exception as e:
                logger.warning(
                    "[bilibili patch] requests failed for %s: %s, fallback to yt-dlp",
                    url,
                    str(e)[:200],
                    exc_info=False,
                )

        return _orig(self, url_or_request, video_id, *args, **kwargs)

    BiliBiliIE._download_webpage_handle = _patched
    setattr(BiliBiliIE._download_webpage_handle, attr, True)


class _YoutubeDL(YoutubeDL):
    """按是否走代理选择 request handler。

    - 有 proxy（国内站回源代理 / 用户显式代理）：强制用 **Urllib** handler。
      yt-dlp 的 Requests handler 不支持 `proxy` option（构造时忽略），强制
      requests-only 会导致请求直连 → Railway 海外 IP 访问国内站被地理围栏
      403（Cloudflare 1010）。urllib 的 ProxyHandler 对 http 代理支持可靠。
    - 无 proxy（直连场景）：维持 requests-only——urllib 频繁 IncompleteRead，
      requests/urllib3 对连接重置、分块传输不完整更健壮。
    """

    def build_request_director(self, handlers, preferences=None):
        proxy = (self.params or {}).get("proxy") or ""
        all_keys = [getattr(h, "RH_KEY", "?") for h in handlers]

        if proxy:
            urllib_handlers = [h for h in handlers if getattr(h, "RH_KEY", None) == "Urllib"]
            if urllib_handlers:
                logger.info(
                    "[yt-dlp] proxy=%s -> forcing urllib handler only (requests ignores proxy)",
                    proxy,
                )
                return super().build_request_director(urllib_handlers, preferences)
            logger.warning("[yt-dlp] proxy set but no Urllib handler available: %s", all_keys)
            return super().build_request_director(handlers, preferences)

        requests_handlers = [h for h in handlers if getattr(h, "RH_KEY", None) == "Requests"]
        logger.info("[yt-dlp] no proxy, available handlers=%s requests_found=%s", all_keys, bool(requests_handlers))

        if requests_handlers:
            logger.info("[yt-dlp] forcing requests request handler only")
            handlers = requests_handlers
        else:
            logger.warning("[yt-dlp] requests handler NOT available, falling back to all handlers")

        director = super().build_request_director(handlers, preferences)

        # 安全网：如果 filtering 没生效（例如 super() 内部又重新加入了 urllib），
        # 强制把 Urllib handler 从 director 里拿掉。
        if requests_handlers and "Urllib" in director.handlers:
            logger.warning("[yt-dlp] removing Urllib handler from director safety net")
            del director.handlers["Urllib"]

        final_keys = list(director.handlers.keys())
        logger.info("[yt-dlp] final director handlers=%s", final_keys)
        return director


def _patch_requests_handler_retries() -> None:
    """让 yt-dlp 的 Requests handler 启用 urllib3 自动重试。

    yt-dlp 默认传 Retry(False) 禁用重试。Railway→国内代理链路不稳定，
    经常读到一半连接被重置，需要 urllib3 对 GET/HEAD 等幂等请求自动重试。
    """
    try:
        from yt_dlp.networking._requests import RequestsRH
        from yt_dlp.networking._requests import RequestsHTTPAdapter
        import urllib3.util.retry
    except Exception as e:
        logger.warning("[requests patch] cannot import yt-dlp requests handler: %s", e)
        return

    if getattr(RequestsRH._create_instance, "_vdl_retries_patched", False):
        return

    _orig_create_instance = RequestsRH._create_instance

    def _patched_create_instance(self, cookiejar, legacy_ssl_support=None):
        # 先调原方法拿到 session（含默认 adapter 配置）
        session = _orig_create_instance(self, cookiejar=cookiejar, legacy_ssl_support=legacy_ssl_support)

        # 重新 mount 一个带重试策略的 adapter
        retry = urllib3.util.retry.Retry(
            total=int(os.environ.get("VDL_REQUESTS_RETRY_TOTAL", "5")),
            connect=int(os.environ.get("VDL_REQUESTS_RETRY_CONNECT", "3")),
            read=int(os.environ.get("VDL_REQUESTS_RETRY_READ", "5")),
            backoff_factor=float(os.environ.get("VDL_REQUESTS_RETRY_BACKOFF", "0.5")),
            # 对 IncompleteRead / Connection reset / read timeout 等做重试
            raise_on_status=False,
            raise_on_redirect=False,
            allowed_methods=["HEAD", "GET", "OPTIONS"],
            status_forcelist=[500, 502, 503, 504, 408, 429],
        )
        adapter = RequestsHTTPAdapter(
            ssl_context=self._make_sslcontext(legacy_ssl_support=legacy_ssl_support),
            source_address=self.source_address,
            max_retries=retry,
        )
        session.adapters.clear()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        logger.debug("[requests patch] mounted retry adapter total=%s", retry.total)
        return session

    RequestsRH._create_instance = _patched_create_instance
    setattr(RequestsRH._create_instance, "_vdl_retries_patched", True)
    logger.info("[requests patch] enabled urllib3 retries for yt-dlp requests handler")


# 模块加载时即启用 requests 重试补丁
_patch_requests_handler_retries()


def _cn_proxy_url() -> str:
    """国内站回源代理地址。

    Railway 环境（VDL 网页版部署在海外）默认走本机反向隧道代理 127.0.0.1:18889，
    该代理经 WebSocket 隧道把流量透明转发到国内 ECS 的 cn_proxy，绕开跨境入站瓶颈。
    本机（桌面/Mac，非 Railway）留空直连即可。VDL_PROXY_CN 可显式覆盖默认值。

    注意：VDL_PROXY_CN 显式置空（''）视同未设置，必须回落默认值——否则 Railway
    网页版国内站会退化成海外直连，被地理围栏 403（Cloudflare 1010）。
    """
    explicit = os.environ.get("VDL_PROXY_CN", "").strip()
    if explicit:
        return explicit
    on_railway = any(
        os.environ.get(k)
        for k in (
            "RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_ID", "RAILWAY_PROJECT_ID",
            "RAILWAY_BRANCH", "RAILWAY_DEPLOYMENT_ID", "RAILWAY_REPLICA_ID",
        )
    )
    return "http://127.0.0.1:18889" if on_railway else ""


def _resolve_proxy(host: str = "") -> str:
    """按目标站点所在地区分流代理，海外站和国内站互不干扰。

    国内站（B站/抖音/腾讯/chrqj 等）：
      VDL_PROXY_CN（国内出口回源代理）> 直连。
      服务部署在海外（Railway 等）时，国内站会被地理围栏 403，必须配 VDL_PROXY_CN
      指向一台国内机器的 HTTP 代理；本机跑在国内则留空直连即可。

    海外站（YouTube/Twitter 等）：
      VDL_PROXY > macOS 系统代理（scutil）> 标准 http(s)_proxy 环境变量。
      刻意避开 WorkBuddy 注入的 127.0.0.1:57885（实测不通海外）。

    关键：绝不能用同一个变量兜住两边——国内代理出不去海外，海外代理进不来国内。
    """
    if host and is_china_host(host):
        return _cn_proxy_url()
    explicit = os.environ.get("VDL_PROXY", "").strip()
    if explicit:
        return explicit
    if sys.platform == "darwin":
        mac = _macos_system_proxy()
        if mac:
            return mac
        # scutil 读空时（双击 .app 是 GUI 进程，不继承终端 http_proxy；
        # Clash 等开了但没写系统代理时）扫描本机常见代理端口兜底
        probed = _probe_local_proxy_ports()
        if probed:
            return probed
    return os.environ.get("https_proxy") or os.environ.get("http_proxy") or ""
DOWNLOAD_RETRIES = 3
# 下载体积上限（MB）：防止被当成免费大盘偷跑带宽 / 撑爆磁盘。设为 0 表示不限。
_MAX_FILE_MB = int(os.environ.get("VDL_MAX_FILE_MB", "2048") or 2048)
_MAX_FILE_BYTES = _MAX_FILE_MB * 1024 * 1024
# 国内站 m3u8 分片并行下载段数。低并发易触发 CDN 慢速 trickle（单连接被限速到几 KB/s），
# 适度提高可让多连接分摊带宽、显著改善长视频下载速度。
# 腾讯等平台实测：单连接限速 ~1KB/s，但**单 IP 总带宽硬顶 ~18KB/s**（与并发数无关）。
# 16 并发已吃满该上限（VPS 实测：5并发=5KB/s, 16并发=18KB/s, 32/64/aria2c 均未突破）。
# 通过 VDL_CONCURRENT_FRAGMENTS 环境变量或下载请求字段可调（1-64，腾讯以外平台可能受益于更高值）。
CONCURRENT_FRAGMENTS = int(os.environ.get("VDL_CONCURRENT_FRAGMENTS", "16") or 16)
# 可选的外部下载器：aria2c 对大量小 .ts 分片可开更多并行连接，某些平台比内置并发上限更高。
# 需本机已安装 aria2c（打包 app 运行时依赖 PATH 上的 aria2c，缺失则自动回退原生下载器）。
# 通过 VDL_DOWNLOADER 环境变量或下载请求字段切换（值为 "aria2c" 时启用）。
VDL_DOWNLOADER = (os.environ.get("VDL_DOWNLOADER") or "native").strip().lower()
_MAX_CONCURRENT = 64  # 单任务并发上限，防止被腾讯封总连接数


def _clamp_concurrency(value: int) -> int:
    if not value or value < 1:
        return CONCURRENT_FRAGMENTS
    return max(1, min(_MAX_CONCURRENT, int(value)))


def _aria2c_path() -> str | None:
    """返回 aria2c 可执行路径；未安装返回 None（调用方回退原生下载器）。

    查找顺序：PATH（本机 brew/apt 安装）→ 打包内置（PyInstaller 冻结的 Resources/bin/aria2c，
    或 macOS .app 的 Contents/Resources/bin/aria2c）。找到打包内置版时把它所在目录前置到
    os.environ["PATH"]，确保 yt-dlp 的 subprocess 能按名检索到（yt-dlp 仅按名调用外部下载器）。
    """
    import shutil

    found = shutil.which("aria2c")
    if found:
        return found
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "bin", "aria2c"))
    exe = sys.executable
    if exe:
        # macOS .app: Contents/MacOS/VideoDownloader -> ../Resources/bin/aria2c
        candidates.append(os.path.join(os.path.dirname(exe), "..", "Resources", "bin", "aria2c"))
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            bin_dir = os.path.dirname(os.path.abspath(c))
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            return c
    return None


def _build_aria2c_args(concurrency: int) -> list[str]:
    n = str(_clamp_concurrency(concurrency))
    # -x 每服务器最大连接 / -s 分片数 / -j 整体并行下载数 / -k 最小分片大小
    return ["-x", n, "-s", n, "-j", n, "-k", "1M", "--continue=true", "--max-tries=5"]


def _has_partial(workdir: Path | None) -> bool:
    """工作目录里是否残留可续传的部分文件（.part / .aria2 控制文件 / .FragN 分片）。"""
    if not workdir or not workdir.is_dir():
        return False
    try:
        for p in workdir.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if name.endswith(".part") or name.endswith(".aria2") or ".Frag" in name or name.endswith(".ytdl"):
                return True
        return False
    except OSError:
        return False
MAX_TITLE_CHARS = 80
MAX_HINT_CHARS = 180
DOWNLOAD_PHASE_CEILING = 97.0  # 下载阶段最多显示到 97%，剩余留给合并/转码

# 直链透传：用户贴的是单个可直接下载的媒体文件（.mp4 等）时，让前端直接从源站
# 把文件拉到本地，跳过服务器落盘与带宽消耗（真正只下一遍）。
_DIRECT_EXT_RE = re.compile(
    r"\.(mp4|webm|m4a|mp3|mov|mkv|ogg|flac|avi|wmv|m4v|ts|flv|f4v|m3u8)(\?|#|$|&)", re.IGNORECASE
)
# 这些域名即使是媒体扩展名结尾，也属于需经 yt-dlp 解析的平台，不能用直链透传绕过
_KNOWN_PLATFORM_HOSTS = {
    "bilibili.com", "b23.tv", "douyin.com", "tiktok.com", "tiktokv.com",
    "youtube.com", "youtu.be", "twitch.tv", "twitter.com", "x.com",
    "vimeo.com", "facebook.com", "instagram.com", "weibo.com", "qq.com",
    "v.qq.com", "iqiyi.com", "youku.com", "chrqj.com",
}


def _looks_like_direct_file(url: str) -> str | None:
    """若 URL 指向单个可直接下载的媒体文件（非已知平台），返回该 URL，否则 None。"""
    host = _host_of(url)
    if not host or host in _KNOWN_PLATFORM_HOSTS or is_china_host(host):
        return None
    if _DIRECT_EXT_RE.search(urlparse(url).path or ""):
        return url
    return None


def _cache_user_cookie(host: str, cookie: str) -> None:
    """把用户在「高级选项」手动粘贴的 Cookie 持久化到本地缓存。

    这样同站点后续解析/下载自动带登录态，不必每次重粘。
    复用 cookie_cache 模块（chmod 600、30 天 TTL、仅本机），合规且不外传。
    """
    try:
        from cookie_cache import _save
        text = cookie.strip()
        if text.lower().startswith("cookie:"):
            text = text[7:].strip()
        if text:
            _save(host, text)
    except Exception:
        pass


def _detect_direct_url(info: dict[str, Any]) -> str | None:
    """yt-dlp 解析结果若本身就是单个可直接下载的媒体文件，返回其直链。"""
    if not info.get("direct"):
        return None
    url = info.get("url") or ""
    if not url:
        return None
    protocol = (info.get("protocol") or "").split("+")[0].lower()
    if protocol not in ("http", "https", ""):
        return None
    if _DIRECT_EXT_RE.search(url) or _DIRECT_EXT_RE.search(f".{info.get('ext') or ''}"):
        return url
    return None


ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
NOISE_PATTERN = re.compile(
    r"(please report this issue.*|Confirm you are on the latest version.*|"
    r"\s*;?\s*filling out the appropriate issue template.*)",
    re.IGNORECASE | re.DOTALL,
)

QUALITY_PRESETS: tuple[tuple[int, str], ...] = (
    (2160, "4K 2160P"),
    (1440, "2K 1440P"),
    (1080, "1080P 高清"),
    (720, "720P 高清"),
    (480, "480P 标清"),
    (360, "360P 流畅"),
)

BEST_KEY = "best"
AUDIO_KEY = "audio"
WEBM_KEY = "webm"
M4A_KEY = "m4a"


class ResolveError(LinkError):
    """解析阶段失败（链接失效、需要登录、地区限制等）。"""


class ResolveRestricted(LinkError):
    """视频疑似会员专享 / 付费 / 地区限制，yt-dlp 拿不到真实数据流。

    与 ResolveError 的区别：这类链接 yt-dlp 往往不报错，而是回填占位符
    元数据（标题形如 "vqq-video video #xxx"、时长为空），属于确认无解的受限内容。
    """


class DownloadCanceled(Exception):
    """用户主动取消下载。"""


class DownloadPaused(Exception):
    """用户暂停下载——保留 .part 文件，后续可断点续传。"""


# --------------------------------------------------------------------------- #
# 信息解析
# --------------------------------------------------------------------------- #

# —— 强反爬平台：服务端校验游客 Cookie（如抖音 s_v_web_id），匿名请求直接被拒 ——
# 这类平台无法直接匿名下载，需从用户已登录/访问过的浏览器读取 Cookie。
# 一旦检测到本机浏览器含该站 Cookie，VDL 自动注入，实现“粘贴链接即下”。
_COOKIE_HARDENED_DOMAINS: tuple[str, ...] = (
    "douyin.com", "iesdouyin.com",
    "kuaishou.com", "chenzhongtech.com", "gifshow.com",
    "xiaohongshu.com", "xhslink.com", "xhslink.cn",
    "tiktok.com", "instagram.com",
    # 腾讯视频：限免/会员视频走另一套播放 API，需要登录态 cookie；
    # 加入后 app 会自动从本机浏览器读 cookie 并注入请求，提示用户粘贴。
    "v.qq.com",
    # 优酷：会员/受限视频需要登录态 Cookie，yt-dlp 解析时会报 -3007"请先登录"。
    # 加入 hardened 后桌面端自动读取浏览器 Cookie，云端可经公共池共享。
    "youku.com",
    # chrqj 影视聚合站：视频流（m3u8/ts CDN）校验播放页会话 Cookie，缺则拒绝。
    # 加入后自动从本机浏览器读该站 Cookie 并注入视频流请求头（无需手动粘贴）。
    "chrqj.com",
)

# 候选浏览器（按优先级）。Chrome/Edge/Brave/Chromium 的 Cookie 解密仅需 cryptography
# （已打包进 .app），不依赖 brotli，故优先；Firefox 需 brotli，暂不入列。
_BROWSER_COOKIE_PROFILES: tuple[tuple[str, str], ...] = (
    ("chrome", "~/Library/Application Support/Google/Chrome/*/Cookies"),
    ("edge", "~/Library/Application Support/Microsoft Edge/*/Cookies"),
    ("brave", "~/Library/Application Support/BraveSoftware/Brave-Browser/*/Cookies"),
    ("chromium", "~/Library/Application Support/Chromium/*/Cookies"),
)


def is_cookie_hardened_host(host: str) -> bool:
    """判断是否为需要浏览器 Cookie 才能解析的强反爬平台。"""
    host = (host or "").lower()
    return any(host == d or host.endswith(f".{d}") for d in _COOKIE_HARDENED_DOMAINS)


def _detect_browser_cookie_source() -> str | None:
    """探测本机已安装且含 Cookie 数据库的浏览器，返回 yt-dlp 可用的浏览器名。"""
    for name, pattern in _BROWSER_COOKIE_PROFILES:
        if glob.glob(os.path.expanduser(pattern)):
            return name
    return None


def _root_domain(host: str) -> str:
    """取根域：v.qq.com → qq.com；www.douyin.com → douyin.com；a.b.com.cn → b.com.cn。"""
    parts = (host or "").strip().lower().split(".")
    if len(parts) <= 2:
        return (host or "").strip().lower()
    if len(parts) >= 3 and parts[-2] in ("com", "net", "org", "gov", "edu", "co") \
            and parts[-1] in ("cn", "hk", "tw", "jp", "uk", "kr", "sg"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# ── CDN 域名 → Cookie 登录域映射 ──────────────────────────────────────
# 部分平台的视频流 URL 托管在独立 CDN 域（如 bilivideo.cn），但浏览器
# Cookie 存在主站域（如 bilibili.com）。_find_host_cookie_profile 按播放
# URL 的 host 查 Cookie 会落空，需回溯到正确的登录域。
_CDN_TO_COOKIE_DOMAIN: dict[str, str] = {
    # B 站：视频 CDN 在 *.bilivideo.[com|cn]，Cookie 在 bilibili.com
    "bilivideo.com": "bilibili.com",
    "bilivideo.cn": "bilibili.com",
}


def _cookie_domains_for_host(host: str) -> list[str]:
    """返回用于查找 Cookie 的候选域名列表（含 CDN→登录域回溯）。

    优先返回 host 自身的根域，再追加映射的登录域（去重）。
    """
    root = _root_domain(host)
    candidates = [root] if root else []
    mapped = _CDN_TO_COOKIE_DOMAIN.get(root)
    if mapped and mapped not in candidates:
        candidates.append(mapped)
    return candidates


def _find_host_cookie_profile(host: str) -> tuple[str, str] | None:
    """探测哪个浏览器的哪个 Profile 含有目标站点的 cookie，返回 (browser, profile)。

    背景：yt-dlp 的 cookiesfrombrowser 若不指定 profile 只读 Default；但用户登录态
    常落在其它 Profile（如 Chrome 的 Profile 33），导致「自动读 cookie」读错地方而落空。
    这里遍历各浏览器的所有 Profile，用 sqlite 直查 Cookies 数据库的 host_key
    是否命中目标根域，返回第一个命中 Profile。

    改进：支持 CDN 域名到 Cookie 登录域的回溯（如 bilivideo.cn → bilibili.com），
    解决 B 站等平台「播放 URL 域 ≠ Cookie 域」导致自动 Cookie 检测失效的问题。
    """
    domains = _cookie_domains_for_host(host)
    if not domains:
        return None
    try:
        import sqlite3 as _sq
    except Exception:
        return None
    for name, pattern in _BROWSER_COOKIE_PROFILES:
        for db in glob.glob(os.path.expanduser(pattern)):
            profile_dir = os.path.basename(os.path.dirname(db))
            try:
                con = _sq.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
                try:
                    found = False
                    for domain in domains:
                        row = con.execute(
                            "SELECT 1 FROM cookies WHERE host_key LIKE ? LIMIT 1",
                            (f"%.{domain}",),
                        ).fetchone()
                        # host_key 有的带前导点(.qq.com)、有的是裸域(qq.com)，两种都试
                        if row is None:
                            row = con.execute(
                                "SELECT 1 FROM cookies WHERE host_key = ? LIMIT 1",
                                (domain,),
                            ).fetchone()
                        if row:
                            found = True
                            break
                    if found:
                        return (name, profile_dir)
                finally:
                    con.close()
            except Exception:
                continue
    return None


def get_browser_cookie_header(host: str, url: str) -> str | None:
    """若本机浏览器含目标站点的登录 Cookie，提取并构造可用于请求头的 Cookie 字符串。

    供「在线观看」代理自动携带登录态，免去手动粘贴。返回 None 表示无可用 Cookie
    （浏览器未安装 / 该站未登录 / 解密失败）。复用 _find_host_cookie_profile 定位
    具体 Profile（登录态常不在 Default），再用 yt-dlp 的 cookie 解密能力导出。

    重要：短链域名（如 v.kuaishou.com）的 Cookie Jar 匹配只能拿到设备指纹 Cookie，
    登录 Session 和反爬 token 通常在主域（www.kuaishou.com）或登录子域（id.kuaishou.com）。
    因此对每个候选域名都尝试 add_cookie_header，合并去重后返回完整 Cookie 头。
    """
    found = _find_host_cookie_profile(host)
    if not found:
        return None
    browser, profile = found
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
        from urllib.request import Request
        jar = extract_cookies_from_browser(browser, profile)

        # 生成候选 URL 列表：原始域名 + 常见主域变体
        # 短链/CDN 域名的登录态在主域，必须合并才能拿到完整 Session
        parts = host.split(".")
        candidates = [url]
        if len(parts) >= 2:
            base = ".".join(parts[-2:])  # e.g. kuaishou.com from v.kuaishou.com
            for sub in ("www", "id", "m"):
                c_url = f"https://{sub}.{base}/"
                if c_url != url:
                    candidates.append(c_url)

        merged_names: dict[str, str] = {}  # name → value (后者覆盖前者)
        for c_url in candidates:
            req = Request(c_url)
            jar.add_cookie_header(req)
            hdr = req.get_header("Cookie")
            if hdr:
                for pair in hdr.split("; "):
                    if "=" in pair:
                        name, _, val = pair.partition("=")
                        merged_names[name.strip()] = val.strip()

        return "; ".join(f"{n}={v}" for n, v in merged_names.items()) or None
    except Exception:
        return None


def detect_browser_cookie(host: str) -> dict[str, Any]:
    """探测本机浏览器是否含目标站点的 Cookie，供前端「检测登录态」按钮与解析结果展示。

    复用 _find_host_cookie_profile 的 sqlite 直查逻辑：若命中则返回具体
    (browser, profile)，前端可据此告诉用户「已自动读取，无需手动粘贴」。
    """
    found = _find_host_cookie_profile(host)
    if found:
        return {"available": True, "browser": found[0], "profile": found[1]}
    # 浏览器装了、但该站无 Cookie：仍返回浏览器名，便于提示「请先在浏览器登录」
    b = _detect_browser_cookie_source()
    return {"available": False, "browser": b, "profile": None}


def _base_options(retries: int = DOWNLOAD_RETRIES, host: str = "", *, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    _cookie_diag("base_options_enter", f"host={host!r} cookie_len={len(cookie)}")
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": retries,
        "extractor_retries": retries,
        "fragment_retries": int(os.environ.get("VDL_FRAGMENT_RETRIES", "10")),
        "file_access_retries": int(os.environ.get("VDL_FILE_ACCESS_RETRIES", "10")),
        "skip_unavailable_fragments": True,
        "ignoreerrors": False,
        "continue": True,   # 断点续传：上次中断的 .part 可从断点接着下，大文件更稳
    }
    # 代理：用户显式传入优先；否则按平台自动策略（VDL_PROXY 环境变量 / 国内站直连 / macOS 系统代理）
    effective_proxy = proxy or _resolve_proxy(host)
    if is_china_host(host):
        # 国内站：海外部署（Railway 等）必须经 VDL_PROXY_CN 回源到国内出口，
        # 否则被地理围栏 403；本机在国内直连时该变量为空，显式置空避免 yt-dlp
        # 误读系统/环境变量里的海外代理导致超时/被拒。
        # 用户显式传入的 proxy 优先（高级选项 → 代理），否则用默认回源代理。
        options["proxy"] = proxy or _cn_proxy_url()
    elif effective_proxy:
        options["proxy"] = effective_proxy
        # 走代理时（Clash/V2Ray/Surge 等常做 HTTPS MITM 中间人解密），
        # 代理替换了 SSL 证书，必须跳过证书校验否则直接 SSL 握手失败
        options["no_check_certificates"] = True
    # 国内站（B站/抖音等）反爬严格：缺 Referer/UA 常被直接 412，无论是否带 cookie 都先补上浏览器请求头
    headers = options.setdefault("http_headers", {})
    if is_china_host(host):
        # 防盗链 Referer 必须用站点自身 origin（与在线观看代理 _stream_referer 一致）。
        # 注意 _host_of() 已剥掉 www. 前缀（host=bilibili.com），若直接 f"https://{host}/"
        # 会生成 https://bilibili.com/ —— bilibili API 校验 Referer 只认带 www 的
        # https://www.bilibili.com/，无 www 直接 403（"Unable to download JSON metadata"）。
        # 抖音也同理（www.douyin.com）。其余站用裸域 origin 即可（chrqj.com 等聚合站
        # 写死 bilibili.com 反而会拿到错误 Referer → CDN 403）。
        if "douyin" in host or "iesdouyin" in host:
            referer = "https://www.douyin.com/"
        elif "bilibili.com" in host or "b23.tv" in host:
            referer = "https://www.bilibili.com/"
        elif "iqiyi.com" in host:
            # 爱奇艺与 B站同理：页面/接口校验 Referer 只认带 www 的 https://www.iqiyi.com/
            referer = "https://www.iqiyi.com/"
        else:
            referer = f"https://{host}/"
        headers.setdefault("Referer", referer)
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        # B站 经国内代理回源时，偶发 IncompleteRead/连接重置：禁用 keep-alive + 关闭压缩，
        # 让代理/服务端按短连接完整传输页面 HTML，避免 chunked/gzip 半包问题。
        if host and ("bilibili.com" in host or "b23.tv" in host):
            headers.setdefault("Connection", "close")
            headers.setdefault("Accept-Encoding", "identity")
    # Cookie：用户粘贴的会话 Cookie（字符串）优先注入请求头，覆盖环境变量级的浏览器 Cookie
    cookie_text = cookie.strip()
    if cookie_text.lower().startswith("cookie:"):
        cookie_text = cookie_text[7:].strip()
    if cookie_text:
        # 容错：用户从 DevTools 复制的常是「裸值」（只复制了 SESSDATA 那一格的 Value，
        # 没有 SESSDATA= 前缀）。B站 用 SESSDATA 单键鉴权，检测到裸值自动补前缀，
        # 避免「明明填了 Cookie 还 403」的困惑。后端直接把该字符串当 Cookie 头发，
        # 裸值会被 B站 当作无效 Cookie 导致 403。
        is_bili = host and ("bilibili.com" in host or "b23.tv" in host)
        if is_bili and "=" not in cookie_text:
            cookie_text = f"SESSDATA={cookie_text}"
            _cookie_diag("cookie_bare_value_fixed", "bilibili bare SESSDATA auto-prefixed")
        headers["Cookie"] = cookie_text
    # YouTube 专用参数：player_client 选择。
    # 2026-08 起 YouTube 对 web/ios client 强制 SABR 流（DASH only），
    # 导致 extract_info 拿不到任何可下载格式（formats 为空或仅含图片）。
    # android_music / tv_embedded / media_connect / create 仍返回完整格式列表。
    # 注意：yt-dlp 的 player_client 是「合并」模式而非「依次尝试」，
    # 多 client 列表会导致 web 的空 SABR 结果污染整体，必须只传一个。
    if host and ("youtube.com" in host or "youtu.be" in host):
        options.setdefault("extractor_args", {}).setdefault("youtube", {})["player_client"] = ["tv_embedded"]
    elif not cookie_text:
        # 自动登录态：仅当用户未手动粘贴 Cookie 时才尝试（用户粘贴的优先级最高，
        # 避免本机缓存/公共池覆盖用户显式提供的登录态）。
        # B站 短链 b23.tv 与长链 bilibili.com 同属一个站，云端公共池 Cookie 以
        # bilibili.com 域存储。用户用 b23.tv 短链时 host=b23.tv，必须回退到
        # bilibili.com 取 Cookie，否则云端池匹配不到 → web 端无登录态 → B站 412。
        cookie_host = "bilibili.com" if "b23.tv" in host else host
        try:
            from cookie_cache import get_cached_cookie_header
            cached = get_cached_cookie_header(cookie_host)
        except Exception as e:
            cached = None
            _cookie_diag("cache_exception", str(e)[:200])
        if cached:
            headers["Cookie"] = cached
            _cookie_diag("cache_hit", f"len={len(cached)} names={[p.split('=')[0] for p in cached.split('; ')]}")
        else:
            # 公共池：服务器（Railway）无本机浏览器、本机缓存也不存在时，用
            # 「用户/开发端上报、验真过的共享登录态」兜底，让 douyin/快手等
            # hardened 站在网页版也能带 Cookie 下载（配合 VDL_PROXY_CN 即闭环）。
            pooled = None
            try:
                from cookie_pool import get_cookie as _pool_get
                pooled = _pool_get(cookie_host)
            except Exception as e:
                _cookie_diag("pool_exception", str(e)[:200])
            logger.info("[cookie] host=%s cookie_host=%s pool=%s", host, cookie_host, "hit" if pooled else "miss")
            if pooled:
                headers["Cookie"] = pooled
                _cookie_diag("pool_hit", f"len={len(pooled)}")
            else:
                # 公共池 miss 时，异步召唤 VPS 守护进程补推（非阻塞、带冷却），
                # 下次请求即可命中登录态，实现「容器重建/重启后秒级自愈」。
                try:
                    from cookie_pool import request_refill as _pool_refill
                    _pool_refill(cookie_host)
                except Exception:
                    pass
                browser = os.environ.get("VDL_COOKIES_FROM_BROWSER", "").strip()
                _cookie_diag("cache_miss", f"host={host}")
                if browser:
                    options["cookiesfrombrowser"] = (browser,)
                    _cookie_diag("cfb_env", browser)
                else:
                    # 全站默认尝试从本机浏览器读登录态（不再局限于白名单），
                    # 覆盖更多需要 Cookie 的站点（影视聚合站、会员专享、地区限制等）。
                    # 精确定位「含目标站点 cookie」的具体 Profile（登录态常不在 Default）。
                    found = _find_host_cookie_profile(host)
                    _cookie_diag("find_profile", str(found))
                    if found:
                        options["cookiesfrombrowser"] = found  # (browser, profile)
                        _cookie_diag("cfb_found", str(found))
                    else:
                        # 回退：探测不到具体 Profile 时仍用默认（Default）读，至少给一次机会
                        b = _detect_browser_cookie_source()
                        if b:
                            options["cookiesfrombrowser"] = (b,)
    return options


def _clean_message(raw: str) -> str:
    """去掉 yt-dlp 输出里的 ANSI 颜色码与"请去 GitHub 提 issue"之类的噪声。"""
    text = ANSI_PATTERN.sub("", raw)
    text = NOISE_PATTERN.sub("", text)
    text = text.replace("ERROR:", "").strip(" ;\n")
    return " ".join(text.split())[:MAX_HINT_CHARS]


def _effective_cookie_source(options: dict[str, Any], user_cookie: str = "") -> str:
    """根据 _base_options 产物判断 Cookie 实际来源。"""
    if user_cookie.strip():
        return "user_pasted"
    headers = options.get("http_headers", {})
    if headers.get("Cookie"):
        # 用户未粘贴时，_base_options 会从 cache / pool 注入 Cookie
        return "auto"
    if options.get("cookiesfrombrowser"):
        return "browser"
    return "none"


def _build_diag_context(
    url: str,
    cookie: str = "",
    proxy: str = "",
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造下载异常（403 等）所需的诊断上下文。"""
    host = _host_of(url) or ""
    headers = (options or {}).get("http_headers", {})
    effective_proxy = proxy or _resolve_proxy(host)
    return {
        "url": url,
        "host": host,
        "is_hardened": is_cookie_hardened_host(host),
        "is_china": is_china_host(host),
        "cookie_present": bool(headers.get("Cookie")),
        "cookie_source": _effective_cookie_source(options or {}, cookie),
        "proxy_used": bool(effective_proxy),
        "proxy": effective_proxy if effective_proxy else None,
        "referer": headers.get("Referer"),
        "user_agent": headers.get("User-Agent"),
        "is_cloud": os.environ.get("VDL_INSTANCE", "").strip().lower() == "cloud",
    }


def _diag_403(context: dict[str, Any], exc: Exception) -> None:
    """把 403 发生的上下文结构化写入临时日志，供线上排查。"""
    try:
        path = os.path.join(tempfile.gettempdir(), "vdl_403_diag.txt")
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a") as f:
            f.write(
                f"[{ts}] 403 host={context.get('host')} "
                f"url={str(context.get('url', ''))[:120]} "
                f"cookie_source={context.get('cookie_source')} "
                f"proxy={context.get('proxy')} "
                f"referer={context.get('referer')} "
                f"ua={context.get('user_agent')} "
                f"exc={type(exc).__name__}: {str(exc)[:200]}\n"
            )
    except Exception:
        pass


def _friendly_error(exc: Exception, context: dict[str, Any] | None = None) -> ResolveError:
    """把 yt-dlp 的英文异常转成用户能看懂的提示，并对 403 做根因分层。"""
    text = _clean_message(str(exc))
    lowered = text.lower()
    ctx = context or {}
    is_cloud = ctx.get(
        "is_cloud", os.environ.get("VDL_INSTANCE", "").strip().lower() == "cloud"
    )

    # yt-dlp 抛出 UnsupportedError 通常意味着「域名不在 yt-dlp 支持的 extractor 列表」
    # ——对 VDL 用户来说，含义比 1625 行通用提示更具体：要么找原视频、要么找
    # 页面里的 m3u8/iframe 直链、要么通知我们加 extractor。
    if isinstance(exc, UnsupportedError) or "unsupported url" in lowered:
        host = ctx.get("host", "")
        # yt-dlp 原生支持的平台（仅因链接形态不对才落 UnsupportedError），给更精准的提示，
        # 不误导成"暂未实现解析器"。2026-08-24 补：hotstar / kinopoisk yt-dlp 已有提取器。
        _YTDLP_SUPPORTED = ("hotstar.com", "kinopoisk.ru", "hd.kinopoisk.ru")
        if host and any(h == host or host.endswith("." + h) for h in _YTDLP_SUPPORTED):
            return ResolveError(
                f"该链接形态无法被 {host} 解析器识别",
                "该平台 yt-dlp 已支持，但当前链接不是可解析的播放页形态。\n\n"
                "请确认：\n"
                "① 粘贴具体的视频/影片播放页链接（而非首页或分类页）；\n"
                f"② {host} 多为地区限制内容，需对应支持地区网络与账号（数据中心 IP 常被 geo 拦截）。",
                category="unsupported_url_form",
            )
        # 已收录平台（白名单内但 yt-dlp 无 extractor）与完全未知域名区分提示，
        # 避免误报「不在支持列表」（2026-08-22 实测 yy.com/inke.cn 等已在白名单）。
        if host and any(
            host == d or host.endswith(f".{d}")
            for p in SUPPORTED_PLATFORMS
            for d in p.domains
        ):
            return ResolveError(
                f"该平台已收录（{host}），但暂未实现该站的解析器",
                "yt-dlp 未提供该网站的解析器，VDL 正在为它开发专用提取器。\n\n"
                "当前可尝试：\n"
                "① 若页面里有 m3u8 / mp4 直链（右键查看源代码搜索），直接粘贴直链可下载；\n"
                "② 等后续版本支持（常用站可反馈优先开发）。",
                category="pending_extractor",
            )
        return ResolveError(
            f"该链接暂不在 VDL 支持的 {len(SUPPORTED_PLATFORMS)} 个平台列表中",
            f"yt-dlp 也未提供 {host or '该域名'} 的解析器（可能是第三方视频聚合/解析站）。\n\n"
            "请尝试以下任一方式：\n"
            "① 在该网站播放页面右键 → 查看页面源代码，搜索 `m3u8` / `mp4` / `<video` / `<iframe src=`，"
            "把找到的直链 URL 粘贴到 VDL（m3u8 / mp4 直链可被 yt-dlp 直接下载）。\n"
            "② 跳转到原视频平台（YouTube / B站 / 抖音等）后再粘贴。\n"
            "③ 如果是您常用的网站，告诉我该网站任意一个播放页，我可以为它写个 extractor 加进 VDL。",
            category="unsupported_platform",
        )

    cloud_cookie_hint = (
        "网页版由「桌面版 VDL」共享登录态：请在桌面版 VDL 中打开并保持该平台登录，"
        "点『同步 Cookie 到云端』刷新后重试；或直接用桌面版 VDL 解析本链接。"
    )

    # 403 单独做根因分层，并记录结构化诊断日志
    if any(word in lowered for word in ("403", "forbidden", "http error 403")):
        _diag_403(ctx, exc)
        host = ctx.get("host", "")
        hardened = ctx.get("is_hardened", is_cookie_hardened_host(host)) if host else False
        cookie_present = ctx.get("cookie_present", False)
        if hardened and not cookie_present:
            return ResolveError(
                "下载被服务器拒绝（403）：该站需要登录 Cookie",
                cloud_cookie_hint if is_cloud else (
                    "该平台为强反爬站点，未检测到有效 Cookie。"
                    "请在常用浏览器登录该平台后重试，或到「高级选项 → Cookie」手动粘贴 Cookie。"
                ),
                category="cookie_required",
                context=ctx,
            )
        if hardened and cookie_present:
            return ResolveError(
                "下载被服务器拒绝（403）：Cookie 无效或已过期",
                "已检测到 Cookie，但服务器仍拒绝访问。可能原因："
                "① Cookie 已过期，请重新登录并同步；② 账号权限不足；"
                "③ 该视频为会员/付费/地区限制。",
                category="cookie_invalid_or_expired",
                context=ctx,
            )
        # 兜底通用 403
        hint = (
            "该链接被目标网站 CDN 拒绝。可能原因：①该站需要登录或 Cookie；②视频有防盗链/地区限制；"
            "③若为 YouTube：确认代理已开启且对 VDL 生效（双击 .app 不继承终端代理，需在 Clash 开启「系统代理」或 TUN 模式）；"
            "④换更低画质重试；⑤稍后再试"
        )
        # 临时诊断：把实际生效的代理/Referer 拼进 detail，定位「改了没生效」类问题
        if ctx and (ctx.get("proxy") or ctx.get("referer")):
            hint += (
                f"\n[diag] host={ctx.get('host')} proxy={ctx.get('proxy') or '(none)'} "
                f"referer={ctx.get('referer') or '(none)'} is_china={ctx.get('is_china')} "
                f"cookie={ctx.get('cookie_source') or '(none)'}"
            )
        # 折中：B站 不进 _COOKIE_HARDENED_DOMAINS（避免一刀切改文案），
        # 仅在通用 403 文案后追加专属提示，保留全部排查步骤。
        if host and ("bilibili.com" in host or "b23.tv" in host):
            hint += "；B站 高画质/会员视频常需登录态 Cookie，请先在本机常用浏览器登录 B站 后重试（VDL 会自动读取浏览器 Cookie 注入请求）。"
        return ResolveError(
            "下载被服务器拒绝（403）",
            hint,
            category="cdn_forbidden",
            context=ctx,
        )

    # 云端(网页版)实例遇到强反爬站需要登录态时，提示用户依赖桌面版共享登录态，
    # 而不是让用户去"本机浏览器登录"（网页版访客没有本机浏览器）。
    cloud_note = ""
    if is_cloud:
        cloud_note = cloud_cookie_hint
    # 每条规则带 category，使前端能按错误类型给出差异化行动建议
    # （cookie_required → 去粘贴 Cookie；network → 重试；restricted → 官方渠道）。
    # 注意：优酷 -3007、会员/登录类文案也归入 cookie_required，让「去粘贴 Cookie」
    # 按钮在该出现的场景都能出现。
    rules: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
        (("fresh cookies", "not necessarily logged in", "-3007", "please log in",
          "login required", "sign in", "authentication", "请登录", "需登录", "会员"),
         "该平台需要登录/游客 Cookie 才能访问",
         ("请在常用浏览器（Chrome 等）打开并登录过该平台，VDL 会自动读取浏览器 Cookie；"
          "或到「高级选项 → Cookie」手动粘贴该平台的 Cookie 字符串")
         if not cloud_note else cloud_note, "cookie_required"),
        (("private", "members-only"), "该视频需要登录或为私密内容", "请更换公开可访问的视频链接", "cookie_required"),
        (("geo", "not available in your country", "region"), "该视频在当前网络所在地区不可播放", "可尝试更换网络环境后重试", "restricted"),
        (("unsupported url", "no video"), "无法从该链接中找到视频", "请确认链接指向的是视频播放页，而不是首页或列表页", "unknown"),
        (("404", "not found", "removed", "unavailable", "does not exist"), "视频不存在或已被删除", "请检查链接是否正确、视频是否仍然在线", "unknown"),
        (("timed out", "timeout", "connection", "network", "resolve", "proxy", "ssl"), "网络连接超时", "请检查本机网络（部分海外站点需要代理）后重试", "network"),
        (("drm", "protected"), "该视频有版权保护，无法下载", "请通过官方渠道观看", "restricted"),
        (("extractor error", "keyerror", "unable to extract"), "无法识别该链接对应的视频", "请确认链接完整且指向具体的视频页面", "unknown"),
        (("ffmpeg", "postprocessing", "post processing", "merging"), "音视频合并失败，可能是该画质源文件格式兼容性问题", "建议：①点「重试」试一次（偶发）；②换 720P 或其他画质重新下载；③仍不行请反馈该链接", "unknown"),
    )
    for keywords, message, hint, category in rules:
        if any(word in lowered for word in keywords):
            return ResolveError(message, hint, category=category)
    return ResolveError("视频解析失败", text)


def _is_restricted_placeholder(info: dict[str, Any]) -> bool:
    """判断 yt-dlp 是否只扒到一个"壳"——标题是占位符、时长缺失。

    腾讯等平台的会员/付费受限视频，提取器不会报错，而是回填形如
    "vqq-video video #q4100..." 的占位标题且 duration 为空。这是确认无解的受限内容。
    """
    title = info.get("title")
    duration = info.get("duration")
    if duration is None and isinstance(title, str) and title.startswith("vqq-video video #"):
        return True
    # 兜底：标题完全缺失、时长缺失、且无缩略图 —— 视为根本没有解析到内容
    if duration is None and not title and not info.get("thumbnail"):
        return True
    return False


# --------------------------------------------------------------------------- #
# 抖音（douyin）专用解析：走 VPS Playwright 真实浏览器拦截视频流
# --------------------------------------------------------------------------- #
# yt-dlp 抖音提取器在抖音 2026 初反爬升级后失效（aweme_detail API 需 a_bogus
# 签名，未实现，报 "Fresh cookies needed"），即使带完整登录态 cookie 也无解。
# 故网页端抖音改走 VPS 上的 Playwright 无头浏览器（douyin_resolve.py），拿到真实
# 视频/音频轨 URL（音视频分离），再交给 yt-dlp process_info 下载 + ffmpeg 合并。
_DOUYIN_HOSTS: tuple[str, ...] = ("douyin.com", "iesdouyin.com", "ixigua.com")
_DOUYIN_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _is_douyin_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _DOUYIN_HOSTS)


# ---- VPS worker 解析结果短时缓存（resolve → 下载复用）----
# 用户先点「解析」拿直链，再点「下载」——_run_once 对 worker 平台会再次调用
# _xxx_info → _call_vps_worker，等于 VPS 上二次冷启动 Chromium（20-60s）。
# 这里按 (platform, url) 缓存 90s（覆盖「看完解析结果再点下载」的典型间隔；
# 签名直链时效：抖音 dy_q ~1h、cc auth_key ~5min、bestv s 短期，90s 保守安全），
# 命中后下载阶段解析从 20-60s 降到 <1s。失败结果不缓存。
_RESOLVE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RESOLVE_CACHE_LOCK = threading.Lock()
_RESOLVE_CACHE_TTL = 90.0
# 复用连接（keep-alive）省去每次 TLS 握手/建连（~100-300ms）
_worker_http: Any = None


def _resolve_cache_get(key: str) -> dict[str, Any] | None:
    with _RESOLVE_CACHE_LOCK:
        item = _RESOLVE_CACHE.get(key)
        if item and time.time() - item[0] <= _RESOLVE_CACHE_TTL:
            return dict(item[1])
        if item:
            _RESOLVE_CACHE.pop(key, None)
    return None


def _resolve_cache_put(key: str, data: dict[str, Any]) -> None:
    with _RESOLVE_CACHE_LOCK:
        if len(_RESOLVE_CACHE) > 200:  # 防膨胀：超限先清过期
            now = time.time()
            for k, (ts, _v) in list(_RESOLVE_CACHE.items()):
                if now - ts > _RESOLVE_CACHE_TTL:
                    _RESOLVE_CACHE.pop(k, None)
        _RESOLVE_CACHE[key] = (time.time(), data)


def _maybe_refresh_vps_token() -> str:
    """403/401 自愈：桌面端经线上 /v1/resolve 转发时，若 cloud_sync.json 的
    token 已更新（_ensure_vps_env 只在启动注入一次），重读并刷新 env，
    返回新 token（供 _call_vps_worker 重建 endpoint 重试一次）。"""
    try:
        cfg = Path.home() / ".videodownloader" / "cloud_sync.json"
        if not cfg.exists():
            return ""
        d = json.loads(cfg.read_text(encoding="utf-8"))
        tok = (d.get("token") or "").strip()
        if tok and tok != os.environ.get("VDL_COOKIE_SYNC_TOKEN", ""):
            os.environ["VDL_COOKIE_SYNC_TOKEN"] = tok
            return tok
    except Exception:  # noqa: BLE001
        pass
    return ""


def _call_vps_worker(platform: str, url: str, cookie: str = "") -> dict[str, Any]:
    """调用 VPS Playwright 解析 worker（/v1/resolve?platform=xx），返回真实流元数据。

    ``cookie``：可选的用户登录态 Cookie，原样透传给 VPS worker（如微信视频号
    finder worker 用其注入浏览器会话；为空则 worker 回退 VPS 本地共享 Cookie）。

    默认经反向隧道访问 VPS 本机 daemon：Railway 侧连 127.0.0.1:18889 隧道代理，
    转发到 ECS 127.0.0.1:18731。通过显式 ``proxies`` 参数覆盖 Railway 环境变量中的
    ``http_proxy``/``https_proxy``，避免外部代理把本地隧道/内网请求误拦截为 407。

    配置优先级：
      1. VDL_WORKER_URL / VDL_WORKER_PROXY（推荐，语义最清晰）
      2. VDL_COOKIE_REFILL_URL / VDL_COOKIE_PULL_PROXY（向后兼容）
      3. 默认值 http://127.0.0.1:18731 经 http://127.0.0.1:18889 隧道代理
    """
    worker_base = os.environ.get("VDL_WORKER_URL") or os.environ.get("VDL_COOKIE_REFILL_URL", "")
    _wp_env = (os.environ.get("VDL_WORKER_PROXY") or "").strip()
    if _wp_env.lower() in ("none", "off", "disable"):
        # 显式禁用代理：桌面 App 直连线上转发端点（https://hanyuxz.top/v1/resolve），
        # 无本地隧道，绝不能走 18889 默认代理（会连接拒绝）
        worker_proxy = None
    else:
        worker_proxy = _wp_env or os.environ.get("VDL_COOKIE_PULL_PROXY", "http://127.0.0.1:18889")
    if not worker_base:
        worker_base = "http://127.0.0.1:18731"
    # 兼容旧配置：若把隧道代理地址错填成 worker 目标，自动纠正为 daemon 目标
    if ":18889" in worker_base:
        worker_base = "http://127.0.0.1:18731"
    # 兼容旧配置：worker 目标若指向 cn_proxy(18888，需 Basic 认证)，应改为直连
    # daemon(18731) 并经隧道 18889 访问，否则会被 cn_proxy 拦成 407 误报「需要 Cookie」。
    # ⚠️ 同时检查 worker_proxy：环境变量错把 VDL_COOKIE_PULL_PROXY 配成 18888
    # 时（连到 cn_proxy 而非 tunnel client），会被拒成 400 "不可达"。
    if ":18888" in worker_base:
        worker_base = "http://127.0.0.1:18731"
        worker_proxy = "http://127.0.0.1:18889"
    if worker_proxy and ":18888" in worker_proxy:
        worker_proxy = "http://127.0.0.1:18889"

    token = os.environ.get("VDL_COOKIE_REFILL_TOKEN") or os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    if not token:
        raise ResolveError(
            "视频解析服务未配置",
            "该平台下载依赖 VPS 解析节点，请配置 VDL_COOKIE_REFILL_URL / VDL_COOKIE_REFILL_TOKEN 或 VDL_COOKIE_SYNC_TOKEN",
        )
    # 命中缓存：probe() 刚解析过（90s 内），下载阶段直接复用，跳过二次 Playwright
    # ⚠️ cookie 参与缓存 key：带用户登录态的解析结果不能与无登录态共享，
    #    否则「无 Cookie 用户命中带 Cookie 用户解析出的直链」会绕过登录态语义。
    _ckey = platform + "|" + url + ("|ck" if cookie else "")
    _cached = _resolve_cache_get(_ckey)
    if _cached is not None:
        logger.info("[worker cache] hit %s %s", platform, url[:80])
        return _cached

    def _build_endpoint(_tok: str) -> str:
        _ep = (
            worker_base.rstrip("/") + "/v1/resolve?token="
            + urllib.parse.quote(_tok, safe="")
            + "&platform=" + urllib.parse.quote(platform, safe="")
            + "&url=" + urllib.parse.quote(url, safe="")
        )
        if cookie:
            _ep += "&cookie=" + urllib.parse.quote(cookie, safe="")
        return _ep

    endpoint = _build_endpoint(token)
    # 显式指定代理并覆盖环境变量代理，确保本地隧道/内网请求不被外部 http_proxy 截获
    proxies = {"http": worker_proxy, "https": worker_proxy} if worker_proxy else None
    try:
        if _requests is None:
            raise RuntimeError("requests 库未安装")
        global _worker_http
        if _worker_http is None:
            _worker_http = _requests.Session()
        r = _worker_http.get(
            endpoint,
            headers={"User-Agent": "vdl-platform-resolve"},
            proxies=proxies,
            timeout=90,
        )
        # 403 自愈（桌面端经线上转发）：cloud_sync.json 的 token 更新后，运行中的
        # app 无需重启即可刷新（_ensure_vps_env 只在启动时注入一次）
        if r.status_code == 403 and "127.0.0.1" not in worker_base:
            _new_tok = _maybe_refresh_vps_token()
            if _new_tok and _new_tok != token:
                logger.info("[worker] token 刷新重试 %s %s", platform, url[:60])
                r = _worker_http.get(
                    _build_endpoint(_new_tok),
                    headers={"User-Agent": "vdl-platform-resolve"},
                    proxies=proxies,
                    timeout=90,
                )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        status = 0
        body = ""
        try:
            resp = getattr(e, "response", None)
            if resp is not None:
                status = getattr(resp, "status_code", 0)
                body = (getattr(resp, "text", None) or "")[:300]
        except Exception:
            pass
        # 透传 daemon 返回的业务错误（502 时 body 通常含 {"error": "..."}）
        try:
            bd = json.loads(body) if body else {}
            if isinstance(bd, dict) and bd.get("error"):
                err_msg = str(bd["error"])
                # 仅当 body 明确包含爱奇艺业务错误时才提示「需要 Cookie」；
                # 外部代理返回的 407 不应被误归类为 cookie_required。
                if platform == "iqiyi" and status == 407 and "爱奇艺" in err_msg:
                    raise ResolveError(
                        "爱奇艺该链接需要登录 Cookie",
                        "请在「高级选项 → Cookie」粘贴爱奇艺网页版的 Cookie 后重试；粘贴后 VDL 会走 yt-dlp 长期稳定路径直接解析。",
                        category="cookie_required",
                    ) from e
                # 任何 407 都按代理异常提示，避免外部代理认证错误误导成站点需登录
                if status == 407:
                    raise ResolveError(
                        "视频解析服务代理异常",
                        f"本地请求被外部代理拦截（HTTP 407）。原始响应：{err_msg[:120]}",
                    ) from e
                raise ResolveError(err_msg, f"HTTP {status}") from e
        except ResolveError:
            raise
        except Exception:
            pass
        # 非业务错误的网络/代理/超时异常：给运维侧明确提示
        if status == 407:
            raise ResolveError(
                "视频解析服务代理异常",
                f"本地请求被外部代理拦截（HTTP 407），请检查 Railway 环境变量 http_proxy/https_proxy 是否误伤内网地址。原始响应：{body[:120]}",
            ) from e
        raise ResolveError("视频解析服务不可达", f"{_clean_message(str(e))}") from e
    if not data.get("ok"):
        raise ResolveError("视频解析失败", data.get("error") or "未知错误")
    # 成功结果写入短时缓存（供「解析 → 立即下载」复用，避免二次 Playwright）
    _resolve_cache_put(_ckey, data)
    return data


_PLAYLIST_PATHS = ("/playlist", "/discover/toplist", "/album/")


def is_playlist_url(url: str) -> bool:
    """判断链接是否是「歌单/专辑」（网易云歌单、榜单、喜马拉雅专辑）。"""
    host = _host_of(url)
    path = (url or "").split("?", 1)[0]
    if host in ("music.163.com", "y.music.163.com"):
        return any(p in path for p in ("/playlist", "/discover/toplist"))
    if host in ("ximalaya.com",):
        return "/album/" in path
    return False


def probe_playlist(url: str) -> dict[str, Any]:
    """解析歌单/专辑，返回 {title, count, items:[{index,title,duration,url,is_paid?}]}。

    喜马拉雅专辑走 VPS Playwright（yt-dlp XimalayaAlbumIE 已失效，
    revision/album/v1/getTracksList 需登录；新路径 revision/album/getTracksList
    需浏览器游客态）；网易云歌单/榜单走 yt-dlp extract_flat 快速提取（~1s/200条）。
    """
    host = _host_of(url)
    if host in ("ximalaya.com",):
        data = _call_vps_worker("ximalaya_album", url)
        items = data.get("items") or []
        if not items:
            raise ResolveError("专辑解析失败", data.get("error") or "未获取到剧集列表")
        return {
            "title": data.get("title") or "喜马拉雅专辑",
            "count": data.get("count") or len(items),
            "items": items,
        }

    # 网易云歌单/榜单（及通用 playlist 兜底）→ yt-dlp extract_flat 快速提取
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "extract_flat": "in_playlist",
        "playlist_items": "1-500",
        "ignoreerrors": True,
    }
    if is_china_host(host):
        opts["proxy"] = _cn_proxy_url()
    try:
        with _YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception as e:
        raise ResolveError("歌单解析失败", _clean_message(str(e))) from e
    if info.get("_type") != "playlist":
        raise ResolveError("该链接不是歌单/专辑", "请粘贴网易云歌单或喜马拉雅专辑的完整链接")
    items: list[dict[str, Any]] = []
    for idx, e in enumerate((info.get("entries") or []), 1):
        if not e:
            continue
        item_url = e.get("url") or e.get("webpage_url") or ""
        if host in ("music.163.com", "y.music.163.com") and item_url:
            # extract_flat 返回 music.163.com/#/song?id=xxx，转标准单曲链
            m = re.search(r"[?&]id=(\d+)", item_url)
            if m:
                item_url = "https://music.163.com/song?id=" + m.group(1)
        items.append({
            "index": idx,
            "title": e.get("title") or "",
            "duration": e.get("duration"),
            "url": item_url,
        })
    if not items:
        raise ResolveError("歌单/专辑为空", "该歌单没有可下载的内容")
    return {
        "title": info.get("title") or "歌单",
        "count": info.get("playlist_count") or len(items),
        "items": items,
    }


def _douyin_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿抖音真实流，构造成 yt-dlp 兼容的 info dict。

    抖音 PC 网页两种流形态（2026-08-23 无声问题根因）：
    - 混合轨（v{id}-web.douyinvod.com 带音轨 mp4）→ 单直链下载（HttpFD，最稳）
    - 分离轨（media-video-avc1 纯视频 + media-audio 音频）→ formats + requested_formats
      让 yt-dlp 走 FFmpegFD 双轨合并分支；各自带 Referer（抖音 CDN 校验，缺则 403）。
    """
    data = _call_vps_worker("douyin", url)
    headers = {"Referer": "https://www.douyin.com/", "User-Agent": _DOUYIN_UA}
    video_url = data.get("video_url") or ""
    audio_url = data.get("audio_url") or ""
    video_has_audio = bool(data.get("video_has_audio"))
    if not video_url:
        raise ResolveError("抖音解析失败", "worker 未返回视频流", category="parse_failed")
    # 混合轨（视频自带音轨）：单直链下载，绝不构造分离 format（避免无谓合并/双音轨）
    if video_has_audio or not audio_url:
        return {
            "id": data.get("video_id") or "",
            "title": data.get("title") or "抖音视频",
            "duration": data.get("duration"),
            "thumbnail": data.get("thumbnail") or "",
            "webpage_url": data.get("webpage_url") or url,
            "extractor_key": "Douyin",
            "extractor": "douyin",
            "ext": "mp4",
            "url": video_url,
            "protocol": "https",
            "direct": True,
            "http_headers": dict(headers),
        }
    # 分离轨：视频 + 音频两轨合并
    height = int(data.get("height") or 0) or 720
    width = int(data.get("width") or 0) or (height * 16 // 9)
    formats: list[dict[str, Any]] = [{
        "url": video_url,
        "format_id": "dy-video",
        "format": "dy-video",
        "ext": "mp4",
        "protocol": "https",
        "vcodec": "avc1",
        "acodec": "none",
        "width": width,
        "height": height,
        "http_headers": dict(headers),
    }, {
        "url": audio_url,
        "format_id": "dy-audio",
        "format": "dy-audio",
        "ext": "m4a",
        "protocol": "https",
        "vcodec": "none",
        "acodec": "mp4a",
        "abr": 128,
        "http_headers": dict(headers),
    }]
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "抖音视频",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Douyin",
        "extractor": "douyin",
        "ext": "mp4",
        "requested_formats": list(formats),
        "formats": formats,
        "http_headers": dict(headers),
    }


# 快手（kuaishou.com）：SSR 无设备指纹 Cookie 时 __APOLLO_STATE__ 为空，纯 requests
# 拿不到主视频数据；改走 VPS Playwright 解析（kuaishou_resolve.py），返回合并好的
# mp4 直链（音视频已合并，无需 ffmpeg 再合）。
_KUAISHOU_HOSTS: tuple[str, ...] = ("kuaishou.com", "chenzhongtech.com", "gifshow.com")


def _is_kuaishou_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _KUAISHOU_HOSTS)


def _kuaishou_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿快手真实流（合并 mp4），构造成 yt-dlp 兼容的 info dict。

    快手视频是「音视频已合并的单个 mp4」，走 process_info 单文件分支直接下载，
    无需 requested_formats。快手 CDN 不校验 Referer，带 UA 即可。
    """
    data = _call_vps_worker("kuaishou", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "快手视频",
        "uploader": data.get("uploader") or "",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Kuaishou",
        "extractor": "kuaishou",
        "ext": "mp4",
        # worker 返回的是「音视频已合并的单个 mp4 直链」，标记为直接可下载媒体，
        # 否则 summarize → _detect_direct_url 因缺少 direct 标记丢弃直链，play_url 为空
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA},
    }


def _douyu_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿斗鱼直播/回放流，构造成 yt-dlp 兼容的 info dict。

    斗鱼直播间返回当前直播 m3u8/flv 流（is_live 由调用方按 m3u8 判断），
    回放页返回回放流。worker 用 Playwright 真实浏览器渲染，绕过 yt-dlp
    DouyuTVIE 旧正则（room_id 格式已改）与 DouyuShowIE 的 PhantomJS 依赖。
    """
    data = _call_vps_worker("douyu", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "斗鱼直播",
        "uploader": data.get("uploader") or "",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Douyu",
        "extractor": "douyu",
        "ext": data.get("ext") or "mp4",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "is_live": bool(data.get("is_live")),
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.douyu.com/"},
    }


# 央视频（yangshipin.cn）：播放地址来自 playvinfo JSONP 接口（带动态 cKey 签名），
# 纯 requests 无法复现；走 VPS Playwright 解析（ysp_resolve.py），返回带签名 mp4 直链。
def _yangshipin_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿央视频真实流（签名 mp4），构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("yangshipin", url)
    video_url = data.get("video_url") or ""
    # worker 返回的 duration 可能是字符串（"2504.2722"），转 float 兼容
    duration = data.get("duration")
    try:
        if duration is not None:
            duration = float(duration)
    except (TypeError, ValueError):
        duration = None
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "央视频",
        "duration": duration,
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Yangshipin",
        "extractor": "yangshipin",
        "ext": data.get("ext") or "mp4",
        # worker 返回的是带签名的单个 mp4 直链（音视频已合并）
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.yangshipin.cn/"},
    }


# 1905 电影网（1905.com）：详情页 SSR + 反爬（数据中心 IP 403），
# 走 VPS Playwright 解析（m1905_resolve.py），返回 vodfile.m1905.com 的 mp4/m3u8 直链。
def _m1905_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿 1905 真实流，构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("1905", url)
    video_url = data.get("video_url") or ""
    duration = data.get("duration")
    try:
        if duration is not None:
            duration = float(duration)
    except (TypeError, ValueError):
        duration = None
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "1905电影网",
        "duration": duration,
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "M1905",
        "extractor": "1905",
        "ext": data.get("ext") or "mp4",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.1905.com/"},
    }


# 风行网（fun.tv）：播放地址来自 pm.funshion.com/v7/media/play/ 接口（带
# fudid/token 签名），走 VPS Playwright 解析（fun_resolve.py），返回 mp4 直链。
def _funshion_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿风行真实流，构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("funshion", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "风行网",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Funshion",
        "extractor": "funshion",
        "ext": data.get("ext") or "mp4",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.fun.tv/"},
    }


# 百视TV（bestv.com.cn）：web 端播放地址由 wasm 函数 window.makepreviewquery(vid)
# 生成签名参数 s，再请求 /api/source/preview.m3u8?s={s} 返回 HLS 流；纯 requests 无法
# 复现 wasm 签名，走 VPS Playwright worker 调该函数拿 m3u8 直链。
def _bestv_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿百视TV HLS 流（preview.m3u8），构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("bestv", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "百视TV",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Bestv",
        "extractor": "bestv",
        "ext": data.get("ext") or "m3u8",
        # m3u8 是 HLS 清单（含 ts 分片），需经 ffmpeg/yt-dlp 拉流；标记 direct 让
        # summarize 当作直链透传给前端（前端用 hls.js 或下载器拉流）。
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.bestv.com.cn/"},
    }


# 红果短剧（hongguoduanju.com）：字节系短剧平台，播放地址来自字节 CDN
# （*.qznovelvod.com 的 /video/tos/cn/... 链接），URL 无扩展名但属可直接下载的
# 渐进式流；纯 requests 拿不到签名，走 VPS Playwright worker 点开播放器捕获真流。
def _hongguo_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿红果短剧真流，构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("hongguo", url)
    video_url = data.get("video_url") or ""
    # 字节 CDN 校验 Referer 域名：分享页(novelquickapp.com)流必须带同域 referer，
    # 写死 hongguoduanju.com 会 403。worker 已回传页面真实 referer，优先使用。
    referer = data.get("referer") or "https://www.hongguoduanju.com/"
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "红果短剧",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Hongguo",
        "extractor": "hongguo",
        # 字节 CDN 流无扩展名，但 ext 固定 mp4 让 _detect_direct_url 经 ext 兜底匹配透传
        "ext": data.get("ext") or "mp4",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": referer},
    }


# 映客直播/回放（inke.cn）：直播间 URL 形如 liveroom/index.html?uid={uid}&id={liveid}。
# 公开接口 live_share_pc 无需登录即可返回主播昵称/直播状态；真实流地址规律为
#   https://record2.inke.cn/record_{liveid}/{liveid}.m3u8?uid=0
# 该 m3u8 带 EXT-X-ENDLIST，是「开播至今的 DVR 窗口」，可一次性下载为 mp4。
# 纯 HTTP 即可构造，走 VPS worker（中国 IP 出口稳，与 bestv/hongguo 一致）。
def _inke_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿映客直播/回放 m3u8，构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("inke", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "映客直播",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Inke",
        "extractor": "inke",
        "ext": data.get("ext") or "m3u8",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "is_live": bool(data.get("is_live")),
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://www.inke.cn/"},
    }


# 网易CC直播（cc.163.com）：房间 URL 形如 cc.163.com/{cuteid}。
# 公开接口（无需登录）：
#   https://vapi.cc.163.com/video_play_url/{cuteid}?webrtc=0&src=webcc_4000_h5&...
#   → 返回签名 FLV 直链 videourl（alirelayhdl/hsrelayhdl/alirtspull.cc.netease.com），
#     离线频道返回 HTTP 410 + {"code":"Gone","data":"no live"}。
# 标题从房间页 SSR JSON 的 "title" 字段提取。纯 HTTP 走 VPS worker（中国 IP 出口稳）。
def _cc_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿网易CC直播 FLV 直链，构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("cc", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "网易CC直播",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "CCLive",
        "extractor": "cc",
        "ext": data.get("ext") or "flv",
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "is_live": bool(data.get("is_live")),
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://cc.163.com/"},
    }


# 微博（weibo.com）：非浏览器请求返回 Sina Visitor System 反爬验证页，yt-dlp 内置
# WeiboIE 也已失效；改走 VPS Playwright 解析（weibo_resolve.py），返回合并 mp4 直链。
_WEIBO_HOSTS: tuple[str, ...] = ("weibo.com", "weibo.cn", "t.cn")


def _is_weibo_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _WEIBO_HOSTS)


def _weibo_info(url: str) -> dict[str, Any]:
    """调 VPS worker 拿微博真实流（合并 mp4），构造成 yt-dlp 兼容的 info dict。"""
    data = _call_vps_worker("weibo", url)
    video_url = data.get("video_url") or ""
    return {
        "id": data.get("video_id") or "",
        "title": data.get("title") or "微博视频",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Weibo",
        "extractor": "weibo",
        "ext": "mp4",
        # worker 返回的是「音视频已合并的单个 mp4 直链」，标记为直接可下载媒体
        "direct": True,
        "url": video_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": "https://weibo.com/"},
    }


# 爱奇艺（iqiyi.com / iq.com）：分享页 playShare.html?shareId=X 是纯 JS SPA，
# yt-dlp IqiyiIE 提取不到 tvid 报 "Can't find any video"；改走 VPS Playwright
# 解析（iqiyi_resolve.py），等 JS 渲染出 data-player-tvid/videoid 后调 tmts API
# 拿 m3u8 直链（音视频合一清单，由 yt-dlp 分段下载）。
_IQIYI_HOSTS: tuple[str, ...] = ("iqiyi.com", "iq.com")


def _is_iqiyi_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _IQIYI_HOSTS)


def _iqiyi_info(url: str, cookie: str = "") -> dict[str, Any] | None:
    """调 VPS worker 拿爱奇艺真实流，构造成 yt-dlp 兼容的 info dict。

    返回 None 表示应回退 yt-dlp 通用流程（用户已提供 Cookie 且 worker 不可用/失败，
    走 yt-dlp IqiyiIE 直下——不受爱奇艺对 worker 的反爬检测影响，VIP Cookie 也能生效）。

    分发策略（覆盖爱奇艺所有链接形式——playShare 分享页 / v_xxx.html 普通播放页 /
    a_xxx.html 专辑详情页 / intl iq.com 等）：
    1. 用户已贴 Cookie 且链接非 playShare 分享页 → 跳过 worker，直接回退 yt-dlp 直下
       （长期稳定路径，避免 worker 反爬抖动影响已知可用链接，也省 VPS 资源）。
    2. 其余情况（playShare 分享页 / 普通页无 Cookie）→ 优先走 VPS worker，
       worker 成功即返回 m3u8 直链，无需用户 Cookie。
    3. worker 不可用（未配置/不可达）或解析失败（407 需登录 / VIP / 链接失效等）：
       - 用户已贴 Cookie → 回退 yt-dlp 直下兜底；
       - 用户未贴 Cookie → 提示去「高级选项 → Cookie」粘贴爱奇艺网页版 Cookie。
    """
    # 长期稳定路径：用户已贴 Cookie 且非分享页 → 跳过 worker 直下 yt-dlp
    if cookie and "playShare" not in url:
        return None

    try:
        data = _call_vps_worker("iqiyi", url)
    except ResolveError as e:
        cat = getattr(e, "category", None)
        # playShare 分享页只能走 worker（yt-dlp 无对应提取器），直接透传 worker 真实错误——
        # 无论是真需要登录（cookie_required）还是隧道/VPS 故障（不可达/代理异常），
        # 都如实抛出，不伪造提示。
        if "playShare" in url:
            raise
        # 非分享页：用户已贴 Cookie → 回退 yt-dlp 直下兜底（长期稳定路径，VIP Cookie 也生效）
        if cookie:
            return None
        # 非分享页 + 无 Cookie：
        # - worker 明确需要登录（category=cookie_required）→ 透传，提示去「高级选项 → Cookie」粘贴；
        # - 其余（未配置 / 不可达 / 代理异常 / 解析失败）→ 透传服务真实状态，
        #   绝不再伪装成「需要 Cookie」，避免把隧道/VPS 故障误导成登录问题。
        raise

    stream_url = data.get("video_url") or ""
    if not stream_url:
        if cookie:
            return None
        raise ResolveError(
            "爱奇艺解析失败",
            "worker 未返回视频流，可能是付费/VIP 专享、链接失效或页面未加载。",
            category="parse_failed",
        )
    # 2026-08 实测：免费/低清内容播放器走 *.inter.71edge.com 的 f4v 完整直链
    # （FLV 容器），VIP/部分内容仍走 m3u8 HLS。按 URL 形态区分协议与扩展名：
    is_hls = _is_hls_url(stream_url)
    return {
        "id": data.get("video_id") or data.get("tvid") or "",
        "title": data.get("title") or "爱奇艺视频",
        "duration": data.get("duration"),
        "thumbnail": data.get("thumbnail") or "",
        "webpage_url": data.get("webpage_url") or url,
        "extractor_key": "Iqiyi",
        "extractor": "iqiyi",
        "ext": "mp4" if is_hls else "flv",
        "url": stream_url,
        # f4v 是完整媒体文件（非 HLS），需 direct 标记才能经 _detect_direct_url
        # 透传给前端（play_url / watch_options / 直链下载入口）；
        # m3u8 走 _detect_play_url 的 HLS 检测，无需 direct。
        "direct": not is_hls,
        "protocol": "m3u8_native" if is_hls else "https",
        "http_headers": {"User-Agent": _DOUYIN_UA, "Referer": data.get("webpage_url") or url},
    }


# Rumble（rumble.com）：yt-dlp 的 RumbleIE/RumbleEmbedIE 被 Cloudflare 反爬
# 403 拦截（数据中心 IP + 非浏览器指纹请求），线上实测 embedJS JSON 也 403。
# 方案：带完整浏览器头的直连请求 embedJS/u3 API（SPA 播放器同源接口），
# 解析 mp4/hls 直链（sp.rmbl.ws CDN 不受 Cloudflare 挑战）。若仍 403 则
# 明确提示需要海外浏览器环境，交由通用兜底报错。
_RUMBLE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 优酷：yt-dlp 内置 YoukuIE 不生成 ckey 播放签名，缺则 UPS 返回 -3007。
# 这里走专用 UPS 通道：带 Cookie（来自共享池）+ ckey（来自共享池）直接拿 m3u8。
# ckey 由用户从已登录浏览器「Copy as cURL」贡献（有时效，过期重新贡献即可）。
# ---------------------------------------------------------------------------
_YOUKU_M_UA = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36"
)


def _youku_vid(url: str) -> str:
    m = re.search(r"id_([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    # player.youku.com 形态
    m = re.search(r"vid=([A-Za-z0-9]+)", url)
    if m:
        return m.group(1)
    return ""


def _youku_ups_ckey(vid: str, cookie: str, ckey: str, utid: str, proxy: str = "") -> list[dict]:
    """直接打 UPS 接口（带 ckey），返回 stream 列表。失败抛 ResolveError。"""
    import json as _json
    import ssl as _ssl

    cctx = _ssl.create_default_context()
    cctx.check_hostname = False
    cctx.verify_mode = _ssl.CERT_NONE
    # 优酷对境外服务器出口 IP 有地域风控（UPS 返回空 stream）。
    # 优先用调用方传入的 proxy（probe 已通过 _resolve_proxy 算出国内站 cn_proxy），
    # 否则落回 _cn_proxy_url()（Railway 上自动指向 127.0.0.1:18889 国内隧道）。
    _proxy = (proxy or "").strip() or _cn_proxy_url()
    _handlers = {}
    if _proxy:
        _handlers["http"] = _proxy
        _handlers["https"] = _proxy
    _opener = urllib.request.build_opener(urllib.request.ProxyHandler(_handlers)) if _handlers else None
    params = (
        f"vid={vid}&ccode=0501&client_ip=0.0.0.0&app_ver=1.0.75&client_ts=1787502724"
        f"&fu=0&vr=0&rst=mp4&dq=mp4&os=android&bt=phone&bd=&tict=0&d=0&needbf=1"
        f"&site=1&aw=w&vs=1.0&pver=1&wintype=xplayer_m3u8&play_ability=1024"
        f"&utid={utid}&ckey={ckey}"
    )
    api = f"https://ups.youku.com/ups/get.json?{params}"
    req = urllib.request.Request(
        api,
        headers={
            "User-Agent": _YOUKU_M_UA,
            "Referer": "https://m.youku.com/",
            "Origin": "https://m.youku.com",
            "Cookie": cookie or "",
        },
    )
    try:
        if _opener:
            with _opener.open(req, timeout=20) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
        else:
            with urllib.request.urlopen(req, timeout=20, context=cctx) as resp:
                data = _json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        raise ResolveError(
            "优酷解析失败",
            f"无法连接优酷 UPS 接口（{type(e).__name__}）。",
            category="parse_failed",
        )
    code = data.get("code")
    if code not in (None, 0, "0", 200) and "stream" not in data.get("data", {}):
        raise ResolveError(
            "优酷解析失败",
            f"UPS 返回错误码 {code}：{data.get('msg')}。常见为 ckey 过期或 Cookie 失效，"
            f"请在已登录浏览器重新「Copy as cURL」贡献后重试。",
            category="parse_failed",
        )
    d = data.get("data", {}) or {}
    # 优酷完整可播放流在 streams 字段（会员/有权限内容才有）；stream 是含试看的混合字段。
    # 优先 streams，避免拿试看流当完整片（会下到 6 分钟残片）。
    full = d.get("streams") or []
    if full:
        return full
    # 无完整流：判断是否会员专享受限
    pay = d.get("pay") or {}
    if isinstance(pay, dict) and pay.get("can_play") is False:
        raise ResolveError(
            "优酷视频需会员/付费权限",
            "该优酷视频为 VIP/付费专享，当前共享池账号无播放权限，仅能试看。"
            "请用会员账号的 Cookie+ckey 重新贡献，或更换免费视频。",
            category="need_vip",
        )
    # 兜底诊断：把 UPS 原始返回片段带上，便于定位（境外 IP 风控 / ckey 失效等）
    _ec = (d.get("e") or {}).get("code")
    _has_stream = bool(d.get("stream"))
    raise ResolveError(
        "优酷解析失败",
        f"UPS 未返回可播放流。诊断：e.code={_ec}, stream={_has_stream}, "
        f"ckey_len={len(ckey)}, 可能为 ckey 失效或服务器出口 IP 被优酷地域限制。",
        category="parse_failed",
    )


def _youku_info(url: str, cookie: str = "", ckey: str = "", proxy: str = "") -> dict[str, Any]:
    """优酷专用解析：UPS + ckey 拿 m3u8。返回 yt-dlp 兼容 info dict。"""
    vid = _youku_vid(url)
    if not vid:
        raise ResolveError("优酷解析失败", "无法从链接识别视频 ID。", category="parse_failed")

    # utid = cna cookie 值（UPS 必需）
    utid = ""
    for part in (cookie or "").split(";"):
        k, _, v = part.partition("=")
        if k.strip() == "cna":
            utid = v.strip()
            break

    if not ckey:
        raise ResolveError(
            "优酷需要播放签名(ckey)",
            "该优酷视频需登录态播放签名(ckey)，当前公共池没有有效 ckey。"
            "请在已登录优酷的浏览器中：F12 → Network → 过滤 ups → 刷新 → 右键 "
            "该请求 Copy as cURL，把内容发给我即可自动贡献。",
            category="need_ckey",
        )

    streams = _youku_ups_ckey(vid, cookie, ckey, utid, proxy=proxy)
    if not streams:
        raise ResolveError(
            "优酷解析失败",
            f"UPS 通道未拿到可播放流。ckey_len={len(ckey)}, cookie_len={len(cookie)}, "
            "可能为 ckey 失效或服务器出口 IP 被优酷地域限制。",
            category="parse_failed",
        )

    # 选最高清晰度（按 height 排序）
    def _h(s: dict) -> int:
        return int(s.get("height") or 0)

    streams_sorted = sorted(streams, key=_h, reverse=True)
    best = streams_sorted[0]
    m3u8 = best.get("m3u8_url") or best.get("playurl") or ""
    if not m3u8:
        # 退而求其次：任意含 url 的字段
        for k, v in best.items():
            if "url" in k.lower() and isinstance(v, str) and v.startswith("http"):
                m3u8 = v
                break
    if not m3u8:
        raise ResolveError("优酷解析失败", "UPS 返回的流缺少 m3u8 地址。", category="parse_failed")

    # 标题：优先从网页 <title> 取，失败用 video id
    title = f"优酷视频_{vid}"
    try:
        import requests as _req

        r = _req.get(
            url,
            headers={"User-Agent": _YOUKU_M_UA},
            timeout=10,
            cookies={k.strip(): v.strip() for k, _, v in (p.partition("=") for p in (cookie or "").split(";") if "=" in p)},
        )
        mt = re.search(r"<title>(.*?)</title>", r.text, re.S)
        if mt:
            title = re.sub(r"[_-]?优酷.*$", "", mt.group(1)).strip() or title
    except Exception:
        pass

    return {
        "id": vid,
        "title": title,
        "webpage_url": url,
        "extractor_key": "YoukuCkey",
        "extractor": "youku",
        "ext": "mp4",
        "direct": True,
        "url": m3u8,
        "protocol": "m3u8_native",
        "http_headers": {
            "User-Agent": _YOUKU_M_UA,
            "Referer": "https://m.youku.com/",
            "Cookie": cookie or "",
            "Origin": "https://m.youku.com",
        },
        # 标记优酷 m3u8 直连（下载时用 ffmpeg/yt-dlp 带 header 拉）
        "_youku_m3u8": True,
    }


def _rumble_info(url: str, cookie: str = "") -> dict[str, Any]:
    import json as _json

    # curl_cffi 提供 Chrome TLS 指纹模拟，可绕过 Cloudflare 基础反爬；
    # 未安装（本地测试环境）时降级 urllib（生产 Railway 已装依赖）。
    try:
        from curl_cffi import requests as _cf_requests
    except Exception:
        _cf_requests = None

    m = re.search(r"rumble\.com/(?:embed/)?(?:v(?!ideos))?([0-9a-z]+)", url)
    vid = m.group(1) if m else ""
    if not vid:
        raise ResolveError("Rumble 解析失败", "无法从链接中识别视频 ID。", category="parse_failed")

    headers = {
        "User-Agent": _RUMBLE_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://rumble.com/",
        "Connection": "keep-alive",
    }

    def _cf_fetch(_u: str) -> str:
        """curl_cffi 带浏览器指纹抓取；403 限流时重试并轮换指纹。返回响应文本。"""
        if _cf_requests is None:
            req = urllib.request.Request(_u, headers=headers)
            return urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
        body = ""
        _last_status = 0
        for _attempt in range(3):
            _imp = ("chrome", "chrome124", "firefox133")[_attempt % 3]
            _resp = _cf_requests.get(_u, headers=headers, impersonate=_imp, timeout=20)
            if _resp.status_code == 200:
                body = _resp.text
                break
            _last_status = _resp.status_code
        if not body:
            raise ResolveError(
                "Rumble 解析失败",
                f"Rumble 接口返回 {_last_status}（Cloudflare 反爬或地区限制）。"
                f"建议：①稍后重试；②在「高级选项」设置海外代理后重试。",
                category="parse_failed",
            )
        return body

    # 关键：v{短id} 与 embedJS 的真实 embed id 未必相同，embedJS 用错 id 会返回
    # 随机/推荐视频（实测同一 URL 三次返回三个不同视频，均为无效 id 的兜底响应）。
    # 对齐 RumbleIE 做法：先抓视频页提取 Rumble("play",{video:"xxx"}) 的 embed id，
    # 提取不到就明确报错（绝不退回短 id 请求 embedJS）。
    try:
        _page_html = _cf_fetch(url)
    except ResolveError:
        raise
    except Exception as e:
        raise ResolveError(
            "Rumble 解析失败",
            f"无法抓取 Rumble 视频页（{type(e).__name__}），可能是 Cloudflare 反爬。"
            f"建议：①稍后重试；②在「高级选项」设置海外代理后重试。",
            category="parse_failed",
        ) from None
    _m = re.search(
        r'Rumble\(\s*"play"\s*,\s*{[^}]*?\bvideo\b\s*:\s*["\']([0-9a-z]+)["\']',
        _page_html,
    )
    if not _m:
        _m = re.search(r"rumble\.com/embed/([0-9a-z]+)", _page_html)
    embed_id = _m.group(1) if _m else ""
    if not embed_id:
        raise ResolveError(
            "Rumble 解析失败",
            "视频页已加载但未找到播放器配置（页面结构可能变化）。请反馈该链接。",
            category="parse_failed",
        )

    api = f"https://rumble.com/embedJS/u3/?request=video&ver=2&v={embed_id}"
    try:
        body = _cf_fetch(api)
    except ResolveError:
        raise
    except urllib.error.HTTPError as e:
        raise ResolveError(
            "Rumble 解析失败",
            f"Rumble 接口返回 {e.code}（Cloudflare 反爬或地区限制）。"
            f"建议：①稍后重试；②在「高级选项」设置海外代理后重试。",
            category="parse_failed",
        ) from None
    except Exception as e:
        raise ResolveError("Rumble 解析失败", f"访问 Rumble 接口失败：{e}", category="parse_failed") from None

    try:
        data = _json.loads(body)
    except Exception:
        raise ResolveError("Rumble 解析失败", "Rumble 接口返回非 JSON 数据（可能被反爬拦截）。",
                           category="parse_failed") from None

    # 无效 id / 无会话时 embedJS 返回的 sys.msg 提示 + 随机推荐内容
    # （实测同一 URL 多次调用返回不同视频——必须拦截，避免误导用户）
    _sys_msg = ((data.get("sys") or {}).get("msg") or "").strip()
    if _sys_msg:
        raise ResolveError(
            "Rumble 解析失败",
            f"Rumble 返回：{_sys_msg}。建议：①稍后重试；"
            f"②在「高级选项」设置海外代理（住宅 IP）后重试。",
            category="parse_failed",
        )

    title = data.get("title") or "Rumble 视频"
    # 流选择：优先 mp4 直链（sp.rmbl.ws，完整文件可直接下载/播放），
    # 无 mp4 时退回 hls 清单。
    stream_url = ""
    ext = "mp4"
    is_hls = False
    ua = data.get("ua") or {}
    mp4s = ua.get("mp4") or {}
    if isinstance(mp4s, dict) and mp4s:
        # 按清晰度降序取最高清直链
        for h in sorted((k for k in mp4s.keys() if isinstance(k, (str, int))), key=lambda x: int(x), reverse=True):
            v = mp4s[h]
            if isinstance(v, dict) and v.get("url"):
                stream_url = v["url"]
                break
    if not stream_url:
        hls = ua.get("hls") or {}
        if isinstance(hls, dict):
            for h, v in hls.items():
                if isinstance(v, dict) and v.get("url"):
                    stream_url = v["url"]
                    ext = "m3u8"
                    is_hls = True
                    break
    if not stream_url:
        raise ResolveError("Rumble 解析失败", "未从 Rumble 接口获取到可用视频流。",
                           category="parse_failed")

    return {
        "id": vid,
        "title": title,
        "duration": data.get("duration"),
        "thumbnail": (data.get("i") or ""),
        "webpage_url": url,
        "extractor_key": "Rumble",
        "extractor": "rumble",
        "ext": ext,
        "url": stream_url,
        "direct": not is_hls,
        "protocol": "m3u8_native" if is_hls else "https",
        "http_headers": {"User-Agent": _RUMBLE_UA, "Referer": "https://rumble.com/"},
    }


# Tubi（tubitv.com）：免费 AVOD（广告支持），内容无 DRM。yt-dlp TubiTvIE 在
# 数据中心 IP 偶发失败（页面 window.__data 提取不到，可能是反爬页或 GDPR 页）。
# 专用解析：抓视频页 → 提取 window.__data 的 video_resources（dash/hlsv3/hlsv6
# manifest，无 DRM）→ 优先 HLS m3u8（适配 VDL 直链模式），失败时带页面特征诊断。
_TUBI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _tubi_info(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    import json as _json

    m = re.search(r"tubitv\.com/(?:[a-z]{2}-[a-z]{2}/)?(video|movies|tv-shows)/(\d+)", url)
    if not m:
        raise ResolveError("Tubi 解析失败", "无法识别 Tubi 视频 ID。", category="parse_failed")
    vtype, vid = m.group(1), m.group(2)
    page_url = f"https://tubitv.com/{vtype}/{vid}/"
    headers = {
        "User-Agent": _TUBI_UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        # 支持代理（Tubi 需美/加/澳/新/英/墨等住宅 IP；数据中心 IP 返回反爬壳页）
        if proxy:
            _handler = urllib.request.ProxyHandler({
                "http": proxy if proxy.startswith("http") else f"http://{proxy}",
                "https": proxy if proxy.startswith("http") else f"http://{proxy}",
            })
            _opener = urllib.request.build_opener(_handler)
            req = urllib.request.Request(page_url, headers=headers)
            html = _opener.open(req, timeout=30).read().decode("utf-8", "replace")
        else:
            req = urllib.request.Request(page_url, headers=headers)
            html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise ResolveError(
            "Tubi 解析失败",
            f"Tubi 页面返回 {e.code}（地区限制或反爬）。建议：①稍后重试；"
            f"②在「高级选项」设置美国/加拿大等 Tubi 支持地区的代理。",
            category="parse_failed",
        ) from None
    except Exception as e:
        raise ResolveError("Tubi 解析失败", f"访问 Tubi 页面失败：{e}", category="parse_failed") from None

    # 诊断特征（页面被反爬/GDPR 时 hint 会带出）
    _feat = []
    if "window.__data" not in html:
        _feat.append("no-__data")
    low = html.lower()
    for _k in ("gdpr", "captcha", "robot", "access denied", "just a moment"):
        if _k in low:
            _feat.append(_k)

    # 提取 window.__data 的 video_resources（dash/hlsv3/hlsv6 manifest，均无 DRM）
    dash_url = hls_url = ""
    _m2 = re.search(r"window\.__data\s*=\s*(\{)", html)
    if _m2:
        _start = _m2.start(1)
        _depth = 0
        _i = _start
        while _i < len(html):
            if html[_i] == "{":
                _depth += 1
            elif html[_i] == "}":
                _depth -= 1
                if _depth == 0:
                    break
            _i += 1
        if _i < len(html):
            _raw = html[_start:_i + 1]
            # 简化 js_to_json：key 补引号 + 单引号转双引号 + undefined→null
            try:
                _raw2 = _json.loads(_raw)
            except Exception:
                try:
                    _fixed = re.sub(r"([{,])\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*:", r'\1"\2":', _raw)
                    _fixed = re.sub(r":\s*'((?:[^'\\]|\\.)*)'", r':"\1"', _fixed)
                    _fixed = _fixed.replace("undefined", "null").replace("NaN", "null")
                    _raw2 = _json.loads(_fixed)
                except Exception:
                    _raw2 = None
            if isinstance(_raw2, dict):
                _vd = ((_raw2.get("video") or {}).get("byId") or {}).get(vid) or {}
                for _res in _vd.get("video_resources") or []:
                    _u = (_res.get("manifest") or {}).get("url") or ""
                    if not _u:
                        continue
                    if _u.startswith("//"):
                        _u = "https:" + _u
                    _t = (_res.get("type") or "").lower()
                    if _t == "dash" and not dash_url:
                        dash_url = _u
                    elif _t in ("hlsv3", "hlsv6") and not hls_url:
                        hls_url = _u
                _title = _vd.get("title") or ""
    if not dash_url and not hls_url:
        # 兜底：正则直接抓 manifest URL
        for _mm in re.finditer(r'"manifest"\s*:\s*\{[^}]*?"url"\s*:\s*"([^"]+)"[^}]*?"type"\s*:\s*"([a-z0-9]+)"', html):
            _u = _mm.group(1)
            if _u.startswith("//"):
                _u = "https:" + _u
            _t = _mm.group(2).lower()
            if _t == "dash" and not dash_url:
                dash_url = _u
            elif _t in ("hlsv3", "hlsv6") and not hls_url:
                hls_url = _u

    # 页面无 __data（反爬/GDPR 页）→ 尝试公开清单 API（oz/videos/{id}/manifest.*）。
    # 302 到 gdpr 页时 urllib 跟随后返回 HTML，不匹配 #EXTM3U/<MPD 即跳过。
    if not hls_url and not dash_url:
        for _api in (
            f"https://tubitv.com/oz/videos/{vid}/manifest.m3u8",
            f"https://tubitv.com/oz/videos/{vid}/manifest.mpd",
        ):
            try:
                _req = urllib.request.Request(_api, headers=headers)
                _resp = urllib.request.urlopen(_req, timeout=15)
                _body = _resp.read().decode("utf-8", "replace")
                _ct = (_resp.headers.get("Content-Type") or "").lower()
                if _body.lstrip().startswith("#EXTM3U"):
                    hls_url = _api
                    _feat.append("oz-api-hls")
                    break
                if "<MPD" in _body or "mpd" in _ct or _body.lstrip().startswith("<?xml"):
                    dash_url = _api
                    _feat.append("oz-api-mpd")
                    break
            except Exception:
                continue

    # 优先 HLS（m3u8，VDL 直链可播放可下载）；无 HLS 时 DASH 仅提示（MPD 需 DASH 下载器）
    stream_url = hls_url
    is_hls = bool(hls_url)
    if not stream_url:
        raise ResolveError(
            "Tubi 解析失败",
            f"未找到可用的视频流（页面特征: {', '.join(_feat) or '正常但无 manifest'}）。"
            f"当前网络出口（数据中心 IP）可能不在 Tubi 支持地区（美/加/澳/新/英/墨等）"
            f"或触发反爬。建议在「高级选项」设置 Tubi 支持地区的住宅代理后重试。",
            category="parse_failed",
        )

    return {
        "id": vid,
        "title": _title or "Tubi 视频",
        "duration": None,
        "webpage_url": url,
        "extractor_key": "TubiTv",
        "extractor": "tubitv",
        "ext": "m3u8" if is_hls else "mpd",
        "url": stream_url,
        "direct": True,
        "protocol": "m3u8_native" if is_hls else "https",
        "http_headers": {"User-Agent": _TUBI_UA, "Referer": "https://tubitv.com/"},
    }

# YouTube（youtube.com / youtu.be）：2025 起对数据中心 IP 强制 bot 检测
# （"Sign in to confirm you're not a bot"），所有 player_client 轮换无效，
# 必须带登录态 Cookie 或 PO Token 才能解析。实现自动降级：
#   方法一：无 Cookie + bgutil PO Token（Docker 集成，尽力而为）；
#   方法二：方法一被 bot 拦截 → 自动按序尝试 Cookie 源：
#          用户显式粘贴 > 环境变量 VDL_YOUTUBE_COOKIE > 本机缓存 > 公共池。
# 全部失败 → 抛 cookie_required 提示（粘贴一次即缓存复用，免每次手动）。
_YOUTUBE_HOSTS: tuple[str, ...] = ("youtube.com", "youtu.be", "youtube-nocookie.com")
_BOT_KEYWORDS: tuple[str, ...] = (
    "sign in to confirm", "not a bot", "please sign in",
    "login_required", "confirm you're not", "sign in to continue",
)


def _is_youtube_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in _YOUTUBE_HOSTS)


class _YouTubeBotBlocked(Exception):
    """yt-dlp 返回 bot 检测类错误，应触发 Cookie 降级。"""


def _youtube_cookie_candidates(user_cookie: str) -> list[tuple[str, str]]:
    """收集 YouTube Cookie 候选（去重，用户显式优先）。"""
    cands: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(src: str, ck: str) -> None:
        ck = (ck or "").strip()
        if ck and ck not in seen:
            seen.add(ck)
            cands.append((src, ck))

    if user_cookie:
        _add("user", user_cookie)
    _add("env", os.environ.get("VDL_YOUTUBE_COOKIE", ""))
    try:
        from cookie_cache import get_cached_cookie_header
        _add("cache", get_cached_cookie_header("youtube.com") or "")
    except Exception:
        pass
    try:
        from cookie_pool import get_cookie as _pool_get
        _add("pool", _pool_get("youtube.com") or "")
    except Exception:
        pass
    return cands


def _fetch_youtube_visitor_data(proxy: str = "") -> str:
    """无 Cookie 从 YouTube 首页 HTML 提取 visitorData（PO Token 免 Cookie 链路）。

    2026-08 实测：数据中心 IP 访问 www.youtube.com 首页返回 200，HTML 内含
    "VISITOR_DATA":"Cg..."（ytcfg），无需登录态。yt-dlp 的 GVS PO Token 必须
    绑定 visitor_data，之前因缺它而无法触发 bgutil server 生成 token——
    这里自动补上，即可走「PO Token 免 Cookie」路径。

    失败（被限流/网络）返回空串，调用方回退 Cookie 源。
    """
    import re as _re
    import requests as _requests
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = _requests.get(
            "https://www.youtube.com/", headers=headers, proxies=proxies, timeout=15
        )
        if r.status_code == 200:
            m = _re.search(r'"VISITOR_DATA":"([^"]+)"', r.text)
            if m:
                return m.group(1)
    except Exception as _e:  # noqa: BLE001
        logger.info("[youtube] 获取 visitor_data 失败（不影响 Cookie 兜底）: %s", str(_e)[:100])
    return ""


def _resolve_youtube(url: str, user_cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """YouTube 自动降级解析：方法一（免 Cookie + PO Token）→ 方法二（Cookie 源自动切换）。

    方法一：自动获取 visitor_data → 注入 yt-dlp（youtube:visitor_data + fetch_pot=always）
    → bgutil server 生成 PO Token → 免 Cookie 解析；
    方法一被 bot 拦截或不可用时，自动按序尝试 Cookie 源（user > env > cache > pool）。

    返回 yt-dlp info dict；全部失败抛 ResolveError（bot 拦截时 category=cookie_required）。
    """
    host = _host_of(url)
    effective_proxy = proxy or _resolve_proxy(host)
    # 方法一（免 Cookie）先自动拿 visitorData；拿不到则走纯 Cookie 链路
    visitor_data = _fetch_youtube_visitor_data(effective_proxy)
    if visitor_data:
        logger.info("[youtube] 已自动获取 visitor_data（%s…），启用 PO Token 免 Cookie 路径", visitor_data[:20])

    def _try(cookie_text: str, use_visitor: bool = True) -> dict[str, Any]:
        opts = _base_options(PROBE_RETRIES, host, cookie=cookie_text, proxy=proxy)
        opts["format"] = None
        # 免 Cookie 路径：注入 visitor_data + 强制 fetch PO Token（bgutil 自动生效）
        if use_visitor and not cookie_text and visitor_data:
            ya = opts.setdefault("extractor_args", {}).setdefault("youtube", {})
            ya["visitor_data"] = [visitor_data]
            ya["fetch_pot"] = ["always"]
        try:
            with _YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            return info or {}
        except (DownloadError, ExtractorError) as exc:
            low = str(exc).lower()
            if any(k in low for k in _BOT_KEYWORDS):
                raise _YouTubeBotBlocked(str(exc)[:300]) from exc
            # 非 bot 错误（format not available 等 SABR 问题）：extract_flat 降级一次
            try:
                opts2 = _base_options(PROBE_RETRIES, host, cookie=cookie_text, proxy=proxy)
                opts2["extract_flat"] = "in"
                with _YoutubeDL(opts2) as ydl2:
                    info2 = ydl2.extract_info(url, download=False)
                if info2:
                    return info2
            except Exception:
                pass
            raise

    # 方法一：无 Cookie（bgutil PO Token 尽力）
    try:
        info = _try(user_cookie or "")
        if info:
            return info
    except _YouTubeBotBlocked:
        logger.info("[youtube] %s 被 bot 检测拦截，自动切换 Cookie 源", url[:60])
    except (DownloadError, ExtractorError) as exc:
        # 非 bot 错误（视频不可用/链接失效等）→ 转 ResolveError 透传真实原因
        raise ResolveError("视频解析失败", _clean_message(str(exc))[:300]) from exc

    # 方法二：Cookie 源自动切换（user > env > cache > pool）
    for src, ck in _youtube_cookie_candidates(user_cookie):
        try:
            info = _try(ck)
            if info:
                logger.info("[youtube] bot 拦截后自动切换 Cookie 源=%s 解析成功", src)
                return info
        except _YouTubeBotBlocked:
            logger.info("[youtube] Cookie 源=%s 仍被 bot 拦截，换下一个", src)
            continue
        except (DownloadError, ExtractorError) as exc:
            low = str(exc).lower()
            if any(k in low for k in _BOT_KEYWORDS):
                continue
            logger.info("[youtube] Cookie 源=%s 报非 bot 错误（可能 Cookie 过期），换下一个: %s",
                        src, str(exc)[:120])
            continue

    raise ResolveError(
        "YouTube 需要登录 Cookie 才能解析",
        "YouTube 2025 起对服务器数据中心 IP 强制 bot 检测，需带登录态才能绕过。\n"
        "请在「高级选项 → Cookie」粘贴一次 YouTube 登录 Cookie（后端自动缓存，后续免粘贴）；"
        "或由管理员配置环境变量 VDL_YOUTUBE_COOKIE 全局生效。",
        category="cookie_required",
    )


# =========================================================================== #
# B 类平台专用提取器（平台列表已收录但 yt-dlp 无原生提取器）
#   netease（网易云 MV）/ tudou（土豆→优酷合并）/ weishi（微视）/
#   yy（YY 直播）/ hotstar（Disney+ Hotstar）/ kinopoisk（KinoPoisk）
# 说明：hotstar / kinopoisk 实际 yt-dlp 已有提取器，之前误报"暂未实现"是
# 测试 URL/域名不匹配所致，这里显式分发让它们走 yt-dlp 正常解析。
# =========================================================================== #

# 网易云 MV：yt-dlp 的 NetEaseIE 对 MV 在数据中心 IP 返回 404（风控），且无稳定
# 提取器。改走社区逆向的 weapi 加密（AES-128-ECB 两层，cryptography 库实现，
# 不新增依赖），直连 music.163.com 官方接口拿 MV 直链（多清晰度 brs）。
_NE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_NE_NONCE = b"0CoJUm6Qyw8W8jud"
_NE_SEC_KEY = b"WV9w9Bpu0rX0l3sN"


def _ne_aes_ecb(key: bytes, data: bytes) -> bytes:
    e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return e.update(data) + e.finalize()


def _ne_weapi(obj: dict) -> tuple[str, str]:
    """网易云 weapi 加密：明文 → AES( nonce, PKCS7 ) → AES( seckey, PKCS7 ) → base64。"""
    text = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    padder = PKCS7(128).padder()
    e1 = _ne_aes_ecb(_NE_NONCE, padder.update(text) + padder.finalize())
    padder2 = PKCS7(128).padder()
    e2 = _ne_aes_ecb(_NE_SEC_KEY, padder2.update(e1) + padder2.finalize())
    return base64.b64encode(e2).decode(), base64.b64encode(_NE_SEC_KEY).decode()


def _ne_http_get(url: str, proxy: str = "", headers: dict | None = None) -> str:
    h = {"User-Agent": _NE_UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update(headers)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    import requests as _req
    r = _req.get(url, headers=h, proxies=proxies, timeout=20)
    r.raise_for_status()
    return r.text


def _ne_info(url: str, proxy: str = "") -> dict[str, Any]:
    """网易云音乐 MV 解析（music.163.com/mv?id=xxx）。"""
    m = re.search(r"(?:/mv\?id=|/mv/|id=)(\d+)", url)
    if not m:
        raise ResolveError(
            "网易云 MV 解析失败", "无法从链接中识别 MV ID（应为 music.163.com/mv?id=数字）。",
            category="parse_failed",
        )
    mv_id = m.group(1)
    params, enc_sec = _ne_weapi({"id": mv_id, "csrf_token": ""})
    data = ("params=%s&encSecKey=%s" % (params, enc_sec)).encode()
    try:
        import requests as _req
        r = _req.post(
            "https://music.163.com/weapi/mv/detail/?csrf_token=",
            data=data,
            headers={
                "User-Agent": _NE_UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://music.163.com/",
            },
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=20,
        )
        j = r.json()
    except Exception as e:
        raise ResolveError(
            "网易云 MV 解析失败", f"请求网易云接口异常：{type(e).__name__} {e}", category="parse_failed"
        ) from None
    if j.get("code") not in (200, 0) or not j.get("data"):
        raise ResolveError(
            "网易云 MV 解析失败",
            "网易云返回空数据（可能是该 MV 已下架，或当前出口 IP 被风控限流）。",
            category="parse_failed",
        )
    d = j["data"]
    # brs 为多清晰度字典 {br: url}，优先选最高清晰度（br 数值越大越清晰）
    brs = d.get("brs") or {}
    best_url = d.get("url") or ""
    if not best_url and brs:
        try:
            _best_br = max(int(k) for k in brs.keys() if str(k).isdigit())
            best_url = brs[str(_best_br)] or brs.get(_best_br) or ""
        except Exception:
            best_url = list(brs.values())[0]
    if not best_url:
        raise ResolveError(
            "网易云 MV 解析失败", "未找到可用的 MV 视频流地址。", category="parse_failed"
        )
    return {
        "id": mv_id,
        "title": d.get("name") or "网易云 MV",
        "duration": (d.get("duration") or 0) / 1000.0 if d.get("duration") else None,
        "thumbnail": d.get("cover") or d.get("picUrl") or "",
        "artist": (d.get("artistName") or ""),
        "webpage_url": url,
        "extractor_key": "NetEaseMV",
        "extractor": "netease",
        "ext": "mp4",
        "direct": True,
        "url": best_url,
        "protocol": "https",
        "http_headers": {"User-Agent": _NE_UA, "Referer": "https://music.163.com/"},
    }


def _tudou_info(url: str, proxy: str = "") -> dict[str, Any]:
    """土豆：2016 年起已并入优酷、主站停止运营。

    若用户给出的是仍能解析的旧土豆视频链接，尝试 302 展开看是否跳转到 youku，
    是则转优酷通道复用其解析能力；否则明确告知已停运，不再误报"暂未实现解析器"。
    """
    try:
        expanded = _expand_generic_302(url, proxy=proxy, allowed_hosts=("youku.com", "v.youku.com"))
        if "youku.com" in expanded and expanded != url:
            logger.info("[tudou] 重定向到优酷，转优酷通道: %s", expanded)
            if expanded.endswith("youku.com"):
                expanded = expanded.replace("www.youku.com", "v.youku.com", 1)
            return _youku_info(expanded, "", "", proxy=proxy)
    except Exception as e:
        logger.info("[tudou] 展开失败: %s", str(e)[:120])
    raise ResolveError(
        "土豆视频已停止运营",
        "土豆网于 2016 年并入优酷，原站视频已迁移/下线。\n"
        "如果该视频在优酷仍有存档，请直接粘贴优酷链接（v.youku.com/...）到 VDL 解析。",
        category="service_discontinued",
    )


def _weishi_info(url: str, proxy: str = "") -> dict[str, Any]:
    """微视（weishi.qq.com）：腾讯系短视频，播放地址原本需 App 端签名（wskey/rticket）。

    解析策略（2026-08-24 接入 VPS worker）：
    1) 若配置了 VPS 解析节点（VDL_COOKIE_REFILL_TOKEN 等），优先走 worker：
       worker 先试 H5 公开接口 WSH5GetPlayPage 拿**无水印**直链（最快），失败则
       Playwright 真实浏览器开播放页抓 <video> 真实流兜底。
    2) 未配置 worker → best-effort 降级：抓取播放页 HTML 看是否有内嵌视频 JSON。
    3) worker 解析失败（接口404/链接失效/无流）→ 透传真实原因，不伪装。
    """
    # 优先走 VPS worker（能真实拿到流，含无水印直链）
    token = os.environ.get("VDL_COOKIE_REFILL_TOKEN") or os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    if token:
        try:
            data = _call_vps_worker("weishi", url)
            # worker 成功返回 dict（含 video_url / title / ext）
            return {
                "id": data.get("video_id", ""),
                "title": data.get("title") or "微视视频",
                "duration": data.get("duration"),
                "webpage_url": data.get("webpage_url") or url,
                "extractor_key": "WeiShi",
                "extractor": "weishi",
                "ext": data.get("ext") or "mp4",
                "direct": True,
                "url": data.get("video_url"),
                "protocol": "https",
                "thumbnail": data.get("thumbnail", ""),
                "http_headers": {"User-Agent": _NE_UA, "Referer": "https://h5.weishi.qq.com/"},
            }
        except ResolveError:
            raise  # 透传 worker 真实错误（链接失效/无流/不可达等）

    # 降级 best-effort：无 worker 时抓 HTML 内嵌 JSON（多为广告/失效场景）
    try:
        html = _ne_http_get(url, proxy=proxy, headers={"Referer": "https://weishi.qq.com/"})
        if "feedid" in url or "feed/" in url:
            for pat in (r'"videoUrl"\s*:\s*"([^"]+)"', r'"playUrl"\s*:\s*"([^"]+)"',
                        r'"video_url"\s*:\s*"([^"]+)"'):
                m = re.search(pat, html)
                if m:
                    vurl = m.group(1).replace("\\u0026", "&").replace("\\/", "/")
                    if vurl.startswith("//"):
                        vurl = "https:" + vurl
                    _ok_cdn = ("vweishi" in vurl or "weishi" in vurl or "gtimg.cn/qzone/weishi" in vurl
                               or vurl.endswith(".mp4"))
                    _bad_cdn = ("qzact" in vurl or "act/extern" in vurl or "gtimg.cn/qzact" in vurl)
                    if _ok_cdn and not _bad_cdn:
                        return {
                            "id": "", "title": "微视视频", "duration": None,
                            "webpage_url": url, "extractor_key": "WeiShi",
                            "extractor": "weishi", "ext": "mp4", "direct": True,
                            "url": vurl, "protocol": "https",
                            "http_headers": {"User-Agent": _NE_UA, "Referer": "https://weishi.qq.com/"},
                        }
    except Exception as e:
        logger.info("[weishi] 抓取失败: %s", str(e)[:120])
    raise ResolveError(
        "微视暂不支持解析",
        "微视的播放地址由手机 App 端签名生成，无公开网页直链接口，VDL 暂无法提取。\n"
        "① 若原视频也发在微信/QQ 内，可尝试用腾讯视频链接（v.qq.com）解析；\n"
        "② 页面源码里若有 .mp4 直链，可直接粘贴直链下载。\n"
        "③ 若已配置 VPS 解析节点但仍失败，多为链接已失效（微视已收缩运营，分享页会跳404）。",
        category="pending_extractor",
    )


def _finder_info(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """微信视频号（weixin.qq.com/sph/* → channels.weixin.qq.com/finder-preview/*）。

    视频号的播放地址由微信客户端/网页登录态签名生成，游客态（无微信会话 Cookie）
    打开分享链接只会看到「请在微信中打开」的壳，拿不到真实视频流。必须用带微信
    登录态的浏览器会话才能加载播放器并请求 CDN 流。

    解析策略（2026-08-24 接入 VPS worker + 用户自带 Cookie）：
    1) 必须走 VPS worker（finder_resolve）：worker 注入微信登录态 Cookie 后真实
       浏览器抓流。无 worker 配置 → 直接诚实报错（不伪装 best-effort，因为游客态
       真的拿不到）。
    2) 登录态来源优先级：
       a. 用户手动粘贴的微信 Cookie（cookie 参数，最高优先，随请求透传 worker）；
       b. 云端共享池 weixin.qq.com Cookie（其他用户/贡献者上报的兜底）；
       c. VPS 本地 /opt/vdl-worker/cookies/weixin.txt（worker 内部兜底）。
    3) worker 解析失败（Cookie 过期 / 链接失效）→ 透传真实原因。
    """
    token = os.environ.get("VDL_COOKIE_REFILL_TOKEN") or os.environ.get("VDL_COOKIE_SYNC_TOKEN", "")
    if token:
        # 用户没自带 Cookie 时，先试云端共享池兜底（weixin.qq.com 域）
        effective_cookie = (cookie or "").strip()
        if not effective_cookie:
            try:
                from cookie_pool import get_cookie as _pool_get
                effective_cookie = _pool_get("weixin.qq.com") or ""
            except Exception:
                effective_cookie = ""
        try:
            data = _call_vps_worker("finder", url, cookie=effective_cookie)
            return {
                "id": data.get("video_id", ""),
                "title": data.get("title") or "微信视频号视频",
                "duration": data.get("duration"),
                "webpage_url": data.get("webpage_url") or url,
                "extractor_key": "WeiXinFinder",
                "extractor": "finder",
                "ext": data.get("ext") or "mp4",
                "direct": True,
                "url": data.get("video_url"),
                "protocol": "https",
                "thumbnail": data.get("thumbnail", ""),
                "http_headers": {"User-Agent": _NE_UA, "Referer": "https://channels.weixin.qq.com/"},
            }
        except ResolveError as e:
            # 视频号登录态相关错误（无 Cookie / 需登录 / 登录态失效）统一归为
            # cookie_required，让前端亮「去粘贴 Cookie」按钮引导用户自带上。
            # ⚠️ 注意检查 hint（worker 的真实错误文本），而非 message（多为通用
            # 的"视频解析失败"，不含平台细节）。
            if getattr(e, "category", None) not in ("cookie_required", "cookie_invalid_or_expired"):
                _msg = str(e) + "\n" + str(getattr(e, "hint", "") or "")
                if any(k in _msg for k in ("微信登录态", "请粘贴", "登录态已失效", "请在微信中打开", "无登录态")):
                    e.category = "cookie_required"
            raise  # 透传 worker 真实错误（无登录态/链接失效/被风控等）

    # 未配置 worker：视频号游客态无论如何拿不到流，诚实告知
    raise ResolveError(
        "微信视频号暂不支持解析",
        "微信视频号的播放地址需微信登录态签名，无登录态时网页只返回"
        "「请在微信中打开」的壳，无法提取视频流。\n"
        "① 请在微信内打开该视频，确认可正常播放；\n"
        "② 本服务已支持经 VPS 解析节点解析视频号，但需先配置微信登录态 Cookie"
        "（在已登录微信的浏览器复制 channels.weixin.qq.com 的 Cookie 粘贴给运维即可）。",
        category="pending_extractor",
    )
def _yy_info(url: str, proxy: str = "") -> dict[str, Any]:
    """YY 直播（yy.com / h.yy.com）：直播流地址需 App 端签名，公开 web 接口拿不到直链。
    best-effort：尝试从播放页/公开 API 提取，失败明确告知。
    """
    try:
        # 公开房间信息接口（无需签名）有时能拿到 stream 相关字段，作为兜底探测
        m = re.search(r"(?:/([a-z0-9_-]+)\.html|uid=|roomId=(\d+)|sid=(\d+))", url)
        html = _ne_http_get(url, proxy=proxy, headers={"Referer": "https://www.yy.com/"})
        for pat in (r'"liveUrl"\s*:\s*"([^"]+)"', r'"streamUrl"\s*:\s*"([^"]+)"',
                    r'"hlsUrl"\s*:\s*"([^"]+)"', r'https?://[^"\'\s]+\.m3u8'):
            mm = re.search(pat, html)
            if mm:
                surl = mm.group(1) if mm.groups() else mm.group(0)
                if surl.startswith("//"):
                    surl = "https:" + surl
                return {
                    "id": "", "title": "YY 直播", "duration": None,
                    "webpage_url": url, "extractor_key": "YYLive",
                    "extractor": "yy", "ext": "mp4", "direct": False,
                    "protocol": "m3u8_native", "url": surl,
                    "http_headers": {"User-Agent": _NE_UA, "Referer": "https://www.yy.com/"},
                }
    except Exception as e:
        logger.info("[yy] 抓取失败: %s", str(e)[:120])
    raise ResolveError(
        "YY 直播暂不支持解析",
        "YY 直播的流地址由客户端签名生成，公开接口无法获取直链，VDL 暂无法提取。\n"
        "① 若是精彩回放且页面源码含 .m3u8 直链，可直接粘贴直链下载；\n"
        "② 更多平台陆续支持中。",
        category="pending_extractor",
    )


def _hotstar_info(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """Disney+ Hotstar：yt-dlp 已有 HotstarIE 提取器，显式分发让链接走 yt-dlp 正常解析。
    仅印度/印尼/马来西亚/泰国等支持地区 + 账号可看；数据中心 IP/非支持地区会被 geo 拦截。
    """
    # 直接复用 yt-dlp 通用解析流程（不在此重复实现，避免与 yt-dlp 维护脱节）
    # 这里仅作为一个清晰的入口，返回 None 触发下方 yt-dlp 兜底路径。
    # 真正执行在 probe() 末尾的 yt-dlp extract_info；此处不提前 return，
    # 交由调用方在 hotstar 分发处选择：调用 _yt_dlp_fallback。
    raise _NeedYtDlp()


class _NeedYtDlp(Exception):
    """标记该平台应交由 probe() 末尾的 yt-dlp 通用流程解析（已收录且 yt-dlp 支持）。"""


def _kinopoisk_info(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """KinoPoisk：yt-dlp 已有 KinoPoiskIE 提取器，显式分发走 yt-dlp 解析。
    俄区内容，部分需登录/地区；数据中心 IP 可能 geo 限制。
    """
    raise _NeedYtDlp()


def probe(url: str, cookie: str = "", proxy: str = "") -> dict[str, Any]:
    """只解析不下载，返回 yt-dlp 的原始 info dict。"""
    effective_proxy = proxy or _resolve_proxy(_host_of(url) or "")
    # 归一化前先做非视频页（空间/动态/番剧）可读拦截，避免 yt-dlp 提取失败后
    # 只给用户一句笼统的"视频解析失败"。与归一化入参解耦：此处只做链接形态判断。
    _norm_for_check = _normalize_share_url(url, proxy=effective_proxy)
    if "space.bilibili.com" in _norm_for_check or "bilibili.com/opus/" in _norm_for_check:
        raise ResolveError(
            "这是 B站 个人空间 / 动态页，不是单个视频",
            "请打开该视频，复制浏览器地址栏中以 bilibili.com/video/BVxxxx 开头的播放页链接，再粘贴解析。",
        )
    if "bangumi" in _norm_for_check:
        raise ResolveRestricted(
            "该链接是 B站 番剧 / 影视（会员 DRM 内容）",
            "番剧、电影、纪录片等受版权 DRM 保护，标准下载方式无法解析。请更换为公开可播放的普通视频链接。",
        )
    # 喜马拉雅专辑链接：走专辑功能（/api/playlist + worker），单集才走 yt-dlp。
    # 直接喂 probe 会被 yt-dlp XimalayaIE 报 KeyError('data')（专辑页非单集结构）。
    _probe_host = _host_of(url)
    if _probe_host in ("ximalaya.com",) and "/album/" in (url or "").split("?", 1)[0]:
        raise ResolveError(
            "这是喜马拉雅专辑链接",
            "专辑请使用「歌单 / 专辑下载」功能：粘贴链接后选择专辑模式即可整批下载；"
            "下载单个音频请打开具体集数的播放页链接。",
        )
    url = _norm_for_check
    # 优酷链接归一化：当前主流为 www.youku.com，而 yt-dlp 内置 YoukuIE 仅匹配
    # v/play/player.youku.com，否则会落 generic 提取 → Unsupported URL（pending_extractor）。
    # 归一化到 v.youku.com 让 YoukuIE 接管（共享池 Cookie 已注入 http_headers）。
    if "www.youku.com" in url:
        url = url.replace("www.youku.com", "v.youku.com", 1)
    # 优酷：yt-dlp 内置 YoukuIE 不生成 ckey 播放签名 → -3007。改走专用 UPS 通道
    # （带共享池 Cookie + ckey 直接拿 m3u8）。ckey 仅取公共池（用户 Copy as cURL 贡献）。
    if (_host_of(url).endswith("youku.com")):
        _yk_cookie = cookie
        _yk_ckey = ""
        if not _yk_cookie or True:  # 你库 ckey 一律走公共池（无本机引擎）
            try:
                from cookie_pool import get_cookie as _pool_get, get_ckey as _pool_ckey

                _yk_cookie = _yk_cookie or (_pool_get("youku.com") or "")
                _yk_ckey = _pool_ckey("youku.com") or ""
            except Exception:
                pass
        try:
            return _youku_info(url, _yk_cookie, _yk_ckey, proxy=effective_proxy)
        except ResolveError:
            raise
        except Exception as _e:
            raise ResolveError(
                "优酷解析失败", f"UPS 通道异常：{type(_e).__name__} {_e}", category="parse_failed"
            ) from None
    # 用户手动粘贴的 Cookie 持久化缓存：本次解析成功后写盘，
    # 后续同站点解析/下载自动复用，免去每次重粘。
    host = _host_of(url)
    if cookie and host:
        _cache_user_cookie(host, cookie)
    # 直播模块已下线（2026-08-22 用户要求）：斗鱼 / 映客 / 网易CC 统一拦截
    if host and any(_lb in host for _lb in ("douyu.com", "inke.cn", "cc.163.com")):
        raise ResolveError(
            "该直播平台已暂时下线",
            "直播模块（斗鱼 / 映客 / 网易CC）已于 2026-08-22 下线，恢复时间待定。"
            "可关注后续更新，或使用其他平台。",
        )
    # 抖音/快手/微博/爱奇艺：yt-dlp 提取器失效或分享页 JS-only，走 VPS Playwright 真实浏览器解析
    if _is_douyin_host(host):
        return _douyin_info(url)
    # DAZN：认证墙 + Widevine DRM + 地区封锁（2026-08-22 深度评估），明确提示
    if "dazn.com" in (host or ""):
        raise ResolveError(
            "DAZN 暂不支持解析",
            "DAZN 为付费体育流媒体：①内容受 Widevine DRM 保护（合规不破解）；"
            "②需 DAZN 支持地区的住宅网络与账号（中国 IP / 数据中心 IP 均被 403 地区封锁）。"
            "免费内容同样走认证+DRM 管线。建议使用已支持的平台或 DAZN 官方离线功能。",
        )
    # Rumble：Cloudflare 反爬 403 拦 yt-dlp，走专用浏览器头接口（embedJS/u3）
    if "rumble.com" in (host or ""):
        return _rumble_info(url, cookie)
    # Tubi：免费 AVOD 无 DRM，yt-dlp 提取器在数据中心 IP 偶发失败，专用页面解析
    if "tubitv.com" in (host or ""):
        return _tubi_info(url, cookie, proxy)
    if _is_kuaishou_host(host):
        return _kuaishou_info(url)
    # 斗鱼：yt-dlp DouyuTVIE 旧正则失效（room_id 格式已改）+ DouyuShowIE 依赖
    # 过时 PhantomJS，走 VPS Playwright 真实浏览器监听流请求
    if "douyu.com" in (host or ""):
        return _douyu_info(url)
    # 央视频：播放地址需 playvinfo JSONP 签名（cKey），走 VPS Playwright 解析
    if "yangshipin.cn" in (host or ""):
        return _yangshipin_info(url)
    # 1905 电影网：详情页反爬（数据中心 IP 403），走 VPS Playwright 解析
    if "1905.com" in (host or ""):
        return _m1905_info(url)
    # 风行网：播放地址需 pm.funshion.com 接口签名，走 VPS Playwright 解析
    if "fun.tv" in (host or ""):
        return _funshion_info(url)
    # 百视TV：web 端播放地址需 wasm 签名（makepreviewquery），走 VPS Playwright 解析
    if "bestv.com.cn" in (host or ""):
        return _bestv_info(url)
    # 红果短剧：字节系 CDN 流需真实浏览器点开播放器捕获，走 VPS Playwright 解析
    if ("hongguoduanju.com" in (host or "")) or ("novelquickapp.com" in (host or "")):
        return _hongguo_info(url)
    # 映客直播/回放：公开接口拿昵称+状态，流地址规律化构造，走 VPS worker
    if "inke.cn" in (host or ""):
        return _inke_info(url)
    # 网易CC直播：vapi 公开接口拿签名 FLV 直链，走 VPS worker
    if "cc.163.com" in (host or ""):
        return _cc_info(url)
    if _is_weibo_host(host):
        return _weibo_info(url)
    if _is_iqiyi_host(host):
        info = _iqiyi_info(url, cookie=cookie)
        if info is not None:
            return info
        # worker 未配置（本地桌面）且非分享页 → 回退 yt-dlp 通用流程
    # —— B 类平台专用提取器分发（平台已收录但 yt-dlp 无原生提取器）——
    # 网易云 MV：走自研 weapi 加密接口（yt-dlp 数据中心 IP 风控 404）
    if "music.163.com" in (host or ""):
        return _ne_info(url, proxy=proxy)
    # 土豆：已并入优酷，尝试重定向或明确停运提示
    if "tudou.com" in (host or ""):
        return _tudou_info(url, proxy=proxy)
    # 微视 / YY 直播：腾讯/YY 签名流，best-effort + 清晰提示
    if "weishi.qq.com" in (host or ""):
        return _weishi_info(url, proxy=proxy)
    # 微信视频号：weixin.qq.com/sph/* 短链 或 channels.weixin.qq.com 直链
    if ("weixin.qq.com" in (host or "")) or ("channels.weixin.qq.com" in (host or "")):
        return _finder_info(url, cookie=cookie, proxy=proxy)
    if "yy.com" in (host or ""):
        return _yy_info(url, proxy=proxy)
    # Disney+ Hotstar / KinoPoisk：yt-dlp 已有提取器，显式放行走末尾通用解析，
    # 不再误报"暂未实现该站解析器"（地区/登录限制由 yt-dlp 返回明确错误）。
    if "hotstar.com" in (host or "") or "kinopoisk" in (host or ""):
        pass  # 落入下方 yt-dlp 通用流程
    # YouTube：自动降级（方法一免 Cookie + PO Token → 方法二 Cookie 源自动切换）
    if _is_youtube_host(host):
        return _resolve_youtube(url, user_cookie=cookie, proxy=proxy)
    # YouTube 诊断日志（临时，定位代理/Cookie 问题后可移除）
    _debug_log = os.path.join(os.environ.get("TMPDIR", "/tmp"), "vdl_probe_debug.log")
    try:
        with open(_debug_log, "a") as _f:
            _f.write(f"[{__import__('datetime').datetime.now().isoformat()}] URL={url[:80]} host={host}\n")
            _f.write(f"  proxy={proxy or '(auto)'} cookie={'yes' if cookie else 'no'}\n")
            _effective = proxy or _resolve_proxy(host)
            _f.write(f"  effective_proxy={_effective or '(none)'}\n")
            _sys_p = _macos_system_proxy()
            _f.write(f"  macos_system_proxy={_sys_p or '(none)'}\n")
    except Exception:
        pass
    direct = _looks_like_direct_file(url)
    if direct:
        # 本身就是完整媒体文件，跳过 yt-dlp，直接交给前端从源站下载（不走服务器）
        filename = Path(urlparse(url).path).name or "video.mp4"
        return {
            "direct": True,
            "url": url,
            "title": filename,
            "ext": (Path(filename).suffix or ".mp4").lstrip("."),
            "webpage_url": url,
        }
    # info 可能因 yt-dlp 抛异常（网络重试失败等）从未被赋值，必须初始化，
    # 否则下方 `if not info:` 会抛 UnboundLocalError（搜狐等平台实测触发）
    info: dict[str, Any] | None = None
    # 记录最后一次异常信息，用于 info 为空时透传真实原因
    _last_err: str | None = None
    # 收集 yt-dlp logger 的 WARNING/ERROR 输出，供 `if not info:` 时透传真实业务原因。
    # handler 的添加/移除严格包在内层 try/finally（紧贴 extract_info 调用），
    # 确保任何路径（包括 raise）都清理，不污染后续请求的 logger。
    import logging as _logging

    class _YdlLogCapture(_logging.Handler):
        def emit(self, record):  # noqa: ANN401
            if record.levelno >= _logging.WARNING:
                _ydlp_logs.append(self.format(record))

    _ydlp_logs: list[str] = []

    try:
        opts = _base_options(PROBE_RETRIES, _host_of(url), cookie=cookie, proxy=proxy)
        # B站 经国内代理回源时，yt-dlp 原生 urllib 读取页面偶发 IncompleteRead；
        # 用 requests 预下载视频页 HTML 并注入 extractor，提高连接稳定性。
        _h = _host_of(url)
        if _h and ("bilibili.com" in _h or "b23.tv" in _h):
            # patch 必须带最终生效的 Cookie（含公共池自动注入），而不是用户原始输入
            _patch_bilibili_webpage_download(
                proxy=effective_proxy,
                cookie=(opts.get("http_headers") or {}).get("Cookie", cookie),
                ua=(opts.get("http_headers") or {}).get("User-Agent"),
            )
        # 解析阶段只拿 info dict，不做格式选择（避免 YouTube 等站因格式不匹配
        # 直接抛 "Requested format is not available"）。下载阶段再由 _format_selector 选格式。
        opts["format"] = None
        # ignoreerrors：仅 YouTube 通过代理时格式列表可能不完整，需要跳过格式错误
        # 让 extract_info 尽量返回能拿到的信息（标题/时长/缩略图等）。
        # ⚠️ 绝不能对所有站点生效！国内站（快手/抖音等）提取失败时，
        # ignoreerrors 会吞掉 ExtractorError/DownloadError 导致 extract_info 返回 None，
        # 真正的错误原因（页面结构变更/需要登录等）被完全丢失。
        # ⚠️ 也不对 YouTube 生效！"only_download" 在 2026.07 yt-dlp 下对
        # "This video is unavailable"/"Sign in to confirm you're not a bot" 这类业务
        # 错误既不抛 DownloadError 也不 logger.error，导致前端「未获取到视频信息」
        # 误导。让 yt-dlp 真实抛 DownloadError，由下方 except DownloadError 分支
        # 统一捕获 + extract_flat/tv_embedded 降级。
        with _YoutubeDL(opts) as ydl:
            # 诊断：记录 yt-dlp 运行时真实配置（proxy/handlers/每 handler proxies）
            try:
                _rd = ydl._request_director
                with open(_debug_log, "a") as _f:
                    _f.write(
                        f"  ytdlp_runtime proxy={ydl.params.get('proxy')!r} "
                        f"handlers={list(_rd.handlers.keys())} "
                        f"hproxies={{k: str(getattr(h,'proxies','N/A')) for k, h in _rd.handlers.items()}}\n"
                    )
            except Exception as _dge:
                try:
                    with open(_debug_log, "a") as _f:
                        _f.write(f"  ytdlp_runtime diag_err={str(_dge)[:120]}\n")
                except Exception:
                    pass
            # yt-dlp logger 捕获（业务错误透传兜底）。内层 try/finally 确保任何路径都清理
            import logging as _logging
            _capture = _YdlLogCapture()
            _capture.setFormatter(_logging.Formatter("%(levelname)s %(name)s: %(message)s"))
            _ydlp_logger = _logging.getLogger("yt_dlp")
            _old_level = _ydlp_logger.level
            _ydlp_logger.addHandler(_capture)
            _ydlp_logger.setLevel(_logging.WARNING)
            try:
                info = ydl.extract_info(url, download=False)
            finally:
                _ydlp_logger.removeHandler(_capture)
                _ydlp_logger.setLevel(_old_level)
        # 诊断：记录 extract_info 返回值
        try:
            _info_keys = list(info.keys()) if info else ["(None)"]
            _info_title = (info or {}).get("title", "(no title)")
            _fmt_count = len((info or {}).get("formats") or [])
            with open(_debug_log, "a") as _f:
                _f.write(f"  extract_info OK: title={str(_info_title)[:60]} formats={_fmt_count} keys={_info_keys[:15]}\n")
        except Exception:
            pass
    except (UnsupportedError, GeoRestrictedError) as exc:
        raise _friendly_error(exc, _build_diag_context(url, cookie=cookie, proxy=proxy, options=opts)) from exc
    except (DownloadError, ExtractorError) as exc:
        # yt-dlp 2026+ 把 \"Unsupported URL\" 包成 ExtractorError 抛出（而非直接的
        # UnsupportedError 实例），必须在此短路到 unsupported_platform 友好提示，
        # 否则会被 2248 `if not info:` 当作未知失败处理。
        if "unsupported url" in str(exc).lower():
            raise _friendly_error(exc, _build_diag_context(url, cookie=cookie, proxy=proxy, options=opts)) from exc
        _last_err = f"{type(exc).__name__}: {str(exc)[:200]}"
        # 网络类错误（隧道重连窗口/链路抖动）：国内站经反向隧道回源时，本机隧道
        # 端（住宅 IP→Cloudflare）偶发断线，client 5s 后重连。撞上窗口会报
        # "Remote end closed / 1010 / connection" 类错误，等 3s 重试一次可显著
        # 提高成功率（重试用的是全新 _base_options + _YoutubeDL，无状态残留）。
        _exc_low = str(exc).lower()
        _net_like = any(
            w in _exc_low
            for w in ("remote end closed", "connection", "timed out", "1010",
                      "transport", "incompleteread", "reset", "closed")
        )
        if _net_like and _h and is_china_host(_h):
            logger.info("[probe] 网络类错误(%s)，3s 后重试一次", _last_err[:100])
            try:
                import time as _t
                _t.sleep(3)
                _opts2 = _base_options(PROBE_RETRIES, _h, cookie=cookie, proxy=proxy)
                _opts2["format"] = None
                with _YoutubeDL(_opts2) as _ydl2:
                    info = _ydl2.extract_info(url, download=False)
                _last_err = None
                logger.info("[probe] 隧道重试成功")
            except Exception as _re2:
                logger.warning("[probe] 隧道重试仍失败: %s", str(_re2)[:150])
        # B站 走 API 兜底：网络错误（IncompleteRead/连接重置）OR 页面风控（412/403）。
        # 2026-08 B站 风控升级：对 urllib3/requests 的非浏览器 TLS 栈访问视频页返回
        # 412 验证页（curl 不受影响），yt-dlp 抓页面必 412 → 提取器报 403。
        # API（api.bilibili.com）不过页面风控，仍 200，因此直接走 API 构造 info。
        _exc_low = str(exc).lower()
        if _h and ("bilibili.com" in _h or "b23.tv" in _h) and (
            "incompleteread" in _exc_low
            or "error reading response" in _exc_low
            or "connection" in _exc_low
            or "transport" in _exc_low
            or "412" in _exc_low
            or "403" in _exc_low
            or "forbidden" in _exc_low
            or "precondition" in _exc_low
        ):
            logger.info("[probe] yt-dlp Bilibili failed (%s), trying API fallback", _last_err)
            try:
                # 用最终生效的 Cookie（含公共池自动注入），而不是用户原始输入
                _final_cookie = (opts.get("http_headers") or {}).get("Cookie") or cookie
                info = _bilibili_api_extract(url, proxy=effective_proxy, cookie=_final_cookie)
                if info:
                    logger.info("[probe] Bilibili API fallback succeeded")
                    _last_err = None
                else:
                    logger.warning("[probe] Bilibili API fallback returned no info")
            except Exception as fb_err:
                logger.warning("[probe] Bilibili API fallback failed: %s", str(fb_err)[:200])

        if _last_err:
            # YouTube 等站格式选择失败时，降级用 extract_flat 重试（只拿元数据，不含格式列表）
            if "format" in str(exc).lower() or "not available" in str(exc).lower():
                try:
                    opts2 = _base_options(PROBE_RETRIES, _host_of(url), cookie=cookie, proxy=proxy)
                    opts2["extract_flat"] = "in"
                    if "youtube.com" in (_host_of(url) or "") or "youtu.be" in (_host_of(url) or ""):
                        opts2.setdefault("extractor_args", {}).setdefault("youtube", {})["player_client"] = ["tv_embedded"]
                    with _YoutubeDL(opts2) as ydl2:
                        info = ydl2.extract_info(url, download=False)
                        _last_err = None  # 降级成功
                except Exception as fb_err:
                    _last_err = f"{_last_err}; 降级: {type(fb_err).__name__}: {str(fb_err)[:150]}"
                    raise ResolveError(
                        "视频解析失败",
                        f"建议：①检查代理是否通畅；②在「高级选项」粘贴 Cookie；"
                        f"③更换代理节点。\n详情：{_last_err}"
                    ) from exc
            else:
                # 临时诊断：B站 403/1010 时输出实际生效的 proxy/host/Referer
                _probe_host = _host_of(url) or ""
                _diag_note = (
                    f"\n[diag] host={_probe_host!r} is_china={is_china_host(_probe_host)} "
                    f"opts_proxy={opts.get('proxy')!r} effective_proxy={effective_proxy!r} "
                    f"referer={(opts.get('http_headers') or {}).get('Referer')!r}"
                )
                raise ResolveError(
                    "视频解析失败",
                    _clean_message(str(exc))[:200] + _diag_note,
                ) from exc
    except OSError as exc:  # 网络/DNS 层面的错误
        _last_err = f"{type(exc).__name__}: {_clean_message(str(exc))[:200]}"
        raise ResolveError("网络请求失败", _clean_message(str(exc))) from exc
    except Exception as exc:
        # 兜底：捕获任何未预期异常，保留完整错误用于诊断
        _last_err = f"{type(exc).__name__}: {str(exc)[:300]}"

    # 注意：yt-dlp logger handler 的清理已在 extract_info 调用的内层 finally 完成
    # （确保任何 raise 路径都清理，不污染后续请求）

    if not info:
        detail = "请稍后重试或更换链接"
        # 透传诊断信息：如果 extract_info 静默返回空（未抛异常），补充上下文
        if _last_err:
            diag = _last_err
        elif _ydlp_logs:
            # 优先取 ERROR 行（业务原因如"video is unavailable"），其次 WARNING
            errs = [l for l in _ydlp_logs if l.startswith("ERROR")]
            diag = "\n".join(errs[:5]) if errs else "\n".join(_ydlp_logs[:5])
        else:
            diag = (
                "extract_info 返回空结果（无异常）。"
                "常见原因：①代理 MITM 导致 SSL 握手失败但被 ignoreerrors 吞掉；"
                "②站点返回空页面；③需要登录 Cookie。建议在「高级选项」粘贴 Cookie 后重试。"
            )
        detail += f"\n\n诊断信息：{diag}"
        raise ResolveError("未获取到视频信息", detail)
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ResolveError("该链接是一个空的合集", "请粘贴单个视频的播放地址")
        info = entries[0]
    # 多分片视频（如搜狐 SohuIE 返回 _type=multi_video，一个视频切成 N 个 mp4 分片）：
    # 取第一个分片继续（分片间可无缝拼接，youtube-dl 也这样处理）。
    if info.get("_type") == "multi_video":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ResolveError("该视频无法解析出分片", "请更换链接或稍后重试")
        info = entries[0]
    if info.get("is_live"):
        raise ResolveError("暂不支持下载正在直播的内容", "请等直播结束生成回放后再试")
    if _is_restricted_placeholder(info):
        raise ResolveRestricted(
            "该视频疑似会员 / 付费受限，本工具暂不支持",
            "腾讯等平台的会员专享、付费或地区限制内容需官方客户端或登录态才能获取，"
            "标准下载方式无法解析。请更换为公开可播放的视频链接",
        )
    # 优酷：yt-dlp 的 YoukuIE 只返回单集标题，从网页 <title> 补全整部剧名
    if info and _host_of(url) in ("youku.com", "tudou.com"):
        info = _enrich_youku_series(info, effective_proxy)
    return info


# --------------------------------------------------------------------------- #
# 清晰度选项
# --------------------------------------------------------------------------- #

def _best_audio_size(formats: list[dict]) -> int:
    sizes = [
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    return max(sizes, default=0)


def _video_size_at(formats: list[dict], height: int) -> int:
    sizes = [
        f.get("filesize") or f.get("filesize_approx") or 0
        for f in formats
        if f.get("height") == height and f.get("vcodec") not in (None, "none")
    ]
    return max(sizes, default=0)


def build_quality_options(info: dict[str, Any]) -> list[dict[str, Any]]:
    """把 yt-dlp 冗长的 format 列表压缩成几个用户看得懂的选项。"""
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    heights = sorted({f["height"] for f in formats if f.get("height")}, reverse=True)
    audio_size = _best_audio_size(formats)

    # 抖音（VPS 解析）只有单一视频档位，避免展示 480P/360P 等不存在的选项导致下载失败
    if info.get("extractor_key") == "Douyin":
        return [
            {"key": BEST_KEY, "label": "最佳画质（自动）", "note": "视频+音频自动合并", "approx_size": 0}
        ]

    # 纯音频内容（喜马拉雅/网易云等无视频轨）：不展示 4K/1080P 等视频画质选项
    if not heights:
        audio_ex = {str(f.get("ext") or "").lower() for f in formats}
        audio_opts: list[dict[str, Any]] = []
        if "mp3" in audio_ex:
            audio_opts.append({"key": AUDIO_KEY, "label": "仅音频 MP3", "note": "MP3 格式", "approx_size": audio_size})
        if "m4a" in audio_ex:
            audio_opts.append({"key": M4A_KEY, "label": "仅音频 M4A", "note": "M4A 格式", "approx_size": audio_size})
        return [
            {"key": BEST_KEY, "label": "最佳音质（自动）", "note": "自动选择最高音质", "approx_size": audio_size},
        ] + audio_opts

    options: list[dict[str, Any]] = [
        {"key": BEST_KEY, "label": "最佳画质（自动）", "note": "视频+音频自动合并", "approx_size": 0}
    ]
    max_height = heights[0] if heights else 0
    for height, label in QUALITY_PRESETS:
        if max_height and height > max_height:
            continue
        video_size = _video_size_at(formats, height)
        options.append(
            {
                "key": str(height),
                "label": label,
                "note": "MP4",
                "approx_size": (video_size + audio_size) if video_size else 0,
            }
        )
    options.append(
        {"key": AUDIO_KEY, "label": "仅音频 MP3", "note": "提取音轨", "approx_size": audio_size}
    )
    options.append(
        {"key": WEBM_KEY, "label": "WebM 格式", "note": "体积小·适合网页嵌入", "approx_size": 0}
    )
    options.append(
        {"key": M4A_KEY, "label": "仅音频 M4A", "note": "无损音轨", "approx_size": audio_size}
    )
    return options


def _format_selector(quality_key: str) -> str:
    if quality_key == BEST_KEY:
        # 优先 H.264(avc1) —— macOS WKWebView / Safari 不支持 AV1 和 VP9 解码，
        # 选了会导致「下载成功但播不了」。[ext=mp4] 不够（YouTube AV1 也是 .mp4），
        # 必须用 [vcodec^=avc1] 锁编码。降级链：H.264≤1080p → H.264任意 → mp4容器 → 兜底
        return (
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/"
            "bestvideo[vcodec^=avc1][height<=1080]+bestaudio/"
            "bestvideo[vcodec^=avc1]+bestaudio/"
            "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio/bestvideo+bestaudio/bv*+ba/b"
        )
    if quality_key in (AUDIO_KEY, M4A_KEY):
        return "ba/b"
    if quality_key == WEBM_KEY:
        return "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
    height = int(quality_key)
    return (
        f"bv*[vcodec^=avc1][height<={height}]+ba[ext=m4a]/"
        f"bv*[vcodec^=avc1][height<={height}]+ba/"
        f"bv*[height<={height}][ext=mp4]+ba[ext=m4a]/"
        f"bv*[height<={height}]+ba/b[height<={height}]/b[height<={height}]"
    )


def is_valid_quality(quality_key: str) -> bool:
    return quality_key in (BEST_KEY, AUDIO_KEY, WEBM_KEY, M4A_KEY) or quality_key in {
        str(h) for h, _ in QUALITY_PRESETS
    }


def quality_label(quality_key: str) -> str:
    if quality_key == BEST_KEY:
        return "最佳画质（自动）"
    if quality_key == AUDIO_KEY:
        return "仅音频 MP3"
    if quality_key == WEBM_KEY:
        return "WebM 格式"
    if quality_key == M4A_KEY:
        return "仅音频 M4A"
    return dict(QUALITY_PRESETS).get(int(quality_key), f"{quality_key}P")


# --------------------------------------------------------------------------- #
# 下载执行
# --------------------------------------------------------------------------- #

class _ProgressReporter:
    """把 yt-dlp 的分流进度聚合成单调递增的整体百分比。"""

    def __init__(self, task: DownloadTask, store: TaskStore) -> None:
        self._task = task
        self._store = store
        self._streams: dict[str, tuple[int, int]] = {}
        self._last_progress = 0.0
        self._has_download_step = False

    def __call__(self, payload: dict[str, Any]) -> None:
        if self._task.cancel_requested:
            raise DownloadCanceled()
        if getattr(self._task, "pause_requested", False):
            raise DownloadPaused()
        if payload.get("status") != "downloading":
            return
        self._ensure_title(payload)
        if not self._has_download_step:
            self._task.add_step("下载音视频", "running", f"已选清晰度：{self._task.quality}")
            self._task.log(f"开始下载：{self._task.quality}")
            self._has_download_step = True
        key = payload.get("filename") or payload.get("tmpfilename") or "stream"
        downloaded = int(payload.get("downloaded_bytes") or 0)
        total = int(payload.get("total_bytes") or payload.get("total_bytes_estimate") or 0)
        self._streams[key] = (downloaded, max(total, downloaded))
        self._push(payload)

    def _ensure_title(self, payload: dict[str, Any]) -> None:
        """首个进度回调就把标题回填，避免任务卡片长时间显示"解析中"。"""
        if self._task.title:
            return
        title = (payload.get("info_dict") or {}).get("title")
        if title:
            self._store.update(self._task.id, title=title)
            self._task.add_step("解析视频信息", "done", f"已获取标题《{title}》")

    def _push(self, payload: dict[str, Any]) -> None:
        done = sum(d for d, _ in self._streams.values())
        total = sum(t for _, t in self._streams.values())
        percent = (done / total * 100) if total else 0.0
        self._last_progress = min(max(self._last_progress, percent), DOWNLOAD_PHASE_CEILING)
        self._store.update(
            self._task.id,
            status="downloading",
            progress=self._last_progress,
            downloaded_bytes=done,
            total_bytes=total,
            speed=float(payload.get("speed") or 0.0),
            eta=int(payload.get("eta") or 0),
        )

    def on_postprocess(self, payload: dict[str, Any]) -> None:
        if payload.get("status") == "started":
            self._task.add_step("下载音视频", "done", "音视频下载完成")
            self._task.add_step("合并与后处理", "running", "正在合并音视频…")
            self._store.update(self._task.id, status="merging", progress=98.0)


def _download_options(task: DownloadTask, quality_key: str, reporter: _ProgressReporter, *, cookie: str = "", proxy: str = "", format_id: str = "", concurrent_fragments: int = 0, downloader_type: str = "", resume: bool = False) -> dict:
    options = _base_options(DOWNLOAD_RETRIES, _host_of(task.url), cookie=cookie, proxy=proxy) | {
        "format": _format_selector(quality_key),
        "outtmpl": {"default": f"%(title).{MAX_TITLE_CHARS}s.%(ext)s"},
        "paths": {"home": str(task.workdir)},
        "windowsfilenames": True,
        "concurrent_fragment_downloads": _clamp_concurrency(concurrent_fragments),
        "progress_hooks": [reporter],
        "postprocessor_hooks": [reporter.on_postprocess],
        "overwrites": True,
        # HLS 流优先走 Python 原生下载器，下载阶段不用 ffmpeg（沙盒偶发 SIGXCPU 152 强杀）
        # 仅保留最后的 TS→mp4 remux 调用 ffmpeg（快、低风险）
        "hls_prefer_native": True,
    }
    # 断点续传：保留 .part 分片的前提下，显式开启 continue 让 yt-dlp 从上次中断处接上。
    # aria2c 分支已在 _build_aria2c_args 内置 --continue=true；此处覆盖原生下载器场景。
    if resume:
        options["continue"] = True
    # 外部下载器：aria2c（需本机已装）。未安装或类型非 aria2c 时自动回退原生，不影响下载。
    use_aria2c = (downloader_type or VDL_DOWNLOADER) == "aria2c"
    if use_aria2c:
        a2 = _aria2c_path()
        if a2:
            options["downloader"] = "aria2c"
            options["downloader_args"] = {"aria2c": _build_aria2c_args(concurrent_fragments)}
            logger.info("使用 aria2c 下载器（并发=%d, 路径=%s）", _clamp_concurrency(concurrent_fragments), a2)
        else:
            logger.warning("请求 aria2c 但本机未安装，回退原生下载器（请 brew install aria2 或 apt install aria2）")
    if _MAX_FILE_BYTES:
        options["max_filesize"] = _MAX_FILE_BYTES
    if quality_key == AUDIO_KEY:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    elif quality_key == M4A_KEY:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "192"}
        ]
    elif quality_key == WEBM_KEY:
        options["merge_output_format"] = "webm"
    else:
        options["merge_output_format"] = "mp4"
        # 视频 remux 兜底：HLS 原生下载器可能产出 .m3u8/TS 容器（bestv/inke 等
        # 直链 m3u8 平台），flv/f4v 直链同理——统一 ffmpeg -c copy 快速封装成
        # mp4，避免用户拿到扩展名/容器错乱的文件（单文件场景 FFmpegMergerPP
        # 不触发，必须有此 remux 处理器）。
        options.setdefault("postprocessors", []).append(
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
        )
    return options


def _ensure_audio_track(output: Path, info: dict[str, Any], task: DownloadTask) -> Path:
    """成品音轨完整性校验与补救（2026-08-23「部分下载无声」防御）。

    无声两大来源：
    A. DASH 分离下载（YouTube/B站）合并失败 → 成品只有视频轨，info.formats 有独立音频轨
    B. HLS MP2 / FLV MP3 等 mp4 容器不支持的音频编码 → remux -c copy 时被 ffmpeg 丢弃
    补救顺序：A 独立音频轨重新合并 → B 转封装 mkv 保留全轨 → 仍无声则任务日志明确提示。
    ffprobe/ffmpeg 缺失或补救失败均静默降级，绝不阻塞下载成功态。
    """
    def _has_audio(path: Path) -> bool:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                 "-of", "compact", str(path)],
                capture_output=True, text=True, timeout=25,
            )
            return "codec_type=audio" in (r.stdout or "")
        except Exception:
            return True  # ffprobe 不可用/异常不阻塞，跳过校验

    try:
        if _has_audio(output):
            return output
    except Exception:
        return output

    task.log("检测到成品无音轨，尝试补救…")
    # —— 补救 A：info 提供独立音频轨（DASH 分离下载合并失败场景）——
    try:
        auds = [f for f in (info.get("formats") or []) if isinstance(f, dict)
                and f.get("acodec") and f["acodec"] != "none" and f.get("url")]
        if auds:
            auds.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0, reverse=True)
            au = auds[0]
            aurl = str(au["url"])
            headers = au.get("http_headers") or info.get("http_headers") or {}
            atmp = output.with_name(output.stem + ".aud" + (Path(aurl.split("?")[0]).suffix or ".m4a"))
            hdr_args: list[str] = []
            for k, v in (headers or {}).items():
                hdr_args += ["-H", f"{k}: {v}"]
            dl = subprocess.run(
                ["curl", "-s", "-L", "-m", "120"] + hdr_args + ["-o", str(atmp), aurl],
                capture_output=True, timeout=130,
            )
            if atmp.exists() and atmp.stat().st_size > 1024 and _has_audio(atmp):
                merged = output.with_name(output.stem + ".merged.mp4")
                rr = subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-i", str(output), "-i", str(atmp),
                     "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", "-shortest", str(merged)],
                    capture_output=True, timeout=180,
                )
                if merged.exists() and merged.stat().st_size > 1024 and _has_audio(merged):
                    atmp.unlink(missing_ok=True)
                    merged.replace(output)
                    task.log("已重新合并音轨 ✅")
                    return output
            atmp.unlink(missing_ok=True)
    except Exception:
        logger.debug("音轨补救A失败（独立音频轨合并）", exc_info=True)

    # —— 补救 B：转封装 mkv 保留全轨（mp4 不支持的音频编码场景）——
    try:
        mkv = output.with_suffix(".mkv")
        rr = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(output), "-c", "copy", "-map", "0", str(mkv)],
            capture_output=True, timeout=120,
        )
        if mkv.exists() and mkv.stat().st_size > 1024 and _has_audio(mkv):
            output.unlink(missing_ok=True)
            task.log("已转封装 mkv 保留音轨 ✅")
            return mkv
        mkv.unlink(missing_ok=True)
    except Exception:
        logger.debug("音轨补救B失败（转 mkv）", exc_info=True)

    task.log("⚠️ 该视频源无音轨（或合并失败），文件为无声视频，请尝试其他清晰度")
    return output


def _locate_output(info: dict[str, Any], workdir: Path) -> Path:
    """优先用 yt-dlp 回报的路径，兜底扫描工作目录里最大的成品文件。"""
    for entry in info.get("requested_downloads") or []:
        path = entry.get("filepath")
        if path and Path(path).exists():
            return Path(path)
    candidates = [p for p in workdir.glob("*") if p.is_file() and p.suffix != ".part"]
    if not candidates:
        raise ResolveError("下载完成但未找到输出文件", "请重试一次")
    return max(candidates, key=lambda p: p.stat().st_size)


def _write_sidecar(output: Path, task: "DownloadTask", info: dict[str, Any]) -> None:
    """下载完成后在成品旁写一个 .vdlmeta.json，供本地媒体库展示标题/平台/作者/时长。"""
    try:
        meta = {
            "title": info.get("title") or output.stem,
            "platform": task.platform,
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": int(info.get("duration") or 0),
            "source_url": info.get("webpage_url") or task.url,
            "thumbnail": info.get("thumbnail") or "",
            "completed_at": int(time.time()),
        }
        sidecar = output.with_name(output.stem + ".vdlmeta.json")
        sidecar.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.debug("写入元数据侧车失败: %s", output, exc_info=True)


def build_slow_warning(host: str, speed_bps: float) -> dict:
    """构造慢速告警负载：文案 + 建议 + 可一键尝试的更低清晰度。

    国内强反爬站（腾讯/B站等）限速常因单/CDN 节点带宽限制，降清晰度往往换到
    更快的节点；本机 IP 被限则建议代理/VPN。
    """
    speed_txt = _format_bytes(int(speed_bps)) + "/s"
    hardened = host in ("v.qq.com", "bilibili.com", "www.bilibili.com", "iyunying.com")
    message = f"下载速度过慢（{speed_txt}），可能触发了站点限速"
    suggestions = [
        "更换更低清晰度（如 480P / 360P），常分配到更快的 CDN 节点",
        "使用代理 / VPN 绕过本机 IP 限速（已自动注入的浏览器登录态换节点后保留）",
    ]
    if not hardened:
        suggestions.pop(1) if len(suggestions) > 1 else None
    return {
        "level": "warn",
        "speed_bps": int(speed_bps),
        "message": message,
        "suggestions": suggestions,
        "suggested_quality_keys": ["480", "360"] if hardened else ["480", "360"],
    }


def run_download(task: DownloadTask, store: TaskStore, quality_key: str, cookie: str = "", proxy: str = "", max_retries: int = 0, format_id: str = "", concurrent_fragments: int = 0, downloader_type: str = "", resume: bool = False) -> None:
    """在后台线程执行，全部异常都写回任务状态，不向外抛。

    max_retries=N 时，对网络/超时/连接类等「可重试」错误最多再试 N 次（指数退避）。
    重试在 worker 线程内循环进行，不会额外占用并发槽；会员受限 / 链接失效等不可重试
    错误会直接以 failed 结束，避免无效重试浪费带宽。

    健壮性：内置「停滞看门狗 + 整体硬超时」，防止站点/CDN 假死让任务永久挂起、
    占满并发槽拖垮后续所有下载（典型如 m3u8 流慢速 trickle 不触发 socket_timeout）。
    """
    stop = threading.Event()
    last = {"bytes": 0, "disk": 0, "ts": time.time()}
    _workdir = Path(task.workdir) if task.workdir else None

    def _workdir_bytes() -> int:
        """工作目录里最大文件体积——m3u8_native 等协议 yt-dlp 进度钩子只在整段下完才触发，
        不足以作为「还在跑」的信号；用磁盘上文件实际增长作兜底。"""
        if not _workdir or not _workdir.is_dir():
            return 0
        try:
            return max((p.stat().st_size for p in _workdir.iterdir() if p.is_file()), default=0)
        except OSError:
            return 0

    def _watchdog() -> None:
        """下载中但 N 秒无字节增量 → 判定停滞，置取消标记让进度回调抛出终止。
        信号：①yt-dlp 进度钩子报告的 downloaded_bytes ②工作目录里最大文件体积
        （覆盖 m3u8_native/分段合并等无进度钩子场景）"""
        while not stop.is_set() and not task.is_finished:
            time.sleep(WATCHDOG_POLL)
            if task.status != "downloading":
                continue
            cur_disk = _workdir_bytes()
            if cur_disk > last["disk"]:
                last["disk"] = cur_disk
                last["ts"] = time.time()
            if task.downloaded_bytes > last["bytes"]:
                last["bytes"] = task.downloaded_bytes
                last["ts"] = time.time()
            elif cur_disk > last["disk"]:
                pass  # 上一分支已更新时间
            elif time.time() - last["ts"] > DOWNLOAD_STALL_TIMEOUT:
                task.add_step("下载音视频", "error", f"停滞 {DOWNLOAD_STALL_TIMEOUT}s，已自动终止")
                task.log(f"下载停滞超过 {DOWNLOAD_STALL_TIMEOUT}s，自动终止")
                task.cancel_requested = True
                return

    wd = threading.Thread(target=_watchdog, name=f"wd-{task.id}", daemon=True)
    wd.start()
    try:
        for attempt in range(1, max_retries + 2):
            if attempt > 1:
                # 重试前把状态拨回排队，让前端进度条归零、状态显示「重试中」
                store.update(
                    task.id, status="pending", error="", hint="", progress=0.0,
                    downloaded_bytes=0, total_bytes=0, speed=0.0, eta=0,
                )
                last["bytes"] = 0
                last["ts"] = time.time()
            # 实际下载放到子线程，主线程带「整体硬超时」等待，避免解析/下载任意阶段无限挂起
            th = threading.Thread(
                target=_run_once, args=(task, store, quality_key, cookie, proxy, format_id, concurrent_fragments, downloader_type, resume),
                name=f"dl-{task.id}-{attempt}", daemon=True,
            )
            th.start()
            th.join(timeout=DOWNLOAD_HARD_TIMEOUT)
            if th.is_alive():
                task.add_step("下载音视频", "error", f"超过硬上限 {DOWNLOAD_HARD_TIMEOUT}s")
                task.log(f"下载超过整体硬上限 {DOWNLOAD_HARD_TIMEOUT}s，强制结束")
                task.cancel_requested = True
                store.update(
                    task.id, status="failed", error="下载超时",
                    hint="站点响应过慢或连接不稳定，请稍后重试或更换清晰度/代理",
                )
                break
            t = store.get(task.id)
            if t is None:
                return
            if t.status != "failed":
                return  # completed / canceled -> 停止
            if attempt > max_retries:
                return
            if not _is_retryable(t.error):
                return
            time.sleep(min(2 ** attempt, 30))  # 指数退避，最多 30s
    finally:
        stop.set()


def _mark_step_error(task: DownloadTask, detail: str) -> None:
    """根据当前进行中的步骤，把对应步骤标记为失败。"""
    if any(s.get("name") == "合并与后处理" and s.get("status") == "running" for s in task.steps):
        task.add_step("合并与后处理", "error", detail)
    elif any(s.get("name") == "下载音视频" and s.get("status") in ("running", "done") for s in task.steps):
        task.add_step("下载音视频", "error", detail)
    else:
        task.add_step("解析视频信息", "error", detail)


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024:
            return f"{value:.2f} {unit}"
    return f"{value:.2f} PB"


def _run_once(task: DownloadTask, store: TaskStore, quality_key: str, cookie: str = "", proxy: str = "", format_id: str = "", concurrent_fragments: int = 0, downloader_type: str = "", resume: bool = False) -> None:
    """执行一次下载：先解析元数据，再进入实际下载。

    把 extract_info(..., download=False) 与 process_info(info) 拆成两阶段，
    让「解析视频信息」步骤能快速收敛，且下载阶段一旦卡住就能被看门狗识别。
    """
    effective_proxy = task.proxy or _resolve_proxy(_host_of(task.url) or "")
    task.url = _normalize_share_url(task.url, proxy=effective_proxy)
    reporter = _ProgressReporter(task, store)
    task.add_step("排队等待", "done", "已开始执行")
    task.add_step("解析视频信息", "running", f"正在解析：{task.url[:120]}…")
    info: dict[str, Any] = {}
    # 提前初始化，避免 _download_options 自身抛错时下面 except 引用 _dl_opts 触发
    # UnboundLocalError（会被兜底 except 吞掉，丢失错误分类）
    _dl_opts: dict = {}
    try:
        _dl_opts = _download_options(task, quality_key, reporter, cookie=cookie, proxy=proxy, format_id=format_id, concurrent_fragments=concurrent_fragments, downloader_type=downloader_type, resume=resume)
        # YouTube 免 Cookie 路径：下载阶段也注入 PO Token 上下文——process_info
        # 对部分格式会重新取 URL，无 pot 的请求在数据中心 IP 下被 CDN 403
        # （实测 YouTube 无 Cookie 下载卡 29.93% 后 403）。与解析阶段保持一致会话。
        _task_host = _host_of(task.url)
        if _task_host and _is_youtube_host(_task_host) and not (cookie or "").strip():
            try:
                _yd_vd = _fetch_youtube_visitor_data(effective_proxy)
                if _yd_vd:
                    _ya = _dl_opts.setdefault("extractor_args", {}).setdefault("youtube", {})
                    _ya.setdefault("visitor_data", [_yd_vd])
                    _ya["fetch_pot"] = ["always"]
            except Exception:
                logger.debug("YouTube 下载前获取 visitor_data 失败", exc_info=True)
        # B站 经国内代理回源时，yt-dlp 原生 urllib 读取页面偶发 IncompleteRead；
        # 用 requests 预下载视频页 HTML 并注入 extractor，提高连接稳定性。
        _task_host = _host_of(task.url)
        # 直播模块已下线（2026-08-22）：下载链路同样拦截
        if _task_host and any(_lb in _task_host for _lb in ("douyu.com", "inke.cn", "cc.163.com")):
            raise ResolveError(
                "该直播平台已暂时下线",
                "直播模块（斗鱼 / 映客 / 网易CC）已于 2026-08-22 下线，恢复时间待定。",
            )
        if _task_host and ("bilibili.com" in _task_host or "b23.tv" in _task_host):
            # patch 必须带最终生效的 Cookie（含公共池自动注入），而不是用户原始输入
            _patch_bilibili_webpage_download(
                proxy=effective_proxy,
                cookie=(_dl_opts.get("http_headers") or {}).get("Cookie", cookie),
                ua=(_dl_opts.get("http_headers") or {}).get("User-Agent"),
            )
        with _YoutubeDL(_dl_opts) as ydl:
            # 阶段 1：只解析元数据，不下载
            if _is_douyin_host(_task_host):
                # 抖音：yt-dlp 提取器已失效，直接用 VPS 解析出的真实音视频轨 URL
                info = _douyin_info(task.url)
            elif "dazn.com" in (_task_host or ""):
                # DAZN：DRM+地区封锁，明确提示（与 probe 一致）
                raise ResolveError(
                    "DAZN 暂不支持解析",
                    "DAZN 为付费体育流媒体：内容受 Widevine DRM 保护（合规不破解），"
                    "且中国 IP / 数据中心 IP 均被 403 地区封锁。",
                )
            elif "rumble.com" in (_task_host or ""):
                # Rumble：Cloudflare 反爬，专用浏览器头接口解析直链
                info = _rumble_info(task.url)
            elif "tubitv.com" in (_task_host or ""):
                # Tubi：免费 AVOD，专用页面解析 HLS 直链（支持住宅代理）
                info = _tubi_info(task.url, "", effective_proxy)
            elif _is_kuaishou_host(_task_host):
                # 快手：同上，VPS 解析出合并 mp4 直链
                info = _kuaishou_info(task.url)
            elif "douyu.com" in (_task_host or ""):
                # 斗鱼：yt-dlp 旧提取器失效，VPS Playwright 监听流
                info = _douyu_info(task.url)
            elif "yangshipin.cn" in (_task_host or ""):
                # 央视频：播放地址需 playvinfo JSONP 签名，VPS Playwright 解析
                info = _yangshipin_info(task.url)
            elif "1905.com" in (_task_host or ""):
                # 1905：详情页反爬，VPS Playwright 解析
                info = _m1905_info(task.url)
            elif "fun.tv" in (_task_host or ""):
                # 风行：播放地址需接口签名，VPS Playwright 解析
                info = _funshion_info(task.url)
            elif "bestv.com.cn" in (_task_host or ""):
                # 百视TV：wasm 签名，VPS Playwright 解析出 m3u8 直链
                info = _bestv_info(task.url)
            elif ("hongguoduanju.com" in (_task_host or "")) or ("novelquickapp.com" in (_task_host or "")):
                # 红果短剧：字节 CDN 流，VPS Playwright 点开播放器捕获真流直链
                info = _hongguo_info(task.url)
            elif "inke.cn" in (_task_host or ""):
                # 映客直播/回放：公开接口构造 m3u8，VPS worker 解析
                info = _inke_info(task.url)
            elif "cc.163.com" in (_task_host or ""):
                # 网易CC直播：vapi 公开接口拿签名 FLV 直链，VPS worker 解析
                info = _cc_info(task.url)
            elif _is_weibo_host(_task_host):
                # 微博：同上，VPS 解析出合并 mp4 直链
                info = _weibo_info(task.url)
            elif _is_iqiyi_host(_task_host):
                # 爱奇艺：分享页 JS-only，VPS 解析 m3u8；worker 未配置且非分享页时回退 yt-dlp
                info = _iqiyi_info(task.url, cookie=cookie)
                if info is None:
                    info = ydl.extract_info(task.url, download=False) or {}
            elif ("weixin.qq.com" in (_task_host or "")) or ("channels.weixin.qq.com" in (_task_host or "")):
                # 微信视频号：必须微信登录态，用户自带 Cookie 优先，共享池兜底
                info = _finder_info(task.url, cookie=cookie)
            elif _is_youtube_host(_task_host):
                # YouTube：自动降级（免 Cookie + PO Token → Cookie 源自动切换）
                info = _resolve_youtube(task.url, user_cookie=cookie, proxy=effective_proxy)
            else:
                try:
                    info = ydl.extract_info(task.url, download=False) or {}
                except (DownloadError, ExtractorError) as _exc:
                    # B站 页面风控（412/403，2026-08 B站对非浏览器 TLS 栈返回 412 验证页）
                    # 或网络错误 → 下载阶段同样走 API 兜底（api.bilibili.com 不过页面风控）
                    _low = str(_exc).lower()
                    if _task_host and ("bilibili.com" in _task_host or "b23.tv" in _task_host) and (
                        "412" in _low or "403" in _low or "forbidden" in _low
                        or "precondition" in _low or "incompleteread" in _low
                        or "connection" in _low
                    ):
                        task.log("B站 页面被风控拦截，改用 API 直连解析…")
                        info = _bilibili_api_extract(
                            task.url,
                            proxy=effective_proxy,
                            cookie=(_dl_opts.get("http_headers") or {}).get("Cookie", cookie),
                        ) or {}
                    else:
                        raise
            if info.get("webpage_url"):
                task.source_url = info["webpage_url"]
            if info.get("title"):
                store.update(task.id, title=info["title"])
                task.add_step("解析视频信息", "done", f"已获取标题《{info['title']}》")
            else:
                task.add_step("解析视频信息", "done", "未获取到标题")

            # 直链 m3u8 协议归一化：bestv/inke/douyu 等 worker 返回的 info dict
            # protocol=https + m3u8 URL，yt-dlp determine_protocol 会沿用 dict 里
            # 已有的 protocol（不重新看 URL）→ 用 HttpFD 把 HLS 播放清单文本当文件
            # 下载。统一按 URL 修正为 m3u8_native（走 HLS 下载器分段拉流出 mp4，
            # 与 _iqiyi_info 的成熟路径一致）。
            # ⚠️ ext 保持 mp4（勿改成 m3u8）：否则 outtmpl 产出 .m3u8 扩展名的文件
            # （内容实为 TS 流），用户拿到 .m3u8 会误以为下载失败。
            if _is_hls_url(info.get("url") or ""):
                _proto = (info.get("protocol") or "").split("+")[0].lower()
                if _proto in ("http", "https", ""):
                    info["protocol"] = "m3u8_native"
                    if not info.get("ext") or info["ext"] in ("mp4", "m3u8"):
                        info["ext"] = "mp4"

            # 阶段 2：真正开始下载；先把状态置为 downloading，看门狗才能生效
            task.add_step("下载音视频", "running", f"已选清晰度：{task.quality}")
            task.log(f"开始下载：{task.quality}")
            store.update(task.id, status="downloading", progress=0.0)

            # 记录本次 yt-dlp 实际选中的格式 ID（用于 403 降级时向用户说明原因）
            _sel_parts: list[str] = []
            if info.get("requested_formats"):
                _sel_parts = [str(f.get("format_id", "?")) for f in info["requested_formats"]]
            elif info.get("format_id"):
                _sel_parts = [str(info["format_id"])]
            _sel_fmt = "+".join(_sel_parts) or "未知"

            # B站 API 兜底 info 固定选了 best（720P 等），按用户选择的清晰度重建
            # requested_formats，否则用户选 480P/360P 实际也会下成 720P。
            if info.get("extractor") == "bilibili" and info.get("requested_formats"):
                _rebuild_requested_formats(info, quality_key)

            # YouTube 403 自动降级：tv_embedded 等客户端的某些格式 ID
            # （如 AV1 400/39x、部分 H.264 298/18）URL 被 Google CDN 拒绝，
            # 捕获后自动换已知可用格式重试，用户无感知。
            try:
                ydl.process_info(info)
            except (DownloadError, ExtractorError) as _exc:
                _err_str = str(_exc)
                _is_403 = "403" in _err_str or "Forbidden" in _err_str
                _yt_host = _host_of(task.url)
                _is_yt = _yt_host and ("youtube.com" in _yt_host or "youtu.be" in _yt_host)
                if _is_403 and _is_yt:
                    task.log(
                        f"YouTube 格式 {_sel_fmt} 的下载地址被 CDN 拒绝(403，"
                        f"URL 绑定的出口 IP / 签名不匹配)，自动降级重试…"
                    )
                    task.add_step(
                        "下载音视频", "running",
                        f"格式 {_sel_fmt} 被 YouTube 拒绝，自动切换兼容格式…",
                    )
                    # 多轮降级：依次尝试不同格式链，优先 H.264(avc1) 编码
                    # （最不易被 CDN 拒绝），再放宽到 VP9/AV1、降低分辨率，
                    # 最终兜底 best。每一轮独立 try，直到成功或穷尽所有链。
                    _fb_base = _download_options(
                        task, quality_key, reporter, cookie=cookie, proxy=proxy,
                        format_id=format_id, concurrent_fragments=concurrent_fragments,
                        downloader_type=downloader_type, resume=resume,
                    )
                    _fallback_chains = [
                        "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/b[ext=mp4]",
                        "bv*[vcodec^=avc1]+ba/bv*[height<=720]+ba/b[height<=720]",
                        "299+140/248+140/137+140/136+140/135+140/134+140/133+140/160+140",
                        "bv*+ba/best[height<=1080]/best",
                    ]
                    # 2026-08-22 实测（task logs 确认）：数据中心 IP 下
                    #   ios    → Requested format is not available（SABR 无格式）
                    #   tv     → The page needs to be reloaded
                    #   android → ✅ 直连 mp4 对云 IP 放行，成功
                    #   tv_embedded → 解析 OK 但下载分片 403
                    # 故 android 前置（首次尝试即成功，降级 ~35s），其余兜底。
                    _yt_clients = ["android", "ios", "tv", "web_safari", "tv_embedded"]
                    _done = False
                    for _client in _yt_clients:
                        if task.cancel_requested:
                            task.log("用户取消下载，停止 YouTube 降级尝试")
                            break
                        for _chain in _fallback_chains:
                            if task.cancel_requested:
                                break
                            try:
                                _fb_opts = dict(_fb_base)
                                _fb_opts["format"] = _chain
                                _ya = _fb_opts.setdefault("extractor_args", {}).setdefault("youtube", {})
                                _ya["player_client"] = [_client]
                                # 免 Cookie 时保持 PO Token 上下文
                                if not (cookie or "").strip():
                                    _ya.setdefault("fetch_pot", ["always"])
                                with _YoutubeDL(_fb_opts) as _ydl2:
                                    info = _ydl2.extract_info(task.url, download=False) or info
                                    if info.get("title"):
                                        store.update(task.id, title=info["title"])
                                    _ydl2.process_info(info)
                                _done = True
                                task.log(f"已用 client={_client} 格式链 {_chain} 完成下载")
                                break
                            except (DownloadError, ExtractorError) as _e2:
                                _e2_str = str(_e2)
                                if "403" in _e2_str or "Forbidden" in _e2_str:
                                    task.log(f"client={_client} 链 {_chain} 仍被拒绝，继续…")
                                    continue
                                # 非 403 错误（格式不存在等）换下一个 client/链
                                task.log(f"client={_client} 链 {_chain} 失败({_e2_str[:60]})，继续…")
                                continue
                        if _done:
                            break
                    if not _done:
                        # 所有格式链都 403：极可能是代理出口 IP 不一致
                        _eff = proxy or _resolve_proxy(_yt_host)
                        _proxy_note = (
                            "（当前生效代理：%s；双击 .app 不继承终端代理，"
                            "请确认 Clash/V2Ray 已开启「系统代理」或 TUN 模式）"
                            % (_eff or "无，直连")
                        )
                        task.log("YouTube 所有兼容格式均被 CDN 拒绝，疑似代理出口 IP 不匹配" + _proxy_note)
                        raise DownloadError(
                            "YouTube 下载被 CDN 全面拒绝(403)：请检查代理设置后重试" + _proxy_note
                        )
                else:
                    raise

            output = _locate_output(info, task.workdir or Path("."))
            # 无声防御（2026-08-23）：成品音轨校验 + 补救（纯音频任务跳过）
            if task.quality_key not in (AUDIO_KEY, M4A_KEY):
                output = _ensure_audio_track(output, info, task)
            _write_sidecar(output, task, info)
    except DownloadPaused:
        task.add_step("下载音视频", "done", "已暂停（可继续下载）")
        task.log("用户暂停下载")
        store.update(task.id, status="paused",
                     progress=task.progress, downloaded_bytes=task.downloaded_bytes)
        # 保留 .part 文件，不清除——后续继续时 yt-dlp 断点续传
    except DownloadCanceled:
        _mark_step_error(task, "用户已取消")
        # 断点续传：取消时【不清除】工作目录里的 .part 分片，仅当确实残留部分文件时标记可续传，
        # 后续「继续下载」让 yt-dlp 从中断处接上（aria2c 走 --continue，原生下载器走 continue=True）。
        resumable = _has_partial(task.workdir)
        store.update(
            task.id, status="canceled", error="已取消下载",
            progress=task.progress, downloaded_bytes=task.downloaded_bytes,
            resumable=resumable,
        )
        if not resumable:
            store.clear_files(task.id)
    except (UnsupportedError, GeoRestrictedError, ExtractorError, DownloadError) as exc:
        err = _friendly_error(exc, _build_diag_context(task.url, cookie=cookie, proxy=proxy, options=_dl_opts))
        _mark_step_error(task, err.message)
        # 下载中断类失败（网络抖动/限速假死）往往残留部分分片，标记可续传
        store.update(task.id, status="failed", error=err.message, hint=err.hint,
                     category=getattr(err, "category", None) or "unknown",
                     resumable=_has_partial(task.workdir))
    except (OSError, ResolveError) as exc:
        message = getattr(exc, "message", None) or "下载过程中出现错误"
        # OSError 多为网络层错误（DNS/连接/超时），归为 network 便于前端给「重试」建议；
        # ResolveError 自带 category（如优酷 -3007 的 cookie_required），优先采用
        cat = getattr(exc, "category", None)
        if not cat:
            cat = "network" if isinstance(exc, OSError) else "unknown"
        _mark_step_error(task, message)
        store.update(task.id, status="failed", error=message, hint=_clean_message(str(exc)),
                     category=cat, resumable=_has_partial(task.workdir))
    except Exception as exc:  # noqa: BLE001 - 兜底，保证任务状态一定收敛
        logger.exception("下载任务 %s 未预期失败", task.id)
        _mark_step_error(task, "未预期错误")
        store.update(task.id, status="failed", error="下载失败", hint=_clean_message(str(exc)),
                     category="unknown", resumable=_has_partial(task.workdir))
    else:
        if task.cancel_requested or task.is_finished:  # 已被看门狗/硬超时/用户终止，不再写完成态
            return
        # 可选：下载完成后提取文案（口播/简介），失败不影响下载完成态
        if task.extract_mode:
            _run_extraction(task, store, output, info, cookie, proxy)
        task.add_step("合并与后处理", "done", f"输出文件：{output.name}")
        task.add_step("下载完成", "done", f"文件大小：{_format_bytes(output.stat().st_size)}")
        task.log(f"下载完成：{output.name}")
        store.update(
            task.id,
            status="completed",
            progress=100.0,
            filepath=output,
            filename=output.name,
            filesize=output.stat().st_size,
            speed=0.0,
            eta=0,
        )


def _run_extraction(task: DownloadTask, store: TaskStore, output: Path, info: dict[str, Any],
                    cookie: str = "", proxy: str = "", mode: str | None = None) -> None:
    """在后台线程里执行文案提取，结果写回任务。任何失败都降级处理，不影响下载完成态。

    mode 为 None 时沿用 task.extract_mode；info 为 None 时（重提取场景）改用 task.source_url。
    """
    mode = mode or task.extract_mode
    if mode not in ("spoken", "description", "both"):
        return
    source_url = (info.get("webpage_url") if info else None) or task.source_url or task.url
    task.add_step("提取文案", "running", "正在提取文案…")
    store.update(task.id, extract_status="running")
    workdir = task.workdir

    def progress_cb(stage: str, detail: str) -> None:
        task.add_step("提取文案", "running", detail)

    try:
        from extract_text import extract_all
        result = extract_all(
            str(output), source_url=source_url, cookie=cookie, proxy=proxy,
            mode=mode, workdir=workdir, progress_cb=progress_cb,
        )
        task.extracted_text = result
        task.extract_status = "done"
        task.add_step("提取文案", "done", "文案提取完成")
        store.update(task.id, extracted_text=result, extract_status="done")
    except Exception as exc:  # noqa: BLE001 - 文案提取失败绝不能拖垮下载任务
        logger.exception("提取文案失败 task=%s", task.id)
        err = {"error": str(exc)[:300]}
        task.extracted_text = err
        task.extract_status = "error"
        task.add_step("提取文案", "error", f"文案提取失败：{str(exc)[:120]}")
        store.update(task.id, extracted_text=err, extract_status="error")


def _is_retryable(error: str) -> bool:
    """判断失败原因是否值得自动重试：网络/超时/连接/代理/临时服务端错误可重试，
    会员受限、链接失效等应直接失败，避免无效刷带宽。"""
    if not error:
        return True
    lowered = error.lower()
    keywords = (
        "超时", "timeout", "连接", "connection", "网络", "network", "resolve",
        "代理", "proxy", "ssl", "reset", "refused", "unreachable", "中断",
        "interrupted", "503", "502", "500", "429", "temporary", "temp",
        "ffmpeg", "m3u8", "hls",  # HLS 合并偶发，ffmpeg 偶发退出 → 自动重试
    )
    return any(k in lowered for k in keywords)


def _is_hls_url(url: str) -> bool:
    """粗略判断是否为 HLS 播放清单地址。"""
    return bool(url) and (".m3u8" in url or url.rstrip().endswith(".m3u8"))


def _detect_play_url(info: dict[str, Any]) -> tuple[str | None, bool]:
    """返回适合「在线观看」的播放地址与是否为 HLS。

    HLS 优先（腾讯等站原生就是 m3u8 流，浏览器可经后端代理播放）；
    否则退回 MP4 直链。返回 (url, is_hls)。
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    # 1) 从 formats 里挑分辨率最高的 HLS 流
    cands: list[tuple[int, str]] = []
    for f in formats:
        u = f.get("url") or f.get("manifest_url") or ""
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        is_hls = proto in ("m3u8", "m3u8_native") or _is_hls_url(u)
        if is_hls:
            cands.append((int(f.get("height") or 0), u))
    if cands:
        cands.sort(key=lambda x: x[0], reverse=True)
        return cands[0][1], True
    # 2) 合并 info 本身的 url 若是 HLS
    u = info.get("url") or ""
    if _is_hls_url(u):
        return u, True
    # 3) 普通 MP4 直链（info.direct 标记）
    du = _detect_direct_url(info)
    if du:
        return du, False
    # 3b) 从 formats 中挑最高分辨率非 HLS 直链（覆盖 info.direct=False 的站点）
    #     优先 H.264(avc1) —— macOS WKWebView 不支持 AV1/VP9，选了会导致黑屏
    prog_cands: list[tuple[int, int, int, str]] = []  # (is_avc1, height, tbr, url)
    for f in formats:
        u = f.get("url") or ""
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        if proto in ("m3u8", "m3u8_native") or _is_hls_url(u):
            continue
        h = int(f.get("height") or 0)
        if h:
            tbr = float(f.get("tbr") or 0) or 0.0
            vc = (f.get("vcodec") or "").lower()
            is_avc1 = 1 if ("avc1" in vc or "h264" in vc or "avc" in vc) else 0
            prog_cands.append((is_avc1, h, tbr, u))
    if prog_cands:
        # 先按是否 H.264 降序（H.264 优先），同编码按 height 降序，同 height 按 tbr 降序
        prog_cands.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        return prog_cands[0][3], False
    # 3c) 兜底：无 height 的渐进式直链（BitChute 等 _check_format 只返回 url+filesize，
    #     没有 height/protocol 字段，会全部漏过上面 3b 的高度分桶）
    for f in formats:
        u = f.get("url") or ""
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        if proto in ("m3u8", "m3u8_native") or _is_hls_url(u):
            continue
        return u, False
    return None, False


def build_watch_options(info: dict[str, Any]) -> list[dict[str, Any]]:
    """为「在线观看」生成可选清晰度列表（每个清晰度对应一个可直接播放的地址）。

    - 按清晰度（height）去重：同一分辨率下平台常给出多个 format（不同码率/音轨/CDN），
      只保留码率最高、可播放的一个 url，避免下拉出现重复项；
    - 同时覆盖两类可播源：HLS 直播清单（m3u8）与渐进式直链（MP4/WebM，可在 <video> 直接播）；
    - 所有视频清晰度共用同一 HLS url 时，合并为「自动（源站自适应）」；
    - 既无 HLS 又有 MP4 直链时，补一个 MP4 直链选项。
    保证返回的每个 url 都能直接交给后端 /api/stream/proxy 代理播放。
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    by_height: dict[int, dict[str, Any]] = {}   # height -> HLS 最佳可播放项
    prog: dict[int, dict[str, Any]] = {}         # height -> 渐进式直链最佳项
    audio: dict[str, Any] | None = None          # 纯音频 HLS（无 height）
    prog_unknown: dict[str, Any] | None = None   # 无 height 的渐进式直链兜底
    for f in formats:
        u = (f.get("url") or f.get("manifest_url") or "").strip()
        if not u:
            continue
        proto = (f.get("protocol") or "").split("+")[0].lower()
        is_hls = proto in ("m3u8", "m3u8_native") or _is_hls_url(u)
        height = int(f.get("height") or 0)
        note = (f.get("format_note") or "").strip()
        fid = f.get("format_id") or ""
        tbr = float(f.get("tbr") or 0) or 0.0
        item = {"url": u, "note": note, "fid": fid, "tbr": tbr}
        if is_hls:
            if height:
                cur = by_height.get(height)
                if cur is None or tbr > cur["tbr"]:
                    by_height[height] = item
            elif audio is None:
                audio = item
        else:
            # 渐进式直链：放宽过滤——只要有 url + height 就纳入观看选项。
            # 很多第三方提取器 ext/proto 字段不规范；播放失败由前端 onerror 兜底。
            # 同分辨率优先 H.264(avc1) —— WKWebView 不支持 AV1/VP9
            if height:
                cur = prog.get(height)
                vc = (f.get("vcodec") or "").lower()
                is_avc1 = ("avc1" in vc or "h264" in vc or "avc" in vc)
                cur_vc = (cur.get("vcodec") or "") if cur else ""
                cur_is_avc1 = ("avc1" in cur_vc or "h264" in cur_vc or "avc" in cur_vc) if cur else False
                # 优先 H.264；同编码选高码率
                if cur is None or (is_avc1 and not cur_is_avc1) or (is_avc1 == cur_is_avc1 and tbr > cur["tbr"]):
                    item["_vcodec"] = vc  # 保留编码信息供调试
                    prog[height] = item
            else:
                # 无 height 直链（BitChute 等）：保留第一个作兜底
                if prog_unknown is None:
                    prog_unknown = item

    # 合并：同清晰度优先 HLS（自适应更好），无 HLS 才用渐进式直链
    merged: dict[int, tuple[bool, dict[str, Any]]] = {}
    for h, v in by_height.items():
        merged[h] = (True, v)          # (is_hls, item)
    for h, v in prog.items():
        merged.setdefault(h, (False, v))

    opts: list[dict[str, Any]] = []
    video_urls = {v["url"] for v in by_height.values()}
    if len(video_urls) == 1 and by_height:
        # 所有视频清晰度共用同一清单：源站按带宽自适应，无需手动选
        url = next(iter(video_urls))
        tag = f"{max(by_height)}P"
        opts.append({"key": "auto", "label": f"自动（源站自适应） · {tag}",
                     "url": url, "format_id": "", "is_hls": True})
    else:
        for height in sorted(merged, reverse=True):
            is_hls, v = merged[height]
            label = f"{height}P"
            note = v["note"]
            if note and str(height) not in note and note.lower() not in ("hls", "m3u8"):
                label = f"{label} · {note}"
            if not is_hls:
                label += " · MP4"
            opts.append({
                "key": str(height),
                "label": label,
                "url": v["url"],
                "format_id": v["fid"],
                "is_hls": is_hls,
            })

    # 无视频流但有纯音频 HLS 时，单列一个音频选项
    if not opts and audio:
        opts.append({"key": "audio", "label": audio["note"] or "音频",
                     "url": audio["url"], "format_id": "", "is_hls": True})

    # 无 height 的渐进式直链兜底（BitChute 等 _check_format 只给 url+filesize）
    if not opts and prog_unknown:
        _pu_url = prog_unknown["url"]
        _pu_ext = (Path(urlparse(_pu_url).path).suffix or "").lstrip(".").lower()
        _pu_label = f"直链 · {_pu_ext.upper()}" if _pu_ext else "直链"
        opts.append({"key": "direct", "label": _pu_label,
                     "url": _pu_url, "format_id": "", "is_hls": False})

    if not opts:
        # HLS 源兜底：部分平台只设 info.url 为 m3u8（不填充 formats），
        # 如 Rumble 的 hls-vod 路径——此时给出「自动（HLS）」观看选项
        _iu = info.get("url") or ""
        if _is_hls_url(_iu):
            opts.append({"key": "auto", "label": "自动（HLS）",
                         "url": _iu, "format_id": "", "is_hls": True})
            return opts
        du = _detect_direct_url(info)
        if du:
            # 按真实扩展名标注标签（f4v/flv 直链不该标成 "MP4 直链"）
            _du_ext = (Path(urlparse(du).path).suffix or "").lstrip(".").lower()
            _du_ext = _du_ext or (info.get("ext") or "").lower()
            _du_label = {"flv": "FLV 直链", "f4v": "FLV 直链",
                         "m3u8": "HLS 直播流", "mp4": "MP4 直链"}.get(_du_ext, "直链")
            opts.append({"key": _du_ext or "mp4", "label": _du_label,
                         "url": du, "format_id": "", "is_hls": _du_ext == "m3u8"})
    return opts


def _extract_page_title(html: str) -> str:
    """从优酷页面 HTML 抽取剧名候选标题。

    优先 <title>；回退 <meta property="og:title">；再回退 JSON-LD 的 name/title。
    """
    if not html:
        return ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        t = m.group(1).strip()
        if t:
            return t
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
        html, re.IGNORECASE | re.DOTALL,
    )
    if m:
        t = m.group(1).strip()
        if t:
            return t
    for lm in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL,
    ):
        try:
            data = json.loads(lm.group(1))
        except Exception:
            continue
        name = data.get("name") or data.get("title")
        if isinstance(name, str) and name.strip():
            return name.strip()
        items = data.get("itemListElement")
        if isinstance(items, list) and items:
            n = items[0].get("name")
            if isinstance(n, str) and n.strip():
                return n.strip()
    return ""


def _parse_series_from_title(raw: str, title: str, is_show_page: bool = False) -> str:
    """从优酷网页标题里切出整部剧名。

    raw 形如：
      "神墓 辰南觉醒 第1话 我自远古来-动漫-高清完整正版视频在线观看-优酷"
    末尾 "-优酷"（可能带"视频"）之前还夹着站点描述，需整体剥离后再定位单集标题。

    is_show_page=True 时表示这是 show_page 总页（标题只有剧名、无单集标题），
    直接取首个描述分隔符前的内容作为剧名。
    """
    if not raw:
        return ""
    body = re.split(r"[-_|｜]\s*优酷", raw)[0].strip()
    if is_show_page:
        # 总页标题形如 "神墓 辰南觉醒-动漫-高清完整正版视频在线观看"，
        # 剧名在首个 "-" 之前。
        return re.split(r"[-_|｜]", body)[0].strip()
    if not title:
        return ""
    series = ""
    # 策略1：网页标题里能直接定位 yt-dlp 的单集标题，取它之前的部分作为剧名
    if title in body:
        series = body[: body.index(title)].strip(" -_｜|")
    # 策略2：兜底——单集标题与网页略有出入时，按"第X话/集"把剧名切出来
    if not series:
        mm = re.match(r"^(.*?)[\s\-_]+第\s*\d+\s*[话集]", body)
        if mm:
            series = mm.group(1).strip(" -_｜|")
    return series


def _enrich_youku_series(info: dict[str, Any], proxy: str = "") -> dict[str, Any]:
    """优酷剧集：yt-dlp 只返回单集标题，从网页 <title> 提取整部剧名补到 info['series']。

    优酷播放页 HTML 的 title 通常是：
        "神墓 辰南觉醒 第1话 我自远古来-动漫-高清完整正版视频在线观看-优酷"
    而 yt-dlp 返回的 info['title'] 只有：
        "第1话 我自远古来"
    用网页 title 减去单集标题，即可得到 "神墓 辰南觉醒" 作为 series。

    抓取走 VDL_PROXY_CN（海外部署经国内代理回源）；失败时记录 _series_source 诊断，
    便于线上排查（Railway 抓优酷页可能被风控返回非视频页）。
    """
    title = (info.get("title") or "").strip()
    if not title:
        info["_series_source"] = "no_title"
        return info
    # 只对明显是"第X话/集"的单集标题做补全，避免普通短视频也走一次请求
    if not re.search(r"^(?:第\s*\d+\s*[话集]|\d+\s*[话集])", title):
        info["_series_source"] = "not_episode"
        return info

    webpage_url = info.get("webpage_url") or ""
    host = _host_of(webpage_url)
    if not webpage_url or not (host.endswith("youku.com") or host.endswith("tudou.com")):
        info["_series_source"] = "not_youku"
        return info

    try:
        import requests
    except Exception:
        info["_series_source"] = "no_requests"
        return info

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://v.youku.com/",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    timeout = int(os.environ.get("VDL_YOUKU_TITLE_TIMEOUT", "15"))

    # 候选页面：v_show 单集页优先；URL 带 show id（s=）时追加 show_page 总页兜底
    candidates = [webpage_url]
    m_show = re.search(r"[?&]s=([0-9a-f]{12,})", webpage_url)
    if m_show:
        candidates.append(f"https://www.youku.com/show_page/id_{m_show.group(1)}.html")

    raw = ""
    parsed_series = ""
    last_err = ""
    for attempt in range(3):
        for u in candidates:
            try:
                resp = requests.get(
                    u, headers=headers, proxies=proxies,
                    timeout=timeout, allow_redirects=True,
                )
                resp.raise_for_status()
                candidate_raw = _extract_page_title(resp.text)
                if not candidate_raw:
                    continue
                # 只有真正从候选标题里解析出剧名才提前结束；
                # 否则（如被风控返回纯站点名"优酷"）继续尝试下一个候选页。
                is_show_page = "show_page" in u
                series_candidate = _parse_series_from_title(
                    candidate_raw, title, is_show_page=is_show_page
                )
                if series_candidate:
                    parsed_series = series_candidate
                    raw = candidate_raw
                    break
                # 没解析出剧名但拿到了标题，留作诊断候选（不覆盖已成功解析的）
                if not raw:
                    raw = candidate_raw
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {str(exc)[:100]}"
        if parsed_series:
            break
        if attempt < 2:
            time.sleep(1)

    if not parsed_series:
        info["_series_source"] = (
            f"fetch_failed:{last_err or 'empty'}" if not raw else "parse_failed"
        )
        logger.warning(
            "[youku series] no series parsed (raw=%r, err=%s)", raw, last_err
        )
        return info

    info["series"] = parsed_series
    info["_series_source"] = "web_title"
    logger.info("[youku series] extracted series=%r from webpage title", parsed_series)
    return info


def _combine_series_title(info: dict[str, Any]) -> str:
    """组合剧集名 + 单集标题，避免前端只显示"第X话 XXX"。

    yt-dlp 对剧集/动漫/综艺通常把整部剧名放在 series/alt_title，单集标题放在 title。
    如果两者都存在且 title 不含剧名，则拼接为"剧名 - 单集标题"。
    """
    title = (info.get("title") or "").strip()
    series = (info.get("series") or "").strip()
    alt = (info.get("alt_title") or "").strip()
    # 优先用 series；没有则用 alt_title
    show = series or alt
    if show and title:
        # 避免重复拼接（有些平台 title 里已经带了剧名前缀）
        if title.startswith(show) or show in title:
            return title
        return f"{show} - {title}"
    return title or show or "未命名视频"


def summarize(info: dict[str, Any]) -> dict[str, Any]:
    """抽取前端需要的字段。"""
    play_url, is_hls = _detect_play_url(info)
    return {
        "title": _combine_series_title(info),
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": int(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
        "view_count": info.get("view_count") or 0,
        "webpage_url": info.get("webpage_url") or "",
        "extractor": info.get("extractor_key") or "",
        "direct_url": _detect_direct_url(info),
        "play_url": play_url,
        "is_hls": is_hls,
        "is_live": bool(info.get("is_live")),
        "watch_options": build_watch_options(info),
        # 诊断字段（仅供排查优酷剧集剧名补全是否生效，前端忽略即可）
        "_series_source": info.get("_series_source", ""),
    }
