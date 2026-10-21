#!/usr/bin/env python3
"""YouTube 清晰度「选什么下什么」回归（2026-10-21 线上事故）。

背景（必须记住，否则复发）：
    网页版 YouTube 下载是**两阶段**的 ——
      ① `_resolve_youtube()` 用 `format=None` 解析元数据；
      ② `process_info(info)` 直接照 info 里的 `requested_formats` 落盘。
    `format=None` ⇒ yt-dlp 默认选 `bestvideo*+bestaudio` ⇒ **4K**。
    `_download_options()` 里那条精确选择器只在「解析后当场下载」的路径生效，
    YouTube 这条路径从来没被用到 ⇒ 所有档位都下成 4K。

实测证据（任务 533e250dab2147e5，用户选 480P）：
    卡片 138.7 MiB = 145,461,651 B（135 avc1 480 + 140 m4a）
    实下 401(2160p AV1)+251(opus) = 2,039,288,742 B = 1.9 GiB
    360P 任务同样 2,017 MB —— 两档字节几乎相同，正说明「档位根本没参与选择」。
    合并慢是并发症：要 remux 的是 1.9 GB 而不是 145 MB。

判据（这三条同时成立才算修好）：
  1. 具体档位 → 只挑该档（±3% 容差、按短边分档），绝不升/降档；
  2. 卡片报价（`build_quality_options.approx_size`）与**实际挑中的轨**逐字节一致；
  3. 自动档仍封顶 1080（avc1 优先），音频/WebM/竖屏/非标准高度各自不回归。

运行：cd server && python tests/test_yt_quality_lock.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试隔离：绝不写用户家目录
_TMP = tempfile.mkdtemp(prefix="vdl_test_ytq_")
os.environ["VDL_DATA_DIR"] = _TMP

import downloader  # noqa: E402


def _fmt(fmt_id, height, vcodec, acodec="none", ext="mp4", protocol="https",
         filesize=None, tbr=0.0, width=None):
    f = {
        "format_id": fmt_id, "height": height, "vcodec": vcodec, "acodec": acodec,
        "protocol": protocol, "ext": ext, "tbr": tbr,
    }
    if filesize:
        f["filesize"] = filesize
    if width:
        f["width"] = width
    return f


def _incident_info():
    """线上真实视频（Tf2rHVEuBpQ）的 formats 精简复刻：avc1/vp9/av01/opus/m4a + HLS。"""
    return {
        "title": "Traditional Bread Baking",
        "duration": 1269,
        "formats": [
            # 4K/1440/1080/720/480/360（DASH，带真实 filesize）
            _fmt("401", 2160, "av01.0.12M.08", filesize=2016912705, tbr=12715),
            _fmt("313", 2160, "vp9", filesize=2801587734, tbr=17660),
            _fmt("400", 1440, "av01.0.09M.08", filesize=672264888, tbr=4238),
            _fmt("137", 1080, "avc1.640028", filesize=421378010, tbr=2657),
            _fmt("248", 1080, "vp9", filesize=396344179, tbr=2499),
            _fmt("136", 720, "avc1.4d401f", filesize=200616341, tbr=1265),
            _fmt("135", 480, "avc1.4d401f", filesize=124927996, tbr=787),
            _fmt("134", 360, "avc1.4d401e", filesize=74875662, tbr=472),
            # HLS 混合流（mp4 内置 aac，无 filesize）
            _fmt("231", 480, "avc1.4d401f", acodec="mp4a.40.5", protocol="m3u8_native", tbr=1372),
            # 音轨
            _fmt("140", None, "none", acodec="mp4a.40.2", ext="m4a", filesize=20533655, tbr=129),
            _fmt("251", None, "none", acodec="opus", ext="webm", filesize=22012716, tbr=138),
        ],
    }


def test_480_picks_the_priced_track_not_4k():
    """选 480P 必须挑 135(avc1 480)+140，绝不许给 401(4K AV1)。"""
    info = _incident_info()
    v, a = downloader._pick_youtube_tracks(info, "480")
    assert v is not None, "480 档必须能挑出视频轨"
    assert v["format_id"] == "135", f"应挑 135(480 avc1)，实际 {v['format_id']}"
    assert a is not None and a["format_id"] == "140", f"音频应挑 140，实际 {a and a['format_id']}"
    total = (v.get("filesize") or 0) + (a.get("filesize") or 0)
    assert total == 145461651, f"实下体积应为 145,461,651 B，实际 {total}"
    print("✅ 480P → 135+140（145,461,651 B），不再是 4K AV1")


def test_card_size_equals_picked_size():
    """卡片报价必须与真正挑中的轨逐字节一致（单一真源）。"""
    info = _incident_info()
    opts = {o["key"]: o for o in downloader.build_quality_options(info)}
    for key in ("2160", "1440", "1080", "720", "480", "360"):
        if key not in opts:
            continue
        v, a = downloader._pick_youtube_tracks(info, key)
        assert v is not None, f"{key} 档卡片存在但挑不出轨"
        picked = downloader._plan_size(v, a, info["duration"])
        card = opts[key]["approx_size"]
        assert card == picked, f"{key} 档卡片 {card} ≠ 实挑 {picked}（{v['format_id']}）"
    print("✅ 六个档位「卡片报价 == 实际挑中轨体积」逐档对齐")


def test_best_capped_at_1080_avc1():
    """自动档沿用「≤1080 + avc1 优先」，不得因重选逻辑变成 4K。"""
    v, a = downloader._pick_youtube_tracks(_incident_info(), downloader.BEST_KEY)
    assert v["format_id"] == "137", f"自动档应挑 1080 avc1(137)，实际 {v['format_id']}"
    assert a and a["format_id"] == "140"
    print("✅ 自动档封顶 1080 且 avc1 优先（137+140）")


def test_audio_only_never_downloads_video():
    """仅音频/仅 M4A：只返回音频轨，视频轨为 None（否则等于白下 4K）。"""
    for key in (downloader.AUDIO_KEY, downloader.M4A_KEY):
        v, a = downloader._pick_youtube_tracks(_incident_info(), key)
        assert v is None, f"{key} 不得返回视频轨"
        assert a and a["format_id"] == "140", f"{key} 应挑 m4a 140"
    print("✅ 仅音频/仅 M4A 只挑音轨")


def test_muxed_hls_single_track():
    """只有 HLS 混合流时（自带音频）不能叠音轨，否则音轨重复/合并失败。"""
    info = {"duration": 100, "formats": [
        _fmt("231", 480, "avc1.4d401f", acodec="mp4a.40.5", protocol="m3u8_native"),
    ]}
    v, a = downloader._pick_youtube_tracks(info, "480")
    assert v is not None and v["format_id"] == "231"
    assert a is None, "混合流不得再返回独立音轨"
    print("✅ HLS 混合流只返回单轨（不叠音轨）")


def test_vertical_short_side():
    """竖屏 1080x1920 的短边是 1080：选 1080P 必须命中它，不能挑 720。"""
    info = {"duration": 60, "formats": [
        _fmt("v-1080", 1920, "avc1.640028", width=1080, filesize=9000000),
        _fmt("v-720", 1280, "avc1.4d401f", width=720, filesize=5000000),
        _fmt("a", None, "none", acodec="mp4a.40.2", ext="m4a", filesize=900000),
    ]}
    v, a = downloader._pick_youtube_tracks(info, "1080")
    assert v["format_id"] == "v-1080", f"竖屏 1080P 应命中 v-1080，实际 {v['format_id']}"
    print("✅ 竖屏按短边分档（1080x1920 → 1080P）")


def test_tolerance_tier_1072():
    """非标准高度 1072 要归进 1080 档（±3% 容差），否则这一档凭空消失。"""
    info = {"duration": 60, "formats": [
        _fmt("v-1072", 1072, "avc1.640028", width=1912, filesize=8000000),
        _fmt("a", None, "none", acodec="mp4a.40.2", ext="m4a", filesize=900000),
    ]}
    v, _ = downloader._pick_youtube_tracks(info, "1080")
    assert v and v["format_id"] == "v-1072"
    print("✅ 1072 非标准高度归入 1080 档（容差匹配）")


def test_missing_tier_returns_none():
    """档位在 formats 里不存在时返回 None，让调用方保留原样，绝不擅自换档。"""
    info = _incident_info()
    info["formats"] = [f for f in info["formats"] if f.get("height") != 720]
    v, a = downloader._pick_youtube_tracks(info, "720")
    assert v is None and a is None, "缺席档位必须返回 None"
    print("✅ 档位缺席 → None（不擅自升/降档）")


def test_webm_prefers_vp9_and_caps_1080():
    """WebM 档：挑 vp9/av01 且 ≤1080；4K vp9 不得入选。"""
    v, a = downloader._pick_youtube_tracks(_incident_info(), downloader.WEBM_KEY)
    assert v is not None and str(v["vcodec"]).startswith(("vp9", "av01")), v
    assert downloader._short_side(v) <= 1080, "WebM 档必须 ≤1080"
    assert a is not None and a["ext"] == "webm", f"WebM 档音轨应为 webm，实际 {a}"
    print("✅ WebM 档 ≤1080 且优先 vp9/av01 + opus")


def main():
    test_480_picks_the_priced_track_not_4k()
    test_card_size_equals_picked_size()
    test_best_capped_at_1080_avc1()
    test_audio_only_never_downloads_video()
    test_muxed_hls_single_track()
    test_vertical_short_side()
    test_tolerance_tier_1072()
    test_missing_tier_returns_none()
    test_webm_prefers_vp9_and_caps_1080()
    print("\n✅ YouTube 清晰度锁定回归全部通过"
          "（480 定价轨 / 卡片=实下 / 自动档封顶 / 仅音频 / 混合流 / 竖屏 / 容差 / 缺席档 / WebM）")


if __name__ == "__main__":
    main()
