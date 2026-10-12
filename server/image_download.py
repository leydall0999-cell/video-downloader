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
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

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
                    workdir: Path | None = None) -> str:
    """推导落盘文件名：Content-Disposition > URL 末段 > image，扩展名以魔数为准。

    同名冲突时自动加序号（image.jpg → image-1.jpg），避免批量下载互相覆盖。
    """
    cd_name = _sanitize(_name_from_content_disposition(content_disposition))
    url_name = ""
    try:
        path = urllib.parse.urlparse(final_url).path
        url_name = _sanitize(urllib.parse.unquote(path.rsplit("/", 1)[-1]))
    except Exception:  # noqa: BLE001
        url_name = ""
    name = cd_name or url_name or "image"
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
# 核心传输
# --------------------------------------------------------------------------- #
class _Stopped(Exception):
    """内部用：should_stop 命中（用户取消 / 看门狗停滞 / 硬超时）。"""


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
    req.add_header("User-Agent", _UA)
    req.add_header("Accept", "image/avif,image/webp,image/apng,image/*,*/*;q=0.8")
    if referer:
        req.add_header("Referer", referer)
    if cookie:
        req.add_header("Cookie", cookie)

    opener = _build_opener(proxy)
    dest.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    head = b""
    ext = None
    content_type = ""
    final_url = url
    # urllib 的 timeout 会随 connect 落到 socket 上，对后续每次 read 同样生效
    with opener.open(req, timeout=IO_TIMEOUT) as resp:
        final_url = resp.geturl() or url
        content_type = (resp.headers.get("Content-Type") or "")
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        if total and total > limit:
            raise LinkError(
                f"图片体积 {total // 1024 // 1024}MB 超过上限 {limit // 1024 // 1024}MB",
                "可调大 VDL_IMAGE_MAX_BYTES 后重试（仅 desktop 端生效）")
        with open(dest, "wb") as fh:
            while True:
                if should_stop is not None and should_stop():
                    raise _Stopped()
                chunk = resp.read(CHUNK_SIZE)
                if not chunk:
                    break
                if len(head) < 32:
                    head += chunk[:32 - len(head)]
                if ext is None and len(head) >= 12:
                    ext = sniff_image_ext(head, content_type)
                    if ext is None:
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
def run_image_download(task: DownloadTask, store: TaskStore,
                       max_bytes: int | None = None) -> None:
    """后台线程执行：URL → SSRF-safe 流式落盘 → 落终态。全部异常写回任务，不向外抛。"""
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
    filename = derive_filename(meta["final_url"], "", meta["ext"], workdir)
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
