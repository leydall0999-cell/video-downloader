"""配音试听响度后处理：_build_loudness_filter 纯函数 + /api/commentary/voice-preview 端点集成。

覆盖：
- loudness 数字 -> loudnorm 滤镜串
- loudness="off"/空 -> 仅增益兜底（无 loudnorm）
- boost 夹取边界
- 端点拒绝非法 loudness/boost（400），合法值透传给 _run_voice_preview
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "server"))

import app as srv  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def test_build_loudness_filter_numeric():
    f = srv._build_loudness_filter("-14", "1.0")
    assert "loudnorm=I=-14.0" in f
    assert "volume=1.00" in f
    assert "alimiter=limit=0.98" in f


def test_build_loudness_filter_off_only_boost():
    f = srv._build_loudness_filter("off", "1.3")
    assert "loudnorm" not in f
    assert "volume=1.30" in f
    assert "alimiter=limit=0.98" in f


def test_build_loudness_filter_empty_no_loudnorm():
    f = srv._build_loudness_filter("", None)
    assert "loudnorm" not in f
    assert "volume=1.00" in f


def test_build_loudness_filter_boost_clamped():
    # boost 超出 2.0 被夹到 2.0
    f = srv._build_loudness_filter("-12", "5.0")
    assert "volume=2.00" in f
    # boost 低于 0.5 被夹到 0.5
    f2 = srv._build_loudness_filter("-12", "0.1")
    assert "volume=0.50" in f2


def test_build_loudness_filter_boundary():
    lo = srv._build_loudness_filter("-18", "1.0")
    hi = srv._build_loudness_filter("-10", "1.0")
    assert "loudnorm=I=-18.0" in lo
    assert "loudnorm=I=-10.0" in hi


def test_build_loudness_filter_invalid_raises():
    import pytest
    with pytest.raises(ValueError):
        srv._build_loudness_filter("abc", "1.0")


def test_endpoint_rejects_bad_loudness():
    client = TestClient(srv.app)
    resp = client.post("/api/commentary/voice-preview",
                       data={"voice": "zh-CN-XiaoxiaoNeural", "loudness": "5", "boost": "1.0"})
    assert resp.status_code == 400


def test_endpoint_rejects_bad_boost():
    client = TestClient(srv.app)
    resp = client.post("/api/commentary/voice-preview",
                       data={"voice": "zh-CN-XiaoxiaoNeural", "loudness": "-14", "boost": "9"})
    assert resp.status_code == 400


def test_endpoint_passes_loudness_to_runner(tmp_path, monkeypatch):
    """合法 loudness/boost 应透传到 _run_voice_preview，并写出 mp3。"""
    captured = {}
    fake_mp3 = tmp_path / "preview.mp3"
    fake_mp3.write_bytes(b"\x00\x00" + b"\xff\xfb" * 50)  # 假 mp3 帧头

    def fake_run(text, voice, output_mp3, timeout=60, loudness=None, boost=None):
        captured["loudness"] = loudness
        captured["boost"] = boost
        # 把假 mp3 拷到目标路径，模拟 TTS 产物
        import shutil
        shutil.copy(fake_mp3, output_mp3)

    monkeypatch.setattr(srv, "_run_voice_preview", fake_run)
    monkeypatch.setattr(srv, "COMMENTARY_ENABLED", True)
    monkeypatch.setattr(srv, "COMMENTARY_MODE", "local")
    monkeypatch.setattr(srv, "COMMENTARY_DIR", tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "voice_preview.py").write_text("# dummy\n")

    client = TestClient(srv.app)
    resp = client.post("/api/commentary/voice-preview",
                       data={"voice": "zh-CN-XiaoxiaoNeural", "text": "测试",
                             "loudness": "-12", "boost": "1.3"})
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("audio/")
    assert captured.get("loudness") == "-12"
    assert captured.get("boost") == "1.3"


def test_endpoint_pure_tts_no_loudness(tmp_path, monkeypatch):
    """不传 loudness/boost（音色试听场景）应透传 None，保持纯 TTS 行为。"""
    captured = {}

    def fake_run(text, voice, output_mp3, timeout=60, loudness=None, boost=None):
        captured["loudness"] = loudness
        captured["boost"] = boost
        output_mp3.write_bytes(b"\xff\xfb" * 50)

    monkeypatch.setattr(srv, "_run_voice_preview", fake_run)
    monkeypatch.setattr(srv, "COMMENTARY_ENABLED", True)
    monkeypatch.setattr(srv, "COMMENTARY_MODE", "local")
    monkeypatch.setattr(srv, "COMMENTARY_DIR", tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "voice_preview.py").write_text("# dummy\n")

    client = TestClient(srv.app)
    resp = client.post("/api/commentary/voice-preview",
                       data={"voice": "zh-CN-XiaoxiaoNeural", "text": "测试"})
    assert resp.status_code == 200
    assert captured.get("loudness") is None
    assert captured.get("boost") is None
