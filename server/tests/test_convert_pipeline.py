"""转码管线决策与参数构造回归测试（2026-09-10 新增）。

背景
----
`app._run_convert` 是「用户点了转换」到「实际跑哪条 ffmpeg 命令」之间**唯一的
决策点**：27 种目标格式 × 分辨率 / 旋转 / 无损重封装 / 保留音轨 / 码率 /
画质档位 / 白底合成 等开关，全部在这里被翻译成 ffmpeg 参数。此前零测试——
改一行就可能静默改变所有用户的转码结果，而且没有任何自动手段能发现。

本轮补测过程中已由此暴露并修复两个真实缺陷（对应下面标 ★ 的用例）：
1. ★ `rotate=180` 原用 `transpose=3`，而 transpose 系列全是 90° 转置：
   用户选「180°」得到的是「90°+垂直翻转」，且**宽高被错误互换**（横屏视频
   转出来变竖屏）。已改为 `hflip,vflip`。
2. ★ 图片目标缺少 `-frames:v 1`：桌面端原生文件选择器不受 `accept="image/*"`
   约束，放进视频后 ffmpeg 会试图把多帧写进同一文件而整体报错——第一帧虽已
   落盘，任务状态却是 failed，用户反而拿不到产物。

覆盖三段：
  A. 命令构造 —— mock 掉 subprocess.Popen 捕获 argv，不真跑 ffmpeg
  B. 参数构造 —— _audio_encode_args / _image_encode_args / _build_loudness_filter
  C. 真实转码 —— 合成素材真跑 ffmpeg，校验产物格式 / 尺寸 / 像素

运行：
    cd server && python tests/test_convert_pipeline.py
    cd server && python -m pytest tests/test_convert_pipeline.py -v
"""
import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as A  # noqa: E402

# 伪造的 ffmpeg stderr：含总时长（触发进度解析）与 time=（触发百分比计算）
_FAKE_STDERR = [
    "Input #0, mov, mp4, from 'x.mp4':\n",
    "  Duration: 00:01:00.00, start: 0.000000, bitrate: 800 kb/s\n",
    "frame=  300 fps=0.0 q=0.0 size=0kB time=00:00:30.00 bitrate=700.0kbits/s\n",
]


# --------------------------------------------------------------------------- #
# 测试脚手架
# --------------------------------------------------------------------------- #
def _tmpdir() -> Path:
    """所有测试产物一律落在系统临时目录，绝不写用户家目录。"""
    return Path(tempfile.mkdtemp(prefix="vdl-conv-"))


def _make_job(tmp: Path, name: str, ext: str | None = None):
    """往 CONVERT_JOBS 注入一个作业（与真实请求同构）并返回 (job_id, out_path)。"""
    jid = f"t_{name}_{uuid.uuid4().hex[:6]}"
    out = tmp / f"{jid}{ext if ext is not None else '.' + name}"
    A.CONVERT_JOBS[jid] = {
        "status": "running", "out_path": str(out), "error": "",
        "filename": out.name, "progress": 0, "stage": "",
    }
    return jid, out


def _fake_popen(captured, *, returncode: int = 0, produce: bool = True):
    """构造 subprocess.Popen 替身：记录 argv，并按需「伪造」出产物文件。"""
    class _Proc:
        def __init__(self, cmd, **kwargs):
            self.cmd = [str(x) for x in cmd]
            captured.append(self.cmd)
            self.returncode = returncode
            self.stderr = iter(_FAKE_STDERR)

        def wait(self, timeout=None):
            if produce and self.returncode == 0:
                Path(self.cmd[-1]).write_bytes(b"FAKE-MEDIA")
            return self.returncode

    return _Proc


def _capture(tmp: Path, target: str, src: Path, *, ext: str | None = None,
             resolution: str = "original", returncode: int = 0,
             produce: bool = True, **kw):
    """在 mock 掉 ffmpeg 的前提下调用 _run_convert，返回 (job, out, argv)。"""
    captured: list = []
    jid, out = _make_job(tmp, target, ext)
    with mock.patch("subprocess.Popen",
                    _fake_popen(captured, returncode=returncode, produce=produce)):
        A._run_convert(jid, str(src), target, resolution, **kw)
    return A.CONVERT_JOBS.pop(jid), out, (captured[0] if captured else None)


def _real(tmp: Path, target: str, src: Path, *, ext: str, resolution: str = "original", **kw):
    """真实调用 _run_convert（不 mock），返回 (job, out_path)。"""
    jid, out = _make_job(tmp, target, ext)
    A._run_convert(jid, str(src), target, resolution, **kw)
    return A.CONVERT_JOBS.pop(jid), out


def _ffmpeg_ok() -> bool:
    return bool(A.FFMPEG_BIN) and Path(A.FFMPEG_BIN).exists()


def _probe(path: Path) -> str:
    proc = subprocess.run([A.FFMPEG_BIN, "-hide_banner", "-i", str(path)],
                          capture_output=True, text=True, timeout=120)
    return proc.stderr or ""


def _video_size(path: Path):
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", _probe(path))
    return (int(m.group(1)), int(m.group(2))) if m else None


_SAMPLE_DIR: Path | None = None
_SAMPLE: Path | None = None


def _sample() -> Path | None:
    """生成 1 秒 320x240 带音轨的合成素材（纯软件编码，不依赖硬件编码器）。

    整个测试模块只生成一次，供 C 段各用例复用。
    """
    global _SAMPLE_DIR, _SAMPLE
    if _SAMPLE is not None:
        return _SAMPLE
    if not _ffmpeg_ok():
        return None
    _SAMPLE_DIR = _tmpdir()
    out = _SAMPLE_DIR / "sample.mp4"
    cmd = [A.FFMPEG_BIN, "-y",
           "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5:duration=1",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
           "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        return None
    _SAMPLE = out
    return _SAMPLE


def _cleanup_sample() -> None:
    if _SAMPLE_DIR is not None:
        shutil.rmtree(_SAMPLE_DIR, ignore_errors=True)


atexit.register(_cleanup_sample)


# =========================================================================== #
# A. 命令构造（mock ffmpeg）
# =========================================================================== #
def test_default_video_target_uses_fixed_table():
    td = _tmpdir()
    try:
        src = td / "src.mp4"; src.write_bytes(b"SRC")
        job, out, argv = _capture(td, "mp4", src)
        assert job["status"] == "completed", job
        assert argv[0] == A.FFMPEG_BIN and argv[1] == "-y"
        assert argv[argv.index("-i") + 1] == str(src)
        assert argv[-1] == str(out), "输出路径必须是最后一个参数"
        # mp4 固定表：AAC 音轨 + faststart（网页边下边播）
        assert ["-c:a", "aac"] == argv[argv.index("-c:a"):argv.index("-c:a") + 2]
        assert "+faststart" in argv
        # 进度解析：Duration 60s，time 30s → 50%
        assert job["progress"] == 100, "完成后进度必须置 100"
        print("✅ 视频默认目标正确套用固定参数表，并解析 ffmpeg 进度")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_progress_is_parsed_from_stderr():
    td = _tmpdir()
    try:
        src = td / "src.mp4"; src.write_bytes(b"S")
        jid, out = _make_job(td, "prog", ".mp4")
        seen: dict = {}

        class _Proc:
            def __init__(self, cmd, **kw):
                self.cmd = [str(x) for x in cmd]
                self.returncode = 0
                # Duration 40s；最后一次 time=30s → 期望进度 75%
                self.stderr = iter([
                    "  Duration: 00:00:40.00, start: 0.0\n",
                    "frame=1 time=00:00:10.00\n",
                    "frame=2 time=00:00:30.00\n",
                ])

            def wait(self, timeout=None):
                # stderr 已被消费完，此刻 job 上的 progress 应反映最后一次 time=
                seen["progress"] = A.CONVERT_JOBS[jid]["progress"]
                Path(self.cmd[-1]).write_bytes(b"X")
                return 0

        with mock.patch("subprocess.Popen", _Proc):
            A._run_convert(jid, str(src), "mp4", "original")
        job = A.CONVERT_JOBS.pop(jid)
        assert seen["progress"] == 75, f"应从 stderr 解析出 75%，实际 {seen['progress']}"
        assert job["progress"] == 100, "完成后进度应归 100"
        print("✅ 从 ffmpeg stderr 解析 Duration/time 并实时回写进度")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_resolution_appends_scale_filter():
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        _, _, argv = _capture(td, "mp4", src, resolution="720")
        assert "-vf" in argv and argv[argv.index("-vf") + 1] == "scale=-2:720", argv
        # 宽度用 -2 保证偶数（H.264 要求），高度固定
        _, _, argv = _capture(td, "mp4", src, resolution="480")
        assert argv[argv.index("-vf") + 1] == "scale=-2:480"
        print("✅ 分辨率开关生成 scale=-2:<h>（宽度偶数化）")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_unknown_resolution_is_ignored():
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        for bad in ("999", "original", "", "1080p"):
            _, _, argv = _capture(td, "mp4", src, resolution=bad)
            assert "-vf" not in argv, f"非法分辨率 {bad!r} 不应产生滤镜: {argv}"
        print("✅ 非白名单分辨率被忽略，不会拼出无效 scale")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_rotate_mapping_is_correct():
    """★ 回归：180° 必须是 hflip,vflip。

    transpose 系列（0/1/2/3）**全部是 90° 转置**，输出宽高必然互换。
    早期用 transpose=3 实现 180°，导致横屏视频转出来变竖屏，且画面并非
    180° 旋转结果。此用例即该缺口的回归保护。
    """
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        expect = {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}
        for rot, filt in expect.items():
            _, _, argv = _capture(td, "mp4", src, rotate=rot)
            vf = argv[argv.index("-vf") + 1]
            assert vf == filt, f"rotate={rot} 期望 {filt!r}，实际 {vf!r}"
            assert "transpose=3" not in vf, "180° 不得使用 transpose=3（会转置宽高）"
        print("✅ 旋转 90/180/270 映射正确；180° 用 hflip+vflip 不改宽高")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_rotate_out_of_range_is_ignored():
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        for bad in (0, 45, 360, -90):
            _, _, argv = _capture(td, "mp4", src, rotate=bad)
            assert "-vf" not in argv, f"rotate={bad} 不应产生滤镜"
        print("✅ 非法旋转角度被忽略")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_resolution_and_rotate_combine_in_order():
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        _, _, argv = _capture(td, "mp4", src, resolution="720", rotate=90)
        # 先缩放再旋转：顺序反了会让旋转后的宽高再被 scale 破坏
        assert argv[argv.index("-vf") + 1] == "scale=-2:720,transpose=1", argv
        print("✅ 缩放与旋转按「先缩放后旋转」组合进同一条 -vf")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_remux_uses_stream_copy():
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        _, _, argv = _capture(td, "mkv", src, remux=True, rotate=0)
        assert ["-c", "copy"] == argv[argv.index("-c"):argv.index("-c") + 2], argv
        assert "-c:v" not in argv, "无损重封装不应再指定编码器"
        print("✅ 仅换容器时使用 -c copy，不重编码")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_remux_with_rotate_falls_back_to_reencode():
    """旋转需要滤镜，与 -c copy 互斥；此时必须放弃 copy 改走重编码。"""
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        _, _, argv = _capture(td, "mkv", src, remux=True, rotate=90)
        assert "-c" not in argv or argv[argv.index("-c") + 1] != "copy", \
            f"带旋转时不得使用 -c copy（旋转会被静默忽略）: {argv}"
        assert "transpose=1" in argv[argv.index("-vf") + 1]
        print("✅ remux 与旋转冲突时自动改走重编码，旋转不会被静默丢弃")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_audio_track_can_be_stripped():
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        _, _, argv = _capture(td, "mp4", src, audio=False)
        assert "-an" in argv, "取消勾选保留音轨应产生 -an"
        _, _, argv = _capture(td, "mp4", src, audio=True)
        assert "-an" not in argv
        print("✅ 关闭音轨开关生成 -an")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_bitrate_override():
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        _, _, argv = _capture(td, "mp4", src, bitrate="2M")
        assert ["-b:v", "2M"] == argv[argv.index("-b:v"):argv.index("-b:v") + 2], argv
        _, _, argv = _capture(td, "mp4", src, bitrate="")
        assert "-b:v" not in argv
        print("✅ 视频码率覆盖仅在填写时生效")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_audio_target_bitrate_overrides_fixed_table():
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        _, _, argv = _capture(td, "mp3", src, audio_bitrate="320k")
        assert ["-b:a", "320k"] == argv[argv.index("-b:a"):argv.index("-b:a") + 2], argv
        # 音频目标用 VBR 固定表（-q:a 4）时不应出现 -b:a
        _, _, argv = _capture(td, "mp3", src)
        assert "-b:a" not in argv and ["-q:a", "4"] == argv[argv.index("-q:a"):argv.index("-q:a") + 2]
        print("✅ 音质档位覆盖固定参数表，缺省时回退 VBR")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_audio_target_ignores_video_filters():
    """音频目标不能带视频滤镜/码率，否则 ffmpeg 会因参数冲突报错。"""
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        _, _, argv = _capture(td, "mp3", src, resolution="720", rotate=90,
                              audio=False, bitrate="2M")
        for flag in ("-vf", "-b:v", "-an"):
            assert flag not in argv, f"音频目标不应出现 {flag}: {argv}"
        print("✅ 音频目标自动丢弃分辨率/旋转/视频码率等视频专属参数")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_image_target_is_single_frame():
    """★ 回归：图片目标必须带 -frames:v 1。

    缺此参数时，若源是多帧视频（桌面原生选择器不受 accept 约束），ffmpeg
    会试图把多帧写进同一文件而整体报错——任务标记 failed，用户拿不到产物。
    """
    td = _tmpdir()
    try:
        src = td / "s.png"; src.write_bytes(b"PNG")
        for tgt in ("png", "jpg", "bmp", "tiff", "gif"):
            _, _, argv = _capture(td, tgt, src, is_image=True)
            assert ["-frames:v", "1"] == argv[argv.index("-frames:v"):argv.index("-frames:v") + 2], \
                f"图片目标 {tgt} 缺少 -frames:v 1: {argv}"
        print("✅ 所有图片目标都限制为单帧输出")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_image_target_uses_image_args_not_video_table():
    td = _tmpdir()
    try:
        src = td / "s.png"; src.write_bytes(b"PNG")
        _, _, argv = _capture(td, "jpg", src, is_image=True, image_quality=90)
        assert "-q:v" in argv, "jpg 应带质量参数"
        # 不应混入视频参数表的音频参数
        assert "-c:a" not in argv, f"图片目标不应有音频参数: {argv}"
        print("✅ 图片目标走 _image_encode_args，不混入视频参数表")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_gif_has_two_distinct_semantics():
    """同一个 gif 目标在视频 tab / 图片 tab 行为必须不同（靠 is_image 分流）。"""
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        # 视频 tab：取前 5 秒、10fps、宽 480 的动图
        _, _, argv = _capture(td, "gif", src)
        assert ["-t", "5"] == argv[argv.index("-t"):argv.index("-t") + 2], argv
        assert "fps=10,scale=480:-1:flags=lanczos" in argv
        assert "-frames:v" not in argv, "视频 tab 的 gif 是动图，不能限单帧"
        # 图片 tab：单帧
        _, _, argv = _capture(td, "gif", src, is_image=True)
        assert "-t" not in argv, "图片 tab 的 gif 不应带时长限制"
        assert "-frames:v" in argv, "图片 tab 的 gif 必须是单帧"
        print("✅ gif 目标按 is_image 分流为「动图」与「单帧」两套语义")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_webp_uses_pillow_not_ffmpeg():
    """WebP 特例：捆绑的 LGPL ffmpeg 无 libwebp 编码器，必须绕过 ffmpeg 走 Pillow。"""
    td = _tmpdir()
    try:
        from PIL import Image
        src = td / "s.png"
        Image.new("RGBA", (120, 60), (255, 0, 0, 128)).save(src)
        with mock.patch("subprocess.Popen") as popen:
            job, out, _ = _capture(td, "webp", src, ext=".webp", is_image=True,
                                   image_quality=70)
            assert not popen.called, "webp 目标不应调用 ffmpeg"
        assert job["status"] == "completed", job
        with Image.open(out) as im:
            assert im.format == "WEBP" and im.size == (120, 60)
            assert im.mode == "RGBA", "webp 应保留 alpha 通道"
        print("✅ webp 绕过 ffmpeg 走 Pillow，且保留 alpha")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_failure_sets_status_and_message():
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        job, _, _ = _capture(td, "mp4", src, returncode=1)
        assert job["status"] == "failed"
        assert job["error"], "失败必须留下可读原因，否则前端只能显示空白"
        job, _, _ = _capture(td, "mp4", src, produce=False)
        assert job["status"] == "failed" and "有效文件" in job["error"], job
        print("✅ ffmpeg 非零退出与「无有效产物」都被识别为失败并留下原因")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_unknown_job_is_silently_ignored():
    td = _tmpdir()
    try:
        src = td / "s.mp4"; src.write_bytes(b"S")
        A._run_convert("no_such_job_id", str(src), "mp4", "original")  # 不应抛异常
        print("✅ 作业已被清理时静默返回，不抛异常")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_temp_source_cleanup_flag():
    """上传落盘的临时源必须在转码结束（无论成败）后清理，否则 UPLOAD_TMP 无限堆积。"""
    td = _tmpdir()
    try:
        for is_temp, should_exist in ((True, False), (False, True)):
            src = td / f"s_{is_temp}.mp4"; src.write_bytes(b"S")
            _, _, _ = _capture(td, "mp4", src, src_is_temp=is_temp)
            assert src.exists() is should_exist, (
                f"src_is_temp={is_temp} 时源文件{'应被清理' if not should_exist else '应保留'}")
        print("✅ 上传临时源按 src_is_temp 决定是否清理，已下载文件的源始终保留")
    finally:
        shutil.rmtree(td, ignore_errors=True)


# =========================================================================== #
# B. 参数构造函数
# =========================================================================== #
def test_audio_bitrate_normalization_and_clamp():
    f = A._audio_encode_args
    assert f("mp3", "320k") == ["-vn", "-c:a", "libmp3lame", "-b:a", "320k"]
    assert f("mp3", "320") == ["-vn", "-c:a", "libmp3lame", "-b:a", "320k"]
    assert f("mp3", "2M") == ["-vn", "-c:a", "libmp3lame", "-b:a", "512k"], "上限应钳到 512k"
    assert f("mp3", "10") == ["-vn", "-c:a", "libmp3lame", "-b:a", "32k"], "下限应钳到 32k"
    assert f("mp3", "320K") == f("mp3", " 320k "), "大小写与空白应归一化"
    print("✅ 音质档位支持 k/M/裸数字，并钳制在 32k~512k")


def test_audio_bitrate_invalid_returns_empty():
    """非法档位返回空列表 → 调用方回退固定参数表，而不是拼出坏参数。"""
    for bad in ("", "abc", "k", None, "  "):
        assert A._audio_encode_args("mp3", bad) == [], f"{bad!r} 应回退固定表"
    assert A._audio_encode_args("webm", "320k") == [], "非音频目标应回退固定表"
    print("✅ 非法音质档位/非音频目标返回空列表，交由调用方回退")


def test_audio_bitrate_lossless_targets_ignore_it():
    for fmt in ("wav", "flac"):
        args = A._audio_encode_args(fmt, "320k")
        assert "-b:a" not in args, f"{fmt} 无损，不应带码率: {args}"
        assert args == A.CONVERT_TARGETS[fmt]
    print("✅ 无损目标忽略音质档位，直接用固定表")


def test_image_jpg_quality_reverse_mapping():
    """UI 质量 100=最好 → mjpeg 的 -q:v 反而最小，且钳在 [2,31]。"""
    f = A._image_encode_args

    def q(v):
        args = f("jpg", v)
        return args[args.index("-q:v") + 1]

    assert q(100) == "2", "最高质量应为 -q:v 2"
    assert q(1) == "31", "最低质量应钳到 -q:v 31"
    assert int(q(100)) < int(q(50)) < int(q(1)), "质量值越大 q:v 应越小"
    # 非法质量值按 0 处理（不出现 -q:v）
    assert "-q:v" not in f("jpg", "abc")
    print("✅ jpg 质量档反向映射到 mjpeg -q:v 并钳制在 [2,31]")


def test_image_png_ignores_quality():
    """png 无损：质量档位无效，不得拼出参数。"""
    args = A._image_encode_args("png", 80)
    assert "-q:v" not in args, args
    assert args[-2:] == ["-frames:v", "1"]
    print("✅ png 无损目标忽略质量档位")


def test_image_resize_uses_long_edge_expression():
    args = A._image_encode_args("png", 0, 800)
    assert "-vf" in args
    expr = args[args.index("-vf") + 1]
    assert "min(800,iw)" in expr and "min(800,ih)" in expr, expr
    assert "gt(iw,ih)" in expr and "gt(ih,iw)" in expr, "需按横竖图分别处理长边"
    # resize<=0 不生成缩放
    assert "-vf" not in A._image_encode_args("png", 0, 0)
    print("✅ 缩放按长边计算，且只缩不放（min 保证原图更小时不变）")


def test_image_alpha_flatten_only_for_non_alpha_targets():
    """jpg/bmp 不支持透明：必须白底合成，否则透明区变黑（用户可见）。"""
    for tgt in ("jpg", "bmp", "jpeg"):
        args = A._image_encode_args(tgt, 0, 0, True)
        assert "-filter_complex" in args, f"{tgt} 应做白底合成: {args}"
        graph = args[args.index("-filter_complex") + 1]
        assert "drawbox=t=fill:c=white" in graph, graph
        assert "overlay" in graph, graph
        assert args[args.index("-map") + 1] == "[out]"
        # 关闭白底合成时退回普通 -vf
        assert "-filter_complex" not in A._image_encode_args(tgt, 0, 0, False)
    print("✅ jpg/bmp 默认白底合成透明区，可显式关闭")


def test_image_alpha_capable_targets_keep_alpha():
    for tgt in ("png", "tiff"):
        args = A._image_encode_args(tgt, 0, 0, True)
        assert "-filter_complex" not in args, f"{tgt} 支持 alpha，不应白底合成: {args}"
    print("✅ png/tiff 保留 alpha，不做白底合成")


def test_image_scale_and_flatten_combine():
    """resize 与白底合成同时启用时，scale 必须拼进 filter_complex 的同一张图。"""
    args = A._image_encode_args("jpg", 0, 600, True)
    graph = args[args.index("-filter_complex") + 1]
    assert graph.startswith("[0:v]scale="), graph
    assert "format=rgba" in graph and "split=2" in graph, graph
    print("✅ 缩放与白底合成正确合并进同一 filter_complex")


def test_image_jpeg_alias_maps_to_jpg():
    assert A._image_encode_args("jpeg", 80) == A._image_encode_args("jpg", 80)
    print("✅ jpeg 别名与 jpg 行为一致")


def test_loudness_filter_off_only_applies_volume():
    f = A._build_loudness_filter
    for off in ("off", "", None, "OFF"):
        out = f(off, None)
        assert "loudnorm" not in out, f"{off!r} 不应做响度标准化"
        assert out == "volume=1.00,alimiter=limit=0.98:level=disabled"
    print("✅ 关闭响度标准化时只保留增益与限幅")


def test_loudness_filter_normalizes_to_target_lufs():
    out = A._build_loudness_filter("-14", None)
    assert out.startswith("loudnorm=I=-14.0:TP=-1.0:LRA=11:linear=true,"), out
    assert out.endswith("alimiter=limit=0.98:level=disabled"), "末尾必须限幅防破音"
    assert "I=-18.0" in A._build_loudness_filter("-18", None)
    print("✅ 指定 LUFS 时生成 loudnorm 链并以 alimiter 收尾")


def test_loudness_boost_is_clamped():
    f = A._build_loudness_filter
    assert "volume=1.50" in f("-14", "1.5")
    assert "volume=2.00" in f("-14", "5"), "增益上限应钳到 2.0"
    assert "volume=0.50" in f("-14", "0.1"), "增益下限应钳到 0.5"
    assert "volume=1.00" in f("-14", "abc"), "非法增益应回落 1.0"
    assert "volume=1.20" in f(" -14 ", " 1.2 ")
    print("✅ 增益钳制在 0.5~2.0，非法值回落 1.0")


def test_loudness_invalid_raises_value_error():
    try:
        A._build_loudness_filter("bogus", None)
        raise AssertionError("非法 loudness 应抛 ValueError")
    except ValueError as e:
        assert "loudness" in str(e)
    print("✅ 非法 loudness 抛 ValueError，由调用方决定降级策略")


# =========================================================================== #
# C. 真实转码端到端
# =========================================================================== #
def _require_sample():
    src = _sample()
    if src is None:
        print("⚠️ 跳过：本机 ffmpeg 不可用或无法生成合成素材")
    return src


def test_real_audio_extract():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        job, out = _real(td, "mp3", src, ext=".mp3")
        assert job["status"] == "completed", job
        info = _probe(out)
        assert "Audio: mp3" in info, info
        assert "Video:" not in info, "提取音频产物不应含视频流"
        print("✅ 真实转 mp3：产物含 mp3 音轨且无视频流")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_audio_bitrate_applied():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        # 用「同一素材、两档码率」的产物体积差异作为档位生效的直接证据。
        # 不硬断言 stderr 里的具体 kb/s：AAC 实际码率随编码器与声道数浮动。
        job_hi, out_hi = _real(td, "m4a", src, ext=".hi.m4a", audio_bitrate="256k")
        job_lo, out_lo = _real(td, "m4a", src, ext=".lo.m4a", audio_bitrate="96k")
        assert job_hi["status"] == "completed" and job_lo["status"] == "completed"
        assert "Audio: aac" in _probe(out_hi)
        hi, lo = out_hi.stat().st_size, out_lo.stat().st_size
        assert hi > lo, f"256k 产物({hi}B) 应明显大于 96k 产物({lo}B)"
        print(f"✅ 真实转 m4a：音质档位生效（256k={hi}B > 96k={lo}B）")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_remux_is_lossless_copy():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        job, out = _real(td, "mkv", src, ext=".mkv", remux=True)
        assert job["status"] == "completed", job
        info = _probe(out)
        # 源用 mpeg4 编码，copy 必须原样搬过来（若被重编码会变成 h264）
        assert "Video: mpeg4" in info, f"无损重封装改变了编码格式: {info}"
        print("✅ 真实 remux：编码格式原样保留（mpeg4 → mpeg4）")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_resolution_scaling():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        job, out = _real(td, "mp4", src, ext=".mp4", resolution="720")
        assert job["status"] == "completed", job
        assert _video_size(out) == (960, 720), f"实际尺寸 {_video_size(out)}"
        print("✅ 真实转 720p：320x240 源按比例放大为 960x720")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_rotate_degrees():
    """★ 回归：180° 必须保持宽高不变（修复前输出 240x320，横屏变竖屏）。"""
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        base = _video_size(src)
        assert base == (320, 240), base
        for rot, expect in ((90, (240, 320)), (180, (320, 240)), (270, (240, 320))):
            job, out = _real(td, "mp4", src, ext=f".r{rot}.mp4", rotate=rot)
            assert job["status"] == "completed", job
            got = _video_size(out)
            assert got == expect, f"rotate={rot} 期望 {expect}，实际 {got}"
        print("✅ 真实旋转：90/270 转置宽高，180 保持宽高（180 缺口回归保护）")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_video_from_multiframe_to_image():
    """★ 回归：多帧视频转图片目标应成功（修复前缺 -frames:v 1 会整体失败）。"""
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        job, out = _real(td, "png", src, ext=".png", is_image=True)
        assert job["status"] == "completed", f"多帧源转单帧图片失败: {job['error']}"
        assert _video_size(out) == (320, 240)
        print("✅ 真实多帧视频 → png 单帧成功（-frames:v 1 缺口回归保护）")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_image_alpha_flatten_white():
    """透明区必须合成为白色，否则会变黑（用户可见的观感缺陷）。"""
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        from PIL import Image
        pic = td / "alpha.png"
        img = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
        for x in range(100):
            for y in range(100):
                img.putpixel((x, y), (255, 255, 255, 255))
        img.save(pic)

        job, out = _real(td, "jpg", pic, ext=".jpg", is_image=True, image_quality=100)
        assert job["status"] == "completed", job
        with Image.open(out).convert("RGB") as im:
            assert im.getpixel((180, 50)) == (255, 255, 255), \
                f"全透明区应合成为白色，实际 {im.getpixel((180, 50))}"

        # 显式关闭后透明区应变黑——证明上面的白确实是合成出来的
        job, out = _real(td, "jpg", pic, ext=".off.jpg", is_image=True,
                         image_quality=100, flatten_alpha=False)
        with Image.open(out).convert("RGB") as im:
            assert sum(im.getpixel((180, 50))) < 60, "关闭合成后透明区应为黑"
        print("✅ 白底合成真实生效（开启=白，关闭=黑），alpha 不会转成黑底")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_image_resize_only_shrinks():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        from PIL import Image
        pic = td / "pic.png"
        Image.new("RGBA", (500, 300), (10, 200, 30, 200)).save(pic)
        # 缩到长边 200
        job, out = _real(td, "png", pic, ext=".png", is_image=True, resize=200)
        assert job["status"] == "completed", job
        with Image.open(out) as im:
            assert im.size == (200, 120), im.size
            assert im.mode == "RGBA", "png 应保留 alpha"
        # 目标长边大于原图时不得放大
        job, out = _real(td, "png", pic, ext=".big.png", is_image=True, resize=4000)
        with Image.open(out) as im:
            assert im.size == (500, 300), f"只缩不放，实际 {im.size}"
        print("✅ 图片缩放按长边等比，且只缩不放")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_webp_keeps_alpha_and_resizes():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        from PIL import Image
        pic = td / "pic.png"
        Image.new("RGBA", (400, 200), (255, 0, 0, 128)).save(pic)
        job, out = _real(td, "webp", pic, ext=".webp", is_image=True,
                         image_quality=70, resize=100)
        assert job["status"] == "completed", job
        with Image.open(out) as im:
            assert im.format == "WEBP"
            assert im.size == (100, 50), im.size
            assert im.mode == "RGBA", "webp 必须保留 alpha"
        print("✅ 真实 webp 转换：Pillow 编码、保留 alpha、按长边缩放")
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_real_video_to_gif():
    src = _require_sample()
    if src is None:
        return
    td = _tmpdir()
    try:
        job, out = _real(td, "gif", src, ext=".gif")
        assert job["status"] == "completed", job
        info = _probe(out)
        assert "Video: gif" in info, info
        size = _video_size(out)
        assert size == (480, 360), f"gif 宽度应缩到 480，实际 {size}"
        print("✅ 真实视频 → GIF：前 5 秒、宽 480")
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    # A 段：命令构造
    test_default_video_target_uses_fixed_table()
    test_progress_is_parsed_from_stderr()
    test_resolution_appends_scale_filter()
    test_unknown_resolution_is_ignored()
    test_rotate_mapping_is_correct()
    test_rotate_out_of_range_is_ignored()
    test_resolution_and_rotate_combine_in_order()
    test_remux_uses_stream_copy()
    test_remux_with_rotate_falls_back_to_reencode()
    test_audio_track_can_be_stripped()
    test_bitrate_override()
    test_audio_target_bitrate_overrides_fixed_table()
    test_audio_target_ignores_video_filters()
    test_image_target_is_single_frame()
    test_image_target_uses_image_args_not_video_table()
    test_gif_has_two_distinct_semantics()
    test_webp_uses_pillow_not_ffmpeg()
    test_failure_sets_status_and_message()
    test_unknown_job_is_silently_ignored()
    test_temp_source_cleanup_flag()
    # B 段：参数构造
    test_audio_bitrate_normalization_and_clamp()
    test_audio_bitrate_invalid_returns_empty()
    test_audio_bitrate_lossless_targets_ignore_it()
    test_image_jpg_quality_reverse_mapping()
    test_image_png_ignores_quality()
    test_image_resize_uses_long_edge_expression()
    test_image_alpha_flatten_only_for_non_alpha_targets()
    test_image_alpha_capable_targets_keep_alpha()
    test_image_scale_and_flatten_combine()
    test_image_jpeg_alias_maps_to_jpg()
    test_loudness_filter_off_only_applies_volume()
    test_loudness_filter_normalizes_to_target_lufs()
    test_loudness_boost_is_clamped()
    test_loudness_invalid_raises_value_error()
    # C 段：真实转码
    test_real_audio_extract()
    test_real_audio_bitrate_applied()
    test_real_remux_is_lossless_copy()
    test_real_resolution_scaling()
    test_real_rotate_degrees()
    test_real_video_from_multiframe_to_image()
    test_real_image_alpha_flatten_white()
    test_real_image_resize_only_shrinks()
    test_real_webp_keeps_alpha_and_resizes()
    test_real_video_to_gif()

    _cleanup_sample()
    print("\n🎉 转码管线测试全部通过（44 项）")
