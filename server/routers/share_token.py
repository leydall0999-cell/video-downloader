# -*- coding: utf-8 -*-
"""server/routers/share_token.py — 网页版「生成二维码 / 生成网页」的分享凭据下发。

受限版（2026-09-30 定）：仅登录用户可取分享 token；每用户每日限次。
浏览器拿到 token 后**同源直传** /api/upload（主域 nginx 的 `= /api/upload` 已把它
转发到分享节点 8901），文件字节不经过本服务 —— 2C2G 小机扛不住代理大文件，
nginx 对该路由已配流式转发（proxy_request_buffering off）。

为什么不需要 CORS：上传走的是 hanyuxz.top 同源（nginx 反代），不是跨域直连
share.hanyuxz.top；分享节点零改动。

限次语义（2026-09-30 定）：**取 token 即计数**（GET /api/share/token 每调一次
计一次）。前端约定「每次上传前都重新取 token」，故 次数 ≈ 上传次数。这是软闸门：
拿到 token 的客户端理论上可复用同一 token 多次上传 —— 受限版接受该残留风险
（token 本就只发给登录用户，真正的硬约束是分享节点的磁盘水位与总量配额）。
/limits 是只读端点（不计数），供页面展示限额文案。

⚠️ 部署边界：token 文件只存在于国内 ECS（/opt/vdl-share/share_token）。
HK 节点没有分享节点 ⇒ 本端点返回 SHARE_UNAVAILABLE，前端给「当前节点不支持」提示。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Request

router = APIRouter()

# CF 免费版请求体上限 100MB（https 页面也无法走 App 的明文 IP 直连通道，混合内容会被拦）
# ⇒ 网页版单文件 ≤ 95MB；「生成网页」合计 ≤ 60MB（base64 膨胀约 1/3 后 ≈80MB，仍 < 100MB）
MAX_FILE_MB = int(os.environ.get("VDL_WEB_SHARE_MAX_MB", "95"))
PAGETOOL_MAX_TOTAL_MB = int(os.environ.get("VDL_WEB_PAGETOOL_MAX_MB", "60"))
DAILY_LIMIT = int(os.environ.get("VDL_WEB_SHARE_DAILY", "20"))
DEFAULT_EXPIRE_DAYS = 7
EXPIRE_DAYS = (1, 3, 7, 30)          # 受限版不提供「永久」


def _token_file() -> Path:
    return Path(os.environ.get("VDL_SHARE_TOKEN_FILE", "/opt/vdl-share/share_token"))


def _counter_path(uid: str) -> Path:
    """按天分片：文件名含日期，旧文件天然过期，无需清理任务。
    ⚠️ fail-safe：HK 老分支的 auth_store 可能没有 _base_dir() —— 计数失败只当 0，
    绝不让限次逻辑把整个端点打成 500。"""
    try:
        from auth_store import _base_dir
        base = _base_dir()
    except Exception:
        base = Path(os.environ.get("VDL_DATA_DIR", os.path.expanduser("~/.video-downloader")))
    return base / "share_daily" / ("%s.%s.json" % (uid, time.strftime("%Y%m%d")))


def _read_used(uid: str) -> int:
    try:
        return int(json.loads(_counter_path(uid).read_text("utf-8")).get("used", 0))
    except Exception:
        return 0


def _incr_used(uid: str) -> int:
    f = _counter_path(uid)
    used = _read_used(uid) + 1
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"used": used, "t": int(time.time())}), "utf-8")
        os.replace(tmp, f)
    except OSError:
        pass
    return used


def _require_user(request: Request):
    import app
    return app.get_current_user_id(request)


def _limits() -> dict:
    return {"max_file_mb": MAX_FILE_MB,
            "pagetool_max_total_mb": PAGETOOL_MAX_TOTAL_MB,
            "expire_days": list(EXPIRE_DAYS),
            "default_expire_days": DEFAULT_EXPIRE_DAYS,
            "daily_limit": DAILY_LIMIT}


@router.get("/api/share/limits")
def share_limits(request: Request = None) -> dict:
    """只读：展示限额与今日已用（不计数、不发 token）。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    return {"ok": True, "limits": {**_limits(), "used_today": _read_used(uid)}}


@router.get("/api/share/token")
def share_token(request: Request = None) -> dict:
    """下发分享节点上传凭据（登录 + 每日限次闸门；**取一次计一次**）。"""
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    tf = _token_file()
    try:
        token = tf.read_text("utf-8").strip() if tf.is_file() else ""
    except OSError:
        token = ""
    if not token:
        return {"ok": False, "error": "当前节点未开启分享服务", "code": "SHARE_UNAVAILABLE"}
    used = _read_used(uid)
    if used >= DAILY_LIMIT:
        return {"ok": False, "error": "今日分享次数已用完（每天 %d 次），明天再来" % DAILY_LIMIT,
                "code": "DAILY_LIMIT"}
    used = _incr_used(uid)
    return {"ok": True, "token": token, "limits": {**_limits(), "used_today": used}}
