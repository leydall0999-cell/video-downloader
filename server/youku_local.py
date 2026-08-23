"""优酷本机 ckey 存储（桌面端自治，仅本机、不出本机、不进公共池）。

为什么需要它：
- 优酷 UPS 播放签名 ckey 有时效（约 40 分钟），过期后 UPS 返回 -3007 / 空 stream。
- 桌面 app 跑在用户本机（境内直连 + 浏览器已登录优酷），完全可以从本机取新鲜 ckey，
  无需依赖网页版公共池、也无需把 ckey 发给任何人。
- 本模块把 ckey（及配套 Cookie）存到本机文件 ~/.videodownloader/youku_local.json，
  由「优酷本机登录」卡片通过 bookmarklet 或粘贴 cURL 写入，下载器优先读取。

与 cookie_pool 的关系：本机优酷 ckey 与公共池严格隔离——公共池是网页版公共服务用的
（跨用户共享、需知情同意上报）；本机 ckey 只服务当前桌面 app 自己，隐私边界更干净。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from pathlib import Path

# ckey 有效期：实测约 40 分钟开始失效，这里取 35 分钟作为「仍视为有效」的安全阈值。
CKEY_TTL = 35 * 60

_LOCAL_DIR = Path.home() / ".videodownloader"
_LOCAL_FILE = _LOCAL_DIR / "youku_local.json"


def _load() -> dict:
    try:
        if _LOCAL_FILE.exists():
            return json.loads(_LOCAL_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save(data: dict) -> None:
    try:
        _LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(_LOCAL_DIR, 0o700)
        _LOCAL_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(_LOCAL_FILE, 0o600)
    except Exception:
        pass


def _now() -> int:
    return int(time.time())


def save_local_ckey(ckey: str, cookie: str = "", source: str = "local") -> bool:
    """写入本机优酷 ckey（及配套 Cookie）。返回是否成功。"""
    ckey = (ckey or "").strip()
    if not ckey:
        return False
    data = _load()
    data["ckey"] = ckey
    data["ckey_ts"] = _now()
    data["ckey_source"] = source
    if cookie:
        data["cookie"] = cookie
    _save(data)
    return True


def get_local_ckey() -> tuple[str, str]:
    """返回 (ckey, cookie)。ckey 在有效期内才返回，否则返回空串。

    调用方据此判断是否需要提示用户刷新。
    """
    data = _load()
    ckey = data.get("ckey") or ""
    if not ckey:
        return "", ""
    ts = data.get("ckey_ts", 0)
    if _now() - ts > CKEY_TTL:
        # 已过期：仍返回空，让上层回退/提示刷新（不静默用过期 ckey）
        return "", ""
    return ckey, data.get("cookie") or ""


def local_ckey_status() -> dict:
    """返回本机 ckey 状态，供前端展示。"""
    data = _load()
    ckey = data.get("ckey") or ""
    if not ckey:
        return {"has": False, "remaining": 0, "source": ""}
    ts = data.get("ckey_ts", 0)
    age = _now() - ts
    remaining = max(0, CKEY_TTL - age)
    return {
        "has": True,
        "remaining": remaining,
        "remaining_min": remaining // 60,
        "source": data.get("ckey_source", ""),
        "expired": age > CKEY_TTL,
    }


def extract_ckey_from_curl(curl_text: str) -> tuple[str, str]:
    """从「Copy as cURL」(bash) 解析 ckey 与 cookie。复用于 web-dev 的解析逻辑。

    返回 (ckey, cookie)；解析不到返回 ("", "")。
    """
    if not curl_text:
        return "", ""
    m = re.search(r"ckey=([^&'\s]+)", curl_text)
    ckey = urllib.parse.unquote(m.group(1)) if m else ""
    # bash cURL 的 cookie 在 -b '...' 或 --cookie '...'
    bm = re.search(r"(?:-b|--cookie)\s+'([^']+)'", curl_text)
    if not bm:
        bm = re.search(r'(?:-b|--cookie)\s+"([^"]+)"', curl_text)
    cookie = bm.group(1) if bm else ""
    return ckey, cookie
