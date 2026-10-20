#!/usr/bin/env python3
"""清晰度档位对「直链视频」的归类回归（2026-10-20）。

背景：网页版原来对 `direct_url` 的视频走浏览器侧分片引擎，**不展示清晰度面板**。
改成统一走「服务器下载任务」后，清晰度面板第一次对这类视频露出，暴露出一个
归类缺陷：yt-dlp 对**裸媒体直链**（例如直接粘贴 .mp4 / 快手 CDN 文件）常常
解析不出 `height` —— `formats` 只有一条、没有 height 字段。于是
`build_quality_options` 的「纯音频」分支（`if not heights`）把它误判成音频，
只给一档「最佳音质（自动）」。用户点「开始下载」拿到的却是视频文件，
观感上就是坏的。

判据（三件事必须同时成立，缺一件就复发）：
  1. 无 height 但**有视频轨**（vcodec 非 none）→ 单一「最佳画质（自动）」；
  2. 无 height 且**无视频轨**（纯音频）→ 仍是「最佳音质」+ 音频格式选项；
  3. 有多档 height 的正常视频 → 720P/1080P 阶梯完全不受影响。

运行：
    cd server && python tests/test_quality_options_direct_video.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试隔离：绝不写用户家目录
_TMP = tempfile.mkdtemp(prefix="vdl_test_quality_")
os.environ["VDL_DATA_DIR"] = _TMP

import downloader  # noqa: E402


def _fmt(fmt_id, height, vcodec, acodec, ext="mp4", protocol="https"):
    return {"format_id": fmt_id, "height": height, "vcodec": vcodec,
            "acodec": acodec, "protocol": protocol, "ext": ext}


def test_direct_file_video_is_not_labeled_audio():
    """直链文件（probe 短路 ⇒ formats 为 0 条）必须按视频给档位。"""
    # probe() 的直链分支真实返回形态（downloader.py: _looks_like_direct_file 命中时）：
    # {"direct": True, "url": url, "title": 文件名, "ext": "mp4", "webpage_url": url}
    # —— **没有 formats 键**，yt-dlp 根本没跑。这是生产上最主要的一条路径。
    for info in (
        {"direct": True, "url": "https://x.invalid/dir/video.mp4?token=1",
         "title": "video.mp4", "ext": "mp4", "webpage_url": "https://x.invalid/dir/video.mp4?token=1"},
        # ext 缺失，只能从 URL 兜底推断
        {"direct": True, "url": "https://x.invalid/dir/video.mp4", "title": "video.mp4"},
        # 无 query 的裸地址
        {"url": "https://x.invalid/dir/clip.webm", "ext": "webm"},
    ):
        opts = downloader.build_quality_options(info)
        assert len(opts) == 1, f"直链视频应只给一档，实际 {[o['label'] for o in opts]}"
        assert opts[0]["key"] == downloader.BEST_KEY, f"key 应为 best，实际 {opts[0]['key']}"
        assert opts[0]["label"] == "最佳画质（自动）", (
            f"有视频轨却按音频给档位（回归）：{opts[0]['label']}")
        assert "音质" not in opts[0]["label"], "直链视频不得出现「音质」字样"
    print("✅ 直链视频（probe 短路 / formats 为 0 条）→ 单一「最佳画质」，不再误标「最佳音质」")


def test_direct_file_audio_still_audio():
    """直链**音频**文件必须仍按音频给档位（不能一类切全推成视频）。"""
    for info in (
        {"direct": True, "url": "https://x.invalid/song.mp3", "title": "song.mp3", "ext": "mp3"},
        {"direct": True, "url": "https://x.invalid/song.m4a?x=1", "title": "song.m4a", "ext": "m4a"},
        {"url": "https://x.invalid/a.flac", "ext": "flac"},
    ):
        opts = downloader.build_quality_options(info)
        labels = [o["label"] for o in opts]
        assert "最佳音质（自动）" in labels, f"直链音频必须仍按音频给档位：{labels}"
    print("✅ 直链音频文件（.mp3/.m4a/.flac）不受影响：仍给「最佳音质」")


def test_unknown_codecs_without_height_is_video():
    """yt-dlp 跑起来了但返回「三项全未知」的裸媒体 → 仍按视频。"""
    # 本地 `yt-dlp -J` 实测 mov_bbb.mp4：height=None, vcodec=None, acodec=None
    for fmts in (
        [_fmt("mp4", None, None, None)],
        [_fmt("0", None, "avc1.640028", "mp4a.40.2")],
        [_fmt("0", None, "avc1.640028", "none")],
        [{"format_id": "0", "ext": "mp4", "protocol": "https"}],
    ):
        opts = downloader.build_quality_options({"formats": fmts})
        assert len(opts) == 1, f"应只给一档，实际 {[o['label'] for o in opts]}"
        assert opts[0]["label"] == "最佳画质（自动）", (
            f"有视频轨却按音频给档位（回归）：{opts[0]['label']}")
    print("✅ yt-dlp 三项全未知的裸媒体 → 单一「最佳画质」，不再误标「最佳音质」")


def test_pure_audio_still_gets_audio_options():
    """无 height 且无视频轨 → 纯音频分支必须原样保留。"""
    audio = {"formats": [
        _fmt("140", None, "none", "mp4a.40.2", ext="m4a"),
        _fmt("139", None, "none", "mp3", ext="mp3"),
    ]}
    opts = downloader.build_quality_options(audio)
    labels = [o["label"] for o in opts]
    assert "最佳音质（自动）" in labels, f"纯音频必须仍按音频给档位：{labels}"
    assert "仅音频 M4A" in labels, f"M4A 选项缺失：{labels}"
    assert "仅音频 MP3" in labels, f"MP3 选项缺失：{labels}"
    print("✅ 纯音频内容不受影响：仍给「最佳音质」+ MP3/M4A 选项")


def test_multi_height_ladder_unchanged():
    """有多档 height 的正常视频 → 阶梯完全不受影响。"""
    video = {"formats": [
        _fmt("137", 1080, "avc1.640028", "none"),
        _fmt("136", 720, "avc1.64001F", "none"),
        _fmt("135", 480, "avc1.4D401F", "none"),
        _fmt("140", None, "none", "mp4a.40.2", ext="m4a"),
    ]}
    opts = downloader.build_quality_options(video)
    keys = [o["key"] for o in opts]
    assert "1080" in keys and "720" in keys and "480" in keys, f"多清晰度阶梯必须保留：{keys}"
    assert downloader.AUDIO_KEY in keys, "仅音频 MP3 档缺失"
    print("✅ 多清晰度视频不受影响：480/720/1080 阶梯 + 仅音频 MP3 照旧")


def test_douyin_branch_untouched():
    """抖音单档分支必须仍然优先，不被新判据抢走。"""
    douyin = {"extractor_key": "Douyin", "formats": [
        _fmt("0", None, "avc1.640028", "mp4a.40.2"),
    ]}
    opts = downloader.build_quality_options(douyin)
    assert len(opts) == 1 and opts[0]["key"] == downloader.BEST_KEY
    assert opts[0]["label"] == "最佳画质（自动）"
    print("✅ 抖音单档分支不受影响")


def test_empty_formats_no_crash():
    """空 formats 不得抛异常（沿用既有音频兜底）。"""
    opts = downloader.build_quality_options({"formats": []})
    assert opts and opts[0]["key"] == downloader.BEST_KEY
    print("✅ 空 formats 不炸，仍返回兜底档位")


def main():
    test_direct_file_video_is_not_labeled_audio()
    test_direct_file_audio_still_audio()
    test_unknown_codecs_without_height_is_video()
    test_pure_audio_still_gets_audio_options()
    test_multi_height_ladder_unchanged()
    test_douyin_branch_untouched()
    test_empty_formats_no_crash()
    print("\n✅ 清晰度档位归类回归全部通过（直链视频 / 直链音频 / 三项全未知 / 纯音频 / 多档阶梯 / 抖音 / 空 formats）")


if __name__ == "__main__":
    main()
