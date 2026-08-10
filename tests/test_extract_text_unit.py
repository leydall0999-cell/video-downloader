"""extract_text 纯函数单元测试（不触发网络、不依赖 ffmpeg/whisper）。"""
import sys
from pathlib import Path

SERVER = str(Path(__file__).resolve().parent.parent / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from extract_text import srt_to_plaintext, VALID_MODES  # noqa: E402


def test_srt_to_plaintext_strips_timing_and_indices():
    srt = """1
00:00:01,000 --> 00:00:04,000
Hello world

2
00:00:05,000 --> 00:00:07,000
Second line
"""
    path = Path(__file__).resolve().parent / "_tmp_extract_test.srt"
    path.write_text(srt, encoding="utf-8")
    try:
        text = srt_to_plaintext(path)
        assert "Hello world" in text
        assert "Second line" in text
        assert "00:00" not in text
        assert "1\n" not in text.splitlines()[0]
    finally:
        path.unlink(missing_ok=True)


def test_srt_to_plaintext_handles_vtt():
    vtt = """WEBVTT

00:00:01.000 --> 00:00:04.000
<c.vttcss>Hello</c>

00:00:05.000 --> 00:00:07.000 line:0%
world
"""
    path = Path(__file__).resolve().parent / "_tmp_extract_test.vtt"
    path.write_text(vtt, encoding="utf-8")
    try:
        text = srt_to_plaintext(path)
        assert "Hello" in text
        assert "world" in text
        assert "WEBVTT" not in text
        assert "<c.vttcss>" not in text
        assert "line:0%" not in text
    finally:
        path.unlink(missing_ok=True)


def test_valid_modes_include_empty_and_three_choices():
    assert "" in VALID_MODES
    assert "spoken" in VALID_MODES
    assert "description" in VALID_MODES
    assert "both" in VALID_MODES
    assert len(VALID_MODES) == 4
