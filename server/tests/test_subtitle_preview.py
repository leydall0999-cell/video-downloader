"""字幕结果预览端点单测（2026-09-22，用户要求「识别完要可预览」）。

背景：结果卡此前只有「下载 SRT / 下载 TXT」两个按钮，用户看不到识别出来的内容，
只能先下载再用别的软件打开。新增 `GET /api/subtitle/{job_id}/preview` 把已落盘的
SRT 解析成逐句结构给前端展示 —— 本文件锁定它的四条契约：

  1. 逐句解析正确（序号 / 起止秒 / 紧凑时间戳 / 多行正文）
  2. 超长视频截断到 _PREVIEW_MAX_LINES，但 total 仍数全（前端要显示「共 N 句」）
  3. 设备隔离照旧（别人的 device 取不到，防跨页面偷看）
  4. 任务不存在 / 未完成 → 404（不 500、不泄路径）

运行（独立进程，HOME 隔离）：
    cd server && ../.build_venv/bin/python tests/test_subtitle_preview.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="vdl_sb_preview_")
os.environ["HOME"] = _TMP
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import routers.subtitle as sb_mod  # noqa: E402

client = TestClient(server_app.app, raise_server_exceptions=False)

_SRT_3 = (
    "1\n00:00:01,200 --> 00:00:03,400\n我自远古来\n\n"
    "2\n00:01:03,500 --> 00:01:05,000\n第二句\n换行也要保留\n\n"
    "3\n01:02:03,000 --> 01:02:04,250\n最后一句\n\n"
)


def _make_job(job_id, srt_text, device_id="", status="completed"):
    """直接把一条已完成任务塞进 SUBTITLE_JOBS（不跑 whisper）。"""
    srt = Path(server_app.SUBTITLE_DIR) / f"sub_{job_id}_demo.srt"
    srt.parent.mkdir(parents=True, exist_ok=True)
    srt.write_text(srt_text, encoding="utf-8")
    sb_mod.SUBTITLE_JOBS[job_id] = {
        "status": status, "stage": "", "progress": 100, "error": "",
        "srt_file": str(srt), "txt_file": "", "srt_name": "demo.srt", "txt_name": "demo.txt",
        "lines": 0, "language": "zh", "cpu_threads": 4, "device_id": device_id,
    }
    return srt


def test_parse_basic():
    """逐句解析：序号、起止秒、紧凑时间戳（<1h 用 mm:ss / ≥1h 用 h:mm:ss）、多行正文。"""
    _make_job("prev1", _SRT_3)
    r = client.get("/api/subtitle/prev1/preview")
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d["lines"] == 3, d
    assert d["truncated"] is False, d
    assert d["language"] == "zh", d
    segs = d["segments"]
    assert len(segs) == 3, segs
    assert segs[0]["i"] == 1 and segs[0]["ts"] == "00:01", segs[0]
    assert abs(segs[0]["start"] - 1.2) < 1e-6 and abs(segs[0]["end"] - 3.4) < 1e-6, segs[0]
    assert segs[0]["text"] == "我自远古来", segs[0]
    assert segs[1]["ts"] == "01:03", segs[1]
    assert segs[1]["text"] == "第二句\n换行也要保留", segs[1]
    assert segs[2]["ts"] == "1:02:03", segs[2]
    print("✅ 逐句解析正确（序号 / 起止秒 / mm:ss 与 h:mm:ss / 多行正文保留）")


def test_truncated_but_total_counted():
    """超长视频：只回前 N 句，但 lines 数全部（前端要显示「共 N 句 · 仅预览前 N 句」）。"""
    total = sb_mod._PREVIEW_MAX_LINES + 25
    blocks = []
    for i in range(1, total + 1):
        blocks.append(f"{i}\n00:00:{i % 60:02d},000 --> 00:00:{i % 60:02d},500\n第{i}句\n")
    _make_job("prev2", "\n".join(blocks))
    r = client.get("/api/subtitle/prev2/preview")
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d["lines"] == total, (d["lines"], total)
    assert len(d["segments"]) == sb_mod._PREVIEW_MAX_LINES, len(d["segments"])
    assert d["truncated"] is True, d["truncated"]
    print(f"✅ 超长字幕截断：lines={d['lines']}（全量）segments={len(d['segments'])}（上限）truncated=True")


def test_device_isolation():
    """设备隔离：别的 device 取不到（与字幕状态/下载端点同口径）。"""
    _make_job("prev3", _SRT_3, device_id="owner-device")
    assert client.get("/api/subtitle/prev3/preview",
                      headers={"X-Device-Id": "owner-device"}).status_code == 200
    r = client.get("/api/subtitle/prev3/preview", headers={"X-Device-Id": "other-device"})
    assert r.status_code == 404, f"应 404，实际 {r.status_code}: {r.text[:200]}"
    print("✅ 设备隔离：本设备 200 / 其他设备 404")


def test_missing_and_unfinished():
    """不存在 或 未完成 → 404（不 500、不泄文件路径）。"""
    r = client.get("/api/subtitle/nosuchjob/preview")
    assert r.status_code == 404, r.status_code
    _make_job("prev4", _SRT_3, status="running")
    r = client.get("/api/subtitle/prev4/preview")
    assert r.status_code == 404, f"未完成任务应 404，实际 {r.status_code}"
    print("✅ 任务不存在 / 未完成 → 404（有界错误，不泄路径）")


if __name__ == "__main__":
    test_parse_basic()
    test_truncated_but_total_counted()
    test_device_isolation()
    test_missing_and_unfinished()
    print("\n🎉 字幕预览端点单测全部通过（4 项）")
    shutil.rmtree(_TMP, ignore_errors=True)
