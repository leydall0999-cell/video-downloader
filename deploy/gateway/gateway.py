"""VDL LLM 网关 —— 让真实 API Key 只留在服务端，永不落到用户机器上。

为什么需要它
------------
桌面版解说功能需要一个云端 LLM（DeepSeek）。原先做法是管理员把 Key 写进用户机器的
`~/.video-downloader/llm_managed.json`——自用尚可，但**一旦把 App 分发给他人，等于把
Key 一起发出去**（文件 0600 也挡不住，App 自己就能读）。

本服务是 OpenAI 兼容的反向代理：
  * 客户端只持有 `url + token`，token 可单独吊销、不含任何上游凭据；
  * 上游 Key 只在本服务配置里（`/opt/vdl-gateway/upstream.json`，0600）；
  * 只放行白名单模型，避免被拿去刷别的模型；
  * 用量落 `usage.jsonl`（只记 token 数与模型，**绝不记 prompt 正文**）。

端点（nginx 把公网 `/gw/` 前缀转发到本机 8890，故路由带 /gw 前缀）
-----------------------------------------------------------------
  GET  /gw/health                 无需鉴权，供客户端探测连通性与可用模型
  GET  /gw/v1/models              需鉴权
  POST /gw/v1/chat/completions    需鉴权，支持 stream 透传
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

BASE = Path(os.environ.get("VDL_GATEWAY_HOME", "/opt/vdl-gateway"))
UPSTREAM_FILE = BASE / "upstream.json"
TOKENS_FILE = BASE / "tokens.json"
USAGE_FILE = BASE / "usage.jsonl"
MATTING_FILE = BASE / "matting.json"      # 火山抠图凭据（0600），永不下发

# 上游读取超时给足：长稿分段生成单请求可能跑几十秒
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=15.0)
MAX_BODY_BYTES = 4 * 1024 * 1024  # 4MB，超出直接拒绝（nginx 层还有一道）

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vdl-gateway")

app = FastAPI(title="VDL LLM Gateway", docs_url=None, redoc_url=None)


# ── 配置读取 ────────────────────────────────────────────────────────────────
def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        log.error("配置读取失败 %s: %s", path, e)
        return {}


def _upstream() -> dict[str, Any]:
    return _read_json(UPSTREAM_FILE)


def _tokens() -> dict[str, Any]:
    return _read_json(TOKENS_FILE).get("tokens", {}) or {}


def _client_token(auth: str | None) -> str:
    if not auth:
        return ""
    parts = auth.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


def _authenticate(auth: str | None) -> dict[str, Any]:
    """校验 Bearer token，返回该令牌的元信息；失败抛 401。"""
    token = _client_token(auth)
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    for known, meta in _tokens().items():
        # 定长比较，避免通过响应时间侧信道逐字节猜令牌
        if hmac.compare_digest(token, known):
            if not meta.get("enabled", True):
                raise HTTPException(status_code=403, detail="token disabled")
            return {"name": meta.get("name", "unknown"), **meta}
    raise HTTPException(status_code=401, detail="invalid token")


def _record_usage(meta: dict[str, Any], model: str, usage: dict[str, Any] | None, ok: bool) -> None:
    """只记统计量，不记 prompt / completion 正文。"""
    try:
        row = {
            "ts": int(time.time()),
            "who": meta.get("name", "unknown"),
            "model": model,
            "ok": bool(ok),
            "prompt_tokens": (usage or {}).get("prompt_tokens"),
            "completion_tokens": (usage or {}).get("completion_tokens"),
        }
        with USAGE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 - 记量失败绝不能影响主链路
        log.warning("用量记录失败: %s", e)


# ── 端点 ────────────────────────────────────────────────────────────────────
@app.get("/gw/health")
def health() -> dict:
    """无需鉴权：只回「是否就绪 + 可用模型 + 可代做的能力位」，绝不回上游 Key。"""
    up = _upstream()
    key = (up.get("api_key") or "").strip()
    return {
        "ok": bool(key and up.get("base_url")),
        "provider": up.get("provider", ""),
        "models": up.get("models", []) or [],
        "tokens_issued": len(_tokens()),
        "matting": _matting_caps(),
    }


@app.get("/gw/v1/models")
def list_models(authorization: str | None = Header(default=None)) -> dict:
    _authenticate(authorization)
    up = _upstream()
    models = up.get("models") or []
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": up.get("provider", "vdl")} for m in models],
    }


@app.post("/gw/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Any:
    meta = _authenticate(authorization)
    up = _upstream()
    base = (up.get("base_url") or "").rstrip("/")
    key = (up.get("api_key") or "").strip()
    if not base or not key:
        raise HTTPException(status_code=503, detail="gateway upstream not configured")
    allow = set(up.get("models") or [])

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request too large")
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    model = str(payload.get("model") or "").strip()
    if allow and model not in allow:
        raise HTTPException(status_code=403, detail=f"model not allowed: {model}")
    # 强制关闭客户端侧的流式计费统计，避免与服务端语义不一致
    payload.pop("stream_options", None)
    stream = bool(payload.get("stream"))

    url = base + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }

    # ⚠️ 流式绝不能用 `async with AsyncClient()` 包住 return：上下文会在 FastAPI
    # 开始迭代生成器**之前**就退出，客户端被关闭 → SSE 返回 0 字节、客户端收到
    # IncompleteRead（2026-09-15 实测：/gw/ 流式永远空而非流式正常，排查绕了一大圈）。
    # 正解：客户端生命周期绑定到生成器，在 _gen 的 finally 里关闭。
    client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)

    if not stream:
        try:
            try:
                r = await client.post(url, headers=headers, json=payload)
            except httpx.RequestError as e:
                log.error("上游请求失败: %s", e)
                raise HTTPException(status_code=502, detail=f"upstream error: {e}")
            body: dict[str, Any] = {}
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = {"raw": r.text[:2000]}
            _record_usage(meta, model, body.get("usage") if isinstance(body, dict) else None,
                          r.status_code < 400)
            if r.status_code >= 400:
                log.warning("上游返回 %s: %s", r.status_code, str(body)[:400])
            return JSONResponse(status_code=r.status_code, content=body)
        finally:
            await client.aclose()

    req = client.build_request("POST", url, headers=headers, json=payload)

    async def _gen():  # noqa: ANN202
        usage: dict[str, Any] | None = None
        try:
            async with client.stream(req.method, req.url, headers=req.headers,
                                     content=req.content) as resp:
                if resp.status_code >= 400:
                    err = (await resp.aread()).decode("utf-8", "replace")[:2000]
                    _record_usage(meta, model, None, False)
                    yield (
                        "data: "
                        + json.dumps({"error": {"message": err, "status": resp.status_code}},
                                     ensure_ascii=False)
                        + "\n\n"
                    ).encode()
                    return
                async for chunk in resp.aiter_bytes():
                    if not chunk:
                        continue
                    # 从 SSE 里挑出带 usage 的那一帧用于记量（不落正文）
                    try:
                        for line in chunk.decode("utf-8", "replace").splitlines():
                            if line.startswith("data: ") and '"usage"' in line:
                                data = json.loads(line[6:])
                                if isinstance(data.get("usage"), dict):
                                    usage = data["usage"]
                    except Exception:  # noqa: BLE001
                        pass
                    yield chunk
                _record_usage(meta, model, usage, True)
        except httpx.RequestError as e:
            _record_usage(meta, model, None, False)
            log.error("上游流式请求失败: %s", e)
            yield (
                "data: " + json.dumps({"error": {"message": str(e)}}, ensure_ascii=False) + "\n\n"
            ).encode()
        finally:
            await client.aclose()

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ── 云端抠图代理（火山引擎视觉智能 / AI MediaKit）────────────────────────────
# 为什么放在这里
# --------------
# 桌面端原先**直连火山**：AK/SK 必须写到用户机器上（`cloud_matting_managed.json`
# 0600 挡不住 App 自己读它）⇒ 一旦分发给他人，等于把钥匙一起发出去。
# 改为本服务代做之后：凭据只在 `/opt/vdl-gateway/matting.json`(0600)，
# 客户端只持可单独吊销的令牌，与 LLM 网关同一套鉴权。
#
# 端点
# ----
#   GET  /gw/v1/matting/capabilities        需鉴权，只回布尔能力位
#   POST /gw/v1/image/remove-background     需鉴权，MediaKit「增强 + 抠图」全流程
#   POST /gw/v1/image/segment               需鉴权，CV SigV4 分割（人像/通用）
#
# 换算出的一张图**原样透传字节**（本服务不解码图像，也不落盘）。
_MEDIAKIT_HOST = "mediakit.cn-beijing.volces.com"
_MK_UPLOAD_URL = f"https://{_MEDIAKIT_HOST}/api/v1/tools-sync/request-media-upload-url"
_MK_REMOVE_URL = f"https://{_MEDIAKIT_HOST}/api/v1/tools-sync/remove-image-background"
_MK_ENHANCE_URL = f"https://{_MEDIAKIT_HOST}/api/v1/tools-sync/enhance-image"

_VOLC_HOST = "visual.volcengineapi.com"
_VOLC_REGION = "cn-north-1"
_VOLC_SERVICE = "cv"
_VOLC_VERSION = "2020-08-26"

# 单张图上限：base64 前 96MB（与原直连链路的量级一致；nginx /gw/ 已同步放宽）
IMAGE_MAX_BYTES = 96 * 1024 * 1024
_MK_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=180.0, pool=15.0)

# 上游证书校验：默认开启；仅当运维显式设 VDL_GW_TLS_INSECURE=1 才关闭（排障用）
_TLS_VERIFY = (os.environ.get("VDL_GW_TLS_INSECURE") or "").strip().lower() not in (
    "1", "true", "yes", "on")


def _matting_cfg() -> dict[str, Any]:
    """服务端抠图凭据。环境变量可覆盖文件（容器/运维下发）。"""
    cfg = dict(_read_json(MATTING_FILE))
    for env, key in (("VDL_GW_MAT_AK", "ak"),
                     ("VDL_GW_MAT_SK", "sk"),
                     ("VDL_GW_MAT_MEDIAKIT_KEY", "mediakit_api_key")):
        val = (os.environ.get(env) or "").strip()
        if val:
            cfg[key] = val
    return cfg


def _matting_caps() -> dict[str, bool]:
    """能力位——**只回布尔，绝不回凭据**。"""
    cfg = _matting_cfg()
    return {
        "mediakit": bool((cfg.get("mediakit_api_key") or "").strip()),
        "visual": bool((cfg.get("ak") or "").strip() and (cfg.get("sk") or "").strip()),
    }


def _img_content_type(data: bytes) -> str:
    """按魔数判图片类型（本服务不解码图像，只据此填上传的 Content-Type）。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _mk_post(url: str, key: str, payload: dict, timeout: int = 180) -> dict:
    """MediaKit JSON 接口（Bearer 鉴权由本服务注入，客户端看不到 Key）。"""
    try:
        r = httpx.post(url, json=payload, timeout=_MK_TIMEOUT, verify=_TLS_VERIFY,
                       headers={"Authorization": f"Bearer {key}",
                                "Content-Type": "application/json",
                                "Accept": "application/json"})
    except httpx.RequestError as e:
        raise RuntimeError(f"MediaKit 请求失败 {url.rsplit('/', 1)[-1]}: {e}") from e
    if r.status_code >= 400:
        raise RuntimeError(f"MediaKit {url.rsplit('/', 1)[-1]} HTTP {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError as e:
        raise RuntimeError(f"MediaKit 返回非 JSON: {r.text[:200]}") from e


def _mk_request_upload(key: str) -> dict:
    resp = _mk_post(_MK_UPLOAD_URL, key, {})
    if not resp.get("success"):
        raise RuntimeError(f"MediaKit 申请上传地址失败: {resp.get('error') or resp}")
    result = resp.get("result") or {}
    if not result.get("file_id") or not result.get("upload_url"):
        raise RuntimeError(f"MediaKit 上传地址字段缺失: {resp}")
    return result


def _mk_put(upload_url: str, img: bytes, content_type: str) -> None:
    try:
        r = httpx.put(upload_url, content=img, timeout=_MK_TIMEOUT, verify=_TLS_VERIFY,
                      headers={"Content-Type": content_type})
    except httpx.RequestError as e:
        raise RuntimeError(f"MediaKit 上传失败: {e}") from e
    if r.status_code >= 300:
        raise RuntimeError(f"MediaKit 上传返回 HTTP {r.status_code}")


def _mk_get(url: str) -> bytes:
    try:
        r = httpx.get(url, timeout=_MK_TIMEOUT, verify=_TLS_VERIFY, follow_redirects=True)
    except httpx.RequestError as e:
        raise RuntimeError(f"MediaKit 结果图下载失败: {e}") from e
    if r.status_code >= 400:
        raise RuntimeError(f"MediaKit 结果图下载 HTTP {r.status_code}")
    return r.content


def _mk_enhance_and_remove(img: bytes, scene: str, ver: str, multiple: float,
                           key: str) -> tuple[bytes, bool]:
    """MediaKit「（可选）画质增强 → 抠图」全流程，返回 (结果图字节, 是否增强过)。

    增强是独立计费步骤；失败时**降级为不增强**而不是整单失败——与桌面端直连时的
    行为一致（那边也是 warning 后回退原始链路）。
    """
    enhanced = False
    if ver in ("standard", "professional", "max"):
        try:
            up = _mk_request_upload(key)
            _mk_put(up["upload_url"], img, _img_content_type(img))
            resp = _mk_post(_MK_ENHANCE_URL, key,
                            {"image_url": up["file_id"], "tool_version": ver,
                             "multiple": multiple})
            if not resp.get("success"):
                raise RuntimeError(f"enhance failed: {resp.get('error') or resp}")
            url = (resp.get("result") or {}).get("image_url")
            if not url:
                raise RuntimeError("enhance 未返回结果图")
            img = _mk_get(url)
            enhanced = True
        except Exception as e:  # noqa: BLE001 - 增强失败回退原图，不拖垮抠图
            log.warning("画质增强失败，回退未增强链路: %s", e)

    up = _mk_request_upload(key)
    _mk_put(up["upload_url"], img, _img_content_type(img))
    payload = {"image_url": up["file_id"], "scene": scene}
    try:
        resp = _mk_post(_MK_REMOVE_URL, key, payload)
        if not resp.get("success"):
            raise RuntimeError(f"抠图失败: {resp.get('error') or resp}")
        url = ((resp.get("result") or {}).get("image_url") or "")
        if not url:
            raise RuntimeError(f"抠图未返回结果图: {str(resp)[:200]}")
        return _mk_get(url), enhanced
    except Exception:
        # 提交/查询阶段抖动重试 1 次：复用同一个 file_id，不重复上传、不重复计费
        log.warning("抠图请求失败，2s 后重试 1 次")
        time.sleep(2.0)
        resp = _mk_post(_MK_REMOVE_URL, key, payload)
        if not resp.get("success"):
            raise RuntimeError(f"抠图失败: {resp.get('error') or resp}")
        url = ((resp.get("result") or {}).get("image_url") or "")
        if not url:
            raise RuntimeError(f"抠图未返回结果图: {str(resp)[:200]}")
        return _mk_get(url), enhanced


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _volc_signed_post(action: str, body: bytes, content_type: str, ak: str, sk: str) -> dict:
    """火山视觉智能 SigV4 变体签名后调用（签名只在本服务内完成）。"""
    query = f"Action={action}&Version={_VOLC_VERSION}"
    url = f"https://{_VOLC_HOST}/?{query}"
    now = datetime.datetime.utcnow()
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{_VOLC_HOST}\n"
        f"x-content-sha256:{payload_hash}\n"
        f"x-date:{x_date}\n"
    )
    canonical_request = "\n".join([
        "POST", "/", query, canonical_headers, signed_headers, payload_hash,
    ])
    scope = f"{short_date}/{_VOLC_REGION}/{_VOLC_SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k_date = _hmac(sk.encode("utf-8"), short_date)
    k_region = _hmac(k_date, _VOLC_REGION)
    k_service = _hmac(k_region, _VOLC_SERVICE)
    k_signing = _hmac(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "Host": _VOLC_HOST,
        "X-Date": x_date,
        "X-Content-Sha256": payload_hash,
        "Content-Type": content_type,
        "Authorization": (f"HMAC-SHA256 Credential={ak}/{scope}, "
                          f"SignedHeaders={signed_headers}, Signature={signature}"),
    }
    try:
        r = httpx.post(url, content=body, headers=headers, timeout=_MK_TIMEOUT,
                       verify=_TLS_VERIFY)
    except httpx.RequestError as e:
        raise RuntimeError(f"火山 {action} 请求失败: {e}") from e
    if r.status_code >= 400:
        raise RuntimeError(f"火山 {action} HTTP {r.status_code}: {r.text[:200]}")
    try:
        parsed = r.json()
    except ValueError as e:
        raise RuntimeError(f"火山 {action} 返回非 JSON: {r.text[:200]}") from e
    if parsed.get("code") not in (0, None, 10000):
        raise RuntimeError(f"火山 {action} 业务错误 code={parsed.get('code')} "
                           f"msg={parsed.get('message')}")
    return parsed


def _record_matting(meta: dict[str, Any], scene: str, ok: bool) -> None:
    """只记统计量（谁 / 场景 / 成败 / 耗时无从得知就不编），不记图像内容。"""
    try:
        row = {"ts": int(time.time()), "who": meta.get("name", "unknown"),
               "kind": "matting", "model": scene, "ok": bool(ok)}
        with USAGE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 - 记量失败绝不能影响主链路
        log.warning("抠图用量记录失败: %s", e)


async def _read_image_payload(request: Request, field: str = "image_b64") -> dict:
    raw = await request.body()
    if len(raw) > IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="request too large")
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid json")
    b64 = str(payload.get(field) or "").strip()
    if not b64:
        raise HTTPException(status_code=400, detail=f"{field} required")
    try:
        blob = base64.b64decode(b64, validate=False)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"{field} not base64")
    if not blob or len(blob) > IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="image too large")
    return {"payload": payload, "blob": blob, "b64": b64}


@app.post("/gw/v1/image/enhance")
async def image_enhance(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """MediaKit 画质增强（豆包同源生成式）。失败直接 502，由客户端按其原有逻辑回退。"""
    meta = _authenticate(authorization)
    cfg = _matting_cfg()
    key = (cfg.get("mediakit_api_key") or "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="gateway matting not configured")
    srv_ver = str(cfg.get("enhance_version") or "").strip().lower()
    if srv_ver == "off":
        raise HTTPException(status_code=503, detail="enhance disabled by server")

    got = await _read_image_payload(request)
    ver = str(got["payload"].get("version") or "professional").strip().lower()
    if srv_ver and srv_ver != "auto":
        ver = srv_ver
    if ver not in ("standard", "professional", "max"):
        ver = "professional"
    try:
        multiple = float(got["payload"].get("multiple") if got["payload"].get("multiple") is not None else 2.0)
    except (TypeError, ValueError):
        multiple = 2.0

    try:
        up = _mk_request_upload(key)
        _mk_put(up["upload_url"], got["blob"], _img_content_type(got["blob"]))
        resp = _mk_post(_MK_ENHANCE_URL, key,
                        {"image_url": up["file_id"], "tool_version": ver,
                         "multiple": multiple})
        if not resp.get("success"):
            raise RuntimeError(f"增强失败: {resp.get('error') or resp}")
        url = ((resp.get("result") or {}).get("image_url") or "")
        if not url:
            raise RuntimeError(f"增强未返回结果图: {str(resp)[:200]}")
        out = _mk_get(url)
    except Exception as e:  # noqa: BLE001
        _record_matting(meta, "enhance", False)
        log.error("画质增强上游失败: %s", e)
        raise HTTPException(status_code=502,
                            detail=f"upstream enhance failed: {type(e).__name__}")
    _record_matting(meta, "enhance", True)
    return {"ok": True, "kind": "rgb", "format": "png",
            "image_b64": base64.b64encode(out).decode("ascii"), "version": ver}


@app.get("/gw/v1/matting/capabilities")
def matting_capabilities(authorization: str | None = Header(default=None)) -> dict:
    """需鉴权：告诉客户端「服务端能代做哪种抠图」。只回布尔。"""
    _authenticate(authorization)
    caps = _matting_caps()
    return {"ok": True, **caps}


@app.post("/gw/v1/image/remove-background")
async def image_remove_background(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """MediaKit 软 alpha 抠图（可带画质增强）。返回结果图 base64。"""
    meta = _authenticate(authorization)
    cfg = _matting_cfg()
    key = (cfg.get("mediakit_api_key") or "").strip()
    if not key:
        raise HTTPException(status_code=503, detail="gateway matting not configured")

    got = await _read_image_payload(request)
    payload = got["payload"]

    scene = str(payload.get("scene") or "general").strip().lower()
    if scene not in ("general", "human", "product"):
        scene = "general"
    ver = str(payload.get("enhance_version") or "").strip().lower()
    # 金库侧可强制档位（控费）；默认不覆盖客户端选择
    srv_ver = str(cfg.get("enhance_version") or "").strip().lower()
    if srv_ver and srv_ver != "auto":
        ver = srv_ver
    if ver not in ("standard", "professional", "max"):
        ver = ""
    try:
        multiple = float(payload.get("multiple") if payload.get("multiple") is not None else 2.0)
    except (TypeError, ValueError):
        multiple = 2.0

    try:
        out, enhanced = _mk_enhance_and_remove(got["blob"], scene, ver, multiple, key)
    except Exception as e:  # noqa: BLE001
        _record_matting(meta, scene, False)
        log.error("抠图上游失败: %s", e)
        raise HTTPException(status_code=502,
                            detail=f"upstream matting failed: {type(e).__name__}")
    _record_matting(meta, scene, True)
    return {"ok": True, "kind": "rgba", "format": "png",
            "image_b64": base64.b64encode(out).decode("ascii"),
            "enhanced": bool(enhanced)}


@app.post("/gw/v1/image/segment")
async def image_segment(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    """火山 CV 分割（HumanSegment / GeneralSegment）。返回前景 PNG 或 mask。"""
    meta = _authenticate(authorization)
    cfg = _matting_cfg()
    ak = (cfg.get("ak") or "").strip()
    sk = (cfg.get("sk") or "").strip()
    if not ak or not sk:
        raise HTTPException(status_code=503, detail="gateway matting not configured")

    got = await _read_image_payload(request)
    person = bool(got["payload"].get("person"))
    action = "HumanSegment" if person else "GeneralSegment"
    body = ("image_base64=" + urllib.parse.quote(got["b64"], safe="")
            + "&return_foreground_image=1&refine=1").encode("utf-8")
    try:
        parsed = _volc_signed_post(action, body, "application/x-www-form-urlencoded", ak, sk)
    except Exception as e:  # noqa: BLE001
        _record_matting(meta, action, False)
        log.error("CV 分割上游失败: %s", e)
        raise HTTPException(status_code=502,
                            detail=f"upstream segment failed: {type(e).__name__}")
    data = parsed.get("data") or {}
    fg = data.get("foreground_image") or data.get("foreground")
    if fg:
        _record_matting(meta, action, True)
        return {"ok": True, "kind": "foreground", "image_b64": fg}
    mask = data.get("mask")
    if mask:
        _record_matting(meta, action, True)
        return {"ok": True, "kind": "mask", "image_b64": mask}
    _record_matting(meta, action, False)
    raise HTTPException(status_code=502, detail="upstream segment missing foreground/mask")

