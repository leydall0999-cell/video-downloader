"""server/routers/image.py — 图片下载（「无水印图片」P0，2026-10-12）。

入口：POST /api/image/download（urls 批量）→ 逐条建 kind=image 任务 →
      scheduler 提交 image_download.run_image_download 落盘。

与 /api/download 的同口径契约（刻意保持一致，避免「下载」有两种语义）：
  · 限流 _check_rate_limit；
  · 配额门 _download_gate_error（免费 10 次/日 → 会员 1000 次/日），每个成功
    创建的任务计 1 次，与视频下载/批量下载相同；
  · SSRF：初始 URL 过 app._assert_safe_url（重定向每一跳由 image_download 内部校验）；
  · 归属：owner_id = app._task_owner(request)（安全审计 P0-2 同款隔离）；
  · 计费时机：任务创建成功才烧额度（失败/被拒不烧）。

合规（需求文档.md 模块二硬约束）：仅下载用户有权获取的公开图片资源，入口文案
已明示；不做付费墙绕过。
"""

import app
import image_download
from fastapi import APIRouter, HTTPException
from stats import record_event

router = APIRouter()

# 单次请求允许粘贴的链接数（与 VDL_BATCH_MAX_ITEMS 同量级，防一次打爆队列）
MAX_URLS_PER_REQUEST = 50


class ImageDownloadRequest(app.BaseModel):
    urls: list[str] = app.Field(default_factory=list,
                                max_length=MAX_URLS_PER_REQUEST)
    # 来源页 Referer：部分图片 CDN 有防盗链，带上来源页才返回 200（可选）
    referer: str = app.Field(default="", max_length=512)
    cookie: str = app.Field(default="", max_length=8192)
    proxy: str = app.Field(default="", max_length=256)


def _gate(request) -> str | None:
    """下载配额门（延迟 import 避开 app ↔ routers 的初始化顺序耦合）。"""
    from routers.core import _download_gate_error
    return _download_gate_error(request)


@router.post('/api/image/download')
def create_image_download(payload: ImageDownloadRequest,
                          request: app.Request) -> dict:
    app._check_rate_limit(request)
    urls = [u.strip() for u in (payload.urls or []) if u and u.strip()]
    if not urls:
        raise HTTPException(status_code=400, detail='没有提供有效的图片链接')
    referer = (payload.referer or '').strip()
    cookie = (payload.cookie or '').strip()
    proxy = (payload.proxy or '').strip()
    owner = app._task_owner(request)
    task_ids: list[str] = []
    skipped = 0
    quota_exhausted = False
    for raw in urls:
        # 配额门逐条判定（与 /api/batch 一致）：用尽即停，已建的任务不回收
        _err = _gate(request)
        if _err:
            quota_exhausted = True
            break
        try:
            url = image_download.normalize_image_url(raw)
            # SSRF 护栏：私网/环回/链路本地/云元数据地址一律拒绝（P1-4 同款）
            app._assert_safe_url(url)
        except app.LinkError:
            skipped += 1
            continue
        task = app.store.create(
            url=url, title='', platform='图片', quality='原图',
            quality_key='origin', kind='image',
            cookie=cookie, proxy=proxy, referer=referer, owner_id=owner,
        )
        app.scheduler.submit(image_download.run_image_download, task, app.store)
        # 任务创建成功才计费（失败/被拒不烧免费额度）—— 与 /api/download 同规
        app.current_member_store(request).use_daily('download', 1)
        task_ids.append(task.id)
    if not task_ids:
        if quota_exhausted:
            raise HTTPException(
                status_code=402,
                detail='MEMBER_QUOTA|' + (_gate(request)
                                          or '今日免费下载次数已用尽 — 开通VIP会员可解锁'))
        raise HTTPException(status_code=400,
                            detail='链接均无法识别，请确认粘贴的是图片直链')
    record_event('image_download', {'count': len(task_ids)})
    _qs = app.current_member_store(request).quota_state('download')
    return {'task_ids': task_ids, 'count': len(task_ids), 'skipped': skipped,
            'quota_exhausted': quota_exhausted,
            'quota': {'subscribed': _qs.get('tier') == 'member',
                      'free_used': _qs.get('used', 0),
                      'free_daily': _qs.get('limit', 10)}}
