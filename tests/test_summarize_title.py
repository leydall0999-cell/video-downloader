"""summarize 标题组合测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from downloader import _combine_series_title, summarize


def test_combine_series_and_episode():
    info = {
        "title": "第1话 我自远古来",
        "series": "万维猫动画",
        "duration": 1250,
    }
    assert _combine_series_title(info) == "万维猫动画 - 第1话 我自远古来"
    assert summarize(info)["title"] == "万维猫动画 - 第1话 我自远古来"


def test_use_alt_title_when_series_missing():
    info = {"title": "EP01", "alt_title": "某部剧", "duration": 0}
    assert _combine_series_title(info) == "某部剧 - EP01"


def test_no_duplicate_when_title_contains_series():
    info = {"title": "万维猫动画 第1话 我自远古来", "series": "万维猫动画"}
    assert _combine_series_title(info) == "万维猫动画 第1话 我自远古来"


def test_plain_title_when_no_series():
    info = {"title": "普通视频标题"}
    assert _combine_series_title(info) == "普通视频标题"


def test_fallback_when_no_title():
    info = {"series": "万维猫动画"}
    assert _combine_series_title(info) == "万维猫动画"


def test_unnamed_fallback():
    assert _combine_series_title({}) == "未命名视频"
