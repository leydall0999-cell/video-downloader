"""云盘集成：把已下载的文件存到用户自己的网盘（WebDAV）。

设计要点：
- 全部为「用户自己的网盘」——服务端只做临时代理上传，不留存、不托管他人内容。
- WebDAV 零额外依赖（仅 requests），任意 Nextcloud / 群晖 / 自建 WebDAV 均可。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import requests

logger = logging.getLogger("vdl.cloud")


# --------------------------------------------------------------------------- #
# aria2c 工具（供百度网盘下载走并发拉取；与 downloader._aria2c_path 同源逻辑，
# 但放在本模块以避免跨模块循环 import）
# --------------------------------------------------------------------------- #

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

