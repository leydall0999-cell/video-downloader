"""字幕提取「音乐兜底二次识别」回归（2026-09-22/23 两轮用户实测）。

案例1 阿刁(6:18)：VAD 只放行 14s（3.7%）→ 只出 51s-78s 四句；
案例2 天使的翅膀(3:40)：VAD 放行 61%（止于 2:22）→ 尾部副歌整段丢。

触发条件（任一）：a) 语音占比 < 15%；b) 末句结束 < 全长 92% 且尾部有能量（非静音）。
无 VAD 重识别后**按时间段合并**（VAD 段优先，只补空隙）。
过滤以 avg_logprob 为主：≤-1.0 必丢；(nsp≥0.7 且 lp<-0.8) 丢；
音乐真歌词 nsp=0.84/lp=-0.37 必须保留。套话黑名单照丢。

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
    """按调用顺序返回预设结果；记录每次 transcribe 的 (vad_filter, kwargs)。"""

    def __init__(self, passes, duration=100.0):
        self._passes = list(passes)
        self._duration = duration
        self.calls = []

    def transcribe(self, path, **kw):
        self.calls.append((kw.get("vad_filter"), kw))
        return iter(self._passes.pop(0)), _FakeInfo(duration=self._duration, language="zh")


def _setup_model(fake, tail_rms=1.0, ref_rms=1.0):
    sb._SUBTITLE_MODELS.clear()
    sb._get_model = lambda size, threads: fake
    # 尾部调用形如 rms(wav, last_end, total=100/220)；参考调用 rms(wav, start, ≤70)
    sb._wav_region_rms = lambda path, a, b: (tail_rms if b >= 100 else ref_rms)
    server_app.SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)

    def fake_run(cmd, capture_output=False, text=False, timeout=None, **kwargs):
        Path(cmd[-1]).write_bytes(b"fake")
        return types.SimpleNamespace(returncode=0, stderr="")

    sb.subprocess.run = fake_run


def _do_job(job_id, duration=100.0):
    sb.SUBTITLE_JOBS.clear()
    sb.SUBTITLE_JOBS[job_id] = {"stage": "", "progress": 0}
    sb._run_subtitle(job_id, "/fake/src.mp4", "base", "zh", False, fast=True, cpu_threads=4)
    return sb.SUBTITLE_JOBS[job_id]


def run():
    ok = True

    # 1) 低覆盖(7%)触发重识别；合并跳过与 VAD 重叠的段，只补空隙
    #    VAD=[51-58] nvad=[10-30 ✓, 30-60 ✗重叠, 60-90 ✓] → 3 句
    m = _FakeModel([
        [_FakeSeg(51, 58, "清唱一句")],
        [_FakeSeg(10, 30, "第一句"), _FakeSeg(30, 60, "重叠句"), _FakeSeg(60, 90, "第三句")],
    ])
    _setup_model(m)
    job = _do_job("job_music")
    passed = len(m.calls) == 2 and m.calls[1][0] is False and job.get("lines") == 3
    srt = Path(job["srt_file"]).read_text(encoding="utf-8") if job.get("srt_file") else ""
    passed = passed and "清唱一句" in srt and "重叠句" not in srt and "第三句" in srt
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"低覆盖触发重识别+按时间段合并（lines={job.get('lines')}）")

    # 2) 过滤规则：lp 主导。nsp=0.84/lp=-0.37 真歌词必留；lp≤-1.0 丢；(nsp≥0.7且lp<-0.8) 丢；套话丢
    m = _FakeModel([
        [_FakeSeg(200, 210, "清唱一句")],
        [_FakeSeg(10, 30, "真歌词好句"),                                   # nsp/lp 默认 → 留
         _FakeSeg(30, 50, "尾部真歌词", nsp=0.84, lp=-0.37),               # 案例2 实测值 → 留
         _FakeSeg(50, 70, "低置信句", lp=-1.5),                            # lp≤-1.0 → 丢
         _FakeSeg(70, 90, "双重坏句", nsp=0.9, lp=-1.2),                   # → 丢
         _FakeSeg(90, 100, "Thank you for watching this video."),          # 套话 → 丢
         _FakeSeg(100, 110, "请订阅我的频道")],                            # 套话 → 丢
    ], duration=220.0)
    _setup_model(m)
    job = _do_job("job_halluc", duration=220.0)
    passed = job.get("lines") == 3
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"lp主导过滤+高nsp真歌词保留（lines={job.get('lines')}，应=3）")

    # 3) 尾部未覆盖 + 尾部有能量 → 触发重识别并补上尾部
    m = _FakeModel([
        [_FakeSeg(10, 60, "前半句")],                                      # 止于 60/220=27%
        [_FakeSeg(150, 210, "尾部副歌")],
    ], duration=220.0)
    _setup_model(m, tail_rms=1.0, ref_rms=1.0)
    job = _do_job("job_tail_music", duration=220.0)
    passed = len(m.calls) == 2 and job.get("lines") == 2
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"尾部未覆盖+有能量触发重识别（calls={len(m.calls)}, lines={job.get('lines')}）")

    # 4) 尾部未覆盖但尾部是静音（正常语音视频静音收尾）→ 不重识别
    m = _FakeModel([
        [_FakeSeg(10, 60, "前半句")],
        [_FakeSeg(150, 210, "不该出现")],
    ], duration=220.0)
    _setup_model(m, tail_rms=0.01, ref_rms=1.0)   # 尾部静音
    job = _do_job("job_tail_silence", duration=220.0)
    passed = len(m.calls) == 1 and job.get("lines") == 1
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"尾部静音不触发重识别（calls={len(m.calls)}）")

    # 5) 正常语音高覆盖 → 不重识别
    m = _FakeModel([
        [_FakeSeg(5, 60, "对话一"), _FakeSeg(60, 95, "对话二")],
    ], duration=100.0)
    _setup_model(m)
    job = _do_job("job_speech", duration=100.0)
    passed = len(m.calls) == 1 and job.get("lines") == 2
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"正常语音高覆盖不触发（calls={len(m.calls)}, lines={job.get('lines')}）")

    # 5.5) 案例3 爱死了昨天：VAD 放行 0 段（rows 空）→ 仍必须触发兜底（曾因 and rows 直接报错）
    m = _FakeModel([
        [],
        [_FakeSeg(30, 60, "是我爱死了昨天"), _FakeSeg(60, 90, "看你虚伪的表演")],
    ], duration=267.0)
    _setup_model(m)
    job = _do_job("job_vad_empty", duration=267.0)
    passed = len(m.calls) == 2 and job.get("lines") == 2 and job.get("status") == "completed"
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"VAD 零放行仍触发兜底（calls={len(m.calls)}, lines={job.get('lines')}, status={job.get('status')}）")

    # 6) 触发了但合并后没有更多内容 → 保留首轮
    m = _FakeModel([
        [_FakeSeg(10, 60, "唯一内容")],                                    # 60/100=60% 结束但尾部静音？
        [_FakeSeg(20, 40, "完全重叠")],                                    # 全重叠 → 合并无增益
    ], duration=100.0)
    _setup_model(m, tail_rms=1.0, ref_rms=1.0)   # 尾部有能量 → 会触发
    job = _do_job("job_keep_first", duration=100.0)
    srt = Path(job["srt_file"]).read_text(encoding="utf-8") if job.get("srt_file") else ""
    passed = "唯一内容" in srt and "完全重叠" not in srt
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"合并无增益时保留首轮（lines={job.get('lines')}）")

    print("\n通过" if ok else "\n失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
