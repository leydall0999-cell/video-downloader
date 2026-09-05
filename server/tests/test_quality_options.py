"""清晰度选项全档平铺回归测试（2026-09-06 优化）。

验证 build_quality_options：
  - 视频内容不再出现「最佳画质（自动）」聚合项
  - 按 formats 真实存在的高度逐档平铺（含抖音等 VPS 平台，不走单档特例）
  - 纯音频内容保留「最佳音质（自动）」+ 格式选项
  - 视频只有低档时不虚构更高假档
  - is_valid_quality 放开任意真实高度（1..4320），拒绝越界/非法

运行：
    cd server && python tests/test_quality_options.py
    .build_venv/bin/python -m pytest tests/test_quality_options.py -v
"""
import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from downloader import (  # noqa: E402
    AUDIO_KEY, BEST_KEY, M4A_KEY, WEBM_KEY, build_quality_options, is_valid_quality,
)


def _mk_formats(heights, extractor="BiliBili"):
    fmts = []
    for h in heights:
        fmts += [
            {"height": h, "vcodec": "avc1", "ext": "mp4", "filesize": h * 1000},
            {"height": h, "vcodec": "av01", "ext": "mp4", "filesize": h * 800},
        ]
    fmts.append({"height": None, "vcodec": "none", "ext": "m4a", "acodec": "mp4a", "filesize": 5000})
    return {"extractor_key": extractor, "formats": fmts}


def _keys(opts):
    return [o["key"] for o in opts]


def test_video_no_best_aggregate():
    """视频内容首位不再是 best 聚合项，按真实高度平铺。"""
    info = _mk_formats([1080, 720, 480, 360])
    opts = build_quality_options(info)
    ks = _keys(opts)
    assert BEST_KEY not in ks, "视频内容不应再有「最佳画质（自动）」聚合项"
    assert ks[0] == "1080", f"最高档应居首，实际 {ks[0]}"
    assert ks[:4] == ["1080", "720", "480", "360"], ks
    labels = [o["label"] for o in opts[:4]]
    assert labels == ["1080P 高清", "720P 高清", "480P 标清", "360P 流畅"], labels
    # 附加格式保留
    assert AUDIO_KEY in ks and WEBM_KEY in ks and M4A_KEY in ks
    print("✅ 视频内容全档平铺、无 best 聚合项、附加格式保留")


def test_douyin_no_single_aggregate():
    """抖音（VPS）不再走「最佳画质」单档特例，按真实档列出。"""
    info = _mk_formats([720], "Douyin")
    opts = build_quality_options(info)
    ks = _keys(opts)
    assert BEST_KEY not in ks
    assert "720" in ks and ks[0] == "720"
    assert len(ks) == 4  # 720 + 3 附加格式
    print("✅ 抖音单档平台按真实 720 展示，无「最佳画质」聚合")


def test_no_fake_higher_tiers():
    """视频最高 720 时不应虚构 1080/480 等不存在档位。"""
    info = _mk_formats([720])
    ks = _keys(build_quality_options(info))
    assert ks[0] == "720" and "1080" not in ks and "480" not in ks
    print("✅ 只列真实存在档位，不虚构假档")


def test_youtube_full_resolutions():
    """YouTube 多档全列（含 preset 之外的 144P）。"""
    info = _mk_formats([2160, 1440, 1080, 720, 480, 360, 144])
    ks = _keys(build_quality_options(info))
    assert ks == ["2160", "1440", "1080", "720", "480", "360", "144", AUDIO_KEY, WEBM_KEY, M4A_KEY], ks
    labels = [o["label"] for o in build_quality_options(info) if o["key"] == "144"]
    assert labels == ["144P"], labels
    print("✅ YouTube 4K 级全部 7 档 + 3 附加格式平铺")


def test_audio_only_keeps_best_audio():
    """纯音频内容保留「最佳音质（自动）」语义（无画质可言）。"""
    info = {"extractor_key": "Ximalaya", "formats": [
        {"height": None, "vcodec": "none", "acodec": "mp4a", "ext": "m4a", "filesize": 100},
        {"height": None, "vcodec": "none", "acodec": "mp3", "ext": "mp3", "filesize": 90},
    ]}
    opts = build_quality_options(info)
    ks = _keys(opts)
    assert BEST_KEY in ks and "最佳音质" in opts[0]["label"]
    assert AUDIO_KEY in ks and M4A_KEY in ks
    print("✅ 纯音频保持最佳音质 + mp3/m4a 选项")


def test_valid_quality_any_real_height():
    assert is_valid_quality("144") and is_valid_quality("1080") and is_valid_quality("4320")
    assert is_valid_quality(BEST_KEY) and is_valid_quality(AUDIO_KEY)
    assert not is_valid_quality("0") and not is_valid_quality("-1")
    assert not is_valid_quality("99999") and not is_valid_quality("abc") and not is_valid_quality("")
    print("✅ is_valid_quality 放开任意真实高度、拒绝越界/非法")


if __name__ == "__main__":
    test_video_no_best_aggregate()
    test_douyin_no_single_aggregate()
    test_no_fake_higher_tiers()
    test_youtube_full_resolutions()
    test_audio_only_keeps_best_audio()
    test_valid_quality_any_real_height()
    print("\n🎉 清晰度全档平铺测试全部通过（6 项）")
