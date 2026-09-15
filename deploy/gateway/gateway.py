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

import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

BASE = Path(os.environ.get("VDL_GATEWAY_HOME", "/opt/vdl-gateway"))
UPSTREAM_FILE = BASE / "upstream.json"
TOKENS_FILE = BASE / "tokens.json"
USAGE_FILE = BASE / "usage.jsonl"

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
    """无需鉴权：只回「是否就绪 + 可用模型」，绝不回上游 Key。"""
    up = _upstream()
    key = (up.get("api_key") or "").strip()
    return {
        "ok": bool(key and up.get("base_url")),
        "provider": up.get("provider", ""),
        "models": up.get("models", []) or [],
        "tokens_issued": len(_tokens()),
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

    async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
        if not stream:
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
            _record_usage(meta, model, body.get("usage") if isinstance(body, dict) else None, r.status_code < 400)
            if r.status_code >= 400:
                log.warning("上游返回 %s: %s", r.status_code, str(body)[:400])
            return JSONResponse(status_code=r.status_code, content=body)

        req = client.build_request("POST", url, headers=headers, json=payload)

        async def _gen():  # noqa: ANN202
            usage: dict[str, Any] | None = None
            try:
                async with client.stream(req.method, req.url, headers=req.headers, content=req.content) as resp:
                    if resp.status_code >= 400:
                        err = (await resp.aread()).decode("utf-8", "replace")[:2000]
                        _record_usage(meta, model, None, False)
                        yield f"data: {json.dumps({'error': {'message': err, 'status': resp.status_code}}, ensure_ascii=False)}\n\n".encode()
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
                yield f"data: {json.dumps({'error': {'message': str(e)}}, ensure_ascii=False)}\n\n".encode()

        return StreamingResponse(_gen(), media_type="text/event-stream")
