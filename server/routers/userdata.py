"""server/routers/userdata.py — 用户信息类数据云备份 HTTP 层（2026-10-16）。

对外两个端点：
  GET  /api/userdata/status   本机侧的同步状态（有无密钥缓存、本机有多少 cookie/设置）
  POST /api/userdata/sync     执行一次同步（拉云端更新 + 按需推送本机变更）

同步的判定与隐私边界都在 `server/user_data_cloud.py`，本层只做三件事：
取「当前登录账号的云端 token」、传参、把结构化报告原样回给前端。

`password` 仅用于**本机派生 Cookie 加密密钥**（端到端加密，服务端只存密文），
本层不落盘、不打日志。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

import user_data_cloud as udc

router = APIRouter()
logger = logging.getLogger(__name__)


def _cloud_token(request: Request) -> str:
    """当前登录账号的**云端** token（授权中心签发的那个，不是本机 token）。"""
    try:
        from routers.quota import _cloud_token as _qtok
        return str(_qtok(request) or "").strip()
    except Exception:
        return ""


@router.get("/api/userdata/status")
def userdata_status(request: Request) -> dict[str, Any]:
    """本机同步状态（只读本机，不联网）。"""
    st = udc.status()
    return {"ok": True, "logged_in": bool(_cloud_token(request)), **st}


@router.post("/api/userdata/sync")
def userdata_sync(request: Request,
                  payload: Optional[dict[str, Any]] = Body(default=None)) -> dict[str, Any]:
    """执行一次同步。

    body（全部可选）：
      password     登录密码，用于派生 Cookie 加密密钥（缺省则只同步设置，
                   并保留云端既有 Cookie 密文不动）
      force_push   无条件上传（忽略「本机没变」判定）
      allow_rekey  密码与云端既有密文不匹配时，允许用当前密码重新加密上传
                   （接受旧备份作废；默认 False = 拒绝上传以免污染云端）
    """
    data = payload if isinstance(payload, dict) else {}
    token = _cloud_token(request)
    if not token:
        return {"ok": False, "error": "未登录云端账号，无法同步", "action": "noop"}
    pw = data.get("password")
    return udc.sync(token, str(pw) if pw else None,
                    force_push=bool(data.get("force_push")),
                    allow_rekey=bool(data.get("allow_rekey")))
