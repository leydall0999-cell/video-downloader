"""云盘集成：把已下载的文件存到用户自己的网盘（WebDAV / 百度网盘）。

设计要点：
- 全部为「用户自己的网盘」——服务端只做临时代理上传，不留存、不托管他人内容。
- WebDAV 零额外依赖（仅 requests），任意 Nextcloud / 群晖 / 自建 WebDAV 均可。
- 百度网盘走官方 OAuth2 授权码流程，需部署者自备百度开放平台应用
  （VDL_BAIDU_APP_KEY / VDL_BAIDU_APP_SECRET / VDL_BAIDU_REDIRECT_URI）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

logger = logging.getLogger("vdl.cloud")


# --------------------------------------------------------------------------- #
# aria2c 工具（供百度网盘下载走并发拉取；与 downloader._aria2c_path 同源逻辑，
# 但放在本模块以避免跨模块循环 import）
# --------------------------------------------------------------------------- #

def _aria2c_path() -> str | None:
    """返回 aria2c 可执行路径；未安装返回 None（调用方回退 requests 流式下载）。

    查找顺序：PATH（本机 brew/apt 安装）→ 打包内置（PyInstaller 冻结的 _MEIPASS/bin/aria2c，
    或 macOS .app 的 Contents/Resources/bin/aria2c）。
    """
    found = shutil.which("aria2c")
    if found:
        return found
    candidates: list[str] = []
    meipass = os.environ.get("MEIPASS") or getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(os.path.join(meipass, "bin", "aria2c"))  # type: ignore[arg-type]
    exe = sys.executable or ""
    if "Contents/MacOS" in exe:
        # .../Contents/MacOS/VideoDownloader -> ../Resources/bin/aria2c
        candidates.append(os.path.join(os.path.dirname(exe), "..", "Resources", "bin", "aria2c"))
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def _aria2c_download(
    dlink: str,
    dest: Path,
    total: int,
    concurrency: int = 8,
    progress=None,
    timeout: int = 1800,
) -> int:
    """用 aria2c 并发拉取 dlink 到 dest，返回写入字节数。

    进度通过后台线程轮询 dest 当前大小回报（aria2c 边下边落盘）。
    断点续传：dest 已存在部分内容时 --continue=true 续传。
    注意：百度对 dlink 仍按账号等级限速，aria2c 多连接无法突破服务端限速，
    仅能提升小文件并发与利用续传；大文件免费账号仍可能被限。
    """
    a2 = _aria2c_path()
    if not a2:
        raise CloudError(
            "本机未安装 aria2c",
            "请 brew install aria2 或由打包版提供；已自动回退 requests 流式下载",
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        a2,
        "--dir", str(dest.parent),
        "--out", dest.name,
        "-x", str(concurrency),       # 每服务器最大连接数
        "-s", str(concurrency),       # 分片数
        "-k", "4M",                   # 分块 4MB（大文件分片更粗，配合 -s 16 提升并发效率）
        "--continue=true",
        "--summary-interval=0",       # 关掉周期摘要，靠文件大小轮询进度
        "--connect-timeout=30",
        "--timeout=300",
        "--max-tries=5",
        "--retry-wait=3",
        "--user-agent", "pan.baidu.com",
        # 百度 dlink 必须用「像样的」UA 请求，否则在鉴权跳转里 403/死循环
        "--header", "User-Agent: pan.baidu.com",
        dlink,
    ]

    stop = threading.Event()
    written = [0]

    def _poll() -> None:
        while not stop.is_set():
            try:
                if dest.exists():
                    sz = dest.stat().st_size
                    written[0] = sz
                    if progress:
                        progress(sz, total or sz)
            except OSError:
                pass
            stop.wait(0.5)

    poller = threading.Thread(target=_poll, daemon=True)
    poller.start()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stop.set()
        raise CloudError("aria2c 下载超时", str(exc)) from exc
    finally:
        stop.set()
        poller.join(timeout=2)

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[-2000:]
        raise CloudError("aria2c 下载失败", f"exit={proc.returncode} {err}")
    final = dest.stat().st_size
    if progress:
        progress(final, total or final)
    return final


class CloudError(Exception):
    """云盘上传失败（含用户凭据错误、网络错误、服务端拒绝）。"""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


def _slice_size() -> int:
    return 4 * 1024 * 1024  # 百度分片上传标准 4MB


def _md5_of(chunk: bytes) -> str:
    return hashlib.md5(chunk).hexdigest()


class _ProgressReader:
    """包装文件对象，边读边回报进度（供 requests PUT 流式上传）。"""

    def __init__(self, path: Path, total: int, progress):
        self.fp = open(path, "rb")
        self.total = total
        self.progress = progress
        self.sent = 0

    def __len__(self) -> int:
        return self.total

    def read(self, n: int = -1) -> bytes:
        data = self.fp.read(n)
        if data:
            self.sent += len(data)
            if self.progress:
                self.progress(self.sent, self.total)
        return data

    def close(self) -> None:
        self.fp.close()


# --------------------------------------------------------------------------- #
# WebDAV
# --------------------------------------------------------------------------- #

def _safe_rel_path(dest_path: str, fallback_name: str) -> str | None:
    """安全规范化用户给定的目标路径，杜绝 '..' 穿越与绝对路径跳出服务根。

    返回相对路径段（用 '/' 连接）；为空或仅含 '.'/'..' 时返回 None，调用方应回退到 fallback_name。
    不保留前导/尾随斜杠，也不允许任何段为 '..' —— 这样无论 WebDAV 还是百度，
    文件都只能落在用户网盘根目录之下，无法覆盖根外的既有文件。
    """
    if not dest_path or not dest_path.strip():
        return None
    parts = [p for p in dest_path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts) or None


class WebDAVProvider:
    name = "webdav"

    @staticmethod
    def _join(base_url: str, path: str) -> str:
        return base_url.rstrip("/") + "/" + path.lstrip("/")

    def _mkdir_p(self, session: requests.Session, base_url: str, dir_path: str) -> None:
        """递归创建远程目录集合（WebDAV 无 mkdir -p，需逐级 MKCOL）。"""
        segments = [s for s in dir_path.strip("/").split("/") if s]
        current = base_url.rstrip("/")
        for seg in segments:
            current = current + "/" + seg
            try:
                resp = session.request("MKCOL", current, timeout=30)
            except requests.RequestException as exc:
                raise CloudError(f"创建远程目录失败：{current}", str(exc)) from exc
            code = resp.status_code
            if code in (201, 200, 204, 405, 409):
                # 201 已建 / 405 已存在 / 409 并发已存在，均视为成功
                continue
            raise CloudError(f"创建远程目录失败（HTTP {code}）", current)

    def upload(self, local_path: Path, dest_path: str, creds: dict, progress=None) -> str:
        base_url = (creds.get("url") or "").strip().rstrip("/")
        if not base_url:
            raise CloudError("WebDAV 地址为空", "请在设置中填写完整的 WebDAV 地址")
        norm = _safe_rel_path(dest_path, local_path.name)
        dest_path = (norm or local_path.name).lstrip("/")  # base 已含用户根（如 Nextcloud 的 .../dav/files/user），过滤 '..' 防穿越

        session = requests.Session()
        user = (creds.get("user") or "").strip()
        pwd = creds.get("pass") or ""
        if user:
            session.auth = (user, pwd)

        parent = "/".join(dest_path.split("/")[:-1])
        if parent:
            self._mkdir_p(session, base_url, parent)

        target_url = self._join(base_url, dest_path)
        total = local_path.stat().st_size
        reader = _ProgressReader(local_path, total, progress)
        try:
            resp = session.put(
                target_url,
                data=reader,
                headers={"Content-Type": "application/octet-stream"},
                timeout=1800,
            )
        except requests.RequestException as exc:
            raise CloudError("上传到 WebDAV 失败", str(exc)) from exc
        finally:
            reader.close()
        if resp.status_code not in (201, 204, 200):
            raise CloudError(f"WebDAV 上传失败（HTTP {resp.status_code}）", target_url)
        return dest_path


# --------------------------------------------------------------------------- #
# 百度网盘（官方 OAuth2 + xpan/file 分片上传）
# --------------------------------------------------------------------------- #

class BaiduProvider:
    name = "baidu"
    # 百度网盘开放平台（pan.baidu.com/union）创建的应用，
    # OAuth 授权/换令牌端点统一使用 openapi.baidu.com（官方文档明确）。
    # API 调用端点用 pan.baidu.com/rest/2.0/xpan/file
    # ⚠️ device_id（=AppID）是必需参数，缺省会导致 invalid_client 或跳转首页
    OAUTH_BASE = "https://openapi.baidu.com"
    PAN_API = "https://pan.baidu.com/rest/2.0/xpan/file"

    def __init__(self) -> None:
        # 缓存 share_meta（verify+list 结果），避免下载时重复 verify 被百度限频
        # key: (surl, pwd, sub_dir) → value: dict (sekey, share_id, uk, items, session)
        self._share_meta_cache: dict[tuple[str, str, str], dict] = {}

    def upload(self, local_path: Path, dest_path: str, creds: dict, progress=None) -> str:
        token = (creds.get("token") or "").strip()
        if not token:
            raise CloudError("未授权百度网盘", "请先在弹窗中完成百度账号授权")
        norm = _safe_rel_path(dest_path, local_path.name)
        dest_path = "/" + (norm or local_path.name)  # 百度以 '/' 为根，过滤 '..' 防穿越
        size = local_path.stat().st_size

        # 1) 计算每块（4MB）md5
        slice_size = _slice_size()
        block_list: list[str] = []
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(slice_size)
                if not chunk:
                    break
                block_list.append(_md5_of(chunk))
        if not block_list:
            raise CloudError("文件为空，无法上传", "")

        # 2) 预创建：拿到 uploadid；命中秒传则直接完成
        pre = requests.post(
            self.PAN_API,
            params={"method": "precreate", "access_token": token},
            data={
                "path": dest_path,
                "size": str(size),
                "isdir": "0",
                "autoinit": "1",
                "block_list": json.dumps(block_list),
                "rtype": "1",  # 重名覆盖
            },
            timeout=60,
        ).json()
        if pre.get("errno", 0) != 0:
            raise CloudError("百度网盘预创建失败", f"errno={pre.get('errno')} {pre.get('errmsg', '')}")
        if pre.get("return_type") == 1:
            return dest_path  # 秒传命中，文件已落盘

        upload_id = pre.get("uploadid") or pre.get("uploadId")
        if not upload_id:
            raise CloudError("百度网盘未返回 uploadid", str(pre))

        # 3) 逐块上传（type=tmpfile）
        with open(local_path, "rb") as f:
            for partseq in range(len(block_list)):
                chunk = f.read(slice_size)
                if not chunk:
                    break
                up = requests.post(
                    self.PAN_API,
                    params={
                        "method": "upload",
                        "access_token": token,
                        "type": "tmpfile",
                        "path": dest_path,
                        "uploadid": upload_id,
                        "partseq": str(partseq),
                    },
                    files={"file": (f"{partseq}.dat", chunk, "application/octet-stream")},
                    timeout=1800,
                ).json()
                if up.get("errno", 0) != 0:
                    raise CloudError("百度网盘分片上传失败", f"partseq={partseq} errno={up.get('errno')}")
                if progress:
                    progress(min((partseq + 1) * slice_size, size), size)

        # 4) 合并文件
        create = requests.post(
            self.PAN_API,
            params={"method": "create", "access_token": token},
            data={
                "path": dest_path,
                "size": str(size),
                "isdir": "0",
                "uploadid": upload_id,
                "block_list": json.dumps(block_list),
                "rtype": "1",
            },
            timeout=60,
        ).json()
        if create.get("errno", 0) != 0:
            raise CloudError("百度网盘合并文件失败", f"errno={create.get('errno')} {create.get('errmsg', '')}")
        return dest_path

    # ------------------------------------------------------------------ #
    # 下载：从用户自己的网盘把文件拉回本机（官方 PCS，速度由账号等级决定）
    # ------------------------------------------------------------------ #

    def list_files(self, token: str, dir_path: str = "/", page: int = 1, limit: int = 200) -> dict:
        """列出用户网盘某目录下的文件（含子目录）。返回百度原始 JSON。"""
        if not token:
            raise CloudError("未授权百度网盘", "请先完成百度账号授权")
        params = {
            "method": "list",
            "access_token": token,
            "dir": dir_path or "/",
            "order": "time",
            "desc": "1",
            "limit": str(min(max(int(limit), 1), 1000)),
            "page": str(max(int(page), 1)),
        }
        try:
            resp = requests.get(self.PAN_API, params=params, timeout=30)
        except requests.RequestException as exc:
            raise CloudError("列出百度网盘文件失败", str(exc)) from exc
        data = resp.json()
        if data.get("errno", 0) not in (0, None):
            raise CloudError("列出百度网盘文件失败", f"errno={data.get('errno')} {data.get('errmsg', '')}")
        return data

    def download_url(self, token: str, fs_id: int, path: str) -> dict:
        """换取单个文件的下载直链 dlink（短时效，请求时须带正确 UA）。"""
        if not token:
            raise CloudError("未授权百度网盘", "请先完成百度账号授权")
        try:
            resp = requests.get(
                self.PAN_API,
                params={
                    "method": "download",
                    "access_token": token,
                    "fid": str(fs_id),
                    "path": path,
                },
                timeout=30,
            )
        except requests.RequestException as exc:
            raise CloudError("获取百度网盘下载链接失败", str(exc)) from exc
        data = resp.json()
        if data.get("errno", 0) != 0 or not data.get("dlink"):
            raise CloudError(
                "获取百度网盘下载链接失败",
                f"errno={data.get('errno')} {data.get('errmsg', '')} {data.get('error_msg', '')}",
            )
        return data

    def download(self, token: str, fs_id: int, path: str, local_path: Path, progress=None, backend: str = "auto") -> int:
        """把网盘文件下载到本地 local_path，回报进度（已下载字节 / 总字节）。返回写入字节数。

        backend:
          - "auto"（默认）：优先 aria2c 并发拉取，缺失则自动回退 requests 流式。
          - "aria2c"：强制走 aria2c，缺失则报错。
          - "requests"：强制走 requests 流式（单连接）。

        注意：免费账号大文件常被百度服务端限速或要求「提速」（会员），这部分速度由账号等级决定，
        本方法只做合规的官方下载，不绕过平台限速；aria2c 多连接同样无法突破服务端限速。
        """
        info = self.download_url(token, fs_id, path)
        dlink = info["dlink"]
        total = int(info.get("size") or 0)

        if backend in ("auto", "aria2c") and _aria2c_path() is not None:
            try:
                return _aria2c_download(dlink, local_path, total, concurrency=16, progress=progress)
            except CloudError:
                if backend == "aria2c":
                    raise
                logger.warning("aria2c 不可用，回退 requests 流式下载百度网盘文件")

        # requests 流式（单连接）兜底
        # 百度 dlink 必须用「像样的」UA 请求，否则会在鉴权跳转里 403/死循环
        headers = {"User-Agent": "pan.baidu.com"}
        try:
            resp = requests.get(dlink, headers=headers, stream=True, timeout=30, allow_redirects=True)
        except requests.RequestException as exc:
            raise CloudError("百度网盘下载请求失败", str(exc)) from exc
        if resp.status_code != 200:
            raise CloudError(
                "百度网盘下载被拒绝",
                f"HTTP {resp.status_code}（免费账号大文件可能需开通会员提速）",
            )
        local_path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                written += len(chunk)
                if progress:
                    progress(written, total or written)
        return written

    # ------------------------------------------------------------------ #
    # 分享链接下载：列出分享内容 → 转存到自己网盘 → 从自己网盘下载（官方通道）
    # ------------------------------------------------------------------ #

    def _parse_share_surl(self, share_url: str) -> str:
        """从分享链接中提取 surl（短链核心码）。支持 /s/xxx 与 ?surl=xxx 两种形式。

        关键：百度分享短链 /s/1xxx 的「1」是类型前缀（1=普通分享、5=知识分享、7=三方加密），
        API 调用必须用去掉前缀后的核心码，否则 verify/list 会报 errno=2「链接出错了」。
        """
        from urllib.parse import urlparse, parse_qs

        u = urlparse((share_url or "").strip())
        if u.query:
            qs = parse_qs(u.query)
            if qs.get("surl"):
                return qs["surl"][0].strip()
        # 必须来自 pan.baidu.com 的 /s/ 或 /share/ 短链，避免把脏串当 surl
        if "baidu" in (u.netloc or "").lower():
            parts = [p for p in u.path.split("/") if p]
            for i, p in enumerate(parts):
                if p in ("s", "share", "shareInit", "share_init") and i + 1 < len(parts):
                    return _strip_surl_prefix(parts[i + 1].strip())
            if parts:
                return _strip_surl_prefix(parts[-1].strip())
        raise CloudError("无法解析分享链接", "请检查链接格式，应为 https://pan.baidu.com/s/... 形式")

    def _share_session(self, surl: str):
        """创建带 UA + Referer 的 Session，先访问分享页拿 BAIDUID cookie。

        百度 share/verify 依赖 Referer 头 + 分享页 cookie，缺一不可（否则 errno=2）。
        """
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Referer": f"https://pan.baidu.com/s/1{surl}",
        })
        try:
            s.get(f"https://pan.baidu.com/s/1{surl}", timeout=15)
        except requests.RequestException:
            pass  # cookie 拿不到不致命，verify 仍可能成功
        return s

    def _share_verify(self, surl: str, pwd: str, session=None) -> str:
        """验证分享提取码，返回 sekey（randsk）。"""
        s = session or self._share_session(surl)
        params = {"t": str(int(time.time() * 1000)), "surl": surl}
        try:
            resp = s.post(
                BAIDU_SHARE_API + "/verify",
                params=params,
                data={"pwd": pwd or "", "vcode": "", "vcode_str": ""},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise CloudError("验证分享提取码失败", str(exc)) from exc
        data = resp.json()
        errno = data.get("errno", 0)
        if errno != 0:
            show = data.get("show_msg") or data.get("err_msg") or ""
            msg = {
                -12: "提取码错误",
                105: "提取码错误",
                -9: "分享链接不存在或已失效",
            }.get(errno, f"验证提取码失败(errno={errno})")
            raise CloudError(msg, str(show))
        return data.get("randsk", "")

    def _share_meta(self, surl: str, pwd: str, sub_dir: str = "") -> dict:
        """verify + list，返回 {sekey, share_id, uk, items, session}（transfer 依赖这些）。

        sub_dir: 分享内的子目录路径（如 "/子文件夹"），空字符串=根目录。
        结果会缓存（同 surl+pwd+sub_dir 不重复 verify），避免下载时被百度限频。
        """
        # ── 缓存命中：直接返回，不再重复 verify ─────────────────────
        cache_key = (surl, pwd, sub_dir)
        cached = self._share_meta_cache.get(cache_key)
        if cached is not None:
            return cached

        s = self._share_session(surl)
        sekey = self._share_verify(surl, pwd, session=s)
        params = {
            "shorturl": surl,
            "sekey": sekey,
            "root": "1" if not sub_dir else "0",
            "dir": sub_dir or "",
            "page": "1",
            "num": "100",
            "order": "time",
            "desc": "1",
        }
        try:
            resp = s.get(BAIDU_SHARE_API + "/list", params=params, timeout=30)
        except requests.RequestException as exc:
            raise CloudError("获取分享文件列表失败", str(exc)) from exc
        data = resp.json()
        errno = data.get("errno", 0)
        if errno != 0:
            # 百度分享接口错误提示在 show_msg 字段（"啊哦，链接出错了"等），errmsg 可能为空
            show = data.get("show_msg") or data.get("errmsg") or ""
            msg = {
                -9: "分享链接不存在或已失效",
                -12: "提取码错误",
                -10: "分享已被取消",
                -1: "分享链接无效",
                2: "分享链接已失效或不存在",
            }.get(errno, f"获取分享列表失败(errno={errno})")
            raise CloudError(msg, str(show))
        result = {
            "sekey": sekey,
            "share_id": data.get("share_id"),
            "uk": data.get("uk"),
            "items": data.get("list") or [],
            "session": s,
        }
        # 写入缓存（下载时复用，避免重复 verify 被限频）
        self._share_meta_cache[cache_key] = result
        return result

    def share_list(self, share_url: str, pwd: str = "", sub_dir: str = "") -> dict:
        """列出分享链接里的文件（看分享内容无需登录；转存才需 token）。返回归一化列表。

        sub_dir: 分享内的子目录路径（如 "/子文件夹"），空字符串=根目录（用于点文件夹展开）。
        """
        surl = self._parse_share_surl(share_url)
        meta = self._share_meta(surl, pwd, sub_dir=sub_dir)
        files = [
            {
                "fs_id": it.get("fs_id"),
                "path": it.get("path"),
                "name": it.get("server_filename") or it.get("filename") or "",
                "size": it.get("size", 0),
                "isdir": bool(int(it.get("isdir") or 0)) and not _looks_like_file(it.get("server_filename") or ""),
            }
            for it in meta["items"]
        ]
        files.sort(key=lambda x: (not x["isdir"], -(int(x.get("size") or 0) or 0)))
        return {
            "surl": surl,
            "list": files,
            "has_more": False,
            "share_id": meta["share_id"],
            "uk": meta["uk"],
            "sekey": meta["sekey"],
            "sub_dir": sub_dir,
        }

    def _ensure_dest_dir(self, token: str, dest: str) -> list:
        """确保转存目标目录存在（百度 transfer 要求 dest 路径已创建）。返回创建日志。"""
        logs = []
        if not dest or not token:
            return logs
        segments = [s for s in dest.strip("/").split("/") if s]
        current = ""
        for seg in segments:
            current = current + "/" + seg
            try:
                resp = requests.post(
                    self.PAN_API,
                    params={"method": "create", "access_token": token},
                    data={"path": current, "size": "0", "type": "directory", "isdir": "1"},
                    timeout=30,
                )
                d = resp.json()
                errno = d.get("errno", 0)
                logs.append(f"mkdir {current} → errno={errno} {d.get('errmsg','')}")
                if errno not in (0, -8):
                    pass  # 非致命
            except Exception as exc:
                logs.append(f"mkdir {current} → EXCEPTION: {exc}")
        return logs

    def share_transfer(self, share_url: str, pwd: str, paths: list, dest: str, token: str, sub_dir: str = "") -> list:
        """把分享里的文件转存到用户自己的网盘 dest 目录，返回转存后的 [{fs_id, path}]。

        sub_dir: 分享内子目录（如 "/极简风格(36)"）；列子目录才能找到对应 fs_id。
        """
        if not token:
            raise CloudError("未授权百度网盘", "请先完成百度账号授权")
        surl = self._parse_share_surl(share_url)
        meta = self._share_meta(surl, pwd, sub_dir=sub_dir)
        dest = dest or _baidu_share_dest("VideoDownloader_Share")
        if not dest.startswith("/"):
            dest = "/" + dest
        # ★ 关键：先确保目标目录存在（否则 transfer 报「路径不存在」）
        mkdir_logs = self._ensure_dest_dir(token, dest)
        # 把要转存的 path 映射到 fs_id（transfer 接口用 fsidlist，不是 path/filelist）
        by_path = {it.get("path"): it.get("fs_id") for it in meta["items"]}
        fsids: list = []
        for p in paths:
            fid = by_path.get(p)
            if fid is not None:
                fsids.append(fid)
                continue
            for it in meta["items"]:  # 回退：按文件名匹配
                if (it.get("server_filename") or "") == p:
                    fsids.append(it.get("fs_id"))
                    break
        if not fsids:
            raise CloudError("未找到要转存的文件", "请重新列出分享内容后再试")
        try:
            resp = meta["session"].post(
                BAIDU_SHARE_API + "/transfer",
                params={
                    "shareid": str(meta["share_id"] or ""),
                    "from": str(meta["uk"] or ""),
                    "sekey": meta["sekey"],
                    "channel": "chunlei",
                    "clienttype": "0",
                    "web": "1",
                    "access_token": token,
                },
                data={
                    "fsidlist": json.dumps(fsids, ensure_ascii=False),
                    "path": dest,
                },
                timeout=60,
            )
        except requests.RequestException as exc:
            raise CloudError("转存分享文件失败", str(exc)) from exc
        data = resp.json()
        errno = data.get("errno", 0)
        if errno != 0:
            show = data.get("show_msg") or data.get("errmsg") or ""
            # 详细诊断信息（不再隐藏原始错误）
            diag = f" | dest={dest} | fsids={fsids} | mkdir={'; '.join(mkdir_logs) if mkdir_logs else 'N/A'}"
            # 如果 dest 在 /apps/ 下且失败，fallback 到根目录重试一次
            if dest.startswith("/apps/") and not getattr(self, '_share_transfer_fallback', False):
                self._share_transfer_fallback = True
                simple_dest = "/VideoDownloader_Share"
                try:
                    fallback_logs = self._ensure_dest_dir(token, simple_dest)
                    resp2 = meta["session"].post(
                        BAIDU_SHARE_API + "/transfer",
                        params={
                            "shareid": str(meta["share_id"] or ""),
                            "from": str(meta["uk"] or ""),
                            "sekey": meta["sekey"],
                            "channel": "chunlei",
                            "clienttype": "0",
                            "web": "1",
                            "access_token": token,
                        },
                        data={"fsidlist": json.dumps(fsids, ensure_ascii=False), "path": simple_dest},
                        timeout=60,
                    )
                    d2 = resp2.json()
                    if d2.get("errno", 0) == 0:
                        transferred = (d2.get("extra") or {}).get("list") or []
                        result = [{"fs_id": it.get("fs_id"), "path": it.get("path")} for it in transferred]
                        if not result:
                            result = [{"fs_id": None, "path": simple_dest.rstrip("/") + "/" + os.path.basename(p)} for p in paths]
                        self._share_transfer_fallback = False
                        return result
                except Exception:
                    pass
                self._share_transfer_fallback = False
                diag += f" | fallback_dest={simple_dest} 也失败"
            msg = {
                -9: "分享链接不存在或已失效",
                -12: "提取码错误",
                -30: "文件已在网盘中存在（请更换目标目录或文件名）",
                -70: "网盘容量不足，无法转存",
                2: "转存失败（百度返回errno=2，可能原因：目标目录不存在/分享已被标记/应用无权写入该路径）",
            }.get(errno, f"转存失败(errno={errno})")
            raise CloudError(msg + diag, f"百度原始: errno={errno} show_msg={show}")
        transferred = (data.get("extra") or {}).get("list") or []
        result = [{"fs_id": it.get("fs_id"), "path": it.get("path")} for it in transferred]
        if not result:
            # 兜底：extra.list 为空时按 dest+文件名构造（后续 download 会再查真实 fs_id）
            result = [
                {"fs_id": None, "path": dest.rstrip("/") + "/" + os.path.basename(p)}
                for p in paths
            ]
        return result

    def _share_dlink(self, meta: dict, fs_id: int, share_url: str = "") -> str:
        """从分享获取文件下载直链（高速通道）。

        多级策略（按可靠性排序）：
          策略 A: BDUSS Cookie + /api/sharedownload（最稳定，需用户配置 Cookie）
          策略 B: 页面 HTML 提取 sign + /api/sharedownload（SPA 页面可能失败）
          策略 C: 兜底报错

        拿到 dlink 后配合 aria2c -x16 多线程即可跑满带宽。
        """
        import re
        import time

        share_id = meta["share_id"]
        uk = meta["uk"]
        sekey = meta.get("sekey", "")

        # ── 尝试策略 A：BDUSS Cookie 方式 ──────────────────────────────
        # 优先用前端传入的 BDUSS（meta），其次读本地配置
        bduss = (meta.get("bduss") or "").strip() or self._get_bduss()
        if bduss:
            try:
                return self._share_dlink_bduss(bduss, share_id, uk, fs_id,
                                                sekey, share_url)
            except CloudError as exc:
                logger.warning("BDUSS dlink 失败: %s，尝试页面提取", exc)

        # ── 策略 B：从分享页 HTML 提取 sign/timestamp ─────────────────
        try:
            return self._share_dlink_from_page(meta, fs_id, share_url,
                                                share_id, uk, sekey)
        except CloudError as exc:
            logger.warning("页面提取 dlink 失败: %s", exc)

        # ── 全部失败 ───────────────────────────────────────────────────
        raise CloudError(
            "无法获取下载直链",
            "所有获取方式均失败。如需高速下载，请在设置中配置百度网盘 BDUSS Cookie "
            "（浏览器 F12 → Application → Cookies → BDUSS 值）",
        )

    def _get_bduss(self) -> str | None:
        """返回用户配置的百度 BDUSS Cookie；未配置返回 None。

        优先从环境变量 VDL_BAIDU_BDUSS 读取，
        其次从 ~/.vdl/baidu_bduss.txt 文件读取（首行去空白）。
        """
        import os
        # 环境变量
        env = (os.environ.get("VDL_BAIDU_BDUSS") or "").strip()
        if env:
            return env
        # 文件
        p = Path.home() / ".vdl" / "baidu_bduss.txt"
        try:
            if p.exists():
                return p.read_text("utf-8").strip().splitlines()[0].strip()
        except OSError:
            pass
        return None

    def _share_dlink_bduss(self, bduss: str, share_id: int, uk: int,
                             fs_id: int, sekey: str, share_url: str) -> str:
        """用 BDUSS Cookie 调用 /api/sharedownload 获取直链（最稳定方案）。"""
        import base64

        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/131.0.0.0 Safari/537.36",
            "Referer": share_url or f"https://pan.baidu.com/s/1{share_id}",
            "Origin": "https://pan.baidu.com",
        })
        # 注入 BDUSS Cookie（核心：模拟浏览器登录态）
        s.cookies.set("BDUSS", bduss, domain=".baidu.com")

        # 先访问一次分享页建立会话
        if share_url:
            try:
                s.get(share_url, timeout=15,
                      headers={"Accept-Encoding": "identity"})
            except requests.RequestException:
                pass

        timestamp = str(int(time.time() * 1000))

        # 调用内部 API
        resp = s.post(
            "https://pan.baidu.com/api/sharedownload",
            params={"sign": "", "timestamp": timestamp},
            data={
                "encrypt": 0,
                "product": "share",
                "uk": str(uk),
                "primaryid": fs_id,
                "fid_list": f"[{fs_id}]",
                "shareid": str(share_id),
                "sekey": sekey or "",
            },
            timeout=30,
        )
        data = resp.json()
        errno = data.get("errno", 0)
        if errno != 0:
            show = data.get("show_msg") or data.get("errmsg") or ""
            raise CloudError(
                f"BDUSS 获取直链失败(errno={errno})",
                show,
            )

        dlink = data.get("dlink") or ""
        if not dlink:
            dl_list = data.get("list") or []
            if dl_list and isinstance(dl_list, list):
                dlink = dl_list[0].get("dlink") or ""
        if not dlink:
            raise CloudError("BDUSS 直链为空", str(data)[:300])
        logger.info("[BDUSS] got dlink: %s...", dlink[:80])
        return dlink

    def _share_dlink_from_page(self, meta: dict, fs_id: int, share_url: str,
                                 share_id: int, uk: int, sekey: str) -> str:
        """从分享页 HTML 提取 sign/timestamp → 调 /api/sharedownload（备用方案）。
        
        注意：百度新版 SPA 分享页的初始 HTML 中可能不包含这些参数，
        此方法在 SPA 页面上会失败，仅对旧版页面有效。
        """
        import re

        s = meta.get("session")
        if not s:
            s = requests.Session()
            s.headers.update({
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/131.0.0.0 Safari/537.36",
                "Accept-Encoding": "identity",
            })

        try:
            page_resp = s.get(share_url, timeout=20)
        except requests.RequestException as exc:
            raise CloudError("无法访问分享链接页面", str(exc)) from exc

        page_text = page_resp.text

        # 如果返回的是错误页面（404等），直接失败
        if "页面不存在" in page_text or page_resp.status_code == 404:
            raise CloudError("分享页面无法访问",
                            f"HTTP {page_resp.status_code}，可能是网络问题或链接已失效")

        def _extract(pattern: str, default="") -> str:
            m = re.search(pattern, page_text)
            return m.group(1).strip('"\'') if m else default

        sign = _extract(r'"sign"\s*:\s*"([^"]+)"')
        timestamp = _extract(r'"timestamp"\s*:\s*"?(\d+)"?')

        # 如果 HTML 中没有 sign（SPA 页面），尝试从 JS 数据块提取
        if not sign:
            # 百度可能在 <script> 标签或 JSON 块中注入数据
            for block_pat in [r'yunData\s*=\s*(\{[^}]+)',
                               r'__INITIAL_STATE__\s*=\s*(\{[^}]+)',
                               r'window.__DATA__\s*=\s*(\{[^}]+)']:
                m = re.search(block_pat, page_text)
                if m:
                    block = m.group(1)
                    s2 = re.search(r'"sign"\s*:\s*"([^"]+)"', block)
                    t2 = re.search(r'"timestamp"\s*:\s*"?(\d+)"?', block)
                    if s2:
                        sign = s2.group(1)
                    if t2:
                        timestamp = t2.group(1)
                    break

        if not sign:
            raise CloudError(
                "无法从页面提取签名参数",
                "百度分享页已改为 SPA 架构，HTML 中不包含 sign/timestamp。"
                "请配置 BDUSS Cookie 以使用高速下载。",
            )

        timestamp = timestamp or str(int(time.time()))
        s.headers.update({"Referer": share_url, "Origin": "https://pan.baidu.com"})

        resp = s.post(
            "https://pan.baidu.com/api/sharedownload",
            params={"sign": sign, "timestamp": timestamp},
            data={
                "encrypt": 0,
                "product": "share",
                "uk": str(uk),
                "primaryid": fs_id,
                "fid_list": f"[{fs_id}]",
                "shareid": str(share_id),
                "sekey": sekey or "",
            },
            timeout=30,
        )
        data = resp.json()
        errno = data.get("errno", 0)
        if errno != 0:
            raise CloudError(
                f"获取下载直链失败(errno={errno})",
                data.get("show_msg") or data.get("errmsg") or "",
            )
        dlink = data.get("dlink") or ""
        if not dlink:
            dl_list = data.get("list") or data.get("dlink_list") or []
            if dl_list and isinstance(dl_list, list):
                dlink = dl_list[0].get("dlink") or ""
        if not dlink:
            raise CloudError("下载直链为空", str(data)[:300])
        logger.info("[PAGE] got dlink: %s...", dlink[:80])
        return dlink


    def _download_from_url(self, url: str, local_path: Path, progress=None) -> int:
        """从给定 URL 流式下载文件到本地，返回写入字节数。"""
        import shutil

        resp = requests.get(url, stream=True, timeout=30,
                            headers={"User-Agent": "pan.baidu.com"})
        resp.raise_for_status()

        total = int(resp.headers.get("content-length", 0))
        local_path.parent.mkdir(parents=True, exist_ok=True)
        done = 0

        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=2 * 1024 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)

        return done

    def download_share(
        self,
        share_url: str,
        pwd: str,
        share_path: str,
        local_path: Path,
        token: str,
        progress=None,
        backend: str = "auto",
        dest: str = "",
        # 以下来自 share/list 响应（前端传入），提供后跳过重复 verify
        pre_sekey: str = "",
        pre_share_id: int | None = None,
        pre_uk: int | None = None,
        pre_fs_id: int | None = None,
        bduss: str = "",       # 用户提供的 BDUSS Cookie（用于高速直链）
        pre_dlink: str = "",    # 前端通过 WebView JS 注入预取的直链（优先级最高）
    ) -> int:
        """把分享文件下载到本机。返回写入字节数。

        策略（按优先级）：
          0. pre_dlink → 前端 WebView 注入的直链，直接 aria2c 下载（最可靠）
          1. transfer → 从用户网盘下载（官方合规路径）
          2. dlink 直链 + aria2c（BDUSS 或页面提取）
          3. 浏览器降级
        """
        dest = dest or _baidu_share_dest("VideoDownloader_Share")
        target_name = os.path.basename(share_path or "")
        sub_dir = os.path.dirname(share_path or "").replace("\\", "/")
        surl = self._parse_share_surl(share_url)

        # ── 构造 meta（优先用前端传入的预取值，避免重复 verify）────────
        # 注入 BDUSS（如果有），供 _share_dlink 优先使用
        _bduss = (bduss or "").strip()

        if pre_sekey and pre_share_id is not None and pre_uk is not None:
            # 前端已传入 list 阶段的 verify 结果 → 直接用，不再 verify
            meta = {
                "sekey": pre_sekey,
                "share_id": pre_share_id,
                "uk": pre_uk,
                "items": [],  # transfer/dlink 不依赖 items（已有 fs_id）
                "session": None,
                "bduss": _bduss,
            }
            fs_id = pre_fs_id
        else:
            # 没有预取值 → 回退到 _share_meta（会重新 verify）
            meta = self._share_meta(surl, pwd, sub_dir=sub_dir)
            by_path = {it.get("path"): it.get("fs_id") for it in meta["items"]}
            by_name = {it.get("server_filename") or "": it.get("fs_id") for it in meta["items"]}
            fs_id = by_path.get(share_path) or by_name.get(target_name)
            meta["bduss"] = _bduss

        # ── 策略 0：前端 WebView 注入的预取直链（最可靠，优先级最高）────
        _pre_dlink = (pre_dlink or "").strip()
        if _pre_dlink:
            logger.info("[策略0] 使用 WebView 预取直链: %s...", _pre_dlink[:80])
            total = int(meta.get("items", [{}])[0].get("size", 0) or 0)
            if backend in ("auto", "aria2c") and _aria2c_path() is not None:
                try:
                    return _aria2c_download(_pre_dlink, local_path, total,
                                           concurrency=16, progress=progress)
                except CloudError:
                    if backend == "aria2c":
                        raise
                    logger.warning("aria2c 不可用，回退 requests 流式下载")
            return self._download_from_url(_pre_dlink, local_path, progress=progress)

        # ── 策略 1：transfer 路径（官方合规）─────────────────────────
        try:
            # 只有非预取模式才走 transfer（预取模式下跳过，直接走 dlink）
            if not pre_sekey:
                transferred = self.share_transfer(share_url, pwd, [share_path], dest, token, sub_dir=sub_dir)
                if transferred and transferred[0].get("fs_id"):
                    item = transferred[0]
                    return self.download(token, item["fs_id"], item["path"], local_path,
                                         progress=progress, backend=backend)
        except CloudError as exc:
            transfer_err = exc  # 保存错误信息，用于策略 2 失败时报告

        # ── 策略 2：直链 + aria2c 高速下载（绕过 transfer）──────────────
        if not fs_id:
            raise CloudError("未找到要下载的文件", "请重新列出分享内容后再试")
        try:
            dlink = self._share_dlink(meta, fs_id, share_url=share_url)
            # ★ 用 aria2c 多线程下载（-x16 -s16），跑满带宽
            total = int(meta.get("items", [{}])[0].get("size", 0) or 0)
            if backend in ("auto", "aria2c") and _aria2c_path() is not None:
                try:
                    return _aria2c_download(dlink, local_path, total,
                                           concurrency=16, progress=progress)
                except CloudError:
                    if backend == "aria2c":
                        raise
                    logger.warning("aria2c 不可用，回退 requests 流式下载")
            return self._download_from_url(dlink, local_path, progress=progress)
        except CloudError:
            pass  # dlink 也失败 → 降级到策略3

        # ── 策略 3：浏览器降级（打开百度原生分享页，用户手动/浏览器下载）──
        raise CloudError(
            "API下载不可用，请在浏览器中打开链接下载",
            f"BROWSER_FALLBACK:{share_url}",
        )

    def _find_in_dest_dir(self, token: str, dest_dir: str, name: str) -> dict | None:
        """在用户网盘 dest_dir 下找名为 name 的文件/目录，返回 {fs_id, path}；找不到返回 None。"""
        try:
            data = self.list_files(token, dest_dir, page=1, limit=200)
        except CloudError:
            return None
        for it in data.get("list") or []:
            if (it.get("server_filename") or "") == name:
                return {
                    "fs_id": it.get("fs_id"),
                    "path": it.get("path") or (dest_dir.rstrip("/") + "/" + name),
                }
        return None


# --------------------------------------------------------------------------- #
# 百度分享 / 令牌相关模块级常量与本地存储
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 百度扫码登录（自动获取 BDUSS，免去手动复制 Cookie 的复杂步骤）
# --------------------------------------------------------------------------- #
import threading as _threading

_BAIDU_QR_SESSIONS: "dict" = {}
_BAIDU_QR_LOCK = _threading.Lock()


def baidu_qr_create() -> dict:
    """生成百度网盘扫码登录二维码，返回 {sign, qrcode_url}。"""
    import re as _re
    import time as _time

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
        "Accept-Encoding": "identity",
    })
    tt = int(_time.time() * 1000)
    r = s.get(
        "https://passport.baidu.com/v2/api/getqrcode",
        params={"lp": "pc", "qrloginfrom": "pc", "logPage": "pcLogin",
                "tpl": "netdisk", "apiver": "v3", "tt": tt},
        timeout=15,
    )
    try:
        d = r.json()
    except ValueError:
        raise CloudError("获取登录二维码失败", "响应无法解析")
    sign = d.get("sign")
    if not sign:
        raise CloudError("获取登录二维码失败", str(d)[:200])
    imgurl = d.get("imgurl") or ""
    qrcode_url = imgurl if imgurl.startswith("http") else "https://" + imgurl.lstrip("/")
    with _BAIDU_QR_LOCK:
        _BAIDU_QR_SESSIONS[sign] = s
    return {"sign": sign, "qrcode_url": qrcode_url}


def baidu_qr_poll(sign: str) -> dict:
    """长轮询扫码状态；用户确认后自动兑换 BDUSS 并存盘。返回 {status, ...}。"""
    import re as _re
    import json as _json
    import time as _time

    with _BAIDU_QR_LOCK:
        s = _BAIDU_QR_SESSIONS.get(sign)
    if not s:
        return {"status": "expired"}
    try:
        r = s.get(
            "https://passport.baidu.com/channel/unicast",
            params={"channel_id": sign, "callback": "__cb__",
                    "t": int(_time.time() * 1000)},
            timeout=30,
        )
    except requests.Timeout:
        return {"status": "waiting"}
    except requests.RequestException as exc:
        return {"status": "error", "msg": f"轮询异常: {exc}"}

    m = _re.search(r"__cb__\((.*)\)", r.text, _re.S)
    if not m:
        return {"status": "waiting"}
    try:
        payload = _json.loads(m.group(1))
    except (ValueError, TypeError):
        return {"status": "waiting"}
    errno = payload.get("errno")
    if errno == 1:
        return {"status": "waiting"}
    if errno == 0:
        channel_v = payload.get("channel_v") or "{}"
        try:
            cv = _json.loads(channel_v) if isinstance(channel_v, str) else channel_v
        except (ValueError, TypeError):
            cv = {}
        v = (cv or {}).get("v")
        if not v:
            return {"status": "waiting"}
        try:
            s.get(
                "https://passport.baidu.com/v3/login/main/qrbdusslogin",
                params={"bduss": v},
                timeout=15,
            )
        except requests.RequestException:
            pass
        bduss = s.cookies.get("BDUSS")
        if not bduss:
            return {"status": "error", "msg": "未能从登录响应中获取 BDUSS"}
        _save_bduss(bduss)
        with _BAIDU_QR_LOCK:
            _BAIDU_QR_SESSIONS.pop(sign, None)
        return {"status": "confirmed"}
    return {"status": "error", "msg": str(payload)[:200]}


def _save_bduss(bduss: str, username: str = "") -> None:
    """把 BDUSS 存盘（~/.vdl/baidu_bduss.txt，_get_bduss 读取），并记录登录信息。"""
    import time as _time
    import json as _json

    d = Path.home() / ".vdl"
    try:
        d.mkdir(parents=True, exist_ok=True)
        bp = d / "baidu_bduss.txt"
        bp.write_text((bduss or "").strip() + "\n", "utf-8")
        try:
            bp.chmod(0o600)
        except OSError:
            pass
        info = {
            "username": username,
            "saved_at": int(_time.time()),
            "bduss_masked": (bduss[:6] + "…" + bduss[-4:]) if len(bduss) > 12 else "***",
        }
        (d / "baidu_bduss_info.json").write_text(
            _json.dumps(info, ensure_ascii=False), "utf-8")
    except OSError as exc:
        logger.warning("保存 BDUSS 失败: %s", exc)


def baidu_qr_status() -> dict:
    """返回当前 BDUSS 登录状态（供前端显示）。"""
    import json as _json

    p = Path.home() / ".vdl" / "baidu_bduss_info.json"
    try:
        if p.exists():
            info = _json.loads(p.read_text("utf-8"))
            return {
                "logged_in": True,
                "username": info.get("username", ""),
                "saved_at": info.get("saved_at", 0),
                "bduss_masked": info.get("bduss_masked", ""),
            }
    except (OSError, ValueError):
        pass
    return {"logged_in": False}


BAIDU_SHARE_API = "https://pan.baidu.com/share"


def _strip_surl_prefix(surl: str) -> str:
    """去掉百度分享短链的类型前缀（1=普通分享、5=知识分享、7=三方加密）。

    API（verify/list）必须用去掉前缀后的核心码；老链接本身不带前缀则原样返回。
    """
    surl = (surl or "").strip()
    if surl and surl[0] in "157":
        return surl[1:]
    return surl


def _looks_like_file(name: str) -> bool:
    """判断文件名是否像文件（含扩展名且非隐藏文件）。

    百度新版 share/list 对单文件分享会误返回 isdir=1，用此兜底纠正。
    """
    base = (name or "").strip().rsplit("/", 1)[-1]
    return "." in base and not base.startswith(".")


def _baidu_app_name() -> str | None:
    """返回百度开放平台应用名（来自环境变量 VDL_BAIDU_APP_NAME）。
    2026-06-03 后新建的应用只能访问 /apps/{appname}/ 目录，
    配置此项后转存/下载自动使用该前缀路径；未配置则走根目录（兼容老应用）。"""
    return (os.environ.get("VDL_BAIDU_APP_NAME") or "").strip() or None


def _baidu_share_dest(fallback: str = "VideoDownloader_Share") -> str:
    """返回分享转存的目标目录。有 APP_NAME 时用 /apps/{name}/{fallback}，否则 /{fallback}（根目录，兼容老应用）。"""
    app = _baidu_app_name()
    if app:
        return f"/apps/{app}/{fallback}"
    return f"/{fallback}"


def _baidu_token_path() -> Path:
    """返回本机百度令牌存储路径（跨平台，仅存于用户机器，不进二进制/不进 git）。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home() / ".vdl")))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "VideoDownloader"
    else:
        base = Path.home() / ".local" / "share" / "videodownloader"
    return base / "baidu_token.json"


def save_baidu_token(data: dict) -> None:
    """把用户授权得到的令牌（access_token 等）写到本机文件，权限 600。"""
    if not isinstance(data, dict) or not data.get("access_token"):
        return
    p = _baidu_token_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError as exc:
        logger.warning("保存百度令牌失败: %s", exc)


def load_baidu_token() -> dict:
    """读取本机存储的百度令牌；不存在/损坏返回空 dict。"""
    p = _baidu_token_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8")) or {}
    except (OSError, ValueError):
        return {}


def clear_baidu_token() -> None:
    """删除本机存储的百度令牌（退出登录）。"""
    p = _baidu_token_path()
    try:
        p.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 百度 OAuth 辅助（供 app.py 路由调用）
# --------------------------------------------------------------------------- #

def baidu_auth_url(redirect_uri: str, app_key: str, state: str = "", app_id: str = "") -> str:
    from urllib.parse import urlencode

    q = {
        "response_type": "code",
        "client_id": app_key,
        "redirect_uri": redirect_uri,
        "scope": "basic,netdisk",
        "state": state,
    }
    # device_id（AppID）是百度网盘开放平台 OAuth 的必需参数
    # 文档：https://pan.baidu.com/union/doc/使用入门/接入授权/授权码模式/
    # 硬件应用必填，软件应用也建议填写（缺省可能导致 invalid_client 或跳转首页）
    if app_id:
        q["device_id"] = app_id
    return BaiduProvider.OAUTH_BASE + "/oauth/2.0/authorize?" + urlencode(q)


def baidu_exchange_token(code: str, redirect_uri: str, app_key: str, app_secret: str) -> dict:
    resp = requests.post(
        BaiduProvider.OAUTH_BASE + "/oauth/2.0/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": app_key,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    data = resp.json()
    if "access_token" not in data:
        raise CloudError("百度授权换取令牌失败", str(data))
    return data


def _baidu_callback_html(token: str = "", error: str = "") -> str:
    """OAuth 回调页：把令牌通过 postMessage 回传（兼容 opener 弹窗与 iframe 页内嵌入），服务端不存储用户令牌。"""
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>百度网盘授权</title></head><body><p id='msg'>授权处理中…</p>"
        "<script>"
        "var data={token:" + json.dumps(token) + ",error:" + json.dumps(error) + "};"
        "var msg=document.getElementById('msg');"
        "var sent=false;"
        "function post(d){if(sent)return;sent=true;d.source='vdl-baidu';"
        "  if(window.opener){window.opener.postMessage(d,location.origin);}"
        "  if(window.parent&&window.parent!==window){window.parent.postMessage(d,location.origin);}"
        "  msg.textContent=d.token?'授权成功，请返回原页面':'授权失败：'+(d.error||'未知错误');"
        "  setTimeout(function(){try{window.close();}catch(e){}},1500);"
        "}"
        "post(data);"
        "</script></body></html>"
    )
