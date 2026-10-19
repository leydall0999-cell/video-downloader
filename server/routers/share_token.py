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

🔴 2026-10-19 改口径：计数从「本端点自己的本地文件」改为**账号级每日配额**
（`app.current_member_store(request).use_daily(...)`），免费 20 次/日、会员 200 次/日，
按子页签分派 share_qr / share_page 两个键（见 `_share_resource`）。好处：换浏览器
不再重置、后台可调、与桌面端同一账号共用、并出现在个人中心「使用统计」里。
`DAILY_LIMIT` 与本地文件计数降级为**兜底**（仅在配额引擎取不到时用于展示）。

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
            # ⚠️ daily_limit 只是**兜底**值：真实生效上限由账号档位决定
            # （免费 / 会员各一份，且管理员可在后台改），调用方一律用
            # _used_today() 返回的 limit 覆盖本字段。
            "daily_limit": DAILY_LIMIT}


def _share_resource(src: str) -> str:
    """按分享子页签分派「每日配额」键（2026-10-19）。

    背景：此前本端点用**本地文件**计数（VDL_WEB_SHARE_DAILY，写死 20 次/日），
    既不与账号绑定（换浏览器就重置），也不出现在个人中心「使用统计」里 ——
    用户反馈「统计不全」。现并入账号级每日配额（membership.FREE_DAILY_LIMITS /
    DAILY_QUOTA_LIMITS），同一账号在网页版与桌面端共用，且后台可调。

    分派：「生成网页」转在线链接（src=pagegen）→ share_page；其余（含生成二维码
    src=shareqr、以及老客户端不传 src）→ share_qr。

    ⚠️ 刻意**不**走 app.cloud_quota_gate 的「终身 N 次」口径：分享吃的是分享节点
    磁盘而不是大模型算力，且授权中心 _CLOUD_RESOURCES 只认 8 个旧键，未知键会被
    回退成 cloud_commentary —— 那等于让「生成二维码」去消耗「视频解说」的终身
    额度，还会把现在的 20 次/日倒退成 3 次终身。
    """
    # ⚠️ 写成两条独立 return（不要三元）：守卫 test_feature_usage_gate.mjs 按
    # `return "键"` 逐行确认每个配额键都有真实分派分支，三元表达式的 else 分支
    # 扫不到，会被判成「页面上有、代码里没有」。
    if str(src or "").strip().lower() == "pagegen":
        return "share_page"
    return "share_qr"


def _used_today(request, src: str) -> tuple[int, int]:
    """`(今日已用, 当日上限)`，取自账号级每日配额（按档位 + 后台覆盖）。

    拿不到（会员引擎异常等）时退回本地文件计数与 DAILY_LIMIT，绝不 500。
    """
    import app
    try:
        q = app.current_member_store(request).quota_state(_share_resource(src))
        used = int(q.get("used") or 0)
        lim = q.get("limit")
        return used, (int(lim) if isinstance(lim, int) else DAILY_LIMIT)
    except Exception:
        uid = ""
        try:
            uid = app.get_current_user_id(request) or ""
        except Exception:
            uid = ""
        return (_read_used(uid) if uid else 0), DAILY_LIMIT


@router.get("/api/share/limits")
def share_limits(request: Request = None, src: str = "") -> dict:
    """只读：展示限额与今日已用（不计数、不发 token）。

    src：子页签（shareqr / pagegen），决定读哪个配额键的用量。
    """
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    used, lim = _used_today(request, src)
    return {"ok": True, "limits": {**_limits(), "used_today": used, "daily_limit": lim}}


@router.get("/api/share/token")
def share_token(request: Request = None, src: str = "") -> dict:
    """下发分享节点上传凭据（登录 + 账号级每日配额闸门；**取一次计一次**）。

    2026-10-19：限次由「本地文件计数」改为 `MembershipStore.use_daily(share_qr /
    share_page)` —— 按账号按天、异步上云，同一账号网页版/桌面端共用，且出现在
    个人中心「使用统计」里。
    """
    uid = _require_user(request)
    if not uid:
        return {"ok": False, "error": "请先登录账号", "code": "NO_AUTH"}
    tf = _token_file()
    try:
        token = tf.read_text("utf-8").strip() if tf.is_file() else ""
    except OSError:
        token = ""
    if not token:
        # 🔴 顺序不能换：节点不支持时**不计数**（HK 每次访问都扣额度是错的）
        return {"ok": False, "error": "当前节点未开启分享服务", "code": "SHARE_UNAVAILABLE"}
    import app
    res = _share_resource(src)
    q = app.current_member_store(request).use_daily(res, 1)
    if not q.get("ok"):
        # 契约保持与旧实现一致：ok=False + code=DAILY_LIMIT（前端据此提示）。
        # 文案用配额引擎给的**具体功能名**（生成二维码 / 生成网页），不写死。
        return {"ok": False, "error": q.get("error") or "今日分享次数已用完，明天再来",
                "code": "DAILY_LIMIT", "resource": res}
    used = int(q.get("used") or 0)
    _, lim = _used_today(request, src)
    return {"ok": True, "token": token, "limits": {**_limits(), "used_today": used,
                                                   "daily_limit": lim}}
