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

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from llm_config import _config_dir

import atomic_io

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

    atomic_io.atomic_write_json(_config_path(), normalized)
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
    inject_voice_sample_env(env)


# ───────────────────────── 「我的音色」本地克隆参考样本 ─────────────────────────
# 背景（2026-09-18 修）：界面里的「Qwen3-TTS 本地语音克隆」此前**从未真正克隆**——
#   ① 该选项 value 为空 → 后端不写 VDL_TTS_PROVIDER → 管线落到 tts_config.json 的 provider；
#   ② 全站没有「音色样本」入口，QWEN3TTS_REF_AUDIO/REF_TEXT 恒空 → 即便服务起来了也克隆不了。
# 现在把样本存成 ~/.video-downloader/voice_sample.json，并在每次起解说子进程时注入
# QWEN3TTS_REF_AUDIO / QWEN3TTS_REF_TEXT（管线 scripts/edit_ffmpeg.py 的 qwen3tts 分支会读它们）。
VOICE_SAMPLE_EXTS = (".wav", ".mp3", ".m4a", ".flac", ".aac", ".ogg", ".opus", ".mp4")


def _voice_sample_path() -> Path:
    return _config_dir() / "voice_sample.json"


def get_voice_sample() -> dict[str, Any]:
    """读取「我的音色」参考样本（音频绝对路径 + 该音频的文字稿）。

    返回 {"audio_path", "ref_text", "audio_exists", "ready", "updated_at"}。
    ready = 路径非空 + 扩展名受支持 + 文件此刻仍在 + 文字稿非空；
    任何一项不满足都只是 ready=False（不抛错），供前端显示待配置状态。
    """
    audio, text, updated = "", "", ""
    sp = _voice_sample_path()
    nm = ""
    if sp.is_file():
        try:
            saved = json.loads(sp.read_text(encoding="utf-8"))
            audio = str(saved.get("audio_path") or "").strip()
            text = str(saved.get("ref_text") or "").strip()
            updated = str(saved.get("updated_at") or "").strip()
            nm = str(saved.get("name") or "").strip()
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    exists = bool(audio) and os.path.isfile(audio)
    suffix_ok = audio.lower().endswith(VOICE_SAMPLE_EXTS)
    return {
        "audio_path": audio,
        "ref_text": text,
        # 配音名字（2026-09-20 新增）：用户在录制弹窗里可编辑，缺省用文件名
        "name": nm,
        "audio_exists": exists,
        "ready": bool(exists and suffix_ok and text),
        "updated_at": updated,
    }


def save_voice_sample(audio_path: str, ref_text: str, name: str = "") -> dict[str, Any]:
    """持久化「我的音色」样本（原子写入，权限 0600）。

    校验：音频路径必须存在、扩展名受支持、文字稿非空 —— 缺一样直接抛 ValueError
    （前端立刻提示，避免把「存了个用不了的样本」留到渲染时才炸）。

    name（2026-09-20 新增）：配音名字，随样本一起存，并顺手登记进**音色库**
    （voice_library.json）。登记失败不影响「样本已生效」这个事实 —— 它只是收藏夹。
    """
    import datetime as _dt

    audio = str(audio_path or "").strip().strip('"').strip("'")
    text = str(ref_text or "").strip()
    nm = _clean_voice_name(name)
    if not audio:
        raise ValueError("请先选择一段音色样本音频")
    if not os.path.isfile(audio):
        raise ValueError(f"音频文件不存在：{audio}")
    if not audio.lower().endswith(VOICE_SAMPLE_EXTS):
        allowed = " / ".join(e.lstrip(".") for e in VOICE_SAMPLE_EXTS)
        raise ValueError(f"音频格式不支持（仅 {allowed}）")
    if not text:
        raise ValueError("请填写这段音频里念的内容（文字稿），克隆需要它对齐韵律")

    payload = {
        "audio_path": audio,
        "ref_text": text,
        "name": nm,
        "updated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    atomic_io.atomic_write_json(_voice_sample_path(), payload)
    try:
        upsert_voice_library(nm, audio, text)
    except (OSError, ValueError, TypeError):
        pass
    return get_voice_sample()


def inject_voice_sample_env(env: dict[str, str]) -> None:
    """把「我的音色」样本注入解说子进程（QWEN3TTS_REF_AUDIO / QWEN3TTS_REF_TEXT）。

    只在样本 ready 且同名变量未被显式设置时注入 —— 运维/容器里显式设定的参考音频优先级更高。
    样本没配就不注入，管线会按其原有逻辑报「需要参考音频/文字稿」或回退兜底引擎。
    """
    sample = get_voice_sample()
    if not sample["ready"]:
        return
    if "QWEN3TTS_REF_AUDIO" not in env:
        env["QWEN3TTS_REF_AUDIO"] = sample["audio_path"]
    if "QWEN3TTS_REF_TEXT" not in env:
        env["QWEN3TTS_REF_TEXT"] = sample["ref_text"]


# ───────────────────────── 音色库（命名保存 / 全部音色，2026-09-20）─────────────────────────
# 背景（用户 2026-09-20）：「试听可以编辑配音名字保存」「在我的音色里面应该也加个可以看到
#   全部音色的弹窗」。此前「我的音色」是**单例**（voice_sample.json）——录一段覆盖一段、
#   连名字都没有，用户无法在多个自己录的音色之间切回，也看不到"自己到底有哪些音色"。
#
# 设计（关键是**不让新功能碰到已在跑的那条链路**）：
#   · voice_sample.json 仍是**唯一生效**的样本 —— 管线注入 QWEN3TTS_REF_* 只读它，零改动；
#   · voice_library.json 只是「收藏夹」：记住用户存过的每个音色（名字 + 音频 + 文字稿）；
#   · 在库里选一条 = 拿它的路径/文字稿重新走一次既有 save_voice_sample（同一套校验）。
#   ⇒ 库文件损坏/丢失只会少一个列表，绝不会让渲染失败。
VOICE_LIB_MAX = 40        # 库上限：超出淘汰最旧的，防无声堆积
VOICE_NAME_MAX = 40       # 名字长度上限（界面上是窄卡片，太长会撑破）


def _voice_library_path() -> Path:
    return _config_dir() / "voice_library.json"


def _voice_lib_id(audio_path: str, ref_text: str) -> str:
    """条目 id = 音频路径 + 文字稿的哈希 ⇒ 同一段样本重复保存只更新它自己。"""
    raw = f"{str(audio_path or '').strip()}\x00{str(ref_text or '').strip()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _clean_voice_name(name: str) -> str:
    """清洗用户输入的配音名字：去首尾空白、压掉换行、截断到上限。"""
    raw = str(name or "").replace("\r", " ").replace("\n", " ")
    return " ".join(raw.split()).strip()[:VOICE_NAME_MAX]


def _default_voice_name(audio_path: str) -> str:
    """没填名字时的兜底名：用音频文件名（比"我的音色"更有辨识度）。"""
    base = os.path.splitext(os.path.basename(str(audio_path or "")))[0].strip()
    return (base or "我的音色")[:VOICE_NAME_MAX]


def _read_voice_library() -> list[dict[str, Any]]:
    p = _voice_library_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return []          # 读坏当空库：宁可少一个列表，也不能让面板打不开
    items = data.get("items") if isinstance(data, dict) else None
    return [it for it in (items or []) if isinstance(it, dict)]


def _write_voice_library(items: list[dict[str, Any]]) -> None:
    atomic_io.atomic_write_json(_voice_library_path(), {"items": items})


def list_voice_library() -> dict[str, Any]:
    """列出音色库（新存的在前），并标出哪一条是**当前生效**的。

    ready = 文件还在 + 扩展名受支持 + 文字稿非空（与 get_voice_sample 同一口径）；
    active = 该条就是 voice_sample.json 里那段 —— 前端据此打勾。
    """
    active = get_voice_sample()
    active_path = str(active.get("audio_path") or "")
    out: list[dict[str, Any]] = []
    for it in _read_voice_library():
        audio = str(it.get("audio_path") or "")
        text = str(it.get("ref_text") or "")
        exists = bool(audio) and os.path.isfile(audio)
        out.append({
            "id": str(it.get("id") or _voice_lib_id(audio, text)),
            "name": _clean_voice_name(it.get("name") or "") or _default_voice_name(audio),
            "audio_path": audio,
            "ref_text": text,
            "created_at": str(it.get("created_at") or ""),
            "audio_exists": exists,
            # 是不是 App 自己录的（决定删除时能不能顺手清文件）——只对 voice_samples/ 内的
            "managed": _is_managed_voice_file(audio),
            "ready": bool(exists and audio.lower().endswith(VOICE_SAMPLE_EXTS) and text.strip()),
            "active": bool(active_path) and audio == active_path,
        })
    out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {"items": out, "active_audio_path": active_path, "active_name": str(active.get("name") or "")}


def upsert_voice_library(
    name: str,
    audio_path: str,
    ref_text: str,
    created_at: str = "",
) -> dict[str, Any]:
    """把一段样本登记进音色库（已存在则改名并刷新），返回该条目。

    只校验"路径与文字稿非空"——文件是否存在交给 list_voice_library 的 ready 判定，
    这样即便用户之后把文件删了，库里那条也只是变成"不可用"，不会把整库写坏。
    """
    import datetime as _dt

    audio = str(audio_path or "").strip()
    text = str(ref_text or "").strip()
    if not audio or not text:
        raise ValueError("音色样本缺少音频路径或文字稿")
    nm = _clean_voice_name(name) or _default_voice_name(audio)
    vid = _voice_lib_id(audio, text)
    items = _read_voice_library()
    entry = {
        "id": vid,
        "name": nm,
        "audio_path": audio,
        "ref_text": text,
        "created_at": created_at or _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    replaced = False
    for idx, it in enumerate(items):
        if str(it.get("id") or "") == vid:
            # 重命名/重存保留原始入库时间，排序位置不会莫名乱跳
            entry["created_at"] = str(it.get("created_at") or entry["created_at"])
            items[idx] = entry
            replaced = True
            break
    if not replaced:
        items.insert(0, entry)
    del items[VOICE_LIB_MAX:]
    _write_voice_library(items)
    return {"id": vid, "name": nm, "created": not replaced}


def remove_voice_library(vid: str) -> dict[str, Any]:
    """从音色库删掉一条。

    ⚠️ 只删 App 自己录进 `voice_samples/` 的那个音频文件；用户从别处选的样本**一律不碰**
    （那是他自己的文件，删了不可恢复）。
    """
    key = str(vid or "").strip()
    if not key:
        raise ValueError("缺少要删除的音色 id")
    items = _read_voice_library()
    hit: dict[str, Any] | None = None
    keep: list[dict[str, Any]] = []
    for it in items:
        if hit is None and str(it.get("id") or "") == key:
            hit = it
            continue
        keep.append(it)
    if hit is None:
        raise ValueError("这个音色已经不在库里了")
    _write_voice_library(keep)

    removed_file = False
    audio = str(hit.get("audio_path") or "")
    if audio and _is_managed_voice_file(audio):
        try:
            p = Path(audio)
            if p.is_file():
                p.unlink()
                removed_file = True
        except OSError:
            pass
    return {"id": key, "removed_file": removed_file}


def _is_managed_voice_file(audio_path: str) -> bool:
    """该音频是不是 App 自己录的（落在 voice_samples/ 里）——只有这种才允许被顺手删掉。"""
    if not audio_path:
        return False
    try:
        return Path(audio_path).resolve().parent == _voice_rec_dir().resolve()
    except (OSError, ValueError):
        return False


def rename_active_voice(name: str) -> dict[str, Any]:
    """只改「当前生效样本」的名字（不动音频/文字稿），供界面直接改名用。"""
    cur = get_voice_sample()
    if not cur.get("audio_path"):
        raise ValueError("还没有配置音色样本")
    return save_voice_sample(cur["audio_path"], cur.get("ref_text") or "", name)


def library_audio_paths() -> set[str]:
    """库里所有音频的绝对路径集合 —— 给 `_prune_voice_recordings` 用，别把在用的删了。"""
    out: set[str] = set()
    for it in _read_voice_library():
        audio = str(it.get("audio_path") or "")
        if audio:
            out.add(audio)
    sample = get_voice_sample()
    if sample.get("audio_path"):
        out.add(str(sample["audio_path"]))
    return out


# ── 页面内直接录制（2026-09-19）───────────────────────────────────────────────
# 背景：此前「我的音色」只能「选择录音」挑一个已有文件，用户得先开别的录音软件。
# 现在前端用 WebAudio 采 PCM 自己编 16bit WAV 传上来，这里只做「校验 + 落盘」，
# 不转码 —— WAV 是 libsndfile / ffmpeg 双方都能解的最稳格式（管线侧原本就有
# soundfile 失败再交 ffmpeg 兜底的逻辑，见 working memory）。
VOICE_REC_MIN_SEC = 1.0                    # 短于 1 秒必然是误触/空录
VOICE_REC_MAX_SEC = 60.0                   # 前端 30 秒自动收，这里留一倍余量防绕过
VOICE_REC_MAX_BYTES = 32 * 1024 * 1024     # 32MB：60 秒 48k 16bit 单声道也只 ~5.8MB
VOICE_REC_KEEP = 5                         # 只保留最近几段录音，免得悄无声息地堆满磁盘


def _voice_rec_dir() -> Path:
    """录制音频的落盘目录（与用户自选的外部文件分开，便于按策略清理）。"""
    return _config_dir() / "voice_samples"


def _wav_duration(data: bytes) -> float | None:
    """从 WAV 头算时长；不是合法 WAV（其他容器/头被截断）返回 None。

    只认标准的 `RIFF....WAVE` + 能找到的 `fmt `/`data` 块 —— 够用且不引新依赖。
    """
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    import struct

    pos, byte_rate, data_size = 12, 0, 0
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt " and pos + 8 + 16 <= len(data):
            byte_rate = struct.unpack_from("<I", data, pos + 16)[0]
        elif cid == b"data":
            data_size = size
            break
        pos += 8 + size + (size & 1)
    if byte_rate <= 0 or data_size <= 0:
        return None
    return data_size / float(byte_rate)


def save_recorded_voice_sample(
    data: bytes,
    filename: str = "voice_rec.wav",
    duration: float | None = None,
) -> dict[str, Any]:
    """把页面内录制的音频落盘，返回 {"audio_path", "duration", "bytes"}。

    校验（不满足抛 ValueError，前端直接展示中文原因）：
      数据非空 / 不超 32MB / 扩展名受支持 / 时长在 1~60 秒之间。
    时长优先取 WAV 头实测值，拿不到（非 WAV）才用前端报的值。
    """
    import datetime as _dt
    import re as _re

    blob = data or b""
    if not blob:
        raise ValueError("没有收到录音数据，请重新录制")
    if len(blob) > VOICE_REC_MAX_BYTES:
        raise ValueError("录音数据过大，请缩短到 60 秒以内")

    name = os.path.basename(str(filename or "voice_rec.wav"))
    ext = os.path.splitext(name)[1].lower()
    if ext not in VOICE_SAMPLE_EXTS:
        allowed = " / ".join(e.lstrip(".") for e in VOICE_SAMPLE_EXTS)
        raise ValueError(f"录音格式不支持（仅 {allowed}）")

    dur: float | None = _wav_duration(blob)
    if dur is None and duration is not None:
        try:
            dur = float(duration)
        except (TypeError, ValueError):
            dur = None
    if dur is not None:
        if dur < VOICE_REC_MIN_SEC:
            raise ValueError("录得太短（不到 1 秒），请重新录制")
        if dur > VOICE_REC_MAX_SEC:
            raise ValueError(f"录音过长（{dur:.0f} 秒），请控制在 60 秒以内")

    out_dir = _voice_rec_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(out_dir, 0o700)
    except OSError:
        pass
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_%03d" % (_dt.datetime.now().microsecond // 1000)
    safe = _re.sub(r"[^0-9A-Za-z_.-]", "_", os.path.splitext(name)[0])[:24] or "voice_rec"
    target = out_dir / f"{safe}_{stamp}{ext}"
    # 同一毫秒内连存（测试/连点）也要各占一个文件，否则后一段会静默覆盖前一段
    seq = 1
    while target.exists():
        target = out_dir / f"{safe}_{stamp}_{seq}{ext}"
        seq += 1
    target.write_bytes(blob)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass

    _prune_voice_recordings(keep=str(target))
    return {
        "audio_path": str(target),
        "duration": round(dur, 2) if dur is not None else None,
        "bytes": len(blob),
    }


def _prune_voice_recordings(keep: str = "") -> None:
    """只保留最近 VOICE_REC_KEEP 段录音（含刚写的那段），其余删除。

    只动本模块自己写的 `voice_samples/`（用户从别处选的样本文件不在其内，绝不碰）。

    ⚠️ 2026-09-20：**音色库里还在用的音频一律不删**。否则用户给一个音色起了名字存进库、
    再录 5 次新的，那条库记录指向的 wav 就被这里悄悄删掉，界面上变成「不可用」。
    """
    try:
        files = [p for p in _voice_rec_dir().glob("*") if p.is_file()]
    except OSError:
        return
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    keep_name = os.path.basename(keep or "")
    try:
        protected = library_audio_paths()
    except (OSError, ValueError):
        protected = set()
    kept = 0
    for path in files:
        # 🔴 名额记账必须与改造前一致：**被保留的每一条都占一个名额**（含 keep 那一条）。
        #    曾经把 keep/受保护的文件写成「额外多留、不占名额」，结果最近 N 段之外又多出一份，
        #    test_prune_keeps_recent_only 直接挂（应留 5 段实际 6 段）。
        pinned = bool(keep_name) and path.name == keep_name
        if not pinned:
            try:
                pinned = str(path) in protected or str(path.resolve()) in protected
            except OSError:
                pinned = False
        if pinned or kept < VOICE_REC_KEEP:
            kept += 1
            continue
        try:
            path.unlink()
        except OSError:
            pass

