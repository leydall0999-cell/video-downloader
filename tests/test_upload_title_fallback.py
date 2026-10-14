"""验证 /api/commentary/script-only/upload 的 title 兜底逻辑（用 pytest 形式）"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# 把 server/ 加入 sys.path，方便 import
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import app as srv  # noqa: E402
from routers.commentary import create_script_only_upload  # noqa: E402


def _run_upload_and_capture_title(filename):
    """mock _commentary_run，捕获传给它的 title。

    🔴 2026-10-14：原先依赖 `~/.video-downloader/commentary_out/work/2a0200515e3c/`
    下的真实 mp4（2026-08-25 产物，早已被时效清理）⇒ 第二个用例在没有该产物的
    机器上必 500。改为自包含：临时工作目录 + 哑文件，title 派生只依赖文件名，
    与真实产物无关（无 metadata 正是第一个用例想要的场景）。
    """
    captured = {}

    def fake_run(*args, **kwargs):
        captured["title"] = kwargs.get("title", "<MISSING>")

    fake_file = type("F", (), {
        "filename": filename,
        "file": open("/dev/null", "rb"),
    })()

    work = Path(tempfile.mkdtemp(prefix="vdl_upload_title_"))
    (work / "upload.mp4").write_bytes(b"x")  # 哑文件：无 mp4 metadata
    try:
        with patch.object(srv, "_commentary_run", fake_run), \
             patch.object(srv, "_commentary_lock"), \
             patch.object(srv, "_commentary_work_dir", return_value=work), \
             patch.object(srv, "COMMENTARY_ENABLED", True), \
             patch.object(srv, "COMMENTARY_MODE", "local"), \
             patch.object(srv, "executor") as ex, \
             patch("routers.quota.assert_upload_allowed", lambda *a, **k: None):
            # 🔴 配额闸门显式 no-op：本测试只验 title 派生。两个用例同进程共享
            # member_store，第一个用例会消耗掉唯一免费体验，不 mock 第二个必 402
            # （2026-10-14 实测）。commentary.py 是函数内 import，patch 模块属性即生效。
            ex.submit.side_effect = lambda fn, *a, **kw: fn(*a, **kw)
            create_script_only_upload(
                file=fake_file, vertical=True, voice="", trim_start=0, trim_end=0,
                mode="highlights", commentary_type="deep_hl", highlight_source="ai",
                intro_highlight=False, skip_intro_outro=False,
                no_narrate_intro_outro=True, retain_pct=None, web=False,
                one_click=False, style="none", title="",
            )
    finally:
        fake_file.file.close()
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return captured.get("title")


def test_upload_title_fallback_for_uploadmp4_without_metadata():
    """源文件叫 upload.mp4 且 mp4 metadata 无 title → 应自动短码兜底（绝不回退到 'upload'）"""
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
