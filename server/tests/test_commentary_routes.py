"""解说路由层守卫与路径推导离线回归测试（2026-09-10 新增）。

背景：解说管线的算法侧（渲染/片头片尾/LLM/音量）在独立仓库 commentary-pipeline
已有完整 pytest 覆盖；但 **app 侧路由层 `routers/commentary.py` 一直无测试**。
这一层承载两类风险，且都不属于「算错」而是「放行不该放行的输入」：

  1. /api/commentary/audio-preview 直接接受前端传来的**任意绝对路径**并回传文件流
     —— 若路径守卫失守即是任意文件读取（把 ~/.ssh/id_rsa 当音频读出来）。
  2. BGM 试听 sidecar 路径推导（`<成片>.previews/<kind>.mp3`）：推错则试听 404，
     用户以为渲染失败而重渲，浪费算力。

覆盖：
  _bgm_preview_mp3_path       sidecar 路径推导与存在性判定
  commentary_audio_preview    本地音乐试听的路径守卫（绝对路径/家目录/扩展名白名单）
  commentary_bgm_preview      BGM 风格 kind 白名单
  commentary_config_get       配置读取端点基本可用性

设计约束：**本测试绝不向用户真实家目录写入任何文件**。
需要「家目录内文件」的用例一律通过 `fake_home()` 把 `Path.home` 指向系统临时目录，
临时文件因此落在 /var/folders（系统自行回收）。这是刻意的——早期版本直接在真实
家目录建 `.vdl_cmtest_*` 目录，一旦清理失败（如被沙盒删除策略拦截，而
`ignore_errors=True` 会静默吞掉）就会把垃圾永久留在用户家目录。

运行：
    cd server && python tests/test_commentary_routes.py
    cd server && python -m pytest tests/test_commentary_routes.py -v
"""
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402  先完成 app 初始化，避免 routers 循环导入
from routers import commentary as cm  # noqa: E402
from fastapi import HTTPException  # noqa: E402


@contextmanager
def fake_home():
    """把 pathlib.Path.home 临时指向系统临时目录，供「家目录内文件」用例使用。

    返回的路径已 resolve()——macOS 上 /var 是 /private/var 的软链，若这里给的是
    未解析路径，而被测函数内部会把传入路径 resolve()，二者做 relative_to 时会
    因前缀不同而误判为「不在家目录下」。
    """
    import pathlib
    fake = Path(tempfile.mkdtemp(prefix="vdl_fakehome_")).resolve()
    orig = pathlib.Path.home
    pathlib.Path.home = staticmethod(lambda: fake)
    try:
        yield fake
    finally:
        pathlib.Path.home = orig
        shutil.rmtree(fake, ignore_errors=True)


def _expect_400(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except HTTPException as e:
        assert e.status_code == 400, f"预期 400，实际 {e.status_code}: {e.detail}"
        return e
    raise AssertionError(f"应被拒绝但放行了: {fn.__name__}{args}")


# ---------------------------------------------------------------- sidecar 路径推导

def test_bgm_preview_path_none_without_sidecar():
    """成片存在但没有 .previews 目录 → 返回 None（路由据此提示重新渲染）。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_cm_"))
    try:
        mp4 = tmp / "final.mp4"
        mp4.write_bytes(b"stub")
        assert cm._bgm_preview_mp3_path(mp4, "soft") is None
        print("✅ 缺 sidecar 返回 None，前端可提示「请重新渲染」而非白屏")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bgm_preview_path_resolves_sidecar():
    """有 sidecar 时返回 `<stem>.previews/<kind>.mp3`（成片与试听一一对应）。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_cm_"))
    try:
        mp4 = tmp / "final.mp4"
        mp4.write_bytes(b"stub")
        pv = tmp / "final.previews"
        pv.mkdir()
        (pv / "soft.mp3").write_bytes(b"ID3")
        got = cm._bgm_preview_mp3_path(mp4, "soft")
        assert got is not None and got.name == "soft.mp3"
        assert got.parent.name == "final.previews", f"目录名不符: {got.parent.name}"
        print("✅ sidecar 路径推导正确（final.mp4 → final.previews/soft.mp3）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bgm_preview_path_missing_kind():
    """只落了 soft，查 epic 应返回 None——三种风格各自独立，不可互相顶替。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_cm_"))
    try:
        mp4 = tmp / "final.mp4"
        mp4.write_bytes(b"stub")
        pv = tmp / "final.previews"
        pv.mkdir()
        (pv / "soft.mp3").write_bytes(b"ID3")
        assert cm._bgm_preview_mp3_path(mp4, "epic") is None
        assert cm._bgm_preview_mp3_path(mp4, "light") is None
        print("✅ 缺失的风格返回 None，不会串用其它风格的试听")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bgm_preview_path_handles_no_suffix():
    """无扩展名的成片路径不应抛异常（推导退回本身 + .previews）。"""
    tmp = Path(tempfile.mkdtemp(prefix="vdl_cm_"))
    try:
        nosuf = tmp / "nosuffix"
        nosuf.write_bytes(b"stub")
        assert cm._bgm_preview_mp3_path(nosuf, "soft") is None
        print("✅ 无扩展名路径安全返回 None（不抛异常、不误判）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 本地音乐试听守卫

def test_audio_preview_rejects_relative_or_empty():
    """相对路径 / 空路径 → 400（必须绝对路径，避免以服务进程 cwd 解析）。"""
    _expect_400(cm.commentary_audio_preview, "")
    _expect_400(cm.commentary_audio_preview, "a.mp3")
    _expect_400(cm.commentary_audio_preview, "../etc/hosts")
    print("✅ 非绝对路径被拒绝，不会以进程工作目录为基准解析文件")


def test_audio_preview_rejects_outside_home():
    """家目录外的绝对路径 → 400。这是任意文件读取的闸门。"""
    e = _expect_400(cm.commentary_audio_preview, "/etc/hosts")
    assert "家目录" in e.detail, f"拒绝理由应指明家目录限制，实际: {e.detail}"
    _expect_400(cm.commentary_audio_preview, "/etc/evil.mp3")
    print("✅ 家目录外路径被拒绝（含伪装成 .mp3 的情况），闸门有效")


def test_audio_preview_accepts_audio_in_home():
    """家目录下真实音频通过；扩展名大小写不敏感。"""
    with fake_home() as home:
        ok = home / "bgm.mp3"
        ok.write_bytes(b"ID3")
        resp = cm.commentary_audio_preview(str(ok))
        assert Path(resp.path).name == "bgm.mp3"

        up = home / "UP.MP3"
        up.write_bytes(b"ID3")
        assert Path(cm.commentary_audio_preview(str(up)).path).name == "UP.MP3"
    print("✅ 家目录内音频通过，扩展名大小写不敏感")


def test_audio_preview_rejects_non_audio_extension():
    """扩展名白名单：非音频文件即使在家目录下也拒绝。

    注意校验顺序为「路径 → 存在 → 扩展名」，故此处必须先真实建出文件，
    否则会先命中 404（文件不存在）而非 400（扩展名不允许）。
    """
    with fake_home() as home:
        txt = home / "notes.txt"
        txt.write_bytes(b"x")
        e = _expect_400(cm.commentary_audio_preview, str(txt))
        assert ".txt" in e.detail, f"应指出被拒的扩展名，实际: {e.detail}"

        noext = home / "noext"
        noext.write_bytes(b"x")
        _expect_400(cm.commentary_audio_preview, str(noext))
    print("✅ 非音频扩展名被拒绝，不会把任意文件当音频流回传")


def test_audio_preview_missing_file_is_404():
    """家目录下的合法路径但文件不存在 → 404（与「不允许」区分）。"""
    with fake_home() as home:
        try:
            cm.commentary_audio_preview(str(home / "missing.mp3"))
            raise AssertionError("应返回 404")
        except HTTPException as e:
            assert e.status_code == 404, f"预期 404，实际 {e.status_code}"
    print("✅ 文件不存在返回 404（与 400「不允许」语义区分，便于前端提示）")


# ---------------------------------------------------------------- BGM kind 白名单

def test_bgm_preview_rejects_unknown_kind():
    """kind 只允许 soft/light/epic；其它值（含大小写变体）→ 400。

    若不校验，kind 会被拼进 sidecar 文件名，成为路径穿越的入口。
    """
    for bad in ["bad", "SOFT", "soft/../x", "", "epic.mp3"]:
        e = _expect_400(cm.commentary_bgm_preview, "deadbeef0000", bad)
        assert "soft/light/epic" in e.detail, f"应说明合法取值，实际: {e.detail}"
    print("✅ 非法 kind 被拒绝（白名单校验先于 cid），杜绝拼路径穿越")


def test_bgm_preview_legal_kind_proceeds_to_cid_check():
    """合法 kind 应继续走 cid 校验——证明拒绝并非「一律拒绝」。"""
    try:
        cm.commentary_bgm_preview("deadbeef0000", "soft")
        raise AssertionError("假 cid 应被拒绝")
    except HTTPException as e:
        assert e.status_code == 400
        assert "kind" not in e.detail, f"合法 kind 不应报 kind 错误: {e.detail}"
    print("✅ 合法 kind 通过白名单并进入 cid 校验（拒绝行为有区分度）")


# ---------------------------------------------------------------- 配置端点

def test_commentary_config_get_returns_dict():
    """配置读取端点返回 dict（前端初始化依赖它，不能 500）。"""
    cfg = cm.commentary_config_get()
    assert isinstance(cfg, dict)
    print("✅ 解说配置读取端点正常返回 dict")


# ---------------------------------------------------------------- 字数 / 时长预检
#
# 背景：渲染层原速模式下 out_dur = end - start，ffmpeg 以 -t 收尾，
# 旁白超出窗口会被静默截断；而生成端提示词并未按时长约束字数（"约 40~70 字…不用怕写长"），
# 人工改长后无兜底。故在此校验预算计算与保存回传的 over_limit。

@contextmanager
def _script_job(segments):
    """造一个 script_ready 的假任务（script.json 落临时目录），用完清理。"""
    tmp = tempfile.mkdtemp(prefix="vdl_scriptjob_")
    job_id = "job-narration-budget"
    p = Path(tmp) / "script.json"
    p.write_text(json.dumps({"title": "t", "voice": "v", "segments": segments},
                            ensure_ascii=False), encoding="utf-8")
    server_app.commentary_jobs[job_id] = {
        "status": "script_ready", "script_path": str(p), "voice": "v"}
    try:
        yield job_id, p
    finally:
        server_app.commentary_jobs.pop(job_id, None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_narration_budget_scales_with_duration():
    """10 秒窗口 @5 字/秒 = 50 字；预算随时长线性变化。"""
    assert cm._narration_budget(0, 10) == 50, "10s 应给出 50 字预算"
    assert cm._narration_budget(5, 9) == 20, "4s 应给出 20 字预算"
    print("✅ 字数预算随时长线性计算")


def test_narration_budget_zero_when_duration_unusable():
    """时长缺失/为 0/为负 → 返回 0（表示不校验，而不是给出误导性预算）。"""
    assert cm._narration_budget(None, None) == 0
    assert cm._narration_budget(5, 5) == 0, "零时长不应给出预算"
    assert cm._narration_budget(10, 4) == 0, "负时长不应给出预算"
    assert cm._narration_budget("x", "y") == 0, "非法值应兜底为 0"
    print("✅ 时长不可用时不校验（返回 0）")


def test_narration_chars_ignores_whitespace_but_keeps_punctuation():
    """计字口径：去空白，标点保留（朗读同样占时长）。"""
    assert cm._narration_chars("你好世界") == 4
    assert cm._narration_chars(" 你好\n 世界 ") == 4, "空白应被排除"
    assert cm._narration_chars("你好，世界！") == 6, "标点应计入"
    assert cm._narration_chars(None) == 0
    print("✅ 计字口径正确（去空白、保留标点）")


def test_update_script_reports_over_limit():
    """保存后回传超标段：10s 窗口塞 80 字 → 命中 over_limit。"""
    with _script_job([{"start": 0, "end": 10, "narration": "短句"}]) as (job_id, path):
        long_text = "这" * 80
        res = cm.update_script(job_id, server_app.ScriptUpdateRequest(
            segments=[{"start": 0, "end": 10, "narration": long_text}]))
        assert res["status"] == "updated"
        over = res.get("over_limit") or []
        assert len(over) == 1, f"应有 1 段超长，实际 {over}"
        assert over[0]["index"] == 1, "段序号应为 1-based"
        assert over[0]["chars"] == 80 and over[0]["budget"] == 50, f"明细不符: {over[0]}"
        # 写回已生效（不能只报不存）
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["segments"][0]["narration"] == long_text
    print("✅ 超标段被检出并回传（且已写回 script.json）")


def test_update_script_no_over_limit_when_within_budget():
    """字数在预算内 → over_limit 为空数组（不是 None，前端直接 .length 安全）。"""
    with _script_job([{"start": 0, "end": 10, "narration": "短句"}]) as (job_id, _p):
        res = cm.update_script(job_id, server_app.ScriptUpdateRequest(
            segments=[{"start": 0, "end": 10, "narration": "二十字以内的短解说词"}]))
        assert res.get("over_limit") == [], f"应为 []，实际 {res.get('over_limit')!r}"
    print("✅ 未超时 over_limit 为空数组")


# ---------------------------------------------------------------- 渲染前统一预检（方案 B）
#
# 无论脚本有没有被人工改过，渲染前都校验一遍；检出超标段则自动放弃原速，
# 让渲染层放慢画面把窗口撑开（k = vdur/win），而不是让 -t 把旁白截断。

def test_option_args_emits_no_original_speed_flag():
    """original_speed=False → 带 --no-original-speed；默认不得误带。"""
    assert "--no-original-speed" in server_app._commentary_option_args(original_speed=False)
    assert "--no-original-speed" not in server_app._commentary_option_args(), \
        "默认必须保持原速，不能误加该 flag"
    print("✅ 原速开关正确翻译成 CLI flag")


def test_plan_original_speed_keeps_original_when_within_budget():
    """字数在预算内 → 保持原速，over_limit 为空。"""
    use, over = cm._plan_original_speed([{"start": 0, "end": 10, "narration": "二十字以内"}])
    assert use is True and over == []
    print("✅ 未超长时保持原速")


def test_plan_original_speed_auto_disabled_when_over_limit():
    """检出超标段 → 自动放弃原速（宁可画面放慢，也不出截断的残片）。"""
    use, over = cm._plan_original_speed([{"start": 0, "end": 10, "narration": "这" * 80}])
    assert use is False, "超长时必须放弃原速，否则旁白会被 -t 截断"
    assert len(over) == 1 and over[0]["index"] == 1
    print("✅ 超长时自动放弃原速（防截断）")


def test_plan_original_speed_respects_existing_preference():
    """本就非原速时，即使超长也不能被反向改回原速。"""
    use, over = cm._plan_original_speed([{"start": 0, "end": 10, "narration": "这" * 80}],
                                        prefer_original_speed=False)
    assert use is False and len(over) == 1
    print("✅ 已非原速时不会被反向改回")


def test_plan_original_speed_skips_when_no_duration():
    """时长缺失 → 不校验（不产生误报），保持原速。"""
    use, over = cm._plan_original_speed([{"start": None, "end": None, "narration": "这" * 200}])
    assert use is True and over == []
    print("✅ 无时长信息时不误报")


if __name__ == "__main__":
    test_bgm_preview_path_none_without_sidecar()
    test_bgm_preview_path_resolves_sidecar()
    test_bgm_preview_path_missing_kind()
    test_bgm_preview_path_handles_no_suffix()

    test_audio_preview_rejects_relative_or_empty()
    test_audio_preview_rejects_outside_home()
    test_audio_preview_accepts_audio_in_home()
    test_audio_preview_rejects_non_audio_extension()
    test_audio_preview_missing_file_is_404()

    test_bgm_preview_rejects_unknown_kind()
    test_bgm_preview_legal_kind_proceeds_to_cid_check()

    test_commentary_config_get_returns_dict()

    test_narration_budget_scales_with_duration()
    test_narration_budget_zero_when_duration_unusable()
    test_narration_chars_ignores_whitespace_but_keeps_punctuation()
    test_update_script_reports_over_limit()
    test_update_script_no_over_limit_when_within_budget()

    test_option_args_emits_no_original_speed_flag()
    test_plan_original_speed_keeps_original_when_within_budget()
    test_plan_original_speed_auto_disabled_when_over_limit()
    test_plan_original_speed_respects_existing_preference()
    test_plan_original_speed_skips_when_no_duration()

    print("\n🎉 解说路由层测试全部通过（22 项）")


# ---------------------------------------------------------------- 片头/片尾边界建议（2026-09-20）
# 背景：前端「起点/终点」在「去片头片尾」模式下要预填真实边界（用户：「选了去片头片尾，
# 左边的起点肯定是有时间的不是0」）。/api/commentary/suggest-range 只跑 ffmpeg
# 静音/黑场/静止轻量探测；探测不到 → ok=False，前端保持「自动检测」，渲染时管线
# 再做全量检测（含视觉集数卡）。这里验证路由守卫/透传与探测函数的守卫路径。

def test_suggest_range_passthrough_and_guard():
    """suggest-range 路由：未启用解说 → 503；启用 → 透传 _suggest_intro_outro 结果。"""
    from routers import commentary as cr
    saved_enabled = server_app.COMMENTARY_ENABLED
    try:
        server_app.COMMENTARY_ENABLED = False
        raised = False
        try:
            cr.suggest_range(server_app.CommentaryRequest())
        except Exception as e:
            raised = getattr(e, "status_code", None) == 503
        assert raised, "未启用解说时应 503"
    finally:
        server_app.COMMENTARY_ENABLED = saved_enabled
    saved_src = server_app._resolve_source
    saved_sug = server_app._suggest_intro_outro
    try:
        server_app.COMMENTARY_ENABLED = True   # 透传阶段需要开关打开（测试环境默认关）
        server_app._resolve_source = lambda p: "/tmp/fake.mp4"
        server_app._suggest_intro_outro = lambda src: {
            "ok": True, "lo": 85.0, "hi": 2600.0, "dur": 2734.0}
        res = cr.suggest_range(server_app.CommentaryRequest(file_id="x.mp4"))
        assert res["ok"] is True and res["lo"] == 85.0 and res["hi"] == 2600.0
    finally:
        server_app._resolve_source = saved_src
        server_app._suggest_intro_outro = saved_sug
    print("✅ suggest-range 路由守卫与透传正常")


def test_suggest_intro_outro_guards():
    """_suggest_intro_outro：过短视频直接放弃；找不到 ffmpeg 不抛异常、返回 ok=False。"""
    saved_dur = server_app._probe_video_duration
    saved_which = server_app.shutil.which
    try:
        server_app._probe_video_duration = lambda p: 10.0
        r = server_app._suggest_intro_outro("/tmp/whatever.mp4")
        assert r["ok"] is False and r["dur"] == 10.0
        server_app._probe_video_duration = lambda p: 1800.0
        server_app.shutil.which = lambda name, path=None: ""   # ffmpeg/ffprobe 全部落空
        r = server_app._suggest_intro_outro("/tmp/whatever.mp4")
        assert r["ok"] is False and r["lo"] is None and r["hi"] is None
    finally:
        server_app._probe_video_duration = saved_dur
        server_app.shutil.which = saved_which
    print("✅ suggest 轻量探测守卫正常（短视频/无 ffmpeg 不抛异常）")
