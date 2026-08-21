"""VPS worker 返回的「合并 mp4 直链」必须在 summarize 里透传为 play_url/direct_url。

回归背景（2026-08-21）：快手/微博 worker 正常返回 oskwai/kwaicdn 合并 mp4 直链，
但 _kuaishou_info / _weibo_info 构造的 info dict 缺 `direct: True` 标记，
_detect_direct_url 因此直接返回 None，summarize 的 play_url/direct_url 全为空 →
前端表现为「解析不了 / 无播放地址」。抖音因走 formats 数组不受影响。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

import downloader as dl

_MP4 = "https://v4.oskwai.com/ksc2/NggJNlQyszg2QwJ0ClnaLSDrDtHERmK3ETVde4QIUYJPigxMHDJZTGNnAAFK-8vV1sCu1ehq9JJm5WCxLwEAr1rtTJLFs-fXmV0w9goD2N7qoYW1s4LCb9EdpdOpIdoDxK424YOrdPlmySpNdkDdA--D0cwvW4CqcIJrkSYwCQlW2uGSFQDNAIffRLdD4AuM.mp4?pkey=AAX_dcFRp8A-gxiBSSUG9ifa6S7V6TPgfFUbSpEbVlp08CyspTjoXDg2iGfoG8jg_T0lupvjyQqx1RUgbRe0vBCpS2fsEDkIcMk1WpMQtL7nT7B0XG1DhaNWQfd9PROFwao&tag=1-1787323982-unknown-0"


def _fake_worker_ok(**overrides):
    base = {
        "ok": True,
        "title": "测试视频",
        "uploader": "up主",
        "duration": 15,
        "video_id": "3xydn88hgq5dakk",
        "video_url": _MP4,
        "webpage_url": "https://www.kuaishou.com/short-video/3xydn88hgq5dakk",
        "thumbnail": "https://img/cover.jpg",
    }
    base.update(overrides)
    return base


def test_kuaishou_info_direct_marker(monkeypatch):
    """_kuaishou_info 必须带 direct=True，否则直链被 _detect_direct_url 丢弃。"""
    monkeypatch.setattr(dl, "_call_vps_worker", lambda p, u: _fake_worker_ok())
    info = dl._kuaishou_info("https://www.kuaishou.com/short-video/3xydn88hgq5dakk")
    assert info.get("direct") is True
    assert info.get("url") == _MP4
    assert info.get("protocol") == "https"
    assert info.get("ext") == "mp4"


def test_kuaishou_summarize_play_url_not_null(monkeypatch):
    """summarize 后 play_url / direct_url 必须透传合并 mp4 直链。"""
    monkeypatch.setattr(dl, "_call_vps_worker", lambda p, u: _fake_worker_ok())
    info = dl._kuaishou_info("https://v.kuaishou.com/3xydn88hgq5dakk")
    out = dl.summarize(info)
    assert out["play_url"] == _MP4, "play_url 不应为空"
    assert out["direct_url"] == _MP4, "direct_url 不应为空"
    assert out["is_hls"] is False
    assert out["title"] == "测试视频"


def test_kuaishou_summarize_watch_options(monkeypatch):
    """在线观看选项应包含该 mp4 直链（可经后端代理播放）。"""
    monkeypatch.setattr(dl, "_call_vps_worker", lambda p, u: _fake_worker_ok())
    info = dl._kuaishou_info("https://www.kuaishou.com/short-video/3xydn88hgq5dakk")
    out = dl.summarize(info)
    opts = out["watch_options"]
    urls = [o.get("url") for o in opts]
    assert _MP4 in urls, f"watch_options 应含直链，实际 {urls}"


def test_weibo_info_direct_marker(monkeypatch):
    """微博同快手：worker 返回合并 mp4，必须带 direct=True。"""
    monkeypatch.setattr(
        dl,
        "_call_vps_worker",
        lambda p, u: _fake_worker_ok(video_url="https://f.video.weibocdn.com/o0/abc.mp4?k=1"),
    )
    info = dl._weibo_info("https://weibo.com/tv/show/1034:123456")
    assert info.get("direct") is True
    out = dl.summarize(info)
    assert out["play_url"] and out["play_url"].startswith("https://f.video.weibocdn.com")
    assert out["direct_url"] == out["play_url"]


def test_kuaishou_empty_video_url_no_crash(monkeypatch):
    """worker 返回 ok 但无 video_url：play_url 为空但不抛异常（下载阶段再报错）。"""
    monkeypatch.setattr(dl, "_call_vps_worker", lambda p, u: _fake_worker_ok(video_url=""))
    info = dl._kuaishou_info("https://www.kuaishou.com/short-video/3xydn88hgq5dakk")
    out = dl.summarize(info)
    assert out["play_url"] is None
    assert out["direct_url"] is None
