"""验证 /api/commentary/render/{job_id} 渲染时正确把 title 传给 _commentary_run（不再丢失）。

用户 2026-08-25 反馈：脚本生成时 title 算对了（v<6hex>），但点「生成成片」走
render_script 后成片名又变回 upload-解说完成...。根因：render_script 之前
完全没把 title 传给 _commentary_run，process.py --edit-only 拿不到 --out-name
只能退回 video_path basename (upload)。
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 把 server/ 加入 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import app as srv  # noqa: E402
from routers.commentary import render_script  # noqa: E402


def _make_job(*, title_in_dict: str = "", src_path: str = "", script_llm_title: str = "少帅_国产剧_第8集_..."):
    """构造一个 status=script_ready 的 job dict，模拟脚本生成后的状态。"""
    return {
        "status": "script_ready",
        "script_path": "/tmp/fake_script.json",
        "src_path": src_path,
        "title": title_in_dict,
        "voice": "",
        "trim_start": 0.0,
        "trim_end": 0.0,
    }


def _write_fake_script(path: str, llm_title: str = "少帅_国产剧_第8集_...") -> None:
    """写一个最小合法 script.json（render_script 会读 options 字段）。"""
    data = {
        "title": llm_title,
        "voice": "zh-CN-XiaoxiaoNeural",
        "segments": [],
        "options": {
            "commentary_type": "deep_hl",
            "highlight_source": "ai",
            "intro_highlight": False,
            "skip_intro_outro": False,
            "no_narrate_intro_outro": True,
            "retain_pct": None,
            "web": False,
            "one_click": False,
        },
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _run_render_and_capture_title(job: dict, script_path: str):
    """mock _commentary_run，捕获传给它的 title。"""
    captured = {}

    def fake_run(*args, **kwargs):
        captured["title"] = kwargs.get("title", "<MISSING>")
        captured["edit_only"] = kwargs.get("edit_only", "<MISSING>")
        captured["args"] = list(args)

    with patch.object(srv, "_commentary_run", fake_run), \
         patch.object(srv, "_commentary_lock"), \
         patch.object(srv, "COMMENTARY_ENABLED", True), \
         patch.object(srv, "COMMENTARY_MODE", "local"), \
         patch.object(srv, "executor") as ex, \
         patch.dict(srv.commentary_jobs, {job["_test_id"]: job}, clear=False):
        ex.submit.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
        render_script(job["_test_id"], vertical=False, voice="")
    return captured


def test_render_passes_title_from_job_dict():
    """核心场景：job dict 已存 title='v3a8f1b' → render_script 必须把这个传给 _commentary_run。"""
    job_id = "testjob0001"
    script_path = "/tmp/vdl_test_render_script.json"
    _write_fake_script(script_path, llm_title="LLM 推断的剧名（不应被用作文件名）")

    job = _make_job(title_in_dict="v3a8f1b", src_path="/tmp/fake_src.mp4")
    job["_test_id"] = job_id
    srv.commentary_jobs[job_id] = job

    captured = _run_render_and_capture_title(job, script_path)
    print(f"\n[render] title passed = {captured.get('title')!r}")
    assert captured.get("title") == "v3a8f1b", (
        f"render_script 必须把 job 存的 title 透传给 _commentary_run，"
        f"got {captured.get('title')!r}"
    )


def test_render_uses_meaningful_stem_when_job_missing_title():
    """旧任务（job dict 无 title）→ 从 src_path 走同样的回退链。src_path 名为 少帅.mp4 → 少帅。"""
    job_id = "testjob0002"
    # 临时造一个真实的 mp4 文件，src_path 形如 /tmp/.../少帅.mp4
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        real_mp4 = Path(td) / "少帅.mp4"
        real_mp4.write_bytes(b"\x00")  # ffprobe 读不到 title tag，但 _meaningful_stem('少帅')=True
        job = _make_job(title_in_dict="", src_path=str(real_mp4))
        job["_test_id"] = job_id
        srv.commentary_jobs[job_id] = job
        captured = _run_render_and_capture_title(job, "/tmp/whatever.json")
    print(f"\n[render-fallback] src=少帅.mp4 → title = {captured.get('title')!r}")
    assert captured.get("title") == "少帅", (
        f"旧任务无 title 时应回退到 meaningful stem, got {captured.get('title')!r}"
    )


def test_render_uses_shortcode_when_src_is_uploadmp4():
    """旧任务无 title + src 叫 upload.mp4 → 短码兜底（绝不直接用 'upload'）。"""
    job_id = "testjob0003"
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        real_mp4 = Path(td) / "upload.mp4"
        real_mp4.write_bytes(b"\x00")
        job = _make_job(title_in_dict="", src_path=str(real_mp4))
        job["_test_id"] = job_id
        srv.commentary_jobs[job_id] = job
        captured = _run_render_and_capture_title(job, "/tmp/whatever.json")
    print(f"\n[render-shortcode] src=upload.mp4 → title = {captured.get('title')!r}")
    assert captured.get("title") != "upload", (
        f"upload.mp4 不应直接拿 'upload' 做片名, got {captured.get('title')!r}"
    )
    assert captured.get("title", "").startswith("v") and len(captured.get("title", "")) == 7, (
        f"应自动短码兜底 (v<6hex>), got {captured.get('title')!r}"
    )


if __name__ == "__main__":
    test_render_passes_title_from_job_dict()
    test_render_uses_meaningful_stem_when_job_missing_title()
    test_render_uses_shortcode_when_src_is_uploadmp4()
    print("\n✅ 3/3 passed")
