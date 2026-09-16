"""统一 LLM 配置（服务商选择器）。

解说管线(llm_script.py) 和字幕翻译(subtitles.py) 原本各自硬编码 OpenAI 端点、从不同环境变量
读 Key——没有共享配置层、没有前端 UI。本模块集中管理：提供商预设、持久化 JSON 配置、注入环境变量。

Provider presets:
  - OpenAI / DeepSeek / 通义千问 / 智谱 GLM / Moonshot / Ollama(本机)
  - 「你的托管中转」自定义：base_url + model 都由用户填，endpoint + token 字段预留后期云增强。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import atomic_io

# ── 提供商预设 ─────────────────────────────────────────────────────────
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
    "qwen": {
        "name": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
    },
    "zhipu": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
    },
    "moonshot": {
        "name": "Moonshot (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
    },
    "ollama": {
        "name": "Ollama (本机)",
        "base_url": "http://localhost:11434/v1",
        "default_model": "",
    },
    "custom": {
        "name": "你的托管中转",
        "base_url": "",
        "default_model": "",
    },
}

DEFAULT_PROVIDER = "openai"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.7
# 推理强度：解说管线 Stage2 默认低推理（省钱且质量够用）；关闭=最省 token，高=最佳质量最贵。
DEFAULT_REASONING_EFFORT = "low"
# 避开峰时：北京工作日 09-12 / 14-18 为 DeepSeek 峰时（价格约 2 倍），开启后调度到闲时再生成。
DEFAULT_OFFPEAK_ONLY = False

# ── 配置文件路径 ─────────────────────────────────────────────────────────
def _config_dir() -> Path:
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def _config_path() -> Path:
    return _config_dir() / "llm_config.json"


def _managed_path() -> Path:
    """管理员受管配置路径（与用户配置分离）。

    产品定位（2026-09-15 用户拍板）：普通用户不需要、也不应该在界面上配置模型
    密钥——云端服务由**超级管理员统一配置**。用户只选择「纯云端」或「本机优先 +
    云端配合」两种模式，凭据四件套（provider / api_key / base_url / model）由本
    文件承载，优先级低于环境变量、高于用户 JSON。
    """
    return _config_dir() / "llm_managed.json"


def mask_key(key: str) -> str:
    """密钥脱敏展示（只留首尾各 4 位）。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return k[:4] + "****" + k[-4:]


# ── 本机 Ollama 探测（本地优先开关用）─────────────────────────────────────
_ollama_cache: dict = {"ts": 0.0, "val": None}
_OLLAMA_CACHE_TTL = 10.0  # 秒；探测本机端口，缓存避免高频重复请求


def detect_ollama(timeout: float = 2.0) -> dict[str, Any]:
    """探测本机 Ollama 是否在运行，返回可用模型名列表。

    返回 {"running": bool, "models": [str], "error": str|None}。
    Ollama 未安装/未启动时连接被拒（ConnectionRefused），属"正常运行中"分支，秒级返回。
    """
    global _ollama_cache
    now = time.time()
    if _ollama_cache["val"] is not None and now - _ollama_cache["ts"] < _OLLAMA_CACHE_TTL:
        return _ollama_cache["val"]
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        models = [m.get("name") for m in data.get("models", []) if m.get("name")]
        val = {"running": True, "models": models, "error": None}
    except Exception as e:  # noqa: BLE001
        val = {"running": False, "models": [], "error": str(e)}
    _ollama_cache = {"ts": now, "val": val}
    return val


def resolve_local_model(preferred: str, models: list) -> tuple:
    """在本机模型列表里解析出「该用哪个模型」。

    返回 `(model, stale_original)`，`stale_original` 非 None 表示原选择已失效。

    三档策略（为什么不能只做精确匹配）：
    - **精确命中** → 原样使用，尊重用户选择；
    - **仅基名（冒号前）命中** → 改用探测到的那个变体。Ollama 常见
      `qwen2.5vl:7b` / `qwen2.5vl:latest` 这类同模型不同 tag，探测到的那个一定可用，
      死守原 tag 反而可能失败；
    - **都没命中** → 说明模型已被卸载/改名，回落到第一个可用模型，并回传原值供提示。
    """
    models = [str(m) for m in (models or [])]
    if not models:
        return (preferred or ""), None
    if not preferred:
        return models[0], None
    if preferred in models:
        return preferred, None
    base = preferred.split(":", 1)[0]
    same_base = next((m for m in models if m.split(":", 1)[0] == base), None)
    if same_base:
        return same_base, None
    return models[0], preferred


# ── 配置读写 ─────────────────────────────────────────────────────────────
def _env_api_key() -> str:
    """从环境变量读 API Key（支持 LLM_API_KEY 和 LLM_APIKEY 两种写法）。"""
    return (
        os.environ.get("LLM_API_KEY") or os.environ.get("LLM_APIKEY") or ""
    ).strip()


def get_llm_config() -> dict[str, Any]:
    """读取完整 LLM 配置，优先级：环境变量 > JSON 文件 > 硬编码默认值。

    环境变量为"最终覆盖"（服务端/容器部署不改文件），文件为"前端持久化"（桌面用户 UI 保存）。
    互不冲突：用户在 UI 保存后会写入 JSON，但环境变量设了就会覆盖 JSON 里的同名字段。
    """
    cfg: dict[str, Any] = {
        "provider": DEFAULT_PROVIDER,
        "api_key": "",
        "base_url": "",
        "model": "",
        "max_tokens": DEFAULT_MAX_TOKENS,
        "temperature": DEFAULT_TEMPERATURE,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "offpeak_only": DEFAULT_OFFPEAK_ONLY,
        "local_priority": False,   # 本地优先开关（UI）：开启且显式选了云端(engine=cloud)时回落本机优先(engine=auto=MLX本地优先→云端回落)；不再强切 Ollama
        "local_model": "",         # 本地优先时选用的本机模型（留空=用检测到的第一个）
        "engine": "auto",         # 引擎选择：auto(本机优先→云端回落) / cloud(纯云端) / mlx(强制本地) / ollama(强制Ollama)
        "mlx_model_path": "",      # 本机 MLX 权重目录（仅 macOS Apple Silicon 有效）
        "mlx_python": "",         # 独立 mlx 运行时 python（隔离 venv），留空=默认 python3
        "mlx_max_tokens": 0,      # 本机引擎输出上限：0=用管线内置默认(8192)；>0=显式上限；负数不进子进程（管线侧 <=0 表示不设闸门）
    }

    # 1) JSON 文件（桌面版前端持久化）
    cp = _config_path()
    if cp.is_file():
        try:
            saved = json.loads(cp.read_text(encoding="utf-8"))
            for k in ("provider", "api_key", "base_url", "model", "max_tokens",
                      "temperature", "reasoning_effort", "offpeak_only",
                      "local_priority", "local_model", "engine",
                      "mlx_model_path", "mlx_python", "mlx_max_tokens"):
                if k in saved:
                    cfg[k] = saved[k]
        except (json.JSONDecodeError, OSError):
            pass

    # 1.5) 管理员受管配置（llm_managed.json）：凭据由超级管理员统一配置，用户不接触。
    # 优先级：环境变量 > 受管配置 > 用户 JSON。空值不覆盖（避免误清空已在用的凭据）。
    mp = _managed_path()
    if mp.is_file():
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            if isinstance(m, dict):
                for k in ("provider", "api_key", "base_url", "model"):
                    v = m.get(k)
                    if isinstance(v, str) and v.strip():
                        cfg[k] = v.strip()
        except (json.JSONDecodeError, OSError):
            pass

    # 1.6) 引擎档位收敛为用户可见的两档：auto（本机优先 → 云端配合）/ cloud（纯云端）。
    # 旧版曾提供 mlx / ollama 强制档，现按产品决策下线（用户不必理解引擎细节）。
    # 注意：只归一「配置文件」来源；环境变量仍可强制任意值（管理员/运维通道）。
    _eng = str(cfg.get("engine") or "auto").strip().lower()
    if _eng in ("mlx", "ollama"):
        cfg["engine"] = "auto"

    # 2) 环境变量覆盖（最终裁决）
    env_key = _env_api_key()
    if env_key:
        cfg["api_key"] = env_key
    env_base = os.environ.get("LLM_BASE_URL", "").strip()
    if env_base:
        cfg["base_url"] = env_base.rstrip("/")
    env_model = os.environ.get("LLM_MODEL", "").strip()
    if env_model:
        cfg["model"] = env_model
    env_tok = os.environ.get("LLM_MAX_TOKENS", "").strip()
    if env_tok:
        try:
            cfg["max_tokens"] = int(env_tok)
        except ValueError:
            pass
    # 推理强度 / 避开峰时：环境变量为最终裁决（便于运维/容器覆盖 UI 设置）
    env_re = os.environ.get("VDL_LLM_REASONING_EFFORT", "").strip()
    if env_re:
        cfg["reasoning_effort"] = env_re
    env_off = os.environ.get("LLM_OFFPEAK_ONLY", "").strip()
    if env_off and env_off.lower() in ("1", "true", "yes", "on"):
        cfg["offpeak_only"] = True
    # 本地优先：环境变量为最终裁决（容器/运维可强制开启）
    env_lp = os.environ.get("VDL_LLM_LOCAL_PRIORITY", "").strip()
    if env_lp and env_lp.lower() in ("1", "true", "yes", "on"):
        cfg["local_priority"] = True
    # 引擎选择（最终裁决）：auto / cloud / mlx / ollama
    env_engine = os.environ.get("VDL_LLM_ENGINE", "").strip().lower()
    if env_engine:
        cfg["engine"] = env_engine
    env_mp = os.environ.get("MLX_MODEL_PATH", "").strip()
    if env_mp:
        cfg["mlx_model_path"] = env_mp
    env_mpy = os.environ.get("MLX_PYTHON", "").strip()
    if env_mpy:
        cfg["mlx_python"] = env_mpy

    # 本地优先开关（UI「本地优先」）：若用户显式选了纯云端(engine=cloud)，
    # 开启本地优先则回落本机优先 engine=auto（MLX 本地优先 → 云端回落）。
    # 不再强切 Ollama（本机 Ollama Metal 后端已弃用），本地引擎统一走 MLX。
    if cfg["local_priority"] and cfg.get("engine") == "cloud":
        cfg["engine"] = "auto"

    # 2.5) 本地优先解析：Ollama 在跑则整体切到本机，零成本
    # Ollama 引擎仅在 engine=ollama 时触发；auto/mlx 模式的本地优先由 llm_script.py 走 MLX，不再强切 Ollama
    cfg["_ollama_running"] = False
    if cfg.get("engine") == "ollama":
        det = detect_ollama()
        if det.get("running"):
            cfg["_ollama_running"] = True
            cfg["provider"] = "ollama"
            cfg["base_url"] = PROVIDER_PRESETS["ollama"]["base_url"]
            # 已保存的 local_model 优先——但**仅当它仍在本机**。
            # 前端本机模型是个 <select>，选项直接来自 detect_ollama 的结果，
            # 所以保存值只在"该模型还装着"时才有意义；一旦被卸载/改名，
            # 死守它只会让后续 LLM 调用拿到 Ollama 晦涩的 model not found 报错。
            # 故：探测列表里找不到 → 回落到第一个可用模型，并留下可提示的标记。
            models = det.get("models") or []
            resolved, stale = resolve_local_model(cfg.get("local_model") or "", models)
            cfg["model"] = resolved
            if stale:
                cfg["local_model_stale"] = stale  # 供 UI/日志提示"原模型已不存在，已改用 X"
            cfg["api_key"] = ""  # Ollama 无需 Key

    # 3) 填充缺失：从提供商预设补 base_url + model
    provider = cfg.get("provider", DEFAULT_PROVIDER)
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS[DEFAULT_PROVIDER])
    if not cfg["base_url"]:
        cfg["base_url"] = preset["base_url"]
    if not cfg["model"]:
        cfg["model"] = preset["default_model"] or PROVIDER_PRESETS[DEFAULT_PROVIDER]["default_model"]

    return cfg


def load_user_config_raw() -> dict[str, Any]:
    """只读**用户配置文件本身**（不含受管层与环境变量覆盖）。

    为什么需要它：保存端点原先以 `get_llm_config()`（已叠加受管配置与环境变量）
    为基底做增量合并再整体写回文件——那会把**管理员下发的凭据原样写进用户文件**，
    既破坏「凭据只由管理员统一持有」的设计，也让 Key 在用户机器上多留一份副本。
    保存一律以本函数的结果为基底。
    """
    cp = _config_path()
    if cp.is_file():
        try:
            got = json.loads(cp.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                return got
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_llm_config(data: dict[str, Any]) -> None:
    """持久化 LLM 配置到 JSON 文件（API Key 仅存此文件，权限 0600）。"""
    # 唯一临时名 + 原子 rename（见 atomic_io）：固定名 `.tmp` 在并发写者下会互截断，
    # 一次「保存」就能把整份配置（含 Key）变成半截 JSON。
    atomic_io.atomic_write_json(_config_path(), data)


def save_managed_config(data: dict[str, Any]) -> None:
    """写入管理员受管配置（仅超级管理员使用；普通用户界面不暴露该入口）。"""
    atomic_io.atomic_write_json(_managed_path(), data)


def managed_status() -> dict[str, Any]:
    """云端服务配置状态（供前端展示「已由管理员配置」，**绝不含明文 Key**）。

    前端用它把原来那一整块 Key / Base URL / Model 输入框替换成一行状态提示：
    用户看到「云端解说服务已就绪（DeepSeek · deepseek-v4-flash）」即可，不必
    关心也不需要接触任何凭据。
    """
    mp = _managed_path()
    raw: dict[str, Any] = {}
    if mp.is_file():
        try:
            got = json.loads(mp.read_text(encoding="utf-8"))
            if isinstance(got, dict):
                raw = got
        except (json.JSONDecodeError, OSError):
            raw = {}
    cfg = get_llm_config()
    key = str(cfg.get("api_key") or "").strip()
    if _env_api_key():
        source = "env"            # 容器/运维注入
    elif (raw.get("api_key") or "").strip():
        source = "managed"        # 管理员受管配置
    elif key:
        source = "user"           # 本机用户自行配置（兼容旧配置）
    else:
        source = "none"
    prov = str(cfg.get("provider") or "")
    return {
        "configured": bool(key),
        "source": source,
        "engine": str(cfg.get("engine") or "auto"),
        "provider": prov,
        "provider_name": str(PROVIDER_PRESETS.get(prov, {}).get("name") or prov),
        "base_url": str(cfg.get("base_url") or ""),
        "model": str(cfg.get("model") or ""),
        "api_key_masked": mask_key(key),
        "managed_file": str(mp),
        "managed_present": bool(raw),
    }


def inject_llm_env(env: dict[str, str]) -> None:
    """把当前 LLM 配置注入到环境变量字典（供子进程 env= 使用）。

    规则：
    - Ollama 本机模式（base_url 含 11434）：即使无 Key 也注入 LLM_BASE_URL/LLM_MODEL，
      由本地优先开关驱动，零成本跑解说。
    - 云端模式：仅 api_key 非空时注入，无 Key 不污染子进程环境，
      由 commentary-pipeline 自己的守卫(process.py 检查 LLM_API_KEY)报清晰错误。
    - 本机 MLX 模式（engine=auto/mlx）：注入 LLM_ENGINE + MLX_MODEL_PATH + MLX_PYTHON，
      即使无云端 Key 也注入（纯本地优先，不消耗云端额度）。
    - 云端网关优先：配了网关就用「网关地址 + 令牌」，真实 Key 永不下发到用户机器
      （分发场景下 Key 写在用户机器上等于泄露；见 gateway_config 模块说明）。
    """
    cfg = get_llm_config()
    base = (cfg.get("base_url", "") or "").strip()
    model = (cfg.get("model", "") or "").strip()
    key = (cfg.get("api_key", "") or "").strip()
    ollama = "11434" in base
    engine = (cfg.get("engine") or "auto").strip().lower()
    need_local = engine in ("auto", "mlx")

    # 云端网关：可用时接管云端凭据。探测失败一律回退直连，绝不因此阻断任务。
    gw = None
    try:
        from gateway_config import cloud_env as _gw_cloud_env

        gw = _gw_cloud_env(model)
    except Exception:  # noqa: BLE001
        gw = None
    if not gw and not key and not ollama and not need_local:
        return
    # 引擎选择 + MLX 本地配置：无论是否有云端 Key 都注入，供 llm_script.py 路由
    env["LLM_ENGINE"] = engine
    _mp = (cfg.get("mlx_model_path") or "").strip()
    if _mp:
        env["MLX_MODEL_PATH"] = _mp
    _mpy = (cfg.get("mlx_python") or "").strip()
    if _mpy:
        env["MLX_PYTHON"] = _mpy
    # 本机引擎输出上限（防病理性长时间空转）：仅在显式配了正数时注入，
    # 否则管线侧用内置默认 8192（16384 在 8GB 机上最坏要跑 ~9.5 分钟）。
    try:
        _mmt = int(cfg.get("mlx_max_tokens") or 0)
    except (TypeError, ValueError):
        _mmt = 0
    if _mmt > 0:
        env["MLX_MAX_TOKENS"] = str(_mmt)
    if gw:
        # 走网关：token 是本机持有的可吊销凭据，不是上游 API Key
        env["LLM_BASE_URL"] = gw["base_url"]
        env["LLM_MODEL"] = gw["model"]
        env["LLM_API_KEY"] = gw["api_key"]
        env["VDL_LLM_VIA_GATEWAY"] = "1"
    elif not key and not ollama:
        # 纯本地模式：已注入引擎/MLX 变量，无需云端凭据，停止注入
        return
    else:
        env["LLM_BASE_URL"] = base
        env["LLM_MODEL"] = model
        # Ollama 不校验 Key，但 process.py 守卫要求非空，塞占位避免误报；真正请求时 Ollama 忽略它。
        env["LLM_API_KEY"] = key or "ollama"
    # 推理强度（省钱旋钮）：注入 VDL_LLM_REASONING_EFFORT 供 llm_script.py 读取。
    # 默认 low，可在 UI 选 disabled(最省) / high(最佳质量)。
    env["VDL_LLM_REASONING_EFFORT"] = cfg.get("reasoning_effort", "low")
    # 避开峰时（省钱旋钮）：开启时注入 LLM_OFFPEAK_ONLY=1，llm_script 调度到闲时再调用。
    if cfg.get("offpeak_only"):
        env["LLM_OFFPEAK_ONLY"] = "1"
    # max_tokens / temperature 暂不注入——llm_script.py 有合理默认值，
    # 且这两个参数强绑定特定提示词策略，前端 UI 改可能造成脚本输出异常。


# ── 本机 MLX 权重与运行时探测（「AI 能力与密钥配置」面板的本机引擎选择器用）──────
# 设计取舍：只扫描我们自己管理的模型目录（+ 环境变量可指定），不去解析 HF 缓存
# 那套 models--xxx/snapshots/<hash> 结构——后者易随 huggingface_hub 版本变化而失效，
# 且用户从 HF 直接下载时本来就会选目标目录。要新增搜索位置时改 _local_model_roots。

_LOCAL_MODELS_ENV = "VDL_MLX_MODELS_DIR"
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*B(?![a-zA-Z])", re.IGNORECASE)

# 各档位的体验提示。纯 UI 文案，不参与任何路由决策（引擎选择/回落逻辑在
# commentary-pipeline/scripts/llm_script.py 里）。依据是 M1(8GB) 实测：
# 3B 质量最佳（旁白 34 字、title 正确）；1.5B 快约 1.5x 但旁白偏短、会照抄提示词示例；
# 0.5B 撑不住结构化解说 JSON。
_TIER_HINTS = (
    (2.0, "推荐：质量最佳"),
    (1.0, "较快：约 1.5 倍速度，旁白略短"),
    (0.0, "轻量：速度最快，长脚本可能不达标"),
)


def _model_tier_num(name: str) -> float:
    """从目录名解析参数量（如 Qwen2.5-3B-Instruct-4bit → 3.0）；解析不出返回 999。"""
    m = _SIZE_RE.search(name or "")
    if not m:
        return 999.0
    try:
        return float(m.group(1))
    except ValueError:
        return 999.0


def _model_tier_label(num: float) -> str:
    return "未知" if num >= 999.0 else f"{num:g}B"


def _tier_hint(num: float) -> str:
    for floor, text in _TIER_HINTS:
        if num >= floor:
            return text
    return ""


def _local_model_roots() -> list[Path]:
    """本机权重候选目录（按优先级去重）。"""
    roots: list[Path] = []
    env_dir = (os.environ.get(_LOCAL_MODELS_ENV) or "").strip()
    if env_dir:
        roots.append(Path(env_dir).expanduser())
    roots.append(_config_dir() / "mlx_models")
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _dir_size_mb(path: Path) -> int:
    """目录总体积（MB）。权重可达数 GB，这里只做一次浅层统计，失败返回 0。"""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        return 0
    return int(total / (1024 * 1024))


def list_local_models() -> list[dict[str, Any]]:
    """扫描本机可直接加载的 MLX 权重目录（供前端「本机模型」下拉）。

    判定标准：目录内同时存在 config.json 与至少一个 *.safetensors，
    即 mlx_lm 可直接 load 的权重（不限来源，HF 官方或 mlx-community 皆可）。
    扫描很轻（只查文件是否存在 + 统计体积），不做任何导入，可安全高频调用。
    """
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in _local_model_roots():
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            try:
                if not (child / "config.json").is_file():
                    continue
                has_weights = any(f.suffix == ".safetensors" for f in child.iterdir())
            except OSError:
                continue
            if not has_weights:
                continue
            real = str(child.resolve())
            if real in seen:
                continue
            seen.add(real)
            tier = _model_tier_num(child.name)
            models.append({
                "path": real,
                "name": child.name,
                "tier": _model_tier_label(tier),
                "hint": _tier_hint(tier),
                "size_mb": _dir_size_mb(child),
                "exists": True,
            })
    models.sort(key=lambda m: (_model_tier_num(m["name"]), m["name"]))
    return models


def local_runtime_status(python: str = "") -> dict[str, Any]:
    """探测本机 MLX 运行时（找得到 mlx_lm 即视为可用）。

    关键：只用 find_spec 定位包、不执行导入。真导入在慢盘/低配机上可达数十秒
    （实测冷启动曾到 200s+），放在配置页热路径里会让面板卡死。
    """
    candidates: list[str] = []
    for c in ((python or "").strip(),
              (os.environ.get("MLX_PYTHON") or "").strip(),
              str(_config_dir() / "mlx_runtime" / "bin" / "python"),
              "python3"):
        if c and c not in candidates:
            candidates.append(c)
    probe = ("import importlib.util,sys;"
             "sys.exit(0 if importlib.util.find_spec('mlx_lm') else 1)")
    for c in candidates:
        # 绝对路径的候选先确认存在，避免 subprocess 抛 FileNotFoundError 拖慢探测
        if "/" in c and not os.path.exists(c):
            continue
        try:
            res = subprocess.run([c, "-c", probe], capture_output=True,
                                 text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode == 0:
            return {"available": True, "python": c}
    return {"available": False, "python": ""}


def local_models_dir() -> str:
    """本机权重的默认存放目录（面板提示用：让用户知道模型该放哪）。"""
    roots = _local_model_roots()
    return str(roots[0]) if roots else ""
