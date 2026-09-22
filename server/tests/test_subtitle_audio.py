"""字幕提取接受音频文件回归（2026-09-22 用户报「选 .m4a 报请选择视频文件」）。

此前 extract 端点只收视频后缀，但下游管线是「ffmpeg -vn 抽 16k mono → ASR」，
对纯音频天然兼容 ⇒ 放开为 视频 + 音频（与桌面选择器 choose_files("media") 对齐）。

运行（独立进程，HOME 隔离）：
    cd server && .build_venv/bin/python tests/test_subtitle_audio.py
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="vdl_sb_audio_")
os.environ["HOME"] = _TMP
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server_app.app, raise_server_exceptions=False)


def _setup():
    server_app.executor.submit = lambda *a, **k: None
    server_app._check_rate_limit = lambda request: None
    sb_mod = __import__("routers.subtitle", fromlist=["_resolve_safe_local_path"])
    sb_mod._resolve_safe_local_path = lambda p: Path(p)


def _ext_job(ext: str, member: bool = True):
    """造一个指定后缀的文件并提交，返回 (status_code, body)。"""
    p = os.path.join(_TMP, f"sample_{ext.strip('.')}{ext}")
    Path(p).write_text("fake", encoding="utf-8")
    if member:
        # 会员档绕过免费日配额墙（配额墙由 test_subtitle_quota.py 单独覆盖）
        ms = os.path.join(_TMP, ".video-downloader", "membership.json")
        Path(ms).write_text('{"download_member": {"active": true}}', encoding="utf-8")
        server_app.member_store._loaded = False
    return client.post("/api/subtitle/extract", json={"local_path": p, "model_size": "base"})


def run():
    _setup()
    ok = True

    # 1) 常见音频后缀都应被接受（拿到 job_id，而非 409）
    for ext in (".m4a", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".opus", ".wma"):
        r = _ext_job(ext)
        passed = r.status_code == 200 and r.json().get("job_id")
        ok &= bool(passed)
        print(("✅" if passed else "❌"), f"音频 {ext} → HTTP {r.status_code}", (r.json() if not passed else "")[:120])

    # 2) 视频后缀仍被接受（回归）
    r = _ext_job(".mp4")
    passed = r.status_code == 200 and r.json().get("job_id")
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"视频 .mp4 → HTTP {r.status_code}")

    # 3) 真正不支持的类型仍要被拒（如 .txt），且文案已更新
    r = _ext_job(".txt")
    body = r.json()
    passed = r.status_code == 409 and "音频" in str(body.get("detail", ""))
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"不支持的 .txt → HTTP {r.status_code} detail={body.get('detail')}")

    print("\n通过" if ok else "\n失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
