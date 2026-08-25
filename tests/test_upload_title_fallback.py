"""验证 /api/commentary/script-only/upload 的 title 兜底逻辑（用 pytest 形式）"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 把 server/ 加入 sys.path，方便 import
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import app as srv  # noqa: E402
from routers.commentary import create_script_only_upload  # noqa: E402


def _run_upload_and_capture_title(filename):
    """mock _commentary_run，捕获传给它的 title"""
    captured = {}

    def fake_run(*args, **kwargs):
        captured["title"] = kwargs.get("title", "<MISSING>")

    fake_file = type("F", (), {
        "filename": filename,
        "file": open("/dev/null", "rb"),
    })()

    real_mp4 = Path.home() / ".video-downloader/commentary_out/work/2a0200515e3c/upload.mp4"
    with patch.object(srv, "_commentary_run", fake_run), \
         patch.object(srv, "_commentary_lock"), \
         patch.object(srv, "_commentary_work_dir", return_value=real_mp4.parent), \
         patch.object(srv, "COMMENTARY_ENABLED", True), \
         patch.object(srv, "COMMENTARY_MODE", "local"), \
         patch.object(srv, "executor") as ex:
        ex.submit.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
        try:
            create_script_only_upload(
                file=fake_file, vertical=True, voice="", trim_start=0, trim_end=0,
                mode="highlights", commentary_type="deep_hl", highlight_source="ai",
                intro_highlight=False, skip_intro_outro=False,
                no_narrate_intro_outro=True, retain_pct=None, web=False,
                one_click=False, style="none", title="",
            )
        finally:
            fake_file.file.close()
    return captured.get("title")


def test_upload_title_fallback_for_uploadmp4_without_metadata():
    """源文件叫 upload.mp4 且 mp4 metadata 无 title → 应自动短码兜底（绝不回退到 'upload'）"""
    real_mp4 = Path.home() / ".video-downloader/commentary_out/work/2a0200515e3c/upload.mp4"
    if not real_mp4.exists():
        pytest.skip(f"真实 mp4 不存在: {real_mp4}")
    title = _run_upload_and_capture_title("upload.mp4")
    print(f"\n[fallback] filename=upload.mp4 → title={title!r}")
    assert title != "upload", f"不应回退到 'upload'，应自动短码兜底: got {title!r}"
    assert title and title.startswith("v") and len(title) == 7, f"应是 v<6hex>: got {title!r}"


def test_upload_title_uses_meaningful_upload_name():
    """用户核心场景（2026-08-25）：上传《少帅.mp4》→ 片名必须是「少帅」（上传前的名字优先）。"""
    title = _run_upload_and_capture_title("少帅.mp4")
    print(f"\n[meaningful] filename=少帅.mp4 → title={title!r}")
    assert title == "少帅", f"上传前的名字(少帅)应优先作为片名: got {title!r}"


if __name__ == "__main__":
    test_upload_title_fallback_for_uploadmp4_without_metadata()
    test_upload_title_uses_meaningful_upload_name()