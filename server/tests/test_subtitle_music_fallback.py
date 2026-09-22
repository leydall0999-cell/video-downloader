"""字幕提取「音乐兜底二次识别」回归（2026-09-22 用户报「阿刁 6:18 的歌只识别出 51s→78s 四句」）。

根因：Silero VAD 是「说话声」检测器，歌曲的「人声+伴奏」被整段判为非语音——
实测该歌 378s 里 VAD 只放行 14s（占比 3.7%），副歌全丢。
对策：VAD 语音占比 < 15% 时关 VAD 整曲重识别（no_speech_prob/avg_logprob 滤幻觉），
且仅当第二轮语音总量 > 第一轮时才采用。

运行（独立进程，HOME 隔离）：
    cd server && .build_venv/bin/python tests/test_subtitle_music_fallback.py
"""
import os
import sys
import tempfile
import types
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="vdl_sb_music_")
os.environ["HOME"] = _TMP
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
import routers.subtitle as sb  # noqa: E402


class _FakeSeg:
    def __init__(self, start, end, text, nsp=0.01, lp=-0.3):
        self.start, self.end, self.text = start, end, text
        self.no_speech_prob, self.avg_logprob = nsp, lp


class _FakeInfo(types.SimpleNamespace):
    pass


class _FakeModel:
    """按调用顺序返回预设结果；记录每次 transcribe 的 vad_filter 取值。"""

    def __init__(self, passes):
        self._passes = list(passes)   # 每个 pass: list[_FakeSeg]
        self.calls = []               # [(vad_filter, kwargs)]

    def transcribe(self, path, **kw):
        self.calls.append((kw.get("vad_filter"), kw))
        return iter(self._passes.pop(0)), _FakeInfo(duration=100.0, language="zh")


def _setup_model(fake, ffmpeg_fail=False):
    sb._SUBTITLE_MODELS.clear()
    sb._get_model = lambda size, threads: fake
    server_app.SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)

    def fake_run(cmd, capture_output=False, text=False, timeout=None, **kwargs):
        # 模拟 ffmpeg：创建输出 wav（cmd[-1]）
        Path(cmd[-1]).write_bytes(b"fake")
        return types.SimpleNamespace(returncode=0, stderr="")

    sb.subprocess.run = fake_run


def _do_job(job_id):
    sb.SUBTITLE_JOBS.clear()
    sb.SUBTITLE_JOBS[job_id] = {"stage": "", "progress": 0}
    sb._run_subtitle(job_id, "/fake/src.mp4", "base", "zh", False, fast=True, cpu_threads=4)
    return sb.SUBTITLE_JOBS[job_id]


def run():
    ok = True

    # 1) 音乐场景：VAD 只放行 7% → 触发二次识别，采用更全的第二轮
    m = _FakeModel([
        [_FakeSeg(51, 58, "清唱一句")],                                   # 第一轮 VAD：7s/100s = 7%
        [_FakeSeg(10, 30, "第一句"), _FakeSeg(30, 60, "第二句"),
         _FakeSeg(60, 90, "第三句")],                                     # 第二轮无 VAD：80s
    ])
    _setup_model(m)
    job = _do_job("job_music")
    two_calls = len(m.calls) == 2 and m.calls[0][0] is True and m.calls[1][0] is False
    passed = two_calls and job.get("lines") == 3
    ok &= bool(passed)
    print(("✅" if passed else "❌"),
          f"音乐低覆盖(7%)触发无VAD重识别并采用（lines={job.get('lines')}, calls={len(m.calls)}）",
          "" if passed else job)

    # 2) 幻觉过滤：第二轮里 no_speech_prob≥0.6 / avg_logprob≤-1.0 的段被丢弃
    m = _FakeModel([
        [_FakeSeg(51, 58, "清唱一句")],
        [_FakeSeg(10, 30, "好句"), _FakeSeg(30, 50, "幻觉句", nsp=0.8),
         _FakeSeg(50, 70, "低置信句", lp=-1.5)],
    ])
    _setup_model(m)
    job = _do_job("job_halluc")
    passed = job.get("lines") == 1
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"无VAD轮幻觉段被过滤（lines={job.get('lines')}，应=1）")

    # 3) 二轮更差时不采用：保留第一轮结果
    m = _FakeModel([
        [_FakeSeg(5, 60, "正常对话一句"), _FakeSeg(60, 90, "正常对话两句")],  # 85s/100s 高覆盖
        [_FakeSeg(10, 20, "更差")],                                        # 不会触发（覆盖高）
    ])
    _setup_model(m)
    job = _do_job("job_speech")
    passed = len(m.calls) == 1 and job.get("lines") == 2   # 高覆盖不重识别
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"正常语音高覆盖不触发重识别（calls={len(m.calls)}, lines={job.get('lines')}）")

    # 4) 低覆盖但第二轮更差 → 保留第一轮
    m = _FakeModel([
        [_FakeSeg(51, 58, "清唱一句")],                                   # 7%
        [_FakeSeg(52, 56, "更短")],                                       # 4s < 7s
    ])
    _setup_model(m)
    job = _do_job("job_keep_first")
    passed = job.get("lines") == 1 and job.get("srt_file")
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"低覆盖但二轮更差时保留首轮（lines={job.get('lines')}）")
    if job.get("srt_file"):
        srt = Path(job["srt_file"]).read_text(encoding="utf-8")
        passed = "清唱一句" in srt
        ok &= bool(passed)
        print(("✅" if passed else "❌"), "保留的 SRT 内容来自第一轮")

    print("\n通过" if ok else "\n失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
