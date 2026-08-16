"""server/routers/subscriptions.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/subscriptions")
def list_subscriptions() -> dict:
    return {"subscriptions": [s.to_public_dict() for s in app.sub_store.list_all()], "enabled": app.SUB_ENABLED}

@router.post("/api/subscriptions")
def add_subscription(payload: app.SubscribeRequest) -> dict:
    if not app.SUB_ENABLED:
        raise app.HTTPException(status_code=403, detail="当前部署未启用订阅功能")
    if not app.downloader.is_valid_quality(payload.quality):
        raise app.HTTPException(status_code=400, detail="不支持的清晰度选项")
    url, platform = app.parse_source(payload.url)  # 校验为已知公开平台
    sid = app.uuid.uuid4().hex[:app.TASK_ID_LENGTH]
    # 首次添加只记录基线（不下载历史视频），之后发布的新视频才自动下载
    items = app.subs_mod.probe_channel(url, payload.cookie, payload.proxy, limit=app.SUBSCRIBE_PROBE_LIMIT)
    baseline = [it["id"] for it in items][:200]
    sub = app.subs_mod.Subscription(
        id=sid, url=url, name=payload.name or platform.name,
        platform=platform.name, quality_key=payload.quality,
        quality_label=app.downloader.quality_label(payload.quality),
        cookie=payload.cookie, proxy=payload.proxy, auto_check=payload.auto_check,
        last_video_ids=baseline, last_checked=app.time.time(), created_at=app.time.time(),
    )
    app.sub_store.add(sub)
    return sub.to_public_dict()

@router.delete("/api/subscriptions/{sub_id}")
def remove_subscription(sub_id: str) -> dict:
    if not app.sub_store.remove(sub_id):
        raise app.HTTPException(status_code=404, detail="订阅不存在")
    return {"deleted": True}

@router.post("/api/subscriptions/{sub_id}/check")
def check_subscription_route(sub_id: str) -> dict:
    sub = app.sub_store.get(sub_id)
    if not sub:
        raise app.HTTPException(status_code=404, detail="订阅不存在")
    try:
        result = app._run_subscription_check(sub)
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("订阅 %s 手动检查失败", sub_id)
        raise app.HTTPException(status_code=502, detail=f"探查频道失败：{str(exc)[:200]}")
    return result
