#!/usr/bin/env python3
"""YouTube SABR「PO Token 饥饿」检测与补救回归测试（2026-10-01）。

背景：YouTube 2026 SABR 收紧后，会话无 PO Token 时默认客户端链只发
itag18(360p 混合流) + mhtml 雪碧图，App 清晰度列表最高只有 360P。
修复：_yt_is_pot_starved 检测饥饿 → _try 内自动用 web_safari + fetch_pot=never
重试拿 HLS 全清晰度；build_quality_options 排除雪碧图假档（180/90/45/27P）。

运行：
    cd server && python tests/test_yt_pot_starved.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试隔离（铁律 20）：绝不写用户家目录
_TMP = tempfile.mkdtemp(prefix="vdl_test_pot_")
os.environ["VDL_DATA_DIR"] = _TMP

import downloader  # noqa: E402


def _f(fmt_id, height, vcodec, acodec, protocol="https"):
    return {"format_id": fmt_id, "height": height, "vcodec": vcodec,
            "acodec": acodec, "protocol": protocol, "ext": "mp4"}


def test_pot_starved_detection():
    # 饥饿响应：itag18 混合流 + 雪碧图，无 video-only 自适应轨
    starved = {"formats": [
        _f("18", 360, "avc1.42001E", "mp4a.40.2"),
        _f("sb0", 360, None, None, "mhtml"),
        _f("sb1", 180, None, None, "mhtml"),
        _f("sb2", 90, None, None, "mhtml"),
    ]}
    assert downloader._yt_is_pot_starved(starved) is True, "itag18+雪碧图应判饥饿"
    # 正常响应：存在 video-only 自适应轨
    normal = {"formats": [
        _f("137", 1080, "avc1.640028", "none"),
        _f("140", None, "none", "mp4a.40.2"),
        _f("18", 360, "avc1.42001E", "mp4a.40.2"),
    ]}
    assert downloader._yt_is_pot_starved(normal) is False, "有 video-only 轨不该判饥饿"
    # 纯音频内容（无任何真实视频轨）不算饥饿
    audio_only = {"formats": [_f("140", None, "none", "mp4a.40.2")]}
    assert downloader._yt_is_pot_starved(audio_only) is False
    # 空 formats 不算饥饿
    assert downloader._yt_is_pot_starved({"formats": []}) is False
    assert downloader._yt_is_pot_starved({}) is False
    print("✅ PO Token 饥饿检测：饥饿/正常/纯音频/空 四类判定正确")


def test_storyboard_heights_excluded():
    # web_safari 饥饿时代的 formats：HLS 高清轨 + itag18 + 雪碧图
    info = {"formats": [
        _f("96", 1080, "avc1.640028", "mp4a.40.2", "m3u8_native"),
        _f("95", 720, "avc1.64001F", "mp4a.40.2", "m3u8_native"),
        _f("18", 360, "avc1.42001E", "mp4a.40.2"),
        _f("sb1", 180, None, None, "mhtml"),
        _f("sb2", 90, None, None, "mhtml"),
        _f("sb3", 45, None, None, "mhtml"),
        _f("sb4", 27, None, None, "mhtml"),
    ]}
    opts = downloader.build_quality_options(info)
    labels = [o["label"] for o in opts]
    assert "1080P 高清" in labels, f"1080 档缺失: {labels}"
    assert "720P 高清" in labels
    for bad in ("180P", "90P", "45P", "27P"):
        assert bad not in labels, f"雪碧图假档 {bad} 不应出现在清晰度列表: {labels}"
    # HLS 轨无 filesize → 体积标注缺省不炸
    h1080 = next(o for o in opts if o["label"] == "1080P 高清")
    assert h1080["key"] == "1080"
    print("✅ build_quality_options：雪碧图假档（180/90/45/27P）已被排除，HLS 全档平铺正常")


def test_real_heights_helper():
    fmts = [
        _f("96", 1080, "avc1.640028", "mp4a.40.2", "m3u8_native"),
        _f("sb1", 180, None, None, "mhtml"),
        _f("140", None, "none", "mp4a.40.2"),
    ]
    hs = downloader._yt_real_video_heights(fmts)
    assert hs == [1080], f"应只含真实视频轨 1080: {hs}"
    print("✅ _yt_real_video_heights：排除雪碧图与纯音频")


def test_bgutil_probe():
    # 4416 未监听时应快速返回 False（<2s），不挂死
    import time
    t0 = time.time()
    ok = downloader._bgutil_reachable()
    dt = time.time() - t0
    assert ok is False, "测试环境 4416 不应有 bgutil server"
    assert dt < 2.0, f"探测耗时 {dt:.1f}s，必须秒级失败"
    print(f"✅ _bgutil_reachable：未监听时 {dt*1000:.0f}ms 快速返回 False")


if __name__ == "__main__":
    test_pot_starved_detection()
    test_storyboard_heights_excluded()
    test_real_heights_helper()
    test_bgutil_probe()
    print("🎉 PO Token 饥饿回归测试全部通过")
