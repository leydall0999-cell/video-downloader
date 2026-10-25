# -*- coding: utf-8 -*-
"""搜狐多分片聚合 / 档位映射的单元测试（纯函数，不联网）。

背景（2026-10-25 用户报「搜狐解析器要重写」）：
  · SohuIE 把一集剧切成 N 个 mp4（part1 常只有几秒片头）返回 _type=multi_video；
  · 原实现只取 entries[0] ⇒ 用户下到 9 秒 / 472KB 的残片；
  · 把整个 multi_video 丢给 yt-dlp 也不会合并（outtmpl 相同会后片覆盖前片）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import downloader as dl  # noqa: E402


def _entry(pid, dur, sizes):
    """构造一个搜狐分片 entry：sizes = {format_id: (height, filesize)}"""
    formats = []
    for fid, (h, size) in sizes.items():
        formats.append({
            "format_id": fid,
            "url": f"https://cdn.example/{pid}/{fid}.mp4",
            "ext": "mp4",
            "width": int(h * 16 / 9),
            "height": h,
            "filesize": size,
        })
    return {
        "id": pid,
        "title": "第1集",
        "formats": formats,
        "url": f"https://cdn.example/{pid}/super.mp4",  # 顶层最高档（覆盖陷阱）
        "format_id": "super",
        "ext": "mp4",
        "duration": dur,
    }


TIERS = {
    "nor": (268, 10 * 1024 * 1024),
    "high": (468, 25 * 1024 * 1024),
    "super": (534, 45 * 1024 * 1024),
    "h2644k": (534, 45 * 1024 * 1024),
}


def _multi(parts=3):
    return {
        "_type": "multi_video",
        "id": "123456",
        "title": "《夙夜集》第1集",
        "entries": [_entry(f"123456_part{i+1}", 300.0, TIERS) for i in range(parts)],
        "duration": 900.0,
    }


def test_is_multi_only_multi_video():
    """playlist（多集）绝不能被当分片拼起来。"""
    assert dl._sohu_is_multi(_multi()) is True
    pl = {"_type": "playlist", "entries": [{"formats": []}]}
    assert dl._sohu_is_multi(pl) is False


def test_aggregate_sums_filesize_and_duration():
    info = dl._sohu_aggregate(_multi(3))
    by_id = {f["format_id"]: f for f in info["formats"]}
    assert set(by_id) == {"nor", "high", "super", "h2644k"}
    # 体积 = 各片求和（不是第一片的体积）
    assert by_id["nor"]["filesize"] == 3 * 10 * 1024 * 1024
    assert by_id["super"]["filesize"] == 3 * 45 * 1024 * 1024
    # 时长 = 各片求和
    assert info["duration"] == pytest.approx(900.0)
    assert by_id["nor"]["duration"] == pytest.approx(900.0)


def test_aggregate_keeps_raw_entries_for_download():
    """下载阶段要用原始 entry（含顶层 url/format_id 等字段），不能是精简 dict。"""
    info = dl._sohu_aggregate(_multi(2))
    parts = info["_vdl_parts"]
    assert len(parts) == 2
    assert parts[0]["id"] == "123456_part1"
    assert "formats" in parts[0]
    assert info["_vdl_part_count"] == 2
    assert "_type" not in info  # multi_video 标记要清掉，否则上层还会当多分片
    assert "entries" not in info


def test_aggregate_drops_tiers_missing_in_some_part():
    """某片缺档位时该档不可选（否则下到中间就断）。"""
    m = _multi(2)
    m["entries"][1]["formats"] = [f for f in m["entries"][1]["formats"] if f["format_id"] != "nor"]
    info = dl._sohu_aggregate(m)
    ids = {f["format_id"] for f in info["formats"]}
    assert "nor" not in ids
    assert "high" in ids


def test_aggregate_empty_entries_raises():
    with pytest.raises(dl.ResolveError):
        dl._sohu_aggregate({"_type": "multi_video", "entries": []})


@pytest.mark.parametrize("quality,expected", [
    ("360", "nor"),      # 640x268 站内标称 360P（宽银幕裁切）
    ("480", "high"),
    ("720", "h2644k"),
    ("1080", "h2644k"),  # 没有更高档就用最高档
    ("best", "h2644k"),
    ("", ""),            # 未知 → 交给调用方默认逻辑
    ("audio", ""),       # 纯音频不映射
])
def test_pick_format_id(quality, expected):
    formats = dl._sohu_aggregate(_multi(2))["formats"]
    assert dl._sohu_pick_format_id(formats, quality) == expected


def test_pick_format_id_prefers_h264_on_tie():
    formats = [
        {"format_id": "h2654k", "height": 534, "width": 1280},
        {"format_id": "h2644k", "height": 534, "width": 1280},
    ]
    assert dl._sohu_pick_format_id(formats, "720") == "h2644k"


def test_safe_media_name_strips_illegal_chars():
    assert "/" not in dl._safe_media_name("a/b:c*d?e")
    assert dl._safe_media_name("") == "video"
    assert dl._safe_media_name("  ...  ") == "video"
