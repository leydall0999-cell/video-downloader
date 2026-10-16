"""用户信息类数据云备份（客户端 · 2026-10-16）。

定位：把「用户信息类」数据随账号同步到授权中心，做到「换台电脑登录即可见、
本机损坏也不丢」。同步范围按用户口径 —— **用户信息类数据**（设置与偏好、
收藏、最近记录、平台登录态 Cookie），**不含功能产物**（下载成片 / 图片 /
解说成片那些是内容，不是用户信息，仍留在各自本机）。

与公共 Cookie 池（cookie_pool.py）严格区分：
  · 池 = 用户知情同意后**共享**给全体访客复用的运营资源（白名单域 + 入池验真）；
  · 本模块 = 每账号一份**私有备份**，只有本人 token 能取回自己的那一份。

隐私边界（硬约束，改前先想清楚）：
  · Cookie header 等于第三方平台的账号钥匙 ⇒ 必须**本机端到端加密**后才出网；
    服务端及其运维者只拿到密文，读不出内容（面板也只暴露平台清单 / 条数）。
  · 设置与偏好属低敏感结构化数据 ⇒ 明文同步，便于面板显示「有哪些配置」。
  · 本机路径 / 运行态字段不进包（换机无意义，且泄露本机目录结构）。
  · cryptography 不可用时**直接跳过 Cookie 同步**，绝不明文上传。

加密：AES-256-GCM。密钥 = PBKDF2-HMAC-SHA256(用户密码, salt, 20 万轮)；salt 随包
明文存云端（salt 本身不是秘密）。换机后用同一密码 + 同一 salt 派生出同一密钥 ⇒
能解开自己的 Cookie。派生密钥会缓存在 600 权限文件里供免密自动同步；本机丢了也不
影响 —— 新机输一次密码即可重新派生。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

import license_client

try:  # 原子写（与全项目同一套）
    import atomic_io
except Exception:  # pragma: no cover
    atomic_io = None

try:  # 加密（与更新验签同库，随包分发）
    from cryptography.hazmat.primitives import hashes as _hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM as _AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC as _PBKDF2
except Exception:  # pragma: no cover
    _AESGCM = None

logger = logging.getLogger(__name__)

VERSION = 1
KDF_ITERS = 200_000
_MAX_PLAIN_BYTES = 512 * 1024          # 明文分区上限（防畸形文件撑爆云端配额）

# 明文同步白名单：(文件名, 需剔除的键)。剔除项要么是本机路径，要么是运行态。
PLAIN_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("commentary_config.json", ()),
    ("llm_config.json", ("mlx_model_path", "mlx_python", "_ollama_running")),
    ("vision_config.json", ()),
    ("voice_studio_config.json", ()),
    ("proxy.json", ()),
)


# ── 路径 ───────────────────────────────────────────────────────────────────── #
def data_dir() -> Path:
    """数据目录：VDL_DATA_DIR 优先（离线测试隔离，绝不写真实家目录）。

    与 membership._base_dir() / auth_store._base_dir() 同语义。
    """
    override = os.environ.get("VDL_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        return Path(os.environ.get("APPDATA", Path.home())) / "VideoDownloader"
    return Path.home() / ".video-downloader"


def cookie_dir() -> Path:
    """本机浏览器登录态缓存目录（cookie_cache.py 同一处）。"""
    override = os.environ.get("VDL_COOKIE_CACHE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".videodownloader" / "cookies"


def marker_path() -> Path:
    return data_dir() / ".userdata_sync.json"


def key_cache_path() -> Path:
    override = os.environ.get("VDL_USERDATA_KEY_DIR", "").strip()
    base = Path(override) if override else (Path.home() / ".videodownloader")
    return base / ".userdata_key"


# ── 小工具 ─────────────────────────────────────────────────────────────────── #
def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if atomic_io is not None:
        try:
            atomic_io.atomic_write_json(p, obj)
            return
        except Exception:
            pass
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def crypto_available() -> bool:
    return _AESGCM is not None


# ── 明文分区（设置与偏好）───────────────────────────────────────────────────── #
def collect_plain() -> dict[str, Any]:
    """收集白名单里的用户级设置（剔除本机路径 / 运行态键）。"""
    d = data_dir()
    out: dict[str, Any] = {}
    for name, drop in PLAIN_FILES:
        obj = _read_json(d / name)
        if not isinstance(obj, dict):
            continue
        item = {k: v for k, v in obj.items() if k not in drop}
        if item:
            out[name] = item
    return out


def apply_plain(parts: dict[str, Any]) -> list[str]:
    """把云端设置写回本机；返回实际写入的文件名列表。

    合并策略：逐键覆盖（云端值胜），保留本机独有的键 —— 免得云端缺项把本机新加
    的配置抹掉。
    """
    d = data_dir()
    written: list[str] = []
    allow_names = {name for name, _ in PLAIN_FILES}
    for name, incoming in (parts or {}).items():
        if name not in allow_names or not isinstance(incoming, dict):
            continue
        cur = _read_json(d / name)
        cur = cur if isinstance(cur, dict) else {}
        if all(cur.get(k) == v for k, v in incoming.items()):
            continue
        merged = dict(cur)
        merged.update(incoming)
        _write_json(d / name, merged)
        written.append(name)
    return written


# ── 密文分区（Cookie）──────────────────────────────────────────────────────── #
def collect_cookies() -> dict[str, Any]:
    """本机浏览器登录态缓存：{文件名: {"header":…, "ts":…}}。"""
    d = cookie_dir()
    out: dict[str, Any] = {}
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        if p.name.startswith("."):
            continue
        obj = _read_json(p)
        if isinstance(obj, dict) and obj.get("header"):
            out[p.name] = {"header": str(obj.get("header") or ""),
                           "ts": float(obj.get("ts") or 0)}
    return out


def derive_key(password: str, salt: bytes, iters: int = KDF_ITERS) -> Optional[bytes]:
    if _AESGCM is None or not password:
        return None
    kdf = _PBKDF2(algorithm=_hashes.SHA256(), length=32, salt=salt, iterations=iters)
    return kdf.derive(password.encode("utf-8"))


def _encrypt(obj: Any, key: bytes, salt: bytes, iters: int) -> dict[str, Any]:
    nonce = secrets.token_bytes(12)
    ct = _AESGCM(key).encrypt(nonce, _canon(obj).encode("utf-8"), None)
    return {"enc": "aesgcm256", "kdf": {"alg": "pbkdf2-sha256", "iters": iters,
                                        "salt": b64e(salt)},
            "nonce": b64e(nonce), "ct": b64e(ct)}


def _decrypt(blob: Any, key: bytes) -> Any:
    """解密密文分区；结构不对 / 密钥不对 → None（绝不半信半疑地用）。"""
    if _AESGCM is None or not isinstance(blob, dict):
        return None
    try:
        nonce = b64d(str(blob["nonce"]))
        ct = b64d(str(blob["ct"]))
        raw = _AESGCM(key).decrypt(nonce, ct, None)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def _cookie_meta(cookies: dict[str, Any], now: float) -> dict[str, Any]:
    platforms = sorted({str(k).rsplit(".json", 1)[0] for k in cookies.keys()})
    return {"platforms": platforms, "count": len(cookies), "updated_at": now}


# ── 密钥缓存（本机）────────────────────────────────────────────────────────── #
def load_cached_key() -> Optional[dict[str, Any]]:
    rec = _read_json(key_cache_path())
    if isinstance(rec, dict) and rec.get("key") and rec.get("salt"):
        return rec
    return None


def save_cached_key(key: bytes, salt: bytes, iters: int) -> None:
    p = key_cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"v": 1, "key": b64e(key), "salt": b64e(salt),
                                 "iters": int(iters)}), encoding="utf-8")
        os.chmod(p, 0o600)
    except OSError as e:  # 缓存失败只影响「免密自动同步」，不阻断
        logger.warning("[userdata] 密钥缓存写入失败: %s", e)


def _resolve_key(password: Optional[str], cloud_kdf: Optional[dict],
                 create_salt: bool = False) -> Optional[dict[str, Any]]:
    """确定本次加密/解密用的密钥。

    优先级：① 云端已有 kdf（salt 以云端为准，密码在则重派生，否则用缓存且须同 salt）
            ② 无云端 kdf 且有密码 → 新生成 salt（首次上传）
    """
    cached = load_cached_key()
    salt_b = None
    iters = KDF_ITERS
    if isinstance(cloud_kdf, dict) and cloud_kdf.get("salt"):
        try:
            salt_b = b64d(str(cloud_kdf["salt"]))
            iters = int(cloud_kdf.get("iters") or KDF_ITERS)
        except Exception:
            salt_b = None
    if salt_b is None and create_salt and password:
        salt_b = secrets.token_bytes(16)
    if salt_b is None:
        return None
    if password:
        key = derive_key(password, salt_b, iters)
        if key:
            # ⚠️ 这里**不写缓存**：密码可能是错的。缓存只由「确实用这把钥匙加密成功
            # 或解密成功」的路径写入（build_payload / apply_payload）——否则一次错
            # 密码就会污染缓存，让后续免密自动同步一直解不开。
            return {"key": key, "salt": salt_b, "iters": iters}
    if cached and b64d(str(cached["salt"])) == salt_b:
        try:
            return {"key": b64d(str(cached["key"])), "salt": salt_b,
                    "iters": int(cached.get("iters") or iters)}
        except Exception:
            return None
    return None


# ── 资料包 ─────────────────────────────────────────────────────────────────── #
def build_payload(password: Optional[str],
                  cloud_kdf: Optional[dict] = None,
                  device: str = "",
                  existing_cipher: Optional[dict] = None) -> dict[str, Any]:
    """组装资料包。

    Cookie 分区的三条规则（顺序即优先级）：
      ① 本机有 Cookie 且能拿到密钥 → 重新加密上传；
      ② 本机有 Cookie 但拿不到密钥 / 无 cryptography → **保持云端既有密文**，
         绝不明文上传，也绝不把云端备份抹掉；
      ③ 本机当前没有 Cookie → 同样**保留云端既有密文**：这是备份，不是镜像，
         不能因为某台空机器同步过一次就删掉别人机器里的备份。
    """
    now = time.time()
    parts: dict[str, Any] = {}
    plain = collect_plain()
    if plain:
        parts["settings"] = plain
    cookies = collect_cookies()
    keep = (existing_cipher if (isinstance(existing_cipher, dict)
                                and existing_cipher.get("ct")) else None)
    if cookies and crypto_available():
        rk = _resolve_key(password, cloud_kdf, create_salt=True)
        if rk is None:
            logger.info("[userdata] 无可用密钥（需登录密码）→ 保持云端既有 Cookie 密文")
            if keep:
                parts["cookies"] = keep
        else:
            parts["cookies"] = _encrypt(cookies, rk["key"], rk["salt"], rk["iters"])
            parts["cookies"]["meta"] = _cookie_meta(cookies, now)
            # 走到这里意味着：要么云端还没有密文（首次建立），要么 sync 已验过
            # 密码能解开云端密文 ⇒ 这把钥匙可用，允许写入本机缓存。
            save_cached_key(rk["key"], rk["salt"], rk["iters"])
    elif cookies and not crypto_available():
        logger.info("[userdata] cryptography 不可用 → 跳过 Cookie 同步（绝不降级为明文）")
        if keep:
            parts["cookies"] = keep
    elif keep:
        logger.info("[userdata] 本机暂无 Cookie → 保留云端备份，不隐式删除")
        parts["cookies"] = keep
    return {"version": VERSION, "updated_at": now,
            "device": device or _device_name(), "parts": parts}


def _device_name() -> str:
    try:
        import platform
        return f"{platform.system()}-{platform.node()}"[:64]
    except Exception:
        return "unknown"


def apply_payload(payload: dict[str, Any], password: Optional[str] = None
                  ) -> dict[str, Any]:
    """把云端资料包应用到本机。返回 {settings:[写的文件], cookies:int, cookie_state:str}。"""
    parts = payload.get("parts") if isinstance(payload.get("parts"), dict) else {}
    rep: dict[str, Any] = {"settings": [], "cookies": 0, "cookie_state": "none"}

    rep["settings"] = apply_plain(parts.get("settings") or {})

    blob = parts.get("cookies")
    if not isinstance(blob, dict) or not blob.get("ct"):
        return rep
    if not crypto_available():
        rep["cookie_state"] = "no_crypto"
        return rep
    rk = _resolve_key(password, blob.get("kdf"))
    if rk is None:
        rep["cookie_state"] = "locked"       # 需登录密码解锁
        return rep
    data = _decrypt(blob, rk["key"])
    if not isinstance(data, dict):
        rep["cookie_state"] = "bad_key"
        return rep
    # 解密成功 = 这把钥匙确凿可用，此刻才允许写入本机缓存。
    save_cached_key(rk["key"], rk["salt"], rk["iters"])
    d = cookie_dir()
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for name, item in data.items():
        if not isinstance(item, dict) or not item.get("header"):
            continue
        if "/" in str(name) or str(name).startswith(".") or not str(name).endswith(".json"):
            continue                          # 只认裸文件名，防目录穿越
        inc_ts = float(item.get("ts") or 0)
        cur = _read_json(d / str(name))
        # 本机已有**更新**的登录态 → 保留本机（备份不是镜像：不能拿云端旧登录态
        # 盖掉本机刚登录的新态；这份新态会在同一次 sync 的推送分支补上云端）。
        if (isinstance(cur, dict) and cur.get("header")
                and float(cur.get("ts") or 0) > inc_ts):
            continue
        _write_json(d / str(name), {"header": str(item["header"]),
                                    "ts": inc_ts or time.time()})
        n += 1
    rep["cookies"] = n
    rep["cookie_state"] = "ok"
    return rep


# ── 标记（冲突判定）────────────────────────────────────────────────────────── #
def _marker_load() -> dict[str, Any]:
    m = _read_json(marker_path())
    return m if isinstance(m, dict) else {}


def _marker_save(**kw: Any) -> None:
    m = _marker_load()
    m.update({"version": VERSION, **kw})
    try:
        _write_json(marker_path(), m)
    except OSError as e:
        logger.warning("[userdata] 标记写入失败: %s", e)


# ── 对外的防抖自动上传 ─────────────────────────────────────────────────────── #
# 场景：用户在本机新登录了一个平台（比如刚扫码登录 B 站），cookie_cache 落了新文件
# —— 这时应该把这份新登录态备份到云端，而不是等他下次登录才想起。但落盘是高频动
# 作（一次可能连着写好几个域），所以做防抖：攒一小段时间再传一次。
_AUTO_LOCK = threading.Lock()
_AUTO_TIMER: Optional[threading.Timer] = None
AUTO_DELAY = float(os.environ.get("VDL_USERDATA_AUTOSYNC_DELAY") or "20")


def schedule_autosync(delay: Optional[float] = None) -> None:
    """防抖调度一次自动上传（不阻塞调用方；拿不到登录态就静默跳过）。"""
    global _AUTO_TIMER
    if os.environ.get("VDL_USERDATA_AUTOSYNC", "1").strip() == "0":
        return
    try:
        with _AUTO_LOCK:
            if _AUTO_TIMER is not None:
                _AUTO_TIMER.cancel()
            t = threading.Timer(float(delay if delay is not None else AUTO_DELAY),
                                _autosync_once)
            t.daemon = True
            _AUTO_TIMER = t
            t.start()
    except Exception as e:                      # 调度失败无妨，下次改动还会再试
        logger.info("[userdata] 自动上传调度失败: %s", e)


def _autosync_once() -> None:
    """真正执行一次自动上传（token 到这一刻才取）。"""
    global _AUTO_TIMER
    try:
        token = ""
        try:
            from routers.quota import _cloud_token as _qtok
            token = str(_qtok(None) or "").strip()
        except Exception:
            token = ""
        if not token:
            return
        rep = sync(token, None)                 # 无密码 → 用本机缓存的密钥
        logger.info("[userdata] 自动上传: action=%s ok=%s",
                    rep.get("action"), rep.get("ok"))
    except Exception as e:
        logger.info("[userdata] 自动上传跳过: %s", e)
    finally:
        with _AUTO_LOCK:
            _AUTO_TIMER = None


# ── 对外：同步 ─────────────────────────────────────────────────────────────── #
def status() -> dict[str, Any]:
    m = _marker_load()
    return {
        "available": crypto_available(),
        "last_push": float(m.get("last_push") or 0),
        "last_pull": float(m.get("last_pull") or 0),
        "updated_at": float(m.get("updated_at") or 0),
        "cookies_local": len(collect_cookies()),
        "settings_local": sorted(collect_plain().keys()),
        "key_cached": load_cached_key() is not None,
    }


def _signature(parts: dict[str, Any], cookies_plain: dict[str, Any]) -> dict[str, Any]:
    """本机语义指纹：明文设置 + **明文** Cookie（不涉密文）。"""
    return {"settings": parts.get("settings") or {}, "cookies": cookies_plain}


def _cloud_signature(parts: dict[str, Any], password: Optional[str]) -> dict[str, Any]:
    """云端语义指纹：明文设置 + 能解开时的明文 Cookie。

    Cookie 解不开（无密钥 / 换过密码）时记为哨兵 `"<locked>"`，**不与任何本机值
    相等** ⇒ 不会因此误判「无变化」而丢掉本机的新 Cookie，也不会误判「有变化」
    去覆盖云端（覆盖分支另有 build_payload 保持既有密文的保护）。
    """
    sig: dict[str, Any] = {"settings": parts.get("settings") or {}, "cookies": "<locked>"}
    ck = parts.get("cookies")
    if isinstance(ck, dict) and ck.get("ct"):
        rk = _resolve_key(password, ck.get("kdf"))
        data = _decrypt(ck, rk["key"]) if rk else None
        if isinstance(data, dict):
            sig["cookies"] = data
    return sig


def sync(token: str, password: Optional[str] = None,
         base_url: Optional[str] = None, force_push: bool = False,
         allow_rekey: bool = False) -> dict[str, Any]:
    """一次完整同步：先拉（云端更新则覆盖本机），再按需推（本机有变更才推）。

    `allow_rekey`：密码与云端既有密文不匹配时（典型是用户改过密码），默认**拒绝
    上传**以免把好备份换成错密钥加密的版本；置 True 表示「用当前密码重新加密上传，
    接受旧备份作废」。

    返回结构化报告，供 UI / 面板显示：
      {ok, action: 'pulled'|'pushed'|'noop'|'mixed', pulled:{…}, pushed:{…}, error}
    """
    if not token:
        return {"ok": False, "error": "未登录", "action": "noop"}
    rep: dict[str, Any] = {"ok": True, "action": "noop", "pulled": None, "pushed": None}
    try:
        got = license_client.userdata_pull_remote(token, base_url=base_url)
    except Exception as e:
        return {"ok": False, "error": f"拉取失败: {e}", "action": "noop"}
    if not got.get("ok"):
        return {"ok": False, "error": got.get("error") or "拉取失败", "action": "noop"}

    cloud = got.get("payload")
    m = _marker_load()
    local_ts = float(m.get("updated_at") or 0)
    did_pull = False

    if isinstance(cloud, dict) and float(cloud.get("updated_at") or 0) > local_ts + 0.5:
        applied = apply_payload(cloud, password)
        rep["pulled"] = applied
        did_pull = True
        _marker_save(updated_at=float(cloud.get("updated_at") or 0),
                     last_pull=time.time())
        logger.info("[userdata] 已应用云端资料: %s", applied)

    # 组装本机现状（Cookie 的 kdf 以云端为准，保证密钥一致）
    cloud_kdf = None
    existing_cipher = None
    if isinstance(cloud, dict) and isinstance(cloud.get("parts"), dict):
        ck = cloud["parts"].get("cookies")
        if isinstance(ck, dict):
            cloud_kdf = ck.get("kdf")
            existing_cipher = ck

    # 🔴 上传前先验密码：拿当前密码去解云端既有密文，解不开说明密码不对（或云端
    #    那份是别的密码加密的）。此时**绝不能覆盖** —— 否则会把唯一一份好备份换成
    #    用错误密码加密的版本，原机器再也解不开。此处只用 derive_key，不走
    #    _resolve_key，避免把错密钥写进本机缓存。
    if (password and isinstance(existing_cipher, dict)
            and existing_cipher.get("ct") and not allow_rekey):
        _kdf = existing_cipher.get("kdf") if isinstance(existing_cipher.get("kdf"), dict) else {}
        try:
            _salt = b64d(str(_kdf.get("salt")))
            _iters = int(_kdf.get("iters") or KDF_ITERS)
        except Exception:
            _salt = None
        if _salt is not None:
            _probe = derive_key(password, _salt, _iters)
            if _probe is None or _decrypt(existing_cipher, _probe) is None:
                rep["ok"] = False
                rep["error"] = ("登录密码与云端已备份的 Cookie 不匹配，已取消上传；"
                                "云端资料未被改动")
                rep["cookie_state"] = "bad_key"
                rep["rekey_required"] = True
                logger.warning("[userdata] 密码与云端密文不匹配 → 拒绝覆盖云端")
                return rep

    payload = build_payload(password, cloud_kdf=cloud_kdf,
                            existing_cipher=existing_cipher)

    # 「要不要推」按**语义**判定：密文每次重加密必然不同（随机 nonce），直接比
    # 序列化会每次都推 —— 白耗流量，还会把云端 updated_at 顶成当前时间，反过来
    # 让别的机器误判「云端更新了」而执行一次无谓覆盖。
    need_push = bool(force_push) or (cloud is None)
    if not need_push and isinstance(cloud, dict):
        need_push = (_signature(payload.get("parts") or {}, collect_cookies())
                     != _cloud_signature(cloud.get("parts") or {}, password))
    if need_push:
        try:
            sent = license_client.userdata_push_remote(token, payload, base_url=base_url)
        except Exception as e:
            rep["ok"] = False
            rep["error"] = f"上传失败: {e}"
            return rep
        if not sent.get("ok"):
            rep["ok"] = False
            rep["error"] = sent.get("error") or "上传失败"
            return rep
        rep["pushed"] = {"updated_at": sent.get("updated_at"),
                         "meta": sent.get("meta")}
        _marker_save(updated_at=float(sent.get("updated_at") or time.time()),
                     last_push=time.time())
        logger.info("[userdata] 已上传资料包: %s", sent.get("meta"))

    rep["action"] = ("mixed" if (did_pull and rep["pushed"])
                     else "pulled" if did_pull
                     else "pushed" if rep["pushed"] else "noop")
    rep["status"] = status()
    return rep
