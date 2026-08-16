"""server/routers/retention.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/retention/config")
def retention_config_get() -> dict:
    app._require_retention()
    cfg = app.retention_store.get()
    return {
        "config": cfg.to_dict(),
        "labels": app.retention_mod.CATEGORY_LABELS,
        "trash_available": app.retention_mod.trash_available(),
        "usage": app.retention_mod.disk_usage(app.DOWNLOAD_DIR),
    }

@router.post("/api/retention/config")
def retention_config_set(req: app.RetentionConfigRequest) -> dict:
    app._require_retention()
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    # 安全阀：没有可用回收站时不允许开启「删媒体本体」，避免静默硬删用户资产
    if not app.retention_mod.trash_available():
        if fields.get("media_enabled") or fields.get("quota_enabled"):
            raise app.HTTPException(status_code=400, detail="系统回收站不可用，无法开启媒体清理（拒绝直接硬删）")
    if fields.get("media_use_trash") is False:
        raise app.HTTPException(status_code=400, detail="媒体清理必须走回收站，不允许关闭")
    cfg = app.retention_store.update(**fields)
    return {"config": cfg.to_dict()}

@router.post("/api/retention/scan")
def retention_scan() -> dict:
    """只算不删：返回将被清理的分档清单与可释放空间。"""
    app._require_retention()
    cfg = app.retention_store.get()
    plan = app.retention_mod.scan(app.DOWNLOAD_DIR, cfg)
    plan["usage"] = app.retention_mod.disk_usage(app.DOWNLOAD_DIR)
    # 每档只回传前 50 条明细，避免上千条把响应撑爆；总数/总大小已单列
    for cat, entries in plan["categories"].items():
        plan["categories"][cat] = {
            "count": len(entries),
            "size": sum(e["size"] for e in entries),
            "items": entries[:50],
        }
    return plan

@router.post("/api/retention/run")
def retention_run(req: app.RetentionRunRequest) -> dict:
    app._require_retention()
    cfg = app.retention_store.get()
    cats = req.categories or None
    if cats:
        unknown = [c for c in cats if c not in app.retention_mod.CATEGORY_LABELS]
        if unknown:
            raise app.HTTPException(status_code=400, detail=f"未知清理类别：{', '.join(unknown)}")
    try:
        result = app.retention_mod.run(app.DOWNLOAD_DIR, cfg, cats)
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("手动清理失败")
        raise app.HTTPException(status_code=500, detail=f"清理失败：{str(exc)[:200]}")
    app.retention_store.update(last_run=result["ran_at"], last_freed=result["freed"],
                           last_removed=result["removed"])
    result["usage"] = app.retention_mod.disk_usage(app.DOWNLOAD_DIR)
    result["freed_text"] = app.retention_mod.human_size(result["freed"])
    return result
