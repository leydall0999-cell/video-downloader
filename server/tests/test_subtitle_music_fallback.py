"""字幕提取「有声段分段 + 解码参数」回归（2026-09-23 重写）。

⚠️ 本文件原名 music_fallback，测的是**已删除**的「音乐兜底二次识别」三层补丁
（占比<15% / 尾部有能量 / 空结果也兜底）。那套补丁是在补偿 Silero VAD 的错配：
Silero 判断「像不像人在说话」，歌曲人声+伴奏会被整段判为非语音（实测阿刁
6:18 只放行 14s = 3.7%，爱死了昨天放行 0 段）。补丁能救回内容，但每来一个
新案例就要再加一条触发条件，属于治标。

现改为**能量分段**（_energy_segments）：只判断「有没有声音」，对歌曲、录音、
嘈杂环境一律成立。本文件因此重写为对新管线的回归：

  1. 全程有能量的「歌曲样」音频 → 覆盖率必须高（不再出现 3.7% 那种崩塌）
  2. 分段结果合法：单调、不重叠、不越界、长段被切开
  3. 尾部静音被正确排除（不是无脑铺满全长）
  4. 解码参数：condition_on_previous_text 恒 False（曾写成 `not fast`，
     非快速档反而开 True，与紧邻注释自相矛盾 → 幻觉滚雪球）
  5. vad_filter 恒 False（Silero 不再参与）
  6. 过滤规则：lp≤-1.0 丢、套话黑名单丢、正常句留
  7. lyrics 开关：关掉时不查歌词库

合成音频用 numpy 直接写 16k mono wav，不依赖 ffmpeg、不联网、不加载模型。

运行（独立进程，HOME 隔离）：
    cd server && .build_venv/bin/python tests/test_subtitle_music_fallback.py
"""
import os
import sys
import tempfile
import types
import wave
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="vdl_sb_energy_")
os.environ["HOME"] = _TMP
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
import routers.subtitle as sb  # noqa: E402

_SR = 16000


def _write_wav(path, total_s, voiced_spans, noise_floor=0.0015):
    """合成 16k mono wav：voiced_spans 内有信号，其余是底噪。"""
    import numpy as np
    n = int(total_s * _SR)
    a = np.zeros(n, dtype=np.float32)
    rng = np.random.default_rng(7)
    for s, e in voiced_spans:
        i0, i1 = int(s * _SR), min(n, int(e * _SR))
        if i1 <= i0:
            continue
        t = (np.arange(i1 - i0)) / float(_SR)
        env = 0.35 + 0.25 * np.sin(2 * np.pi * 0.7 * t)   # 缓慢包络，模拟乐句起伏
        a[i0:i1] = env * (0.55 * np.sin(2 * np.pi * 220 * t)
                          + 0.30 * np.sin(2 * np.pi * 440 * t)
                          + 0.15 * rng.standard_normal(i1 - i0))
    a += rng.standard_normal(n) * noise_floor
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SR)
        w.writeframes((np.clip(a, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return path


class _FakeSeg:
    def __init__(self, start, end, text, nsp=0.01, lp=-0.3):
        self.start, self.end, self.text = start, end, text
        self.no_speech_prob, self.avg_logprob = nsp, lp


class _FakeInfo(types.SimpleNamespace):
    pass


class _FakeModel:
    """每次 transcribe 返回一段预设结果；记录全部 kwargs。"""

    def __init__(self, passes):
        self._passes = list(passes)
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        if self._passes:
            return iter(self._passes.pop(0)), _FakeInfo(language="zh", duration=60.0)
        return iter([]), _FakeInfo(language="zh", duration=60.0)


def _install(src_wav, model):
    """把 _run_subtitle 的外部依赖换成可控假件。"""
    sb._SUBTITLE_MODELS.clear()
    sb._get_model = lambda size, threads: model

    def fake_run(cmd, capture_output=False, text=False, timeout=None, **kwargs):
        Path(cmd[-1]).write_bytes(Path(src_wav).read_bytes())
        return types.SimpleNamespace(returncode=0, stderr="")

    sb.subprocess.run = fake_run
    server_app.SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)


def _do_job(job_id, src="/fake/src.mp4", language="zh", **kw):
    sb.SUBTITLE_JOBS.clear()
    sb.SUBTITLE_JOBS[job_id] = {"stage": "", "progress": 0}
    args = dict(fast=True, cpu_threads=4, use_lyrics=False)
    args.update(kw)
    sb._run_subtitle(job_id, src, "base", language, False, **args)
    return sb.SUBTITLE_JOBS[job_id]


def run():
    ok = True
    work = Path(_TMP)

    # ── 1) 歌曲样音频（全程有能量）不该被判成静音：对比 Silero 的 3.7% ──────────
    # 歌曲样：两段长乐句 + 中间 3s 间奏静音（真实歌曲有前奏/间奏，不是全片满能量）
    p = _write_wav(work / "song.wav", 120.0, [(0.0, 40.0), (43.0, 80.0), (83.0, 118.0)])
    segs = sb._energy_segments(str(p))
    cov = sum(e - s for s, e in segs) / 120.0
    passed = cov >= 0.85
    ok &= bool(passed)
    print(("✅" if passed else "❌"),
          f"全程有能量的歌曲样音频覆盖率={cov:.0%}（Silero 同场景仅 3.7%）")

    # ── 2) 分段合法性 + 长段被切开 ────────────────────────────────────────────
    passed = bool(segs) and all(s < e for s, e in segs)
    passed = passed and all(segs[i][1] <= segs[i + 1][0] + 1e-6 for i in range(len(segs) - 1))
    passed = passed and all(0.0 <= s and e <= 120.0 + 0.01 for s, e in segs)
    passed = passed and all(e - s <= 28.0 * 1.45 + 1e-6 for s, e in segs)
    ok &= bool(passed)
    print(("✅" if passed else "❌"),
          f"分段合法且长段已切开（{len(segs)} 段，最长 {max(e-s for s,e in segs):.1f}s）")

    # ── 3) 尾部静音应被排除，不是无脑铺满全长 ──────────────────────────────────
    p2 = _write_wav(work / "tail_silence.wav", 100.0, [(0.0, 60.0)])
    segs2 = sb._energy_segments(str(p2))
    passed = bool(segs2) and segs2[-1][1] < 75.0
    ok &= bool(passed)
    print(("✅" if passed else "❌"),
          f"尾部静音被排除（末段止于 {segs2[-1][1]:.1f}s / 全长 100s）")

    # ── 4) 解码参数：condition_on_previous_text 恒 False、vad_filter 恒 False ──
    p3 = _write_wav(work / "speech.wav", 40.0, [(1.0, 12.0), (14.0, 30.0)])
    for fast in (True, False):
        m = _FakeModel([[_FakeSeg(0.0, 5.0, "第一句"), _FakeSeg(5.0, 11.0, "第二句")],
                        [_FakeSeg(0.0, 6.0, "第三句")]])
        _install(str(p3), m)
        _do_job("job_params_%s" % fast, fast=fast)
        passed = bool(m.calls) and all(c.get("condition_on_previous_text") is False for c in m.calls)
        passed = passed and all(c.get("vad_filter") is False for c in m.calls)
        ok &= bool(passed)
        print(("✅" if passed else "❌"),
              f"fast={fast}：condition_on_previous_text/vad_filter 恒 False（{len(m.calls)} 次调用）")

    # ── 5) 过滤规则：lp≤-1.0 丢、套话丢、正常留 ────────────────────────────────
    m = _FakeModel([[_FakeSeg(0.0, 4.0, "正常一句", lp=-0.4),
                     _FakeSeg(4.0, 8.0, "尾部真歌词", nsp=0.84, lp=-0.37),   # 高 nsp 也留
                     _FakeSeg(8.0, 12.0, "低置信句", lp=-1.5),               # lp 太差 → 丢
                     _FakeSeg(12.0, 16.0, "Thank you for watching"),         # 套话 → 丢
                     _FakeSeg(16.0, 20.0, "请不吝点赞 订阅 转发")]])         # 套话 → 丢
    _install(str(p3), m)
    job = _do_job("job_filter")
    passed = job.get("lines") == 2
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"过滤规则（lines={job.get('lines')}，应=2）")

    # ── 6) 时间戳要叠加段起点偏移（不能都从 0 开始） ────────────────────────────
    p4 = _write_wav(work / "two_spans.wav", 60.0, [(2.0, 20.0), (30.0, 50.0)])
    m = _FakeModel([[_FakeSeg(1.0, 5.0, "甲句")], [_FakeSeg(1.0, 5.0, "乙句")]])
    _install(str(p4), m)
    job = _do_job("job_offset")
    srt = Path(job["srt_file"]).read_text(encoding="utf-8") if job.get("srt_file") else ""
    # 第二段起点 ≈ 30+1=31s；若漏了段偏移会退化成 00:00:01
    passed = ("00:00:31" in srt or "00:00:32" in srt or "00:00:30" in srt)
    ok &= bool(passed)
    print(("✅" if passed else "❌"), "时间戳叠加段起点偏移（第二段不从 0 开始）")

    # ── 7) lyrics 开关：关掉时不查歌词库 ──────────────────────────────────────
    called = {"n": 0}
    real = sb._fetch_synced_lyrics

    def spy(kw, dur):
        called["n"] += 1
        return [], ""

    try:
        m = _FakeModel([[_FakeSeg(0.0, 5.0, "听写出来的")]])
        _install(str(p4), m)
        sb._fetch_synced_lyrics = spy
        _do_job("job_nolyrics", src="/fake/song.m4a", use_lyrics=False)
        n_off = called["n"]

        m2 = _FakeModel([[_FakeSeg(0.0, 5.0, "听写出来的")]])
        _install(str(p4), m2)
        sb._fetch_synced_lyrics = spy
        _do_job("job_lyrics", src="/fake/song.m4a", use_lyrics=True)
        n_on = called["n"]
    finally:
        sb._fetch_synced_lyrics = real
    passed = (n_off == 0) and (n_on >= 1)
    ok &= bool(passed)
    print(("✅" if passed else "❌"),
          f"歌词库开关生效（关时查 {n_off} 次 / 开时累计 {n_on} 次）")

    # ── 8) 繁体输出统一转简（噪声/伴奏下会整句出繁体，用户视为错字） ─────────────
    m = _FakeModel([[_FakeSeg(0.0, 5.0, "今天我們要討論的是人工智能在語音識別領域")]])
    _install(str(p3), m)
    job = _do_job("job_trad")
    srt = Path(job["srt_file"]).read_text(encoding="utf-8") if job.get("srt_file") else ""
    passed = ("今天我们要讨论的是人工智能在语音识别领域" in srt) and ("我們" not in srt)
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"繁体输出转简体（{srt.splitlines()[2][:24] if len(srt.splitlines())>2 else '?'}…）")

    # ── 9) 繁体套话也要被拦（转简后正则命中；未转简时正则只写简体 → 漏网） ──────
    m = _FakeModel([[_FakeSeg(0.0, 4.0, "正常歌词一句"),
                     _FakeSeg(4.0, 8.0, "請不吝點贊訂閱轉發打賞支持明鏡與點點欄目"),
                     _FakeSeg(8.0, 12.0, "优优独播剧场——YoYo Television Series Exclusive")]])
    _install(str(p3), m)
    job = _do_job("job_trad_hallu")
    passed = job.get("lines") == 1
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"繁体/尾缀套话被拦（lines={job.get('lines')}，应=1）")

    # ── 10) 中文主体下的纯拉丁行判为伴奏幻觉（实测《阿刁》间奏输出 Zither Harp） ─
    m = _FakeModel([[_FakeSeg(0.0, 4.0, "阿刁 住在西藏某个地方"),
                     _FakeSeg(20.0, 24.0, "Zither Harp"),
                     _FakeSeg(30.0, 34.0, "灰色帽檐下 凹陷的脸颊")]])
    _install(str(p3), m)
    job = _do_job("job_latin")
    srt = Path(job["srt_file"]).read_text(encoding="utf-8") if job.get("srt_file") else ""
    passed = ("Zither" not in srt) and (job.get("lines") == 2)
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"中文主体下纯拉丁行被丢弃（lines={job.get('lines')}，应=2）")

    # ── 11) 英文内容不该被误杀（强制 language=en 时保留纯拉丁行） ────────────────
    m = _FakeModel([[_FakeSeg(0.0, 4.0, "The quarterly revenue grew by eighteen percent"),
                     _FakeSeg(4.0, 8.0, "driven by strong demand in cloud")]])
    _install(str(p3), m)
    job = _do_job("job_en_keep", language="en")
    passed = job.get("lines") == 2
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"强制 en 时英文全部保留（lines={job.get('lines')}，应=2）")

    # ── 12) zhconv 缺失时不能崩、不能吞字（打包漏收会走到这条路径） ──────────────
    real_conv = sb._zhconv_convert
    try:
        sb._zhconv_convert = None
        m = _FakeModel([[_FakeSeg(0.0, 4.0, "繁體一句測試"), _FakeSeg(4.0, 8.0, "正常一句")]])
        _install(str(p3), m)
        job = _do_job("job_no_zhconv")
        srt = Path(job["srt_file"]).read_text(encoding="utf-8") if job.get("srt_file") else ""
        passed = (job.get("status") == "completed" and job.get("lines") == 2
                  and "繁體一句測試" in srt and "正常一句" in srt)
    finally:
        sb._zhconv_convert = real_conv
    ok &= bool(passed)
    print(("✅" if passed else "❌"), "zhconv 缺失时降级不崩、原文保留（lines=%s）" % job.get("lines"))

    # ── 13) SenseVoice 缺失时必须静默降级（不崩、不吞字） ──────────────────────
    #   打包漏收 sherpa_onnx / 模型缺失都会走到这条路径。历史上 zhconv 就是这样
    #   「代码对了但包里没生效」，所以每条静默降级分支都要有回归。
    real_rec = sb._sensevoice_recognizer
    try:
        sb._sensevoice_recognizer = lambda: None
        m = _FakeModel([[_FakeSeg(0.0, 4.0, "降级后第一句"), _FakeSeg(4.0, 8.0, "降级后第二句")]])
        _install(str(p3), m)
        job = _do_job("job_no_sv")
        srt = Path(job["srt_file"]).read_text(encoding="utf-8") if job.get("srt_file") else ""
        passed = (job.get("status") == "completed" and job.get("lines") == 2
                  and "降级后第一句" in srt and "降级后第二句" in srt)
    finally:
        sb._sensevoice_recognizer = real_rec
    ok &= bool(passed)
    print(("✅" if passed else "❌"), "SenseVoice 缺失时降级不崩、原文保留（lines=%s）" % job.get("lines"))

    # ── 14) 段长必须 ≤20s（SenseVoice 超过 ~20s 会严重丢内容） ──────────────────
    #   实测：30s 段 SenseVoice 只吐一句，提示就变成了错的引导。
    long_wav = work / "long.wav"
    _write_wav(long_wav, 300.0, [(0.0, 298.0)])
    segs = sb._energy_segments(str(long_wav))
    mx = max((e - s for s, e in segs), default=0.0)
    cov = sum(e - s for s, e in segs) / 300.0 * 100
    passed = bool(segs) and mx <= 20.5 and cov >= 85
    ok &= passed
    print(("✅" if passed else "❌"), f"分段严格 ≤20s 且覆盖充分（段数={len(segs)} 最长={mx:.1f}s 覆盖={cov:.0f}%）")

    print("\n通过" if ok else "\n失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
