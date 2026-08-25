"""解说(配音/音量)手动可调配置层。

把"旁白响度 / 原声压低 / 旁白增益"三个旋钮从硬编码+环境变量，提升为
前端可持久化的用户设置。配置落盘到 ~/.video-downloader/commentary_config.json，
每次启动解说子进程时通过 inject_commentary_env 注入到环境，覆盖管线 config.py 默认值。

env 变量名与 commentary-pipeline/scripts/config.py 完全对齐：
  VDL_NARRATION_LOUDNESS  (-14 / "off")
  VDL_ORIGINAL_DUCK       (0.05~0.30)
  VDL_NARRATION_BOOST     (1.0~1.6)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from llm_config import _config_dir

# 默认值必须与 pipeline config.py 对齐，避免用户没改时行为突变
DEFAULT_NARRATION_LOUDNESS = -14.0   # LUFS；"off" 表示关闭标准化
DEFAULT_ORIGINAL_DUCK = 0.10         # 原声保留比例（解说期间压低）
DEFAULT_NARRATION_BOOST = 1.0        # 旁白额外线性增益倍数

# 可调节范围（前端滑块边界，后端做夹取兜底）
LOUDNESS_MIN, LOUDNESS_MAX = -18.0, -10.0
DUCK_MIN, DUCK_MAX = 0.05, 0.30
BOOST_MIN, BOOST_MAX = 1.0, 1.6


def _config_path() -> Path:
    return _config_dir() / "commentary_config.json"


def get_commentary_config() -> dict[str, Any]:
    """读取解说配置，缺省回退到与管线一致的默认值。

    优先级：环境变量 > JSON 文件 > 硬编码默认值。
    环境变量为最终裁决（运维/容器覆盖 UI 设置），与 llm_config 的语义一致。
    """
    cfg: dict[str, Any] = {
        "narration_loudness": DEFAULT_NARRATION_LOUDNESS,
        "original_duck": DEFAULT_ORIGINAL_DUCK,
        "narration_boost": DEFAULT_NARRATION_BOOST,
    }

    # 1) JSON 文件（前端持久化）
    cp = _config_path()
    if cp.is_file():
        try:
            saved = json.loads(cp.read_text(encoding="utf-8"))
            if "narration_loudness" in saved:
                cfg["narration_loudness"] = saved["narration_loudness"]
            if "original_duck" in saved:
                cfg["original_duck"] = saved["original_duck"]
            if "narration_boost" in saved:
                cfg["narration_boost"] = saved["narration_boost"]
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    # 2) 环境变量最终裁决（便于不改文件直接调）
    env_l = os.environ.get("VDL_NARRATION_LOUDNESS", "").strip()
    if env_l:
        cfg["narration_loudness"] = env_l if env_l.lower() in ("off", "none", "0") else env_l
    env_d = os.environ.get("VDL_ORIGINAL_DUCK", "").strip()
    if env_d:
        try:
            cfg["original_duck"] = float(env_d)
        except ValueError:
            pass
    env_b = os.environ.get("VDL_NARRATION_BOOST", "").strip()
    if env_b:
        try:
            cfg["narration_boost"] = float(env_b)
        except ValueError:
            pass

    return cfg


def save_commentary_config(data: dict[str, Any]) -> dict[str, Any]:
    """持久化解说配置到 JSON（原子写入，权限 0600）。

    入参 data 的合法字段：
      narration_loudness: int/float（LUFS）或字符串 "off"
      original_duck:      0.05~0.30
      narration_boost:    1.0~1.6
    所有值都会被夹取/规范化，保证写入即安全。
    """
    loud = data.get("narration_loudness", DEFAULT_NARRATION_LOUDNESS)
    if isinstance(loud, str) and loud.strip().lower() in ("off", "none", "0"):
        loud_norm: Any = "off"
    else:
        try:
            loud_f = float(loud)
        except (TypeError, ValueError):
            loud_f = DEFAULT_NARRATION_LOUDNESS
        # 关闭标准化时允许任何值（前端若发 off 走上面分支）；数值型夹到范围
        if not (isinstance(loud, str) and loud.strip().lower() in ("off", "none", "0")):
            loud_f = max(LOUDNESS_MIN, min(LOUDNESS_MAX, loud_f))
        loud_norm = int(loud_f) if loud_f == int(loud_f) else round(loud_f, 1)

    try:
        duck_f = float(data.get("original_duck", DEFAULT_ORIGINAL_DUCK))
    except (TypeError, ValueError):
        duck_f = DEFAULT_ORIGINAL_DUCK
    duck_f = max(DUCK_MIN, min(DUCK_MAX, duck_f))

    try:
        boost_f = float(data.get("narration_boost", DEFAULT_NARRATION_BOOST))
    except (TypeError, ValueError):
        boost_f = DEFAULT_NARRATION_BOOST
    boost_f = max(BOOST_MIN, min(BOOST_MAX, boost_f))

    normalized = {
        "narration_loudness": loud_norm,
        "original_duck": round(duck_f, 2),
        "narration_boost": round(boost_f, 2),
    }

    cd = _config_dir()
    cd.mkdir(parents=True, exist_ok=True)
    cp = _config_path()
    tmp = cp.with_suffix(".tmp")
    tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(cp)
    return normalized


def inject_commentary_env(env: dict[str, str]) -> None:
    """把当前解说配置注入到子进程环境变量字典（供 process.py 读取）。

    仅在对应 env 未被显式设置时才注入，尊重运维/容器级环境变量覆盖。
    """
    cfg = get_commentary_config()
    if "VDL_NARRATION_LOUDNESS" not in env:
        l = cfg["narration_loudness"]
        env["VDL_NARRATION_LOUDNESS"] = "off" if str(l).lower() in ("off", "none", "0") else str(l)
    if "VDL_ORIGINAL_DUCK" not in env:
        env["VDL_ORIGINAL_DUCK"] = str(cfg["original_duck"])
    if "VDL_NARRATION_BOOST" not in env:
        env["VDL_NARRATION_BOOST"] = str(cfg["narration_boost"])
