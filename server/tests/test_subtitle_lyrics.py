"""歌词库直取回归（2026-09-23）：歌曲字幕走「官方歌词」而非「听写」。

背景（实测证据）：
  Whisper 听歌必然翻车——《爱死了昨天》「是我 爱死了昨天 / 誓言 割碎你的脸」被听成
  「是我暗死了昨天 是眼隔碎你的脸」；large-v3 更糟，伴奏段自信幻觉出整段英文
  （"The only thing I know is that I have a lot of people in my heart"）。
  而歌词是现成权威文本且自带逐行时间轴（LRC），命中即秒级产出、逐字准确。
  ⇒ 纯音频文件先查 lrclib.net（开放歌词库，免费无 key），命中直接出字幕，
    未命中 / 无网 / 超时一律静默回落 ASR。

运行（独立进程，HOME 隔离）：
    cd server && .build_venv/bin/python tests/test_subtitle_lyrics.py
"""
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="vdl_sb_lyrics_")
os.environ["HOME"] = _TMP
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from routers import subtitle as sb  # noqa: E402

_FAKE_LRC = "\n".join([
    "[ti:爱死了昨天]",
    "[ar:李慧珍]",
    "[00:12.25] 是我 爱死了昨天",
    "[00:15.54] 誓言 割碎你的脸",
    "[00:18.50] 一切都回不到 那些从前",
    "[00:42.48] 睁开眼 却看不见",
    "",
])


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub_urlopen(handler):
    """打桩 urllib.request.urlopen：handler(url) → 结果 或 raise。"""
    def _open(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        return handler(url)
    sb.urllib = None  # 确保走 import 分支
    import urllib.request as _ur
    _ur.urlopen = _open


def run():
    ok = True

    # 1) 文件名清洗：三个真实样本（ID 段 / 方括号标记 / 空格分隔符）
    cases = {
        "爱死了昨天-李慧珍-254611": "爱死了昨天 李慧珍",
        "[m4a]阿刁-赵雷-16827758": "阿刁 赵雷",
        "安琥 - 天使的翅膀[weiyun]": "安琥 天使的翅膀",
    }
    for raw, want in cases.items():
        got = sb._clean_track_keywords(raw)
        passed = got == want
        ok &= bool(passed)
        print(("✅" if passed else "❌"), f"清洗 {raw!r} → {got!r}（期望 {want!r}）")

    # 2) LRC 解析：元数据行跳过、时间戳正确、end 取下一行起点
    rows = sb._parse_lrc(_FAKE_LRC, duration=267.0)
    passed = len(rows) == 4 and abs(rows[0][0] - 12.25) < 0.01 and rows[0][2] == "是我 爱死了昨天"
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"LRC 解析 {len(rows)} 行（应 4，跳过 [ti:]/[ar:]）: {rows[:2]}")

    # 3) 命中：按 duration 选最贴近的版本（故意混入差 30s 的翻唱）
    payload = [
        {"trackName": "爱死了昨天", "artistName": "翻唱", "duration": 237.0, "syncedLyrics": "[00:01.00] 错的"},
        {"trackName": "爱死了昨天", "artistName": "李慧珍", "duration": 267.0, "syncedLyrics": _FAKE_LRC},
    ]
    _stub_urlopen(lambda url: _Resp(payload))
    got_rows, desc = sb._fetch_synced_lyrics("爱死了昨天 李慧珍", 267.0)
    passed = len(got_rows) == 4 and "李慧珍" in desc
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"按时长选版本：{desc} / {len(got_rows)} 行")

    # 4) 未命中：时长差 30s（超出 6s 容差）→ 不采用翻唱
    payload_far = [{"trackName": "x", "artistName": "y", "duration": 200.0, "syncedLyrics": _FAKE_LRC}]
    _stub_urlopen(lambda url: _Resp(payload_far))
    got_rows2, desc2 = sb._fetch_synced_lyrics("某歌", 267.0)
    passed = got_rows2 == [] and desc2 == ""
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"时长差超限不采用（返回 {len(got_rows2)} 行 / {desc2!r}）")

    # 5) 无网 / 超时：静默回落，绝不抛异常
    def _boom(url):
        raise OSError("network unreachable")
    _stub_urlopen(_boom)
    got_rows3, desc3 = sb._fetch_synced_lyrics("某歌", 267.0)
    passed = got_rows3 == [] and desc3 == ""
    ok &= bool(passed)
    print(("✅" if passed else "❌"), f"无网静默回落（返回 {len(got_rows3)} 行，未抛异常）")

    # 6) 端到端：音频文件命中歌词库 → 不加载 ASR 模型（秒级完成）
    # _run_subtitle 的 finally 会 unlink 临时 wav；沙盒 safe-delete shim 会把删除动作
    # 变成 SystemExit（见 vdl-build-release §12.1）→ 这里把 unlink 打桩成 no-op。
    Path.unlink = lambda self, missing_ok=False: None
    server_app.executor.submit = lambda *a, **k: None
    server_app.SUBTITLE_DIR.mkdir(parents=True, exist_ok=True)
    server_app.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    sb._resolve_safe_local_path = lambda p: Path(p)
    sb._wav_duration = lambda p: 267.0
    loaded = {"model": False}

    def _boom_model(size, threads):
        loaded["model"] = True
        raise AssertionError("命中歌词库时不该加载 ASR 模型")
    sb._get_model = _boom_model
    _stub_urlopen(lambda url: _Resp(payload))

    src = os.path.join(_TMP, "爱死了昨天-李慧珍-254611.mp3")
    Path(src).write_bytes(b"fake")
    # 抽 wav 那步 ffmpeg 打桩（cmd[-1] 是输出 wav）
    sb.subprocess.run = lambda cmd, capture_output, text, timeout: (
        Path(cmd[-1]).write_bytes(b"x" * 100),
        types.SimpleNamespace(returncode=0, stderr=""))[1]

    job_id = "job_lyrics"
    sb.SUBTITLE_JOBS[job_id] = {"status": "running", "stage": "", "progress": 0}
    sb._run_subtitle(job_id, src, "small", "", False, True, 4)
    job = sb.SUBTITLE_JOBS[job_id]
    srt = Path(job.get("srt_file", ""))
    body = srt.read_text(encoding="utf-8") if srt.exists() else ""
    passed = (job.get("status") == "completed" and job.get("source") == "lyrics"
              and not loaded["model"] and "是我 爱死了昨天" in body
              and "誓言 割碎你的脸" in body)
    ok &= bool(passed)
    print(("✅" if passed else "❌"),
          f"端到端歌词直取：status={job.get('status')} source={job.get('source')} "
          f"lines={job.get('lines')} 加载模型={loaded['model']}")

    # 7) 视频文件不查歌词库（视频主体是对话，歌词库会误配）
    loaded2 = {"called": False}
    _stub_urlopen(lambda url: (loaded2.__setitem__("called", True), _Resp(payload))[1])
    src_vid = os.path.join(_TMP, "clip.mp4")
    Path(src_vid).write_bytes(b"fake")
    calls = {"transcribe": 0}

    class _FakeInfo:
        duration = 100.0
        language = "zh"

    class _FakeSeg:
        def __init__(self, s, e, t):
            self.start, self.end, self.text = s, e, t
            self.avg_logprob, self.no_speech_prob = -0.3, 0.1

    class _FakeModel:
        def transcribe(self, path, **kw):
            calls["transcribe"] += 1
            return [_FakeSeg(0, 10, "对话一"), _FakeSeg(10, 20, "对话二")], _FakeInfo()
    sb._get_model = lambda size, threads: _FakeModel()
    job_id2 = "job_video"
    sb.SUBTITLE_JOBS[job_id2] = {"status": "running", "stage": "", "progress": 0}
    sb._run_subtitle(job_id2, src_vid, "small", "", False, True, 4)
    job2 = sb.SUBTITLE_JOBS[job_id2]
    passed = (job2.get("status") == "completed" and job2.get("source") == "asr"
              and not loaded2["called"])
    ok &= bool(passed)
    print(("✅" if passed else "❌"),
          f"视频走 ASR 不查歌词库：source={job2.get('source')} 查库={loaded2['called']}")

    print("\n" + ("✅ 全部通过" if ok else "❌ 存在失败用例"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
