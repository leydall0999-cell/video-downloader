"""server/routers/quota.py — 免费用户配额闸门 HTTP 层。

对应设计文档 docs/llm-cloud-fallback-design.md §12：
- 终身云端事件 3 次（不可恢复）
- 每日 auto 运行 1 次（自然日重置）
- 单条上传视频 ≤ 30 分钟（免费用户；会员不限）
会员解除全部限制。

会员判定复用 app.current_member_store（与 /api/member 共用状态文件）。
配额状态持久化在 ~/.video-downloader/quota.json（与 membership 同目录），
由 pipeline 侧 quota.py 与本文共享同一 JSON schema。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Request

router = APIRouter()


def _is_member(request: Optional[Request] = None) -> bool:
    """当前用户是否为会员（下载会员或 AI 会员任一活跃即视为会员）。"""
    try:
        import app as _app
        store = _app.current_member_store(request) if request is not None else _app.member_store
        st = store.status()
        return bool(st.get("download_member", {}).get("active")) or \
               bool(st.get("ai_member", {}).get("active"))
    except Exception:
        return False


def get_quota_manager(request: Optional[Request] = None):
    """构造注入会员判定的 QuotaManager。"""
    from quota import QuotaManager
    return QuotaManager(is_member_fn=lambda: _is_member(request))


def assert_upload_allowed(request: Optional[Request], duration_sec: float) -> None:
    """上传前服务端二次校验：免费用户视频 > 30 分钟 → 403 人话提示升级会员。

    duration_sec <= 0 表示「未知」（探测失败），按 fail-open 放行（前端主拦截）。
    """
    if duration_sec and duration_sec > 0 and not _is_member(request):
        from quota import QuotaManager, FREE_MAX_DURATION_SEC
        if duration_sec > FREE_MAX_DURATION_SEC:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail=(
                    f"免费版单个解说视频上限 {int(FREE_MAX_DURATION_SEC // 60)} 分钟，"
                    f"当前视频约 {int(duration_sec // 60)} 分钟。开通会员即可解锁长视频解说。"
                ),
            )


@router.get("/api/quota/status")
def quota_status(request: Request) -> dict[str, Any]:
    """返回配额状态，供设置页展示「剩余免费云端 X/3、今日 auto Y/1」。"""
    return get_quota_manager(request).status()
