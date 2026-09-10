"""LGPL 编码器与滤镜选择回归测试（2026-09-10 新增）。

背景
----
App 捆绑的 ffmpeg 是 LGPL 构建（configure 不带 --enable-gpl），因此
libx264 / libx265 / delogo 等 GPL 组件在运行时**不存在**。`codec_utils` 承担
两件事：在运行期探测可用编码器并挑选合法替代（VideoToolbox 硬编 → libopenh264
软编 → mpeg4 兜底），以及为 delogo 提供 LGPL 等价的模糊滤镜链。

这块此前零测试，退化的后果都不轻：
1. 编码器优先级写反 → 老机器没有 VideoToolbox 时转码直接失败；
2. 有人「顺手」加回 libx264 → 整个 App 落入 GPL，是项目的合规铁律红线；
3. delogo 的 band 不钳制 → 极端值让 gblur 变成全黑或形同失效；
4. 探测结果不缓存 → 每次转码都 spawn 一次 `ffmpeg -encoders`。

覆盖：h264_args / hevc_args / available_h264 / available_hevc / probe_encoder /
      delogo_filter / audio_encode_args，以及编码器探测缓存。
全部为纯函数：**不 spawn 子进程、不联网**（探测结果用预填缓存模拟）。

运行：
    cd server && python tests/test_codec_utils.py
    cd server && python -m pytest tests/test_codec_utils.py -v
"""
import os
import sys
from unittest import mock

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import codec_utils as cu  # noqa: E402

# 假二进制名：探测结果预填进 _ENC_CACHE，保证测试绝不真的执行 ffmpeg
_BIN = "vdl-test-ffmpeg"

# 贴近真实 GPL ffmpeg 的 -encoders 输出（用于合规红线回归）
_GPL_FLAVORED_ENCODERS = """
 V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC (codec h264)
 V..... libx265              libx265 H.265 / HEVC (codec hevc)
 V..... h264_videotoolbox    VideoToolbox H.264 Encoder
 V..... libopenh264          OpenH264 H.264 (codec h264)
"""


def _encoders(text: str) -> None:
    """模拟 `ffmpeg -encoders` 的输出文本，绕开真实子进程探测。"""
    cu._ENC_CACHE[_BIN] = text


def _cleanup_cache() -> None:
    for k in (_BIN, "vdl-cache-probe", "vdl-fail-bin"):
        cu._ENC_CACHE.pop(k, None)


# --------------------------------------------------------------------------- #
# 1. H.264 编码器降级链
# --------------------------------------------------------------------------- #
def test_h264_prefers_videotoolbox_hardware():
    """首选 VideoToolbox 硬编；同时关掉 realtime、允许硬编内部回落软编。"""
    _encoders(" h264_videotoolbox libopenh264")
    args = cu.h264_args(_BIN, "balanced")
    assert args[:2] == ["-c:v", "h264_videotoolbox"], args
    assert args[args.index("-q:v") + 1] == "60", "balanced 档应为 -q:v 60"
    # -allow_sw 1：硬编不可用时让 VT 内部走软件，而不是整个命令失败
    assert args[args.index("-allow_sw") + 1] == "1", args
    # -realtime 0：关闭实时模式换取更好的压缩质量
    assert args[args.index("-realtime") + 1] == "0", args
    print("✅ H.264 优先 VideoToolbox 硬编，且带 allow_sw/realtime 兜底参数")


def test_h264_falls_back_to_openh264_with_bitrate():
    """没有 VideoToolbox 时退到 libopenh264（BSD），它不支持 CRF，只能用码率。"""
    _encoders(" libopenh264")
    args = cu.h264_args(_BIN, "fast")
    assert args[:2] == ["-c:v", "libopenh264"], args
    assert "-b:v" in args and args[args.index("-b:v") + 1] == "2500k", args
    assert "-q:v" not in args, "openh264 不支持 -q:v，不应出现"
    print("✅ 无硬编时降级 libopenh264，改用目标码率而非 -q:v")


def test_h264_final_fallback_mpeg4():
    """两者都没有（或探测失败）时退到 mpeg4：兼容性最好，仍是 LGPL 安全。"""
    _encoders("")
    args = cu.h264_args(_BIN, "high")
    assert args[:2] == ["-c:v", "mpeg4"], args
    print("✅ 最终兜底 mpeg4，保证任何 ffmpeg 构建都能转码")


def test_h264_pix_fmt_is_appended_last():
    """指定像素格式时追加在末尾（烧字幕等场景需要 yuv420p）。"""
    _encoders(" h264_videotoolbox")
    args = cu.h264_args(_BIN, "high", pix_fmt="yuv420p")
    assert args[-2:] == ["-pix_fmt", "yuv420p"], args
    assert args[args.index("-q:v") + 1] == "78", "high 档应为 -q:v 78"
    # 未指定时不出现 -pix_fmt
    assert "-pix_fmt" not in cu.h264_args(_BIN, "high")
    print("✅ pix_fmt 仅在指定时追加，档位与 q:v 映射正确")


def test_h264_invalid_quality_falls_back_to_balanced():
    """非法档位一律回退 balanced，不能抛异常（前端可能传任意字符串）。"""
    _encoders(" h264_videotoolbox")
    for bad in ("ultra", "", None, "BALANCED", "high "):
        args = cu.h264_args(_BIN, bad)
        assert args[args.index("-q:v") + 1] == "60", f"档位 {bad!r} 应回退 balanced: {args}"
    print("✅ 非法/未知质量档位静默回退 balanced，不会中断转码")


# --------------------------------------------------------------------------- #
# 2. HEVC
# --------------------------------------------------------------------------- #
def test_hevc_prefers_videotoolbox():
    _encoders(" hevc_videotoolbox")
    args = cu.hevc_args(_BIN, "high")
    assert args[:2] == ["-c:v", "hevc_videotoolbox"], args
    assert args[args.index("-q:v") + 1] == "78", args
    print("✅ HEVC 优先 hevc_videotoolbox")


def test_hevc_fallback_mpeg4():
    """HEVC 没有软件兜底（libx265 是 GPL），直接退 mpeg4 容器内 H.264 系编码。"""
    _encoders(" h264_videotoolbox")
    args = cu.hevc_args(_BIN, "balanced")
    assert args[:2] == ["-c:v", "mpeg4"], args
    print("✅ 无 hevc 硬编时退 mpeg4，绝不回落 libx265")


# --------------------------------------------------------------------------- #
# 3. 合规红线
# --------------------------------------------------------------------------- #
def test_never_selects_gpl_encoders():
    """合规红线：任何探测环境下都不得选出 libx264 / libx265（GPL）。"""
    for encoders in ("", " libx264", " libx265", " libx264 libx265",
                     _GPL_FLAVORED_ENCODERS, " h264_videotoolbox"):
        _encoders(encoders)
        for q in ("fast", "balanced", "high", "非法档位"):
            h = cu.h264_args(_BIN, q)
            e = cu.hevc_args(_BIN, q)
            assert "libx264" not in h, f"选中了 GPL 编码器 libx264: {h}"
            assert "libx265" not in e, f"选中了 GPL 编码器 libx265: {e}"
    print("✅ 任何环境下都绝不选 libx264/libx265（LGPL 合规底线）")


def test_gpl_flavored_ffmpeg_still_picks_safe_encoder():
    """面对「装着 libx264 的 GPL ffmpeg」，仍优先选 VT 而非 libx264。"""
    _encoders(_GPL_FLAVORED_ENCODERS)
    assert cu.available_h264(_BIN) == "h264_videotoolbox"
    _encoders(" libx264")   # 只剩 GPL 编码器可用
    assert cu.available_h264(_BIN) == "mpeg4", "宁可退 mpeg4 也不碰 libx264"
    print("✅ 即使 ffmpeg 里有 libx264，也优先 VT / 退 mpeg4 而非使用它")


# --------------------------------------------------------------------------- #
# 4. 探测与缓存
# --------------------------------------------------------------------------- #
def test_probe_encoder_is_substring_match():
    _encoders(" V..... h264_videotoolbox    VideoToolbox H.264 Encoder")
    assert cu.probe_encoder(_BIN, "h264_videotoolbox") is True
    assert cu.probe_encoder(_BIN, "libopenh264") is False
    print("✅ probe_encoder 按 -encoders 输出做子串判定")


def test_encoder_probe_result_is_cached():
    """探测结果必须缓存：否则每次转码都多一次 ffmpeg 子进程开销。"""
    cu._ENC_CACHE.pop("vdl-cache-probe", None)
    calls: list = []

    class _R:
        stdout = " V..... libopenh264"

    def _fake_run(*a, **k):
        calls.append(a)
        return _R()

    with mock.patch("codec_utils.subprocess.run", _fake_run):
        assert cu.probe_encoder("vdl-cache-probe", "libopenh264") is True
        assert cu.probe_encoder("vdl-cache-probe", "h264_videotoolbox") is False
        assert cu.available_h264("vdl-cache-probe") == "libopenh264"
    assert len(calls) == 1, f"探测应只执行一次子进程，实际 {len(calls)} 次"
    cu._ENC_CACHE.pop("vdl-cache-probe", None)
    print("✅ 编码器探测结果被缓存，同一 ffmpeg 只探测一次")


def test_encoder_probe_failure_caches_empty():
    """ffmpeg 不存在时探测失败必须静默降级，不能把异常抛给转码流程。"""
    cu._ENC_CACHE.pop("vdl-fail-bin", None)

    def _boom(*a, **k):
        raise OSError("no such file or directory")

    with mock.patch("codec_utils.subprocess.run", _boom):
        assert cu.probe_encoder("vdl-fail-bin", "h264_videotoolbox") is False
        assert cu.available_h264("vdl-fail-bin") == "mpeg4"
    assert cu._ENC_CACHE.get("vdl-fail-bin") == "", "失败也应缓存空串，避免反复重试"
    cu._ENC_CACHE.pop("vdl-fail-bin", None)
    print("✅ 探测失败静默降级到 mpeg4，且不重复重试")


# --------------------------------------------------------------------------- #
# 5. delogo 的 LGPL 等价实现
# --------------------------------------------------------------------------- #
def test_delogo_filter_structure_and_coordinates():
    f = cu.delogo_filter(10, 20, 100, 50, 10)
    # 必须是 split → crop → gblur → overlay 的链，而非 GPL 的 delogo 滤镜
    assert f.startswith("split[base][wm];"), f
    assert "crop=100:50:10:20" in f, f
    assert "gblur=sigma=" in f, f
    assert f.endswith("overlay=10:20"), f
    assert "delogo=" not in f, "不得使用 GPL 的 delogo 滤镜"
    print("✅ delogo 用 split/crop/gblur/overlay 的 LGPL 等价链实现")


def test_delogo_band_maps_to_clamped_sigma():
    """band 语义映射为 sigma = clamp(band, 2..60)/2，最终落在 2.0~30.0。"""
    cases = {10: 5.0, 1: 2.0, 0: 2.0, -5: 2.0, 60: 30.0, 500: 30.0}
    for band, sigma in cases.items():
        f = cu.delogo_filter(0, 0, 10, 10, band)
        assert f"gblur=sigma={sigma}" in f, f"band={band} 期望 sigma={sigma}，实际: {f}"
    print("✅ band 映射 sigma 并钳制在 2.0~30.0，极端值不会失控")


def test_delogo_filter_avoids_gpl_filter_names():
    """合规：滤镜串里不得出现 GPL 滤镜名。"""
    f = cu.delogo_filter(5, 5, 60, 30, 20)
    for gpl in ("delogo", "boxblur", "hqdn3d", "remove_logo"):
        assert gpl not in f, f"出现了 GPL 滤镜 {gpl}: {f}"
    print("✅ 滤镜串只用 LGPL 滤镜，无 GPL 组件")


# --------------------------------------------------------------------------- #
# 6. 音频编码参数
# --------------------------------------------------------------------------- #
def test_audio_encode_args_dispatch():
    assert cu.audio_encode_args("mp3", "320k") == ["-vn", "-c:a", "libmp3lame", "-b:a", "320k"]
    assert cu.audio_encode_args("m4a") == ["-vn", "-c:a", "aac", "-b:a", "192k"]
    assert cu.audio_encode_args("aac", "256k") == ["-vn", "-c:a", "aac", "-b:a", "256k"]
    assert cu.audio_encode_args("opus") == ["-vn", "-c:a", "libopus", "-b:a", "128k"]
    assert cu.audio_encode_args("wma") == ["-vn", "-c:a", "wmav2", "-b:a", "192k"]
    assert cu.audio_encode_args("mp2") == ["-vn", "-c:a", "mp2", "-b:a", "192k"]
    print("✅ 音频目标的编码器与默认码率分派正确")


def test_audio_encode_args_mp3_uses_vbr_without_bitrate():
    """mp3 不指定码率时用 VBR(-q:a 4)，指定时才切 CBR(-b:a)。"""
    assert cu.audio_encode_args("mp3", "") == ["-vn", "-c:a", "libmp3lame", "-q:a", "4"]
    assert "-q:a" not in cu.audio_encode_args("mp3", "320k")
    print("✅ mp3 无码率走 VBR，有码率走 CBR")


def test_audio_encode_args_lossless_ignores_bitrate():
    """无损格式（flac/wav）忽略码率档位，不能拼出无效的 -b:a。"""
    for fmt, expect in (("flac", "flac"), ("wav", "pcm_s16le")):
        args = cu.audio_encode_args(fmt, "320k")
        assert args == ["-vn", "-c:a", expect], args
        assert "-b:a" not in args, args
    print("✅ flac/wav 无损目标忽略码率，不会拼出无效参数")


def test_audio_encode_args_normalizes_input():
    """格式与码率都要归一化：大小写、首尾空白不应影响结果。"""
    assert cu.audio_encode_args("MP3", "192k") == cu.audio_encode_args("mp3", "192k")
    assert cu.audio_encode_args("  mp3  ", " 320k ") == cu.audio_encode_args("mp3", "320k")
    print("✅ 目标格式与音质档位会做大小写与空白归一化")


def test_audio_encode_args_unknown_format_returns_none():
    """非音频目标返回 None，让调用方回退到 CONVERT_TARGETS 表。"""
    for bad in ("ogg", "webm", "mp4", "", None, "   "):
        assert cu.audio_encode_args(bad, "192k") is None, bad
    print("✅ 非音频目标返回 None，交由调用方回退固定参数表")


def test_lossy_audio_set_matches_implementation():
    """LOSSY_AUDIO 用于判断「该格式是否支持码率」，与实际分派保持一致。"""
    assert cu.LOSSY_AUDIO == {"mp3", "m4a", "aac", "opus", "wma", "mp2"}
    for fmt in cu.LOSSY_AUDIO:
        args = cu.audio_encode_args(fmt, "192k")
        assert args is not None, f"{fmt} 在有损集合内却没有编码参数"
    # 无损格式不在集合内
    assert "flac" not in cu.LOSSY_AUDIO and "wav" not in cu.LOSSY_AUDIO
    print("✅ LOSSY_AUDIO 集合与音频分派实现一致")


if __name__ == "__main__":
    test_h264_prefers_videotoolbox_hardware()
    test_h264_falls_back_to_openh264_with_bitrate()
    test_h264_final_fallback_mpeg4()
    test_h264_pix_fmt_is_appended_last()
    test_h264_invalid_quality_falls_back_to_balanced()
    test_hevc_prefers_videotoolbox()
    test_hevc_fallback_mpeg4()
    test_never_selects_gpl_encoders()
    test_gpl_flavored_ffmpeg_still_picks_safe_encoder()
    test_probe_encoder_is_substring_match()
    test_encoder_probe_result_is_cached()
    test_encoder_probe_failure_caches_empty()
    test_delogo_filter_structure_and_coordinates()
    test_delogo_band_maps_to_clamped_sigma()
    test_delogo_filter_avoids_gpl_filter_names()
    test_audio_encode_args_dispatch()
    test_audio_encode_args_mp3_uses_vbr_without_bitrate()
    test_audio_encode_args_lossless_ignores_bitrate()
    test_audio_encode_args_normalizes_input()
    test_audio_encode_args_unknown_format_returns_none()
    test_lossy_audio_set_matches_implementation()

    _cleanup_cache()
    print("\n🎉 编码器 / 滤镜选择测试全部通过（21 项）")
