"""server/routers/userdata.py — 用户信息类数据云备份 · 网页版通道（2026-10-17）。

定位：让**网页版**（hanyuxz.top）也进入「账号数据随账号走」的闭环 —— 用户在某台
电脑/某个浏览器上粘过的平台登录态（以及少量界面偏好），换个浏览器登录同一账号
就能自动落回来。

与桌面端（`server/user_data_cloud.py`）的关系：**同一个账号资料包通道，分文件存放**。
服务端按 `scope="web"` 落到 `userdata/<账号sha>.web.json`，桌面端那份是
`userdata/<账号sha>.json`。为什么必须分开：桌面端的资料包是「整体替换 parts」组装
的，如果两端共用一份就会互相抹掉。

与桌面端最大的区别：网页版的「本机」是**浏览器**，所以
  · 资料包的组装 / 差异判定 / 加解密（Cookie 端到端加密 AES-256-GCM，密钥由登录
    密码经 PBKDF2-SHA256 20 万轮派生）**都在前端 `web/app.js` 里做**（WebCrypto）；
  · 本层只做三件事：认人（本机 bearer）、带云端 token 转发、把结果原样回给前端。
⇒ 本服务（及其运维者）与授权中心都只能看到**密文**与分区名，读不到任何 Cookie
内容（面板也只有「有几个平台登录态、什么时候更新的」这类摘要）。

端点：
  GET  /api/userdata/status   本账号备份状态（含云端摘要；会打授权中心，不含内容）
  POST /api/userdata/pull     取回本人云端资料包（前端自行解密，密文原样下发）
  POST /api/userdata/push     上传/覆盖本人云端资料包（密文由前端组装好）

`scope` 固定为 "web"，**不接受前端传参** —— 免得前端一个笔误写到桌面端那份上去。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, Request

import license_client

router = APIRouter()
logger = logging.getLogger(__name__)

SCOPE = "web"          # 网页版专属分区（桌面端 = ""）


def _current_uid(request: Request) -> str:
    try:
        import app
        return str(app.get_current_user_id(request) or "")
    except Exception:
        return ""


def _cloud_token(request: Request) -> str:
    """当前账号的**云端** token（授权中心签发；纯本机账号 / 未登录时为空串）。"""
    try:
        import app
        store = app.current_member_store(request)
        return str((store.cloud_session() or {}).get("token") or "").strip()
    except Exception:
        return ""


@router.get("/api/userdata/status")
def userdata_status(request: Request) -> dict[str, Any]:
    """本账号的云端资料状态（打授权中心取摘要；不含任何内容/密文）。"""
    if not _current_uid(request):
        return {"ok": False, "error": "未登录账号", "code": "NO_LOGIN",
                "logged_in": False, "cloud_linked": False, "cloud": None}
    tok = _cloud_token(request)
    out: dict[str, Any] = {"ok": True, "logged_in": True, "cloud_linked": bool(tok),
                           "cloud": None, "cloud_error": ""}
    if not tok:
        # 离线注册/云端不可达时创建的纯本机账号：没有云端身份，自然没有云端资料
        out["cloud_error"] = "本机账号未接入云端（离线注册的账号），资料不会上云"
        return out
    try:
        got = license_client.userdata_meta_remote(tok, scope=SCOPE)
        if isinstance(got, dict) and got.get("ok"):
            out["cloud"] = got.get("meta")
        else:
            out["cloud_error"] = str((got or {}).get("error") or "云端摘要获取失败")
    except Exception as e:                      # 断网只影响这一行显示
        out["cloud_error"] = f"云端摘要获取失败: {e}"
    return out


@router.post("/api/userdata/pull")
def userdata_pull(request: Request) -> dict[str, Any]:
    """取回本人云端资料包（原样转给浏览器，由浏览器用密码派生的密钥解密）。"""
    if not _current_uid(request):
        return {"ok": False, "error": "未登录账号", "code": "NO_LOGIN"}
    tok = _cloud_token(request)
    if not tok:
        return {"ok": False, "error": "本机账号未接入云端，无法取回云端资料",
                "code": "NO_CLOUD"}
    try:
        return license_client.userdata_pull_remote(tok, scope=SCOPE)
    except Exception as e:
        return {"ok": False, "error": f"取回失败: {e}"}


@router.post("/api/userdata/push")
def userdata_push(request: Request,
                  payload: Optional[dict[str, Any]] = Body(default=None)
                  ) -> dict[str, Any]:
    """上传/覆盖本人云端资料包（密文由浏览器组装好；本层不解析内容）。

    body: {"payload": {"version":1, "updated_at":<ts>, "device":"web-…",
                       "parts": {"settings":{…}, "cookies":{…密文…}}}}
    """
    if not _current_uid(request):
        return {"ok": False, "error": "未登录账号", "code": "NO_LOGIN"}
    tok = _cloud_token(request)
    if not tok:
        return {"ok": False, "error": "本机账号未接入云端，无法上传云端资料",
                "code": "NO_CLOUD"}
    data = payload if isinstance(payload, dict) else {}
    blob = data.get("payload")
    if not isinstance(blob, dict) or not isinstance(blob.get("parts"), dict) or not blob["parts"]:
        return {"ok": False, "error": "资料包结构不合法（parts 必须是非空对象）"}
    try:
        return license_client.userdata_push_remote(tok, blob, scope=SCOPE)
    except Exception as e:
        return {"ok": False, "error": f"上传失败: {e}"}
