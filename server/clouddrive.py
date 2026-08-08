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
from pathlib import Path

import requests

logger = logging.getLogger("vdl.cloud")


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
    AUTH_BASE = "https://openapi.baidu.com"
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
