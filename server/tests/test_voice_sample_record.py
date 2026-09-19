"""「我的音色」页面内录制落盘回归测试（2026-09-19 新增）。

背景：2026-09-19 用户问「录音能在这里直接录吗」→ 新增「WebAudio 采 PCM → 前端编 16bit WAV
      → POST /api/commentary/voice-sample/record → 落盘」这条链路。
      `save_recorded_voice_sample` 是它唯一的落盘点，两头的错都很贵：
        · 校验太松 → 存下「用不了的样本」（时长过短/格式不支持的），渲染时才炸；
        · 校验太紧 → 正常录音被拒，用户以为功能坏了（还配不上「已配置」）。
      另外它是**唯一**会主动删文件的函数（只保留最近 N 段），写错就会误删用户放在
      配置目录里的东西 —— 必须钉住「只动自己那一个子目录」。

覆盖：
    _wav_duration                —— WAV 头解析（合法 / 非 WAV / 头被截断）
    save_recorded_voice_sample   —— 正常落盘（权限 0600 + 目录 0700）/ 空数据 /
                                   格式白名单 / 太短 / 过长 / 过大 / 非 WAV 用前端报的时长
    _prune_voice_recordings      —— 只保留最近 N 段，且不碰目录外的文件

运行：
    cd server && python tests/test_voice_sample_record.py
    cd server && python -m pytest tests/test_voice_sample_record.py -v
⚠️ 全部用例都在 fake_home() 下运行（llm_config._config_dir 跟随 Path.home），
   不会碰真实 ~/.video-downloader。
"""
import contextlib
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import commentary_config as C  # noqa: E402


@contextlib.contextmanager
def fake_home():
    """把 pathlib.Path.home 临时指向临时目录（llm_config._config_dir 跟随它）。"""
    import pathlib
    fake = Path(tempfile.mkdtemp(prefix="vdl_voice_rec_")).resolve()
    orig = pathlib.Path.home
    pathlib.Path.home = staticmethod(lambda: fake)
    try:
        yield fake
    finally:
        pathlib.Path.home = orig
        shutil.rmtree(fake, ignore_errors=True)


def _wav(seconds: float, rate: int = 24000) -> bytes:
    """造一段纯静音 WAV（本用例只关心头与时长，不关心波形内容）。"""
    data = b"\x00\x00" * int(seconds * rate)
    return (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data" + struct.pack("<I", len(data)) + data
    )


def _expect_value_error(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"应被拒绝但放行了: {fn.__name__}{args}")


def test_wav_duration_parses_header():
    assert abs(C._wav_duration(_wav(3.0)) - 3.0) < 0.05
    assert abs(C._wav_duration(_wav(0.5)) - 0.5) < 0.05
    # 非 WAV（MP3 帧头 / 全零）要认不出来，而不是瞎猜出个数字
    assert C._wav_duration(b"\xff\xfb\x90\x64" + b"\x00" * 100) is None
    assert C._wav_duration(b"") is None
    # 头被截断（只有 RIFF/WAVE 12 字节）
    assert C._wav_duration(b"RIFF\x00\x00\x00\x00WAVE") is None
    print("✅ WAV 头时长解析：合法 ≈实际、非 WAV / 截断头一律 None")


def test_save_recorded_ok():
    with fake_home() as home:
        blob = _wav(3.0)
        out = C.save_recorded_voice_sample(blob, "voice_rec.wav", 3.0)
        p = Path(out["audio_path"])
        assert p.is_file(), "落盘文件应存在"
        assert p.read_bytes() == blob, "落盘的必须是原字节（不转码）"
        assert abs(out["duration"] - 3.0) < 0.05, f"时长应≈3.0，实际 {out['duration']}"
        assert out["bytes"] == len(blob)
        # 隐私：样本是用户声音，目录 0700 + 文件 0600
        assert (p.stat().st_mode & 0o777) == 0o600, f"文件权限应为 0600，实际 {oct(p.stat().st_mode & 0o777)}"
        assert (p.parent.stat().st_mode & 0o777) == 0o700, "目录权限应为 0700"
        # 必须落在 fake home 里（证明没写真实家目录）
        assert str(p).startswith(str(home)), f"样本落点不在隔离家目录内：{p}"
        assert p.parent.name == "voice_samples"
    print("✅ 正常录音落盘：原字节保存、时长来自 WAV 头、文件 0600 / 目录 0700")


def test_reject_empty_and_oversize():
    with fake_home():
        msg = _expect_value_error(C.save_recorded_voice_sample, b"", "voice_rec.wav", 3.0)
        assert "没有收到录音数据" in msg, msg

        orig = C.VOICE_REC_MAX_BYTES
        C.VOICE_REC_MAX_BYTES = 64          # 临时收紧到 64 字节，省得造 32MB
        try:
            msg = _expect_value_error(C.save_recorded_voice_sample, _wav(1.0), "voice_rec.wav", 1.0)
            assert "过大" in msg, msg
        finally:
            C.VOICE_REC_MAX_BYTES = orig
    print("✅ 空数据 / 超限体积都被拒（拒绝理由可直接给用户看）")


def test_reject_bad_ext_and_bad_duration():
    with fake_home():
        msg = _expect_value_error(C.save_recorded_voice_sample, _wav(3.0), "voice_rec.txt", 3.0)
        assert "格式不支持" in msg, msg
        # 太短：WAV 头实测 0.3 秒
        msg = _expect_value_error(C.save_recorded_voice_sample, _wav(0.3), "voice_rec.wav", None)
        assert "太短" in msg, msg
        # 过长：WAV 头实测 90 秒（前端 30 秒自动收，这里防绕过）
        msg = _expect_value_error(C.save_recorded_voice_sample, _wav(90.0), "voice_rec.wav", None)
        assert "过长" in msg, msg
    print("✅ 格式白名单 + 时长上下限（1~60 秒）都生效")


def test_duration_fallback_for_non_wav():
    """非 WAV（如将来换 MediaRecorder 给的 m4a）：头里读不到时长 → 用前端报的值。"""
    with fake_home():
        blob = b"\x00\x00\x00\x20ftypM4A " + b"\x11" * 2000     # 假装 m4a
        out = C.save_recorded_voice_sample(blob, "voice_rec.m4a", 5.0)
        assert abs(out["duration"] - 5.0) < 0.01, out
        # 前端报太短同样拒（拿不到实测值时仍要守住下界）
        msg = _expect_value_error(C.save_recorded_voice_sample, blob, "voice_rec.m4a", 0.2)
        assert "太短" in msg, msg
        # 两个来源都没有 → 只做体积/格式校验，放行（前端已保证 1~30 秒）
        out2 = C.save_recorded_voice_sample(blob, "voice_rec.m4a", None)
        assert out2["duration"] is None
    print("✅ 非 WAV 容器：时长回退用前端报值，缺失时放行但保留体积/格式校验")


def test_prune_keeps_recent_only():
    with fake_home():
        paths = []
        for i in range(7):
            out = C.save_recorded_voice_sample(_wav(2.0), f"voice_rec.wav", 2.0)
            p = Path(out["audio_path"])
            os.utime(p, (1_700_000_000 + i * 10, 1_700_000_000 + i * 10))   # 明确先后顺序
            paths.append(p)
        files = sorted(C._voice_rec_dir().glob("*"))
        assert len(files) == C.VOICE_REC_KEEP, f"应只留 {C.VOICE_REC_KEEP} 段，实际 {len(files)}"
        assert paths[-1].is_file(), "最新一段必须留着（它是刚存进配置的那个）"
        assert not paths[0].exists(), "最旧的应被清理"
    print(f"✅ 录制文件只保留最近 {C.VOICE_REC_KEEP} 段，最新一段保留")


def test_prune_does_not_touch_outside_dir():
    with fake_home():
        cfg = C._config_dir()
        cfg.mkdir(parents=True, exist_ok=True)
        keep = cfg / "voice_sample.json"          # 配置本体：绝不能被清理逻辑误删
        keep.write_text("{}", encoding="utf-8")
        other = cfg / "commentary_config.json"
        other.write_text("{}", encoding="utf-8")
        for _ in range(7):
            C.save_recorded_voice_sample(_wav(2.0), "voice_rec.wav", 2.0)
        assert keep.is_file(), "配置目录里的非录制文件不得被清理"
        assert other.is_file(), "配置目录里的非录制文件不得被清理"
    print("✅ 清理只在自己那一个子目录内，配置本体不受影响")


if __name__ == "__main__":
    test_wav_duration_parses_header()
    test_save_recorded_ok()
    test_reject_empty_and_oversize()
    test_reject_bad_ext_and_bad_duration()
    test_duration_fallback_for_non_wav()
    test_prune_keeps_recent_only()
    test_prune_does_not_touch_outside_dir()
    print("\n🎉 我的音色·页面内录制落盘测试全部通过（7 项）")
