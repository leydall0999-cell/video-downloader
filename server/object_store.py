"""对象存储直链分发（2026-10-20，方案③）。

背景（实测数据）：网页版的「保存到本机 ⬇」走的是 `/api/tasks/{id}/file?download=1`，
把成品从**部署节点**单流推给浏览器。节点在海外（Cloudflare → Railway）时，
跨境单连接实测只有 ~120KB/s（418MB ≈ 49 分钟）；多连接可叠加 5 倍以上，说明
瓶颈是**每条连接**的跨境吞吐，不是用户本地带宽。

本模块给出的解法：
    节点上传一次到对象存储 → 浏览器拿预签名直链，从 OSS/CDN 下载（跑满本地带宽）。

如实说明代价（别在文档里美化）：
  · 首次上传仍受**节点出网带宽**限制（这是所有方案共同的天花板）；
  · 但上传只做一次：之后重下、多设备、分享给别人，全是 CDN 全速 —— 与「每次
    保存都单流回传」是本质区别。

配置（env 优先，其次 `~/.video-downloader/object_store.json`，VDL_HOME 可覆盖）：

    VDL_OSS_PROVIDER=aliyun_oss | s3      # 默认 aliyun_oss
    VDL_OSS_ENDPOINT=oss-cn-shenzhen.aliyuncs.com
    VDL_OSS_BUCKET=my-bucket
    VDL_OSS_AK=xxx / VDL_OSS_SK=xxx
    VDL_OSS_PREFIX=vdl/tasks              # 对象前缀，默认 vdl/tasks
    VDL_OSS_CDN=https://cdn.example.com   # 可选：CDN 加速域名
    VDL_OSS_EXPIRES=86400                 # 预签名有效期（秒）
    VDL_OSS_REGION=auto                   # 仅 s3：R2 填 auto，MinIO/其它填真实 region
    VDL_OSS_PATH_STYLE=1                  # 仅 s3：MinIO 等需要 path-style 寻址

签名实现（无第三方 SDK，纯 hashlib/hmac，避免给安装包加体积）：
  · aliyun_oss → OSS V1（HMAC-SHA1）。官方测试向量见 tests/test_object_store.py。
  · s3         → AWS SigV4（预签名 GET + PUT），覆盖 Cloudflare R2 / MinIO / COS 的 S3 兼容口。

⚠️ 安全边界：预签名 URL 在有效期内**无需登录即可下载**（这是直链分发的前提）。
默认 24 小时；调用方（路由层）必须先过 `_require_owned_task`，只签自己人那一份。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from atomic_io import atomic_write_json, mutation

logger = logging.getLogger(__name__)

__all__ = [
    "config_path",
    "load_config",
    "enabled",
    "object_key",
    "presign_get",
    "upload_file",
    "ensure_uploaded",
    "upload_state",
    "mark_uploaded",
    "uploaded_entry",
    "forget",
    "reset_cache",
]

_CHUNK = 1 << 20          # 1MB 读盘/上报粒度
_DEFAULT_PREFIX = "vdl/tasks"
_DEFAULT_EXPIRES = 86400
_MAX_EXPIRES = 7 * 86400


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _home() -> Path:
    override = (os.environ.get("VDL_HOME") or "").strip()
    return Path(override) if override else Path.home() / ".video-downloader"


def config_path() -> Path:
    return _home() / "object_store.json"


def registry_path() -> Path:
    return _home() / "object_uploads.json"


_FILE_CACHE: dict[str, Any] = {"key": None, "data": {}}
_FILE_LOCK = threading.Lock()


def _read_file_config() -> dict[str, Any]:
    p = config_path()
    try:
        st = p.stat()
        cache_key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    with _FILE_LOCK:
        if _FILE_CACHE["key"] == cache_key:
            return dict(_FILE_CACHE["data"])
    data: dict[str, Any] = {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
    except (OSError, ValueError) as e:  # noqa: BLE001
        logger.warning("对象存储配置解析失败（%s）：%s", p, e)
    with _FILE_LOCK:
        _FILE_CACHE["key"] = cache_key
        _FILE_CACHE["data"] = data
    return dict(data)


_ENV_MAP = {
    "provider": "VDL_OSS_PROVIDER",
    "endpoint": "VDL_OSS_ENDPOINT",
    "bucket": "VDL_OSS_BUCKET",
    "access_key_id": "VDL_OSS_AK",
    "access_key_secret": "VDL_OSS_SK",
    "prefix": "VDL_OSS_PREFIX",
    "cdn": "VDL_OSS_CDN",
    "expires": "VDL_OSS_EXPIRES",
    "region": "VDL_OSS_REGION",
    "path_style": "VDL_OSS_PATH_STYLE",
}


def reset_cache() -> None:
    """测试与热更新用：丢掉已缓存的配置文件快照。"""
    with _FILE_LOCK:
        _FILE_CACHE["key"] = None
        _FILE_CACHE["data"] = {}


def _normalize(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把 env / 文件两路来源合成一份可用的配置；关键字段缺失返回 None。"""
    cfg: dict[str, Any] = dict(raw)
    for field, env in _ENV_MAP.items():
        val = os.environ.get(env)
        if val is not None and str(val).strip() != "":
            cfg[field] = val

    provider = str(cfg.get("provider") or "aliyun_oss").strip().lower()
    if provider in ("oss", "aliyun", "aliyun-oss", "aliyunoss"):
        provider = "aliyun_oss"
    if provider not in ("aliyun_oss", "s3"):
        return None

    endpoint = str(cfg.get("endpoint") or "").strip()
    for pre in ("https://", "http://"):
        if endpoint.startswith(pre):
            endpoint = endpoint[len(pre):]
    endpoint = endpoint.rstrip("/")
    scheme = str(cfg.get("scheme") or ("http" if str(cfg.get("endpoint") or "").startswith("http://") else "https"))

    bucket = str(cfg.get("bucket") or "").strip()
    ak = str(cfg.get("access_key_id") or cfg.get("ak") or "").strip()
    sk = str(cfg.get("access_key_secret") or cfg.get("sk") or "").strip()
    if not (endpoint and bucket and ak and sk):
        return None

    try:
        expires = int(cfg.get("expires") or _DEFAULT_EXPIRES)
    except (TypeError, ValueError):
        expires = _DEFAULT_EXPIRES
    expires = max(60, min(_MAX_EXPIRES, expires))

    prefix = str(cfg.get("prefix") or _DEFAULT_PREFIX).strip().strip("/") or _DEFAULT_PREFIX
    cdn = str(cfg.get("cdn") or "").strip().rstrip("/")
    region = str(cfg.get("region") or "auto").strip() or "auto"
    path_style = str(cfg.get("path_style") or "").strip().lower() in ("1", "true", "yes", "on")

    return {
        "provider": provider,
        "scheme": scheme,
        "endpoint": endpoint,
        "bucket": bucket,
        "access_key_id": ak,
        "access_key_secret": sk,
        "prefix": prefix,
        "cdn": cdn,
        "expires": expires,
        "region": region,
        "path_style": path_style,
    }


def load_config() -> Optional[dict[str, Any]]:
    """返回可用配置；未配置 / 配置不全一律返回 None（调用方据此优雅回退）。"""
    return _normalize(_read_file_config())


def enabled() -> bool:
    return load_config() is not None


# ---------------------------------------------------------------------------
# 对象键
# ---------------------------------------------------------------------------
def _slug(text: str, limit: int = 48) -> str:
    """标题 → ASCII 安全片段（OSS 对象名保持纯 ASCII，避免各端编码差异）。"""
    norm = unicodedata.normalize("NFKD", text or "")
    ascii_text = norm.encode("ascii", "ignore").decode("ascii").lower()
    out = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in ascii_text)
    out = "-".join(p for p in out.split("-") if p)
    return out[:limit].strip("-")


def object_key(task_id: str, filename: str = "", prefix: Optional[str] = None) -> str:
    """生成对象键：<prefix>/<task_id>/<slug><ext>。task_id 保证唯一，slug 只为可读。"""
    cfg = load_config() or {}
    pre = (prefix or cfg.get("prefix") or _DEFAULT_PREFIX).strip("/")
    ext = Path(filename or "").suffix.lower()
    if len(ext) > 12 or not ext.replace(".", "").isalnum():
        ext = ""
    stem = _slug(Path(filename or "").stem)
    if len(stem) < 3:
        # 纯中文标题 slug 完会是空串或单个数字（"普通玩家从0打倒…" → "0"），
        # 在桶里翻起来没法认 → 退化成短哈希，至少稳定且不重名。
        stem = "f" + hashlib.md5((filename or "").encode("utf-8")).hexdigest()[:10]
    name = stem + ext
    tid = "".join(ch for ch in (task_id or "") if ch.isalnum() or ch in "-_")[:64] or "task"
    return f"{pre}/{tid}/{name}"


# ---------------------------------------------------------------------------
# 签名：阿里云 OSS V1
# ---------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _hmac_sha1(secret: str, msg: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha1).digest()


def _oss_resource(bucket: str, key: str) -> str:
    return f"/{bucket}/{key}"


def _oss_url(cfg: dict[str, Any], key: str) -> str:
    """对象 URL：默认 virtual-host 风格（bucket.endpoint），path_style 时为 endpoint/bucket。
    后者用于自建 OSS 网关 / 内网测试服务器（它们没有通配 DNS 解析不了 bucket 子域）。"""
    if cfg.get("path_style"):
        return f"{cfg['scheme']}://{cfg['endpoint']}/{cfg['bucket']}/{_quote_key(key)}"
    return f"{cfg['scheme']}://{cfg['bucket']}.{cfg['endpoint']}/{_quote_key(key)}"


def oss_presign_get(cfg: dict[str, Any], key: str, expires_at: int) -> str:
    """OSS V1 预签名 GET（StringToSign: VERB\\nContent-MD5\\nContent-Type\\nExpires\\n/bucket/key）。"""
    sts = f"GET\n\n\n{expires_at}\n{_oss_resource(cfg['bucket'], key)}"
    sig = _b64(_hmac_sha1(cfg["access_key_secret"], sts))
    q = urllib.parse.urlencode({
        "OSSAccessKeyId": cfg["access_key_id"],
        "Expires": str(expires_at),
        "Signature": sig,
    })
    return f"{_oss_url(cfg, key)}?{q}"


def oss_put_headers(cfg: dict[str, Any], key: str, content_type: str,
                    content_disposition: str, size: int,
                    date: Optional[str] = None) -> dict[str, str]:
    """OSS V1 PUT 头（Content-MD5 留空，Content-Type 参与签名）。"""
    date = date or _http_date()
    sts = f"PUT\n\n{content_type}\n{date}\n{_oss_resource(cfg['bucket'], key)}"
    sig = _b64(_hmac_sha1(cfg["access_key_secret"], sts))
    return {
        "Date": date,
        "Content-Type": content_type,
        "Content-Disposition": content_disposition,
        "Content-Length": str(size),
        "Authorization": f"OSS {cfg['access_key_id']}:{sig}",
    }


# ---------------------------------------------------------------------------
# 签名：AWS SigV4（S3 兼容：R2 / MinIO / COS-s3）
# ---------------------------------------------------------------------------
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _s3_signing_key(secret: str, date: str, region: str) -> bytes:
    k = _sign(("AWS4" + secret).encode("utf-8"), date)
    k = _sign(k, region)
    k = _sign(k, "s3")
    return _sign(k, "aws4_request")


def _s3_host(cfg: dict[str, Any]) -> str:
    return cfg["endpoint"] if cfg["path_style"] else f"{cfg['bucket']}.{cfg['endpoint']}"


def _s3_path(cfg: dict[str, Any], key: str) -> str:
    return ("/" + cfg["bucket"] + "/" + key) if cfg["path_style"] else ("/" + key)


def _quote_key(key: str) -> str:
    return urllib.parse.quote(key, safe="/~")


def _amz_date(ts: Optional[float] = None) -> tuple[str, str]:
    t = time.gmtime(ts or time.time())
    return time.strftime("%Y%m%dT%H%M%SZ", t), time.strftime("%Y%m%d", t)


def s3_presign_get(cfg: dict[str, Any], key: str, expires_in: int) -> str:
    amz_date, day = _amz_date()
    scope = f"{day}/{cfg['region']}/s3/aws4_request"
    host = _s3_host(cfg)
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{cfg['access_key_id']}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires_in)),
        "X-Amz-SignedHeaders": "host",
    }
    cq = urllib.parse.urlencode(sorted(params.items()), quote_via=urllib.parse.quote, safe="-_.~")
    canonical_request = "\n".join([
        "GET",
        _quote_key(_s3_path(cfg, key)),
        cq,
        f"host:{host}\n",
        "host",
        "UNSIGNED-PAYLOAD",
    ])
    sts = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        _sha256_hex(canonical_request.encode("utf-8")),
    ])
    sig = hmac.new(_s3_signing_key(cfg["access_key_secret"], day, cfg["region"]),
                   sts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{cfg['scheme']}://{host}{_quote_key(_s3_path(cfg, key))}?{cq}&X-Amz-Signature={sig}"


def s3_put_headers(cfg: dict[str, Any], key: str, payload_hash: str,
                   amz_date: str, day: str) -> dict[str, str]:
    """SigV4 PUT 头：签 host + x-amz-content-sha256 + x-amz-date 三项。"""
    host = _s3_host(cfg)
    scope = f"{day}/{cfg['region']}/s3/aws4_request"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    canonical_request = "\n".join([
        "PUT", _quote_key(_s3_path(cfg, key)), "", canonical_headers, signed_headers, payload_hash,
    ])
    sts = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope, _sha256_hex(canonical_request.encode("utf-8")),
    ])
    sig = hmac.new(_s3_signing_key(cfg["access_key_secret"], day, cfg["region"]),
                   sts.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={cfg['access_key_id']}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}"
        ),
    }


# ---------------------------------------------------------------------------
# 预签名 GET
# ---------------------------------------------------------------------------
def _apply_cdn(url: str, cdn: str) -> str:
    """把 OSS/S3 源站换成 CDN 域名（保留 path 与 query，签名参数原样透传）。"""
    if not cdn:
        return url
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.urlsplit(cdn if "://" in cdn else "https://" + cdn)
    return urllib.parse.urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))


def presign_get(key: str, *, expires_in: Optional[int] = None, use_cdn: bool = True) -> Optional[str]:
    """生成预签名直链；未配置返回 None（调用方回退原 /file 接口）。"""
    cfg = load_config()
    if cfg is None:
        return None
    ttl = int(expires_in or cfg["expires"])
    ttl = max(60, min(_MAX_EXPIRES, ttl))
    if cfg["provider"] == "aliyun_oss":
        url = oss_presign_get(cfg, key, int(time.time()) + ttl)
    else:
        url = s3_presign_get(cfg, key, ttl)
    return _apply_cdn(url, cfg["cdn"]) if use_cdn else url


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------
def _http_date(ts: Optional[float] = None) -> str:
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(ts or time.time()))


def _iter_file(path: Path, progress: Optional[Callable[[int], None]] = None) -> Iterator[bytes]:
    sent = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                return
            sent += len(chunk)
            if progress:
                try:
                    progress(sent)
                except Exception:  # noqa: BLE001
                    pass
            yield chunk


def _content_disposition(filename: str) -> str:
    """RFC 6266/5987：ASCII 兜底 + UTF-8 原名，浏览器保存时用中文名。"""
    name = filename or "download"
    ascii_name = name.encode("ascii", "ignore").decode("ascii") or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


def _guess_type(filename: str) -> str:
    ctype, _ = mimetypes.guess_type(filename or "")
    return ctype or "application/octet-stream"


def _proxies_for(endpoint: str) -> Optional[dict[str, str]]:
    """回环地址一律不走代理。

    Windows/macOS 上常驻 http_proxy 环境变量（Clash/Karing 等），requests 默认 trust_env
    会把 127.0.0.1 的上传也塞进代理 → ProxyError（实测报 ConnectionReset 10054）。
    自建 MinIO / 内网 OSS 网关正是这种场景，必须直连。
    """
    host = (endpoint or "").split(":")[0].strip("[]").lower()
    if host in ("localhost", "::1") or host.startswith("127."):
        return {}
    return None


def upload_file(local_path: Path, key: str, *, filename: str = "",
                progress: Optional[Callable[[int], None]] = None) -> dict[str, Any]:
    """把文件 PUT 到对象存储，返回 {key,size,etag}。出错抛异常（由调用方记录并回退）。"""
    import requests  # 延迟导入：未配置对象存储时不付这个开销

    cfg = load_config()
    if cfg is None:
        raise RuntimeError("对象存储未配置")
    local_path = Path(local_path)
    size = local_path.stat().st_size
    ctype = _guess_type(filename or local_path.name)
    disposition = _content_disposition(filename or local_path.name)

    if cfg["provider"] == "aliyun_oss":
        url = _oss_url(cfg, key)
        headers = oss_put_headers(cfg, key, ctype, disposition, size)
    else:
        payload_hash = _sha256_of_file(local_path)
        amz_date, day = _amz_date()
        url = f"{cfg['scheme']}://{_s3_host(cfg)}{_quote_key(_s3_path(cfg, key))}"
        headers = s3_put_headers(cfg, key, payload_hash, amz_date, day)
        headers["Content-Type"] = ctype
        headers["Content-Disposition"] = disposition
        headers["Content-Length"] = str(size)

    proxies = _proxies_for(cfg["endpoint"])
    # ⚠️ 只传 proxies={} 没用：requests 的 merge_environment_settings 会用 setdefault
    #    把 env 里的 http_proxy 再补回来（实测仍走代理并 ProxyError）。
    #    回环地址必须整段关掉 trust_env 才是真直连。
    sess = requests.Session()
    if proxies is not None:
        sess.trust_env = False
    resp = sess.put(url, data=_iter_file(local_path, progress), headers=headers,
                    timeout=(15, 900))
    if resp.status_code >= 300:
        raise RuntimeError(f"上传失败 HTTP {resp.status_code}: {(resp.text or '')[:200]}")
    return {"key": key, "size": size, "etag": (resp.headers.get("ETag") or "").strip('"')}


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(4 * _CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 上传登记表 + 进行中状态
# ---------------------------------------------------------------------------
_REG_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_STATE: dict[str, dict[str, Any]] = {}
_INFLIGHT: set[str] = set()
# 失败冷却：前端每 3 秒轮询一次，若失败后立刻重启上传，会变成「每 3 秒重传一个 418MB」
# 的惊群（实测 24 秒内起了 30+ 个线程）。失败后 30 秒内直接回报 failed，让前端回退。
_FAILED_TTL = 30.0
_FAILED: dict[str, tuple[float, str]] = {}


def _failed_recent(task_id: str) -> str:
    with _STATE_LOCK:
        hit = _FAILED.get(task_id)
    if hit and (time.time() - hit[0]) < _FAILED_TTL:
        return hit[1]
    return ""


def _load_registry() -> dict[str, Any]:
    try:
        raw = json.loads(registry_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def uploaded_entry(task_id: str) -> Optional[dict[str, Any]]:
    with _REG_LOCK:
        entry = _load_registry().get(task_id)
    return entry if isinstance(entry, dict) else None


def mark_uploaded(task_id: str, key: str, size: int, etag: str = "") -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with mutation(path):
        reg = _load_registry()
        reg[task_id] = {"key": key, "size": int(size), "etag": etag, "at": int(time.time())}
        # 防膨胀：只留最近 200 条
        if len(reg) > 200:
            for tid, _ in sorted(reg.items(), key=lambda kv: kv[1].get("at", 0))[:len(reg) - 200]:
                reg.pop(tid, None)
        atomic_write_json(path, reg)


def forget(task_id: str) -> None:
    path = registry_path()
    with mutation(path):
        reg = _load_registry()
        if reg.pop(task_id, None) is not None:
            atomic_write_json(path, reg)
    with _STATE_LOCK:
        _STATE.pop(task_id, None)
        _FAILED.pop(task_id, None)


def upload_state(task_id: str) -> dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATE.get(task_id) or {})


def _set_state(task_id: str, **kw: Any) -> None:
    with _STATE_LOCK:
        cur = _STATE.setdefault(task_id, {})
        cur.update(kw)


def ensure_uploaded(task_id: str, filepath: Path, filename: str = "") -> dict[str, Any]:
    """确保任务成品已在对象存储；返回可直接给前端的统一状态字典。

    返回 status 取值：
      disabled  —— 未配置对象存储（前端静默回退原 /file 单流下载）
      ready     —— 已上传，带 url / expires_in / size
      uploading —— 后台上传中，带 uploaded / total / percent
      failed    —— 上传失败（带 error，前端回退原路径）
    """
    if not enabled():
        return {"status": "disabled"}

    filepath = Path(filepath)
    try:
        size = filepath.stat().st_size
    except OSError:
        return {"status": "failed", "error": "成品文件不可读"}

    entry = uploaded_entry(task_id)
    if entry and int(entry.get("size") or -1) == size and entry.get("key"):
        url = presign_get(str(entry["key"]))
        if url:
            cfg = load_config() or {}
            return {"status": "ready", "url": url, "size": size,
                    "expires_in": int(cfg.get("expires") or _DEFAULT_EXPIRES),
                    "cdn": bool(cfg.get("cdn"))}

    st = upload_state(task_id)
    if task_id in _INFLIGHT:
        return {"status": "uploading", "uploaded": int(st.get("uploaded") or 0),
                "total": int(st.get("total") or size),
                "percent": round(100.0 * float(st.get("uploaded") or 0) / max(1, size), 1)}
    _recent_err = _failed_recent(task_id)
    if _recent_err:
        return {"status": "failed", "error": _recent_err}

    with _STATE_LOCK:
        if task_id in _INFLIGHT:
            return {"status": "uploading", "uploaded": 0, "total": size, "percent": 0.0}
        _INFLIGHT.add(task_id)
    _set_state(task_id, uploaded=0, total=size, error="", started_at=time.time())
    key = object_key(task_id, filename or filepath.name)

    def _worker() -> None:
        try:
            logger.info("对象存储上传开始 task=%s key=%s size=%d", task_id, key, size)
            res = upload_file(filepath, key, filename=filename or filepath.name,
                              progress=lambda n: _set_state(task_id, uploaded=n, total=size))
            mark_uploaded(task_id, res["key"], res["size"], res.get("etag") or "")
            _set_state(task_id, uploaded=size, total=size, error="", done_at=time.time())
            with _STATE_LOCK:
                _FAILED.pop(task_id, None)
            logger.info("对象存储上传完成 task=%s etag=%s", task_id, res.get("etag") or "-")
        except Exception as e:  # noqa: BLE001
            msg = str(e)[:300]
            _set_state(task_id, error=msg)
            with _STATE_LOCK:
                _FAILED[task_id] = (time.time(), msg)
            logger.warning("对象存储上传失败 task=%s：%s", task_id, e)
        finally:
            with _STATE_LOCK:
                _INFLIGHT.discard(task_id)

    threading.Thread(target=_worker, name=f"oss-upload-{task_id[:8]}", daemon=True).start()
    return {"status": "uploading", "uploaded": 0, "total": size, "percent": 0.0}
