"""server/image_download.py — 图片直链下载（「无水印图片」P0，2026-10-12）。

定位：用户粘贴**图片直链**（CDN 原图地址）→ 流式落盘。这是「无水印图片」方案里的
L1（平台侧原图）：多数 UGC 平台（小红书 / 微博 / 知乎 / B 站动态）的水印加在
**展示层**（App 渲染时叠加或保存相册时客户端烧制），CDN 原图本身不带水印 —— 与
视频下载「拿真实流地址」是同一套哲学。平台页面解析（P1）与 AI 修复（P2）在此之后。

为什么不复用 yt-dlp：yt-dlp 2026.08.19 无通用图片抽取器（--list-extractors 只有
Imgur 等特定站），且图片是「一个 URL 一个文件」的简单传输；进度 / 取消 / 看门狗 /
配额的任务框架（tasks.py）整套复用即可，不引入下载器抽象。

安全（与 /api/download 同口径，缺一不可）：
  ① 初始 URL 在路由层过 app._assert_safe_url（SSRF 护栏，fail-closed）；
  ② 重定向**每一跳**都在本模块内重新校验（_SafeRedirectHandler）—— 否则
     「公网 URL 302 到 169.254.169.254」即可绕过初始检查；
  ③ 「是不是图片」以**文件头魔数**判定，content-type 只作提示 —— 防 text/html
     错误页 / application/octet-stream 伪装落盘（也防「下到一个网页」）；
  ④ 体积上限（默认 64MB，VDL_IMAGE_MAX_BYTES 可调）防磁盘被打满；
  ⑤ SVG 允许下载，但 /file 端点对其强制附件（application/octet-stream）——
     SVG 可含脚本，内联渲染等于把用户会话暴露给图片作者。

取消 / 停滞：与视频下载同一套协作式取消（cancel_requested 在读块边界消费）+
停滞看门狗（复用 downloader 的 DOWNLOAD_STALL_TIMEOUT / WATCHDOG_POLL 常量，
保证同进程内「停滞」定义一致）；另有图片自己的整体硬超时（默认 1800s）。
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

from platforms import LinkError
from tasks import DownloadTask, TaskStore

# 看门狗口径与视频下载同源（import 失败时按同名环境变量兜底，保证模块可独立测试）
try:  # pragma: no cover - 导入路径在打包/源码两种模式都成立
    from downloader import DOWNLOAD_STALL_TIMEOUT, WATCHDOG_POLL
except Exception:  # noqa: BLE001
    DOWNLOAD_STALL_TIMEOUT = int(os.environ.get("VDL_DOWNLOAD_STALL_TIMEOUT", "180"))
    WATCHDOG_POLL = int(os.environ.get("VDL_WATCHDOG_POLL", "5"))

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
CHUNK_SIZE = 64 * 1024                 # 读块大小：取消信号的消费粒度
DEFAULT_MAX_BYTES = 64 * 1024 * 1024   # 单张图片体积上限（64MB，远超正常图片）
IO_TIMEOUT = 30                        # 每个 socket 操作（建连/读）的超时（秒）
IMAGE_HARD_TIMEOUT = int(os.environ.get("VDL_IMAGE_HARD_TIMEOUT", "1800"))
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# 🔴 2026-10-12 实测（小红书分享页，三个变量逐一隔离）：
#    ① urllib 默认发 `Accept-Encoding: identity` → xhs WAF 直接 302 去登录页
#       （真实浏览器一定声明支持压缩，identity 是典型机器人特征）；
#    ② UA 与 sec-ch-ua 版本不一致（UA 124 / sec-ch-ua 131）也会被墙；
#    ③ 只有 `gzip, deflate` 这个精确串能过（单写 gzip、或加 br 都被墙）。
#    声明了压缩就必须**真的解压**：服务端会返回 gzip（77KB 页面 → 27KB）。
_BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
# 分享页解析：页面正文最多读这么多（足够覆盖 og:image 与 __INITIAL_STATE__），
# 超过即按「页面太大」放弃，绝不无限读。
PAGE_MAX_BYTES = 1024 * 1024
# 单个分享页最多展开成几张图片（防「一个链接 200 张图」把队列和配额打爆）
MAX_PAGE_IMAGES = 12
# 页面里这些 URL 一律不当候选：站点静态资源 / 图标 / 脚本 / 样式
_PAGE_JUNK_RE = re.compile(
    r"(picasso-static|fe-static|fe-platform|fe-video|/logo|/icon|/avatar|"
    r"\.js$|\.css$|\.ico$|\.svg$|\.woff|placeholder|default_?avatar)", re.I)
# 小红书 CDN 的图片处理令牌：prv=预览小图，dft=默认大图 → 同一张图只留 dft
_XHS_TOKEN_RE = re.compile(r"!(nd_(?:prv|dft)|prv|dft)[^/]*$", re.I)
# 预览图判定（只认 prv）：用于「同图多令牌只留大图」的偏好
_IS_PREVIEW_RE = re.compile(r"!(?:nd_)?prv[^/]*$", re.I)


class PageLinkError(LinkError):
    """链接返回的是网页而不是图片。

    images：从页面里提取到的图片候选（可能为空 —— 空表示页面里没找到可用图片，
    调用方应给「页面里没有图片」的提示而不是笼统的「不是图片」）。
    """

    def __init__(self, images: list[str], final_url: str = ""):
        super().__init__(
            "该链接打开的是网页，不是图片",
            "已尝试从页面里提取图片" if images
            else "页面里没有找到可下载的图片")
        self.images = list(images or [])
        self.final_url = final_url


# 与 app._PRIVATE_NETS 同口径（100.64.0.0/10 CGNAT 在 Python 3.13 is_private=False，
# 必须显式列举，否则运营级内网段漏放）
_PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
        "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15",
        "::1/128", "fc00::/7", "fe80::/10",
    )
]

# 文件头魔数 → 扩展名（顺序即优先级）
_MAGIC_TABLE = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"II*\x00", ".tiff"),
    (b"MM\x00*", ".tiff"),
)
# ftyp 盒（offset 4..8）→ 扩展名：avif / heic / heif
_FTYP_BRANDS = {b"avif": ".avif", b"avis": ".avif", b"heic": ".heic",
                b"heix": ".heic", b"heim": ".heic", b"heis": ".heic",
                b"mif1": ".heif", b"msf1": ".heif"}
# content-type → 扩展名兜底（魔数识别不了时的次选）
_CT_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/tiff": ".tiff", "image/avif": ".avif", "image/heic": ".heic",
    "image/heif": ".heif", "image/svg+xml": ".svg", "image/x-icon": ".ico",
}


# --------------------------------------------------------------------------- #
# 类型识别
# --------------------------------------------------------------------------- #
def sniff_image_ext(head: bytes, content_type: str = "") -> str | None:
    """按文件头魔数判定图片类型，返回扩展名；不是图片返回 None。

    content-type 只作兜底提示 —— CDN 常把图片标成 application/octet-stream，
    也有把错误页标成 image/* 的，因此**魔数优先**。
    """
    if not head:
        return None
    for magic, ext in _MAGIC_TABLE:
        if head.startswith(magic):
            return ext
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"BM") and len(head) >= 6:
        return ".bmp"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _FTYP_BRANDS:
            return _FTYP_BRANDS[brand]
    # SVG：XML 声明或根元素（给 512B 的探测窗口，DOCTYPE 可能很长）
    window = head[:512].lstrip()
    if window.startswith(b"<?xml") or window.startswith(b"<svg") or b"<svg" in window:
        return ".svg"
    # 魔数不认识时退回 content-type（仍要求它声明自己是图片）
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith("image/"):
        return _CT_EXT.get(ct)
    return None


# --------------------------------------------------------------------------- #
# SSRF（重定向每一跳都校验）
# --------------------------------------------------------------------------- #
def assert_public_url(url: str) -> None:
    """与 app._assert_safe_url 同口径的 SSRF 校验（自包含，供重定向跳转用）。

    路由层已对初始 URL 调过 app._assert_safe_url；这里管住**重定向之后**的每一跳，
    两边判据保持一致，避免「初始放行、跳转偷渡」。
    """
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host:
        raise LinkError("链接缺少主机名", "请检查链接是否完整")
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise LinkError("该主机不在允许范围内", "请粘贴公开可访问的图片链接")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        try:
            infos = socket.getaddrinfo(host, None)   # 重试一次，降低偶发 DNS 抖动误杀
        except socket.gaierror:
            raise LinkError("域名解析失败", "无法解析该域名，请稍后重试或更换链接")
    has_public = False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if any(addr in _net for _net in _PRIVATE_NETS):
            continue
        if not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            has_public = True
            break
    if not has_public:
        raise LinkError("该链接指向非公开网络，已拒绝",
                        "只允许下载公开可访问的图片；内网 / 本地 / 云元数据地址不可用")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向处理器：每一跳都过 SSRF 护栏，私网目标直接抛错终止。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(proxy: str = "") -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_SafeRedirectHandler()]
    if (proxy or "").strip():
        handlers.append(urllib.request.ProxyHandler(
            {"http": proxy.strip(), "https": proxy.strip()}))
    else:
        # 沿用系统环境变量代理（与 yt-dlp 的默认行为一致）
        handlers.append(urllib.request.ProxyHandler())
    return urllib.request.build_opener(*handlers)


def normalize_image_url(raw: str) -> str:
    """规范化用户粘贴的图片链接：补 scheme、拒绝非 http(s)。

    用户经常直接粘 `example.com/a.jpg`（无协议头），按 https 补全。
    🔴 2026-10-12 冒烟实测：早先「无 :// 就补 https」的启发式会被
    `data:image/png;base64,...` 绕过（补完变成 https://data:...，scheme 竟
    然是 https）—— 改为**显式列举拒绝的 scheme 前缀**，只有真正没带 scheme
    的裸域名才补 https。file:// / data: / javascript: 等一律拒绝（既非图片
    直链也是安全隐患）。
    """
    url = (raw or "").strip()
    if not url:
        raise LinkError("链接为空", "请粘贴图片直链")
    low = url.lower()
    for bad in ("data:", "javascript:", "file:", "ftp:", "blob:", "about:", "vbscript:"):
        if low.startswith(bad):
            raise LinkError("仅支持 http/https 图片链接",
                            "请粘贴以 http:// 或 https:// 开头的图片地址")
    if not low.startswith(("http://", "https://")):
        url = "https://" + url
    scheme = urllib.parse.urlparse(url).scheme.lower()
    host = (urllib.parse.urlparse(url).hostname or "")
    if scheme not in ("http", "https"):
        raise LinkError("仅支持 http/https 图片链接",
                        "请粘贴以 http:// 或 https:// 开头的图片地址")
    # 早拒：无主机名 / 含空格等明显不是链接的输入（否则要等到下载时才以
    # 「域名解析失败」收场，用户看到的 skipped 计数也没有可读原因）
    if not host or re.search(r"\s", host):
        raise LinkError("链接格式不正确", "请粘贴完整的图片地址（含域名）")
    return url


# --------------------------------------------------------------------------- #
# 文件名
# --------------------------------------------------------------------------- #
def _sanitize(name: str) -> str:
    """清洗文件名：消目录穿越残留（.. 序列）、路径分隔符 / 控制字符，限长 80 字。

    保留中文与常见符号；连续下划线折叠成一个，首尾的 . / _ 剥掉。
    纯清洗函数：**不提供兜底名** —— 空输入 / 全是垃圾字符的输入一律返回 ""，
    默认名由 derive_filename 统一决定。🔴 2026-10-12 回归测试实测：早先这里
    对空结果返回 "image"，而 derive_filename 的取值顺序是
    `cd_name or url_name or "image"` —— 没有 Content-Disposition 时
    cd_name="image" 恒为真，URL 末段兜底成了死代码，所有图片都被命名成
    image.png / image-1.png（用户看到的就是一串无意义文件名）。
    """
    name = (name or "").replace("..", "_")          # 先消连续点（穿越残留）
    name = re.sub(r"[\\/\x00-\x1f\x7f\"]", "_", name)  # 再消分隔符/控制字符/引号
    name = re.sub(r"_{2,}", "_", name)
    name = name.strip().strip(".").strip("_").strip()
    return name[:80]


def _name_from_content_disposition(value: str) -> str:
    if not value:
        return ""
    # filename*=UTF-8''xxx 优先（RFC 5987），其次 filename="xxx"
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", value, re.I)
    if m:
        try:
            return urllib.parse.unquote(m.group(1).strip())
        except Exception:  # noqa: BLE001
            return ""
    m = re.search(r'filename\s*=\s*"([^"]+)"', value, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"filename\s*=\s*([^;]+)", value, re.I)
    return m.group(1).strip() if m else ""


def derive_filename(final_url: str, content_disposition: str, ext: str,
                    workdir: Path | None = None, name_hint: str = "") -> str:
    """推导落盘文件名：name_hint > Content-Disposition > URL 末段 > image，
    扩展名以魔数为准。

    `name_hint` 是调用方给的可读名（分享页展开的图片传 "图片-1"…）—— 优先级最高，
    否则小红书这类 CDN 的 URL 末段是一串哈希天书，用户在一堆文件夹里根本认不出来。

    同名冲突时自动加序号（image.jpg → image-1.jpg），避免批量下载互相覆盖。
    """
    hint = _sanitize(name_hint or "") if name_hint else ""
    cd_name = _sanitize(_name_from_content_disposition(content_disposition))
    url_name = ""
    try:
        path = urllib.parse.urlparse(final_url).path
        url_name = _sanitize(urllib.parse.unquote(path.rsplit("/", 1)[-1]))
    except Exception:  # noqa: BLE001
        url_name = ""
    name = hint or cd_name or url_name or "image"
    # 扩展名以魔数识别结果为准（CDN 常把 .php/.weixin 动态地址当图片 served）
    stem = name.rsplit(".", 1)[0] or "image" if "." in name else name
    candidate = f"{stem}{ext}"
    if workdir is None:
        return candidate
    if not (workdir / candidate).exists():
        return candidate
    for i in range(1, 1000):
        alt = f"{stem}-{i}{ext}"
        if not (workdir / alt).exists():
            return alt
    return f"{stem}-{int(time.time())}{ext}"


# --------------------------------------------------------------------------- #
# 分享页解析（用户粘贴的是「页面链接」而不是图片直链时）
# --------------------------------------------------------------------------- #
# 页面抓取的最大跳转次数（与 _SafeRedirectHandler 的默认上限同量级）
_PAGE_MAX_HOPS = 5


def _fetch_page_text(url: str, referer: str = "", cookie: str = "",
                     proxy: str = "", should_stop=None) -> tuple[str, str]:
    """带 Chrome TLS 指纹抓网页正文，返回 (页面文本, 最终URL)；失败返回 ("", "")。

    🔴 为什么必须有这一条（2026-10-12 实测定位）：小红书等平台的 WAF 校验
    **TLS 指纹（JA3）**——urllib 就算把浏览器请求头全带对（UA / sec-ch-ua /
    Sec-Fetch-* / Accept-Encoding: gzip, deflate 一个不差），仍会被 302 去登录页，
    拿到的是登录墙 HTML，页面里提取不到笔记配图。curl_cffi 的 chrome131 伪装
    （项目 downloader.py 解析 Rumble 时已在用同一招）可直接拿到正文。

    🔴 安全：**不跟随重定向**。逐跳取 Location 后重新过 assert_public_url ——
    与 _SafeRedirectHandler 完全同口径，杜绝「公网入口 302 到 169.254.169.254」。
    """
    try:
        from curl_cffi import requests as _cf  # type: ignore
    except Exception:  # noqa: BLE001 - 没装就放弃这条兜底（返回空 = 用 urllib 的结果）
        return "", ""
    headers = {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    cur = url
    for _hop in range(_PAGE_MAX_HOPS):
        if should_stop is not None and should_stop():
            return "", ""
        try:
            assert_public_url(cur)          # 每一跳都过 SSRF 护栏（fail-closed）
        except Exception:  # noqa: BLE001
            return "", ""
        try:
            kw = {"impersonate": "chrome131", "headers": headers,
                  "timeout": 20, "allow_redirects": False, "stream": True}
            if (proxy or "").strip():
                kw["proxies"] = {"http": proxy.strip(), "https": proxy.strip()}
            resp = _cf.get(cur, **kw)
        except Exception:  # noqa: BLE001 - 网络类失败一律放弃兜底
            return "", ""
        code = int(getattr(resp, "status_code", 0) or 0)
        if code in (301, 302, 303, 307, 308):
            loc = (resp.headers.get("Location") or resp.headers.get("location") or "")
            nxt = _resolve_against(_clean_candidate(loc), cur)
            if not nxt:
                return "", ""
            cur = nxt
            continue
        if code != 200:
            return "", ""
        try:
            ctype = (resp.headers.get("Content-Type") or "").lower()
        except Exception:  # noqa: BLE001
            ctype = ""
        if "html" not in ctype and "text" not in ctype:
            return "", ""
        buf = b""
        try:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if should_stop is not None and should_stop():
                    return "", ""
                if not chunk:
                    break
                buf += chunk
                if len(buf) >= PAGE_MAX_BYTES:
                    break
        except Exception:  # noqa: BLE001
            if not buf:
                return "", ""
        return _decode_page(buf), cur
    return "", ""


def _looks_like_html(head: bytes, content_type: str) -> bool:
    """判断响应是不是网页：content-type 命中 html，或正文头就是 HTML 标签。"""
    if "html" in (content_type or "").lower():
        return True
    probe = (head or b"")[:512].lstrip().lower()
    return probe.startswith(b"<!doctype html") or probe.startswith(b"<html") \
        or b"<head" in probe[:256]


def _decode_page(raw: bytes) -> str:
    """网页字节 → 文本：优先 utf-8，乱码率过高按 GBK 解（老牌中文站）。

    图片 URL 本身是 ASCII，即便中文部分解错也不影响提取；这里只求「标签结构可读」。
    """
    text = (raw or b"").decode("utf-8", errors="replace")
    if text.count("\ufffd") > max(8, len(text) // 40):
        try:
            text = (raw or b"").decode("gbk", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    return text


def _clean_candidate(u: str) -> str:
    """清洗候选 URL：去空白 / 反转义 / 去 HTML 实体。"""
    u = (u or "").strip().strip("'").strip('"')
    u = u.replace("\\u002F", "/").replace("\\/", "/")
    u = u.replace("&amp;", "&")
    return u


def _resolve_against(u: str, base_url: str) -> str:
    """把协议相对（//host/x）与相对路径（/x）解析成绝对 URL。"""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith(("http://", "https://")):
        return u
    try:
        return urllib.parse.urljoin(base_url or "", u)
    except Exception:  # noqa: BLE001
        return ""


def _candidate_key(u: str) -> str:
    """同一张图的多个变体共用一个 key：忽略协议头（http/https 同图）与处理令牌。"""
    no_scheme = re.sub(r"^https?://", "", u or "", flags=re.I)
    return _XHS_TOKEN_RE.sub("", no_scheme)


def extract_page_images(html: str, base_url: str = "") -> list[str]:
    """从网页正文里提取可下载的图片候选（按可信度排序、去重、限量）。

    优先级：og:image（平台给分享场景准备的主图，几乎都是笔记/博文真实配图）
            → <img src> → srcset 里的最大图。
    过滤：data: URI、站点静态资源（logo/icon/js/css/字体）、SVG（页面图标多为 SVG）。
    小红书 CDN 的 !nd_prv（预览小图）在同图有 !nd_dft（默认大图）时丢弃。
    """
    text = html or ""
    found: list[str] = []

    def _add(u: str) -> None:
        u = _clean_candidate(u)
        if not u or u.lower().startswith("data:"):
            return
        u = _resolve_against(u, base_url)
        if not u or not u.lower().startswith(("http://", "https://")):
            return
        if _PAGE_JUNK_RE.search(u):
            return
        found.append(u)

    # ① og:image（property / name 两种写法都覆盖；content 可能在引号前后有空白）
    for m in re.finditer(
            r'<meta[^>]+(?:property|name)\s*=\s*["\']og:image["\'][^>]*>', text, re.I):
        tag = m.group(0)
        c = re.search(r'content\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if c:
            _add(c.group(1))
    # ② <img src=...>
    for m in re.finditer(r'<img[^>]+>', text, re.I):
        tag = m.group(0)
        s = re.search(r'\bsrc\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if s:
            _add(s.group(1))
    # ③ srcset（取每个候选里描述尺寸最大的那个）
    for m in re.finditer(r'srcset\s*=\s*["\']([^"\']+)["\']', text, re.I):
        best = ""
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            bits = part.split()
            if len(bits) >= 2 and bits[1].endswith("w"):
                try:
                    if int(bits[1][:-1]) >= 999 or not best:
                        best = bits[0]
                except ValueError:
                    if not best:
                        best = bits[0]
            elif not best:
                best = bits[0]
        if best:
            _add(best)

    # 去重 + 预览图降级：同 key 只保留一个，优先非预览（dft 大图）
    chosen: dict[str, str] = {}
    for u in found:
        key = _candidate_key(u)
        cur = chosen.get(key)
        if cur is None:
            chosen[key] = u
            continue
        if _IS_PREVIEW_RE.search(cur) and not _IS_PREVIEW_RE.search(u):
            chosen[key] = u          # 现有的是预览小图，换成默认大图
    return list(chosen.values())[:MAX_PAGE_IMAGES]


# --------------------------------------------------------------------------- #
# 核心传输
# --------------------------------------------------------------------------- #
class _Stopped(Exception):
    """内部用：should_stop 命中（用户取消 / 看门狗停滞 / 硬超时）。"""


def _decompressed_chunks(resp, chunk_size: int = CHUNK_SIZE, should_stop=None):
    """按 Content-Encoding 流式解压，产出**解压后**的块（identity 直通）。

    为什么需要：声明了 `Accept-Encoding: gzip, deflate`（浏览器特征，见
    _BROWSER_HEADERS 注释）之后，服务端真的会 gzip —— 不解压则魔数嗅探拿到的
    是 1f 8b 而不是 PNG/JPEG 头，会误判「不是图片」。

    should_stop 在**每次读之前**检查：单块响应（小图）读完即结束生成器，
    只在循环体里检查会漏掉「写块后取消」那一次（2026-10-12 回归教训）。
    """
    enc = (resp.headers.get("Content-Encoding") or "").lower().strip()
    dec = None
    if "gzip" in enc:
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif "deflate" in enc:
        dec = zlib.decompressobj(-zlib.MAX_WBITS)   # raw deflate
    if dec is None:
        while True:
            if should_stop is not None and should_stop():
                raise _Stopped()
            b = resp.read(chunk_size)
            if not b:
                return
            yield b
        return
    buf = b""
    while True:
        if should_stop is not None and should_stop():
            raise _Stopped()
        raw = resp.read(chunk_size)
        if not raw:
            try:
                buf += dec.flush()
            except zlib.error:  # noqa: BLE001 - 流被截断时能解多少算多少
                pass
            break
        buf += dec.decompress(raw)
        while len(buf) >= chunk_size:
            yield buf[:chunk_size]
            buf = buf[chunk_size:]
    if buf:
        yield buf


def fetch_image(url: str, dest: Path, *, max_bytes: int | None = None,
                referer: str = "", cookie: str = "", proxy: str = "",
                progress=None, should_stop=None) -> dict:
    """把 url 流式下载到 dest（调用方负责 .part → 最终名 的原子改名）。

    progress(downloaded, total) 与 should_stop() -> bool 两个回调供任务层接线
    （进度落库 / 协作式取消），核心传输本身不依赖任务框架，便于单测。
    返回 {"bytes", "content_type", "ext", "final_url"}。
    """
    limit = int(max_bytes or DEFAULT_MAX_BYTES)
    req = urllib.request.Request(url, method="GET")
    for _k, _v in _BROWSER_HEADERS.items():
        req.add_header(_k, _v)
    if referer:
        req.add_header("Referer", referer)
    if cookie:
        req.add_header("Cookie", cookie)

    opener = _build_opener(proxy)
    dest.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    head = b""
    first_raw = None
    ext = None
    content_type = ""
    final_url = url
    # urllib 的 timeout 会随 connect 落到 socket 上，对后续每次 read 同样生效
    with opener.open(req, timeout=IO_TIMEOUT) as resp:
        final_url = resp.geturl() or url
        content_type = (resp.headers.get("Content-Type") or "")
        # 有 Content-Encoding 时 Content-Length 是**压缩后**的大小，拿它做体积
        # 预检会低估；真正的上限由下面的「解压后字节数」把守。
        _enc = (resp.headers.get("Content-Encoding") or "").lower().strip()
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        if total and total > limit and not _enc:
            raise LinkError(
                f"图片体积 {total // 1024 // 1024}MB 超过上限 {limit // 1024 // 1024}MB",
                "可调大 VDL_IMAGE_MAX_BYTES 后重试（仅 desktop 端生效）")
        with open(dest, "wb") as fh:
            for chunk in _decompressed_chunks(resp, should_stop=should_stop):
                if should_stop is not None and should_stop():
                    raise _Stopped()
                if not chunk:
                    break
                if first_raw is None:
                    # 整块留底：确认是 HTML 时页面正文就在这一块里（head 只留 32B）
                    first_raw = chunk
                if len(head) < 32:
                    head += chunk[:32 - len(head)]
                if ext is None and len(head) >= 12:
                    ext = sniff_image_ext(head, content_type)
                    if ext is None:
                        # 不是图片：若返回的是网页（分享页 / 登录墙 / 错误页），
                        # 把正文读出来提取 og:image 等候选，让上层展开成子任务；
                        # 读到的 HTML 一律**不落盘**（_cleanup 由调用方兜底）。
                        if _looks_like_html(head, content_type):
                            page = first_raw or head
                            for more in _decompressed_chunks(
                                resp, should_stop=should_stop):
                                if should_stop is not None and should_stop():
                                    raise _Stopped()
                                if not more:
                                    break
                                page += more
                                if len(page) >= PAGE_MAX_BYTES:
                                    break
                            images = extract_page_images(
                                _decode_page(page), final_url)
                            if not images:
                                # urllib 拿到的可能是登录墙 / 验证页（TLS 指纹被
                                # WAF 识别）→ 用浏览器指纹再抓一次同一链接
                                alt_text, alt_final = _fetch_page_text(
                                    url, referer=referer, cookie=cookie,
                                    proxy=proxy, should_stop=should_stop)
                                if alt_text:
                                    images = extract_page_images(
                                        alt_text, alt_final or final_url)
                            raise PageLinkError(images, final_url)
                        raise LinkError(
                            "该链接返回的不是图片",
                            "请确认粘贴的是图片直链（右键图片「复制图片地址」）")
                downloaded += len(chunk)
                if downloaded > limit:
                    raise LinkError(
                        f"图片体积超过上限 {limit // 1024 // 1024}MB",
                        "可调大 VDL_IMAGE_MAX_BYTES 后重试（仅 desktop 端生效）")
                fh.write(chunk)
                if progress is not None:
                    try:
                        progress(downloaded, total)
                    except Exception:  # noqa: BLE001 - 进度回调异常绝不打断传输
                        pass
    if ext is None:   # 文件不足 12 字节（空响应 / 极短响应）
        ext = sniff_image_ext(head, content_type)
        if ext is None:
            raise LinkError("该链接返回的内容不是图片", "请换一个图片直链重试")
    return {"bytes": downloaded, "content_type": content_type,
            "ext": ext, "final_url": final_url}


# --------------------------------------------------------------------------- #
# 任务执行器（复用 DownloadTask / TaskStore 框架）
# --------------------------------------------------------------------------- #
# 常见「分享页」平台的落地提示：用户手里的链接形态 → 正确的图片地址怎么拿
_PLATFORM_HINTS = [
    (("xhslink.cn", "xiaohongshu.com", "小红书"), "小红书",
     "这是小红书笔记页面链接。请在 App 里打开该笔记 → 点开想存的图片 → 右下角"
     "「分享」→ 复制链接拿到的仍是页面链接；正确做法：长按图片选「复制图片地址」，"
     "或在浏览器打开笔记后右键图片「复制图片地址」再粘贴到这里。"),
    (("weibo.com", "weibo.cn", "微博"), "微博",
     "这是微博页面链接。请在浏览器打开该微博 → 在图片上右键「复制图片地址」后粘贴。"),
    (("zhihu.com", "知乎"), "知乎",
     "这是知乎页面链接。请在浏览器打开回答 → 在图片上右键「复制图片地址」后粘贴。"),
    (("bilibili.com", "b23.tv", "B站"), "B站",
     "这是 B 站页面链接。请在浏览器打开动态/专栏 → 在图片上右键「复制图片地址」后粘贴。"),
    (("douyin.com", "iesdouyin.com", "抖音"), "抖音",
     "这是抖音页面链接。请在浏览器打开 → 在图片上右键「复制图片地址」后粘贴。"),
]


def _platform_hint(url: str) -> str:
    """按链接主机给「怎么拿图片直链」的落地提示（认不出来返回空）。"""
    low = (url or "").lower()
    for hosts, _name, hint in _PLATFORM_HINTS:
        if any(h in low for h in hosts):
            return hint
    return ""


def run_image_download(task: DownloadTask, store: TaskStore,
                       max_bytes: int | None = None,
                       scheduler: Any = None) -> None:
    """后台线程执行：URL → SSRF-safe 流式落盘 → 落终态。全部异常写回任务，不向外抛。

    scheduler 可选：传入后，若链接打开的是**网页**（分享页），会把页面里提取到的
    图片逐张建成子任务提交调度，本任务变成「已展开 N 张」的汇总条目。
    """
    workdir = Path(task.workdir) if task.workdir else None
    if workdir is None:
        store.update(task.id, status="failed", error="任务缺少工作目录")
        return
    workdir.mkdir(parents=True, exist_ok=True)
    part = workdir / "image.part"
    started = time.time()
    last = {"bytes": 0, "ts": started}

    def _should_stop() -> bool:
        """读块边界的协作式取消：用户取消 / 看门狗停滞 / 整体硬超时，三种同入口。"""
        if task.is_finished:
            return True
        if task.cancel_requested:
            return True
        if time.time() - started > IMAGE_HARD_TIMEOUT:
            task.cancel_reason = "timeout"
            task.cancel_requested = True
            task.add_step("下载图片", "error",
                          f"超过硬上限 {IMAGE_HARD_TIMEOUT}s，已自动终止")
            task.log(f"下载超过整体硬上限 {IMAGE_HARD_TIMEOUT}s，自动终止")
            return True
        return False

    def _watchdog() -> None:
        """与视频下载同口径：downloading 中 N 秒无字节增量 → 判停滞，置取消标志。"""
        while not task.is_finished:
            time.sleep(WATCHDOG_POLL)
            if task.status != "downloading":
                continue
            if task.downloaded_bytes > last["bytes"]:
                last["bytes"] = task.downloaded_bytes
                last["ts"] = time.time()
            elif time.time() - last["ts"] > DOWNLOAD_STALL_TIMEOUT:
                task.cancel_reason = "stall"
                task.cancel_requested = True
                task.add_step("下载图片", "error",
                              f"停滞 {DOWNLOAD_STALL_TIMEOUT}s，已自动终止")
                task.log(f"下载停滞超过 {DOWNLOAD_STALL_TIMEOUT}s，自动终止")
                return

    store.update(task.id, status="downloading", progress=0.0, speed=0.0, eta=0)
    task.add_step("下载图片", "running", "连接中…")
    wd = threading.Thread(target=_watchdog, name=f"wd-img-{task.id}", daemon=True)
    wd.start()
    try:
        meta = fetch_image(
            task.url, part,
            max_bytes=max_bytes,
            referer=task.referer,
            proxy=task.proxy,
            progress=lambda done, total: _on_progress(store, task, done, total, started),
            should_stop=_should_stop,
        )
    except _Stopped:
        _cleanup(part)
        _finalize_terminated(store, task)
        return
    except PageLinkError as e:
        # 链接是网页（分享页 / 登录墙）：提取页面里的图片，逐张建成子任务
        _cleanup(part)
        imgs = [u for u in (e.images or [])][:MAX_PAGE_IMAGES]
        if not imgs:
            hint = _platform_hint(task.url) or (
                "页面里没有找到可下载的图片。若这是需要登录的页面，可在「高级选项」"
                "填写该站的 Cookie 后重试；或直接粘贴图片直链。")
            store.update(task.id, status="failed", error="网页里没有找到图片",
                         hint=hint)
            task.add_step("下载图片", "error", "网页里没有找到图片")
            task.log(f"页面无可下载图片：{task.url}")
            return
        created = []
        for idx, u in enumerate(imgs, start=1):
            try:
                child = store.create(
                    url=u, title='', platform='图片', quality='原图',
                    quality_key='origin', kind='image',
                    cookie=task.cookie, proxy=task.proxy,
                    referer=task.referer, owner_id=task.owner_id,
                    # 可读名（用户报「一堆文件夹我上哪找去」）：CDN 的 URL 末段是
                    # 哈希天书，落盘名与卡片标题都该是「图片-1」这种能认出来的。
                    name_hint=f"图片-{idx}",
                )
            except Exception:  # noqa: BLE001 - 单张建任务失败不影响其余
                continue
            created.append(child)
            if scheduler is not None:
                try:
                    scheduler.submit(run_image_download, child, store, None,
                                     scheduler)
                except Exception:  # noqa: BLE001 - 调度失败也保持任务可见（可重试）
                    pass
        if not created:
            store.update(task.id, status="failed", error="页面图片提取失败",
                         hint="请稍后重试，或直接粘贴图片直链")
            task.add_step("下载图片", "error", "页面图片提取失败")
            return
        names = len(created)
        store.update(task.id, status="completed", progress=100.0,
                     title=f"已展开 {names} 张图片", filename="", filesize=0,
                     downloaded_bytes=0, total_bytes=0,
                     children=[c.id for c in created])
        task.add_step("解析页面", "done",
                      f"从网页提取到 {names} 张图片，已逐张开始下载")
        task.log(f"分享页展开：{task.url} → {names} 张图片任务")
        return
    except LinkError as e:
        _cleanup(part)
        msg = getattr(e, "message", "") or str(e) or "下载失败"
        store.update(task.id, status="failed", error=msg,
                     hint=getattr(e, "hint", "") or "")
        task.add_step("下载图片", "error", msg)
        return
    except Exception as e:  # noqa: BLE001 - 网络类异常一律落任务态，不向外抛
        _cleanup(part)
        msg = str(e) or e.__class__.__name__
        hint = "网络波动或对方站点拒绝，请稍后重试"
        if isinstance(e, (socket.timeout, TimeoutError)):
            msg, hint = "连接超时", "对方站点响应过慢，请稍后重试或更换线路"
        elif isinstance(e, urllib.error.HTTPError):
            msg = f"对方返回 HTTP {getattr(e, 'code', 0)}"
            hint = "链接可能已过期或被防盗链拒绝（可尝试在高级选项填写来源页 Cookie）"
        store.update(task.id, status="failed", error=msg, hint=hint)
        task.add_step("下载图片", "error", msg)
        return

    # ---- 成功：.part → 最终名（同目录 rename，原子）----
    filename = derive_filename(meta["final_url"], "", meta["ext"], workdir,
                               name_hint=task.name_hint)
    final = workdir / filename
    try:
        part.rename(final)
    except OSError:
        _cleanup(part)
        store.update(task.id, status="failed", error="保存文件失败",
                     hint="磁盘权限或空间不足，请检查后重试")
        return
    dims = _probe_dimensions(final)
    quality = f"{dims[0]}×{dims[1]}" if dims else "原图"
    # 🔴 filepath 必须显式落库：/api/tasks/{id}/file 以「status==completed 且
    # filepath 存在」放行下载，漏了它用户看得到「已完成」却取不到文件
    # （2026-10-12 任务执行器实测踩过：filename/filesize 都写了、唯独漏它）。
    store.update(task.id, status="completed", progress=100.0,
                 filename=final.name, filesize=meta["bytes"],
                 title=final.name, quality=quality,
                 downloaded_bytes=meta["bytes"], total_bytes=meta["bytes"],
                 filepath=final)
    task.add_step("下载图片", "done", f"{final.name}（{_human(meta['bytes'])}）")
    task.log(f"图片下载完成：{final.name} {meta['bytes']}B")


def _on_progress(store: TaskStore, task: DownloadTask, done: int, total: int,
                 started: float) -> None:
    """进度回调：字节 / 百分比 / 速率 / ETA 一并落库（节流由 store.update 内部做）。"""
    elapsed = max(0.001, time.time() - started)
    speed = done / elapsed
    eta = int((total - done) / speed) if total and speed > 0 else 0
    store.update(task.id, status="downloading", downloaded_bytes=done,
                 total_bytes=total or task.total_bytes,
                 progress=(done / total * 100.0) if total else task.progress,
                 speed=round(speed, 1), eta=eta)


def _finalize_terminated(store: TaskStore, task: DownloadTask) -> None:
    """取消落终态：用户取消 / 看门狗停滞 / 硬超时，三种原因给不同文案。

    与 downloader._finalize_terminated 同思路（按 cancel_reason 分流），但这里是
    图片任务的独立副本 —— 图片没有合并阶段，终态集合只有 completed/failed/canceled。
    """
    reason = task.cancel_reason or "user"
    if reason == "stall":
        store.update(task.id, status="failed", error="下载停滞，已自动终止",
                     hint=f"连接超过 {DOWNLOAD_STALL_TIMEOUT}s 没有任何进展（站点/网络假死）")
    elif reason == "timeout":
        store.update(task.id, status="failed", error="下载超时",
                     hint="对方站点响应过慢，请稍后重试或更换线路")
    else:
        store.update(task.id, status="canceled", error="已取消下载")


def _cleanup(part: Path) -> None:
    try:
        if part.exists():
            part.unlink()
    except OSError:
        pass


def _probe_dimensions(path: Path) -> tuple[int, int] | None:
    """读取图片宽高（PIL 随 App 打包；失败不影响下载结果）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001
        return None


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"
