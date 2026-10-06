"""server/routers/subtitles.py — 由 server/app.py 按域抽取（Phase 1）。
handler 通过 `app.<name>` 访问共享内核（globals/helper/导入）。
所有 profile 均挂载，网页版行为零变化。app 端新功能只改本目录对应文件。
"""
import app
import membership
import user_membership
from fastapi import APIRouter

router = APIRouter()

@router.post("/api/subtitles/list")
def sub_list(req: app.SubListRequest) -> dict:
    video = app._resolve_lib_video(req.lib_id)
    meta = app.library_mod._load_sidecar(video)
    subs = app.subtitles_mod.list_online_subs(meta.get("source_url") or "", req.cookie)
    return {"subs": subs}

@router.post("/api/subtitles/extract")
def sub_extract(req: app.SubExtractRequest, request: app.Request = None) -> dict:
    user_membership.require_login_user(request)
    video = app._resolve_lib_video(req.lib_id)
    meta = app.library_mod._load_sidecar(video)
    out_dir = video.parent
    sub = app.subtitles_mod.extract_online_sub(
        meta.get("source_url") or "", req.lang, req.cookie, "", out_dir, meta.get("title") or ""
    )
    if not sub and not meta.get("source_url"):
        # 无源链接则尝试抽内嵌字幕流
        sub = app.subtitles_mod.extract_embedded_subs(video, out_dir, app.FFMPEG_BIN)
    if not sub or not sub.exists():
        raise app.HTTPException(status_code=404, detail="未找到该语言的字幕（源站无此字幕且无内嵌字幕流）")
    rel = sub.relative_to(out_dir).as_posix()
    return {"sub_rel": rel, "lang": req.lang, "size": sub.stat().st_size}

@router.post("/api/subtitles/burn")
def sub_burn(req: app.SubBurnRequest, request: app.Request = None) -> dict:
    user_membership.require_login_user(request)
    gate = app.cloud_quota_gate(request, resource="cloud_subtitle_burn")   # 云端算力配额：免费 3 次/日（烧录=服务端重编码）
    video = app._resolve_lib_video(req.lib_id)
    out_dir = video.parent
    sub_path = (out_dir / req.sub_rel).resolve()
    if out_dir.resolve() not in sub_path.parents or not sub_path.exists():
        raise app.HTTPException(status_code=404, detail="字幕文件不存在")
    out = app.subtitles_mod.burn_subtitle(video, sub_path, app.FFMPEG_BIN)
    if not out:
        raise app.HTTPException(status_code=500, detail="烧录失败，请检查字幕文件格式")
    app.cloud_quota_count(request, gate)   # 成功才计费
    meta = app.library_mod._load_sidecar(video)
    app.subtitles_mod._write_subtitle_sidecar(out, meta)
    new_id = app.library_mod.encode_id(out.resolve().relative_to(app.DOWNLOAD_DIR.resolve()).as_posix())
    return {"lib_id": new_id, "name": out.name, "title": (meta.get("title") or out.stem) + "（字幕版）"}

@router.post("/api/subtitles/translate")
def sub_translate(req: app.SubTranslateRequest, request: app.Request = None) -> dict:
    user_membership.require_login_user(request)   # 2026-09-29：LLM 翻译吃服务端算力，纳入登录+配额
    # AI 积分（2026-10-05 与桌面端拉齐）：翻译要真调 LLM 并按 chunk 分片，一次可能多次请求。
    # 放在读文件之前 —— 判定失败就别浪费 IO 和（可能付费的）上游调用。
    _msg = membership.gate_message(app.current_member_store(request), "subtitle_translate",
                                   reason="subtitles_translate")
    if _msg:
        raise app.HTTPException(status_code=402, detail=_msg)
    gate = app.cloud_quota_gate(request, resource="cloud_subtitle_translate")
    video = app._resolve_lib_video(req.lib_id)
    out_dir = video.parent
    sub_path = (out_dir / req.sub_rel).resolve()
    if out_dir.resolve() not in sub_path.parents or not sub_path.exists():
        raise app.HTTPException(status_code=404, detail="字幕文件不存在")
    text = sub_path.read_text(encoding="utf-8", errors="ignore")
    # 用统一 LLM 配置做 fallback：用户在前端留空时自动取已保存的 Key/URL/Model
    llm = app.get_llm_config()
    api_key = req.api_key or llm.get("api_key", "")
    base_url = req.base_url or llm.get("base_url", "")
    model = req.model or llm.get("model", "")
    try:
        translated = app.subtitles_mod.translate_srt(text, api_key, base_url, model, req.target)
    except ValueError as exc:
        raise app.HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise app.HTTPException(status_code=502, detail=str(exc))
    app.cloud_quota_count(request, gate)   # 成功才计费
    t = (req.target or "简体中文").strip().lower()
    if any(k in t for k in ("zh", "chinese", "中", "简")):
        ext = "zh"
    else:
        ext = app.re.split(r"[^a-z]", t)[0][:4] or "zh"
    base_stem = app.re.sub(r"\.(zh|en|ja|ko|fr|de|es|ru|pt|it)$", "", sub_path.stem, flags=app.re.I)
    new_path = sub_path.with_name(f"{base_stem}.{ext}.srt")
    new_path.write_text(translated, encoding="utf-8")
    return {"sub_rel": new_path.relative_to(out_dir).as_posix(), "lang": req.target, "text": translated}
