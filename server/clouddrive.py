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
        "-k", "1M",                   # 分块 1MB（便于并发+续传）
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
    # 百度网盘开放平台（pan.baidu.com）的 OAuth 端点，不是通用开放平台 openapi.baidu.com
    # 两者的 AppKey 不互通：在 pan.baidu.com 创建的应用必须用 pan.baidu.com/union/oauth/
    AUTH_BASE = "https://pan.baidu.com/union"
    PAN_API = "https://pan.baidu.com/rest/2.0/xpan/file"

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
                return _aria2c_download(dlink, local_path, total, concurrency=8, progress=progress)
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
        """从分享链接中提取 surl（短链码）。支持 /s/xxx 与 ?surl=xxx 两种形式。"""
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
                    return parts[i + 1].strip()
            if parts:
                return parts[-1].strip()
        raise CloudError("无法解析分享链接", "请检查链接格式，应为 https://pan.baidu.com/s/... 形式")

    def share_list(self, share_url: str, pwd: str = "") -> dict:
        """列出分享链接里的文件（看分享内容无需登录；转存才需 token）。返回归一化列表。"""
        surl = self._parse_share_surl(share_url)
        params = {
            "channel": "chunlei",
            "clienttype": "0",
            "web": "1",
            "surl": surl,
            "page": "1",
            "num": "100",
            "order": "time",
            "desc": "1",
        }
        if pwd:
            params["pwd"] = pwd
        try:
            resp = requests.get(BAIDU_SHARE_API + "/list", params=params, timeout=30)
        except requests.RequestException as exc:
            raise CloudError("获取分享文件列表失败", str(exc)) from exc
        data = resp.json()
        errno = data.get("errno", 0)
        if errno != 0:
            msg = {
                -9: "分享链接不存在或已失效",
                -12: "提取码错误",
                -10: "分享已被取消",
                -1: "分享链接无效",
            }.get(errno, f"获取分享列表失败(errno={errno})")
            raise CloudError(msg, str(data.get("errmsg", "")))
        items = data.get("list") or []
        files = [
            {
                "fs_id": it.get("fs_id"),
                "path": it.get("path"),
                "name": it.get("server_filename") or it.get("filename") or "",
                "size": it.get("size", 0),
                "isdir": bool(it.get("isdir")),
            }
            for it in items
        ]
        files.sort(key=lambda x: (not x["isdir"], -(x.get("size") or 0)))
        return {"surl": surl, "list": files, "has_more": bool(data.get("has_more"))}

    def share_transfer(self, share_url: str, pwd: str, paths: list, dest: str, token: str) -> list:
        """把分享里的文件转存到用户自己的网盘 dest 目录，返回转存后的 [{fs_id, path}]。"""
        if not token:
            raise CloudError("未授权百度网盘", "请先完成百度账号授权")
        surl = self._parse_share_surl(share_url)
        dest = dest or _baidu_share_dest("VideoDownloader_Share")
        if not dest.startswith("/"):
            dest = "/" + dest
        try:
            resp = requests.post(
                BAIDU_SHARE_API + "/transfer",
                params={"channel": "chunlei", "clienttype": "0", "web": "1", "access_token": token},
                data={
                    "surl": surl,
                    "pwd": pwd or "",
                    "filelist": json.dumps(paths, ensure_ascii=False),
                    "dest": dest,
                },
                timeout=60,
            )
        except requests.RequestException as exc:
            raise CloudError("转存分享文件失败", str(exc)) from exc
        data = resp.json()
        errno = data.get("errno", 0)
        if errno != 0:
            msg = {
                -9: "分享链接不存在或已失效",
                -12: "提取码错误",
                -30: "文件已在网盘中存在（请更换目标目录或文件名）",
                -70: "网盘容量不足，无法转存",
            }.get(errno, f"转存失败(errno={errno})")
            raise CloudError(msg, str(data.get("errmsg", "")))
        transferred = (data.get("extra") or {}).get("list") or []
        return [{"fs_id": it.get("fs_id"), "path": it.get("path")} for it in transferred]

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
    ) -> int:
        """把分享里的某个文件：先转存到用户网盘，再从用户网盘下载到本机。返回写入字节数。

        速度由用户账号等级决定，本方法只做官方合规下载，不绕过平台限速。
        """
        dest = dest or _baidu_share_dest("VideoDownloader_Share")
        transferred = self.share_transfer(share_url, pwd, [share_path], dest, token)
        if not transferred:
            raise CloudError("转存后未获得文件信息", "请稍后重试或检查分享链接")
        item = transferred[0]
        return self.download(
            token, item["fs_id"], item["path"], local_path, progress=progress, backend=backend
        )


# --------------------------------------------------------------------------- #
# 百度分享 / 令牌相关模块级常量与本地存储
# --------------------------------------------------------------------------- #

BAIDU_SHARE_API = "https://pan.baidu.com/share"


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

def baidu_auth_url(redirect_uri: str, app_key: str, state: str = "") -> str:
    from urllib.parse import urlencode

    q = {
        "response_type": "code",
        "client_id": app_key,
        "redirect_uri": redirect_uri,
        "scope": "basic,netdisk",
        "state": state,
    }
    return BaiduProvider.AUTH_BASE + "/oauth/2.0/authorize?" + urlencode(q)


def baidu_exchange_token(code: str, redirect_uri: str, app_key: str, app_secret: str) -> dict:
    resp = requests.post(
        BaiduProvider.AUTH_BASE + "/oauth/2.0/token",
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
