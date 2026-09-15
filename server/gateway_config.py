"""云端网关配置 —— 让真实 API Key 只留在服务端，本机只持有可吊销的令牌。

背景
----
桌面版解说需要云端 LLM。原先把管理员 Key 写进 `~/.video-downloader/llm_managed.json`，
自用尚可，但**分发给他人 = 把 Key 一起发出去**（0600 挡不住，App 自己就能读）。

改为服务端代理后：本机只有 `url + token`，token 可单独吊销、不含任何上游凭据；
真正的 Key 只在网关服务的 `upstream.json` 里。

优先级：**环境变量 > 管理员受管文件 > 用户文件**（与 llm_config 同构）。
环境变量供容器/运维下发；受管文件（0600）供桌面版管理员下发；用户文件仅作兜底。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 3.0          # 探测可用模型时的超时：宁可拿不到，也不能卡住任务
MODELS_CACHE_TTL = 300.0       # 模型列表缓存 5 分钟，避免每次任务都打一次网关

_MODELS_CACHE: dict[str, Any] = {"ts": 0.0, "models": []}


def _config_dir() -> Path:
    """配置目录：VDL_HOME 可覆盖（测试隔离用），否则 ~/.video-downloader。"""
    override = (os.environ.get("VDL_HOME") or "").strip()
    base = Path(override) if override else Path.home() / ".video-downloader"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base


def _user_path() -> Path:
    return _config_dir() / "gateway.json"


def _managed_path() -> Path:
    return _config_dir() / "gateway_managed.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _normalize_url(url: str) -> str:
    """去掉结尾斜杠，避免拼出 `//v1`。

    支持 `direct://` 前缀（原样保留）：含义见 _DIRECT_PREFIX——网关是国内 ECS、
    无需代理；本机代理（Karing 等）节点断开时会 0.0s 回 502，把本来可达的网关拖死
    （2026-09-15 实测：验收任务 LLM 8 连 502 全是本机代理回的，ECS 日志里根本没有请求）。
    """
    return (url or "").strip().rstrip("/")


_DIRECT_PREFIX = "direct://"


def _strip_direct(url: str) -> str:
    """剥掉 direct:// 前缀，得到真实可请求的 URL（无前缀则原样返回）。"""
    u = url or ""
    return u[len(_DIRECT_PREFIX):] if u.startswith(_DIRECT_PREFIX) else u


def _has_direct(url: str) -> bool:
    return (url or "").startswith(_DIRECT_PREFIX)


def _opener_for(url: str):
    """direct:// 前缀 → 返回绕开系统代理/环境变量代理的 opener；否则 None（urllib 默认）。"""
    if _has_direct(url):
        import urllib.request
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return None


def get_gateway_config() -> dict[str, Any]:
    """读取网关配置。

    返回 {enabled, url, token, source}；`enabled` 为 False 时调用方应回退到直连凭据。
    """
    cfg: dict[str, Any] = {"enabled": False, "url": "", "token": "", "source": ""}

    # 1) 用户文件（最低优先级）
    saved = _read_json(_user_path())
    if saved.get("url"):
        cfg["url"] = _normalize_url(str(saved.get("url") or ""))
        cfg["token"] = str(saved.get("token") or "").strip()
        cfg["enabled"] = bool(saved.get("enabled", True))
        cfg["source"] = "user"

    # 2) 管理员受管文件（覆盖用户文件）
    managed = _read_json(_managed_path())
    if managed.get("url"):
        cfg["url"] = _normalize_url(str(managed.get("url") or ""))
        cfg["token"] = str(managed.get("token") or "").strip()
        cfg["enabled"] = bool(managed.get("enabled", True))
        cfg["source"] = "managed"

    # 3) 环境变量（最终裁决）
    env_url = _normalize_url(os.environ.get("VDL_GATEWAY_URL", ""))
    env_token = (os.environ.get("VDL_GATEWAY_TOKEN") or "").strip()
    if env_url:
        cfg["url"] = env_url
        cfg["source"] = "env"
    if env_token:
        cfg["token"] = env_token
        if not cfg["source"]:
            cfg["source"] = "env"
    env_enabled = (os.environ.get("VDL_GATEWAY_ENABLED") or "").strip().lower()
    if env_enabled in ("0", "false", "no", "off"):
        cfg["enabled"] = False
    elif env_enabled in ("1", "true", "yes", "on"):
        cfg["enabled"] = True

    # 缺 url 或 token 都视为未接入（半配置比不配置更危险：会让人以为在走网关）
    if not cfg["url"] or not cfg["token"]:
        cfg["enabled"] = False
    return cfg


def mask_token(token: str) -> str:
    """脱敏：只留前缀与末 4 位，便于用户比对是哪一枚令牌。"""
    t = (token or "").strip()
    if len(t) <= 10:
        return t[:2] + "****" if t else ""
    return t[:6] + "****" + t[-4:]


def gateway_status() -> dict[str, Any]:
    """供界面展示的状态（绝不返回完整令牌）。url 展示剥掉 direct:// 前缀（内部配置保留）。"""
    cfg = get_gateway_config()
    return {
        "enabled": bool(cfg["enabled"]),
        "url": _strip_direct(cfg["url"]),
        "direct": _has_direct(cfg["url"]),
        "has_token": bool(cfg["token"]),
        "token_masked": mask_token(cfg["token"]),
        "source": cfg["source"],
        "managed_file": str(_managed_path()),
    }


def upstream_models(ttl: float = MODELS_CACHE_TTL) -> list[str]:
    """网关可用模型列表；拿不到就返回空列表，由调用方容错。

    三条来源，按优先级：
      1. `VDL_GATEWAY_MODELS` 环境变量（离线/内网运维显式指定，零网络）
      2. 网关 `/gw/health`（缓存 ttl 秒）
      3. 空列表（交给服务端白名单去拒绝，错误信息由服务端给出）
    """
    env_models = (os.environ.get("VDL_GATEWAY_MODELS") or "").strip()
    if env_models:
        return [m.strip() for m in env_models.split(",") if m.strip()]

    cfg = get_gateway_config()
    if not cfg["enabled"]:
        return []
    now = time.time()
    if _MODELS_CACHE["models"] and now - _MODELS_CACHE["ts"] < ttl:
        return list(_MODELS_CACHE["models"])

    models: list[str] = []
    try:
        import urllib.request

        url = _strip_direct(cfg["url"]) + "/health"
        opener = _opener_for(cfg["url"])
        _open = opener.open if opener is not None else urllib.request.urlopen
        with _open(url, timeout=DEFAULT_TIMEOUT) as resp:  # noqa: S310 - 固定内网/自建网关
            data = json.loads(resp.read().decode("utf-8", "replace"))
        raw = data.get("models") or []
        if isinstance(raw, list):
            models = [str(m).strip() for m in raw if str(m).strip()]
    except Exception:  # noqa: BLE001 - 探测失败不影响主流程
        models = []

    if models:
        _MODELS_CACHE["ts"] = now
        _MODELS_CACHE["models"] = models
    return models


def resolve_model(preferred: str = "") -> str:
    """在网关白名单内挑一个可用模型。

    preferred 命中白名单就沿用，否则退到白名单第一个——避免客户端拿着旧模型名
    去撞服务端的 403。拿不到白名单时原样返回（失败由服务端给出明确错误）。
    """
    want = (preferred or "").strip()
    allow = upstream_models()
    if not allow:
        return want
    if want in allow:
        return want
    return allow[0]


def cloud_env(preferred_model: str = "") -> dict[str, str] | None:
    """网关可用时返回注入子进程的环境变量；否则 None（调用方回退直连 Key）。

    注意：这里返回的是**令牌**，不是上游 API Key——本机任何时候都不接触真实 Key。
    """
    cfg = get_gateway_config()
    if not cfg["enabled"]:
        return None
    return {
        "base_url": cfg["url"] + "/v1",
        "model": resolve_model(preferred_model),
        "api_key": cfg["token"],
    }
