"""2026-10-25 全平台实测修掉的三处路由/解析问题（离线单测）。

1. **西瓜视频不再改写成抖音**：`_normalize_url` 曾把 `ixigua.com/<ID>` 改写成
   `douyin.com/video/<ID>`（注释称「视频 ID 互通」）。实测不成立 —— 抖音侧一律回
   「视频不存在」，白等 30s+，且真实原因（缺 Cookie 触发反爬）被彻底盖掉。
2. **西瓜视频无 Cookie 时给可操作提示**：直接抛 `cookie_required`，而不是先花 20s
   去撞反爬挑战页。
3. **网易云单曲不再走 weapi `/mv/detail`**：单曲 ID 被当 MV 查，接口返回非 JSON，
   用户看到 `JSONDecodeError: Expecting value: line 1 column 1`。改为 yt-dlp 原生优先。
"""
from __future__ import annotations

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import downloader as D  # noqa: E402


class TestIxiguaRouting:
    def test_normalize_keeps_ixigua_url(self):
        """ixigua 链接必须原样保留，不能被改写成抖音。"""
        url = "https://www.ixigua.com/6996881461559165471"
        assert D._normalize_share_url(url) == url

    def test_ixigua_not_treated_as_douyin_host(self):
        assert not D._is_douyin_host("www.ixigua.com")
        assert not D._is_douyin_host("ixigua.com")
        assert D._is_douyin_host("www.douyin.com")
        assert D._is_douyin_host("v.douyin.com")

    def test_ixigua_without_cookie_raises_actionable_error(self):
        """没有浏览器 Cookie 时快速失败，并给出 category=cookie_required。"""
        url = "https://www.ixigua.com/6996881461559165471"
        with mock.patch.object(D, "get_browser_cookie_header", return_value=None):
            try:
                D._ixigua_info(url)
            except D.ResolveError as e:
                assert e.category == "cookie_required", e.category
                assert "ixigua.com" in (e.hint or "")
                assert "抖音" in (e.hint or "")
            else:  # pragma: no cover
                raise AssertionError("没有 Cookie 时应当直接报错，不该去撞反爬页")

    def test_ixigua_cookie_file_roundtrip(self):
        """Cookie 头 → Netscape 文件的转换要能保住每个键值（yt-dlp 靠 jar 判登录）。"""
        path = D._write_netscape_cookies("a=1; b=2; broken; c=", "ixigua.com")
        try:
            text = open(path, encoding="utf-8").read()
        finally:
            os.remove(path)
        assert text.startswith("# Netscape HTTP Cookie File")
        rows = [ln.split("\t") for ln in text.strip().splitlines()[1:]]
        names = {r[5]: r[6] for r in rows}
        assert names == {"a": "1", "b": "2"}  # broken / 空值必须被丢掉
        assert all(r[0] == ".ixigua.com" for r in rows)


class TestNetEaseRouting:
    def test_probe_prefers_ytdlp_for_song(self):
        """单曲走 yt-dlp 原生；weapi 不应被调用。"""
        fake = {"title": "我不难过", "formats": [{"format_id": "0"}]}
        with mock.patch.object(D, "_YoutubeDL") as ydl_cls, \
                mock.patch.object(D, "_base_options", return_value={}), \
                mock.patch.object(D, "_ne_info") as ne_info:
            ydl_cls.return_value.__enter__.return_value.extract_info.return_value = fake
            out = D._ne_probe("https://music.163.com/song?id=287398")
        assert out is fake
        ne_info.assert_not_called()

    def test_mv_falls_back_to_weapi(self):
        """yt-dlp 失败且是 MV 链接时才退 weapi。"""
        with mock.patch.object(D, "_YoutubeDL") as ydl_cls, \
                mock.patch.object(D, "_base_options", return_value={}), \
                mock.patch.object(D, "_ne_info", return_value={"title": "mv"}) as ne_info:
            ydl_cls.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError("boom")
            out = D._ne_probe("https://music.163.com/mv?id=10909917")
        assert out == {"title": "mv"}
        ne_info.assert_called_once()

    def test_song_failure_message_is_readable(self):
        """单曲解析失败不能把 JSONDecodeError 甩给用户。"""
        with mock.patch.object(D, "_YoutubeDL") as ydl_cls, \
                mock.patch.object(D, "_base_options", return_value={}):
            ydl_cls.return_value.__enter__.return_value.extract_info.side_effect = RuntimeError("boom")
            try:
                D._ne_probe("https://music.163.com/song?id=287398")
            except D.ResolveError as e:
                assert e.category == "parse_failed"
                assert "JSONDecodeError" not in (e.hint or "")
                assert "song?id=" in (e.hint or "")
            else:  # pragma: no cover
                raise AssertionError("应当抛出 ResolveError")


class TestNetEaseTrial:
    """VIP 专享曲目未登录只发 30s 试听 —— 必须当场拦下。

    2026-10-25 实测《我不难过》(id=287398)：探针报 duration=320s，
    实际落盘 30s / 481,115 B，用户会以为下到了整首。
    """

    def test_detects_trial_clip(self):
        info = {"duration": 320, "formats": [
            {"filesize": 481115, "abr": 128, "ext": "mp3"}]}
        assert D._ne_trial_only(info) is True

    def test_full_track_not_flagged(self):
        # 完整 128kbps 128 秒 ≈ 2.05MB；这里给 2.05MB
        info = {"duration": 128, "formats": [
            {"filesize": 2048000, "abr": 128, "ext": "mp3"}]}
        assert D._ne_trial_only(info) is False

    def test_unknown_duration_not_flagged(self):
        assert D._ne_trial_only({"duration": None, "formats": [{"filesize": 1}]}) is False
        assert D._ne_trial_only({"formats": []}) is False

    def test_probe_raises_need_vip(self):
        trial = {"duration": 320, "title": "我不难过",
                 "formats": [{"filesize": 481115, "abr": 128, "ext": "mp3"}]}
        with mock.patch.object(D, "_YoutubeDL") as ydl_cls, \
                mock.patch.object(D, "_base_options", return_value={}):
            ydl_cls.return_value.__enter__.return_value.extract_info.return_value = trial
            try:
                D._ne_probe("https://music.163.com/song?id=287398")
            except D.ResolveError as e:
                assert e.category == "need_vip"
                assert "VIP" in (e.message or "")
                assert "30 秒" in (e.hint or "")
            else:  # pragma: no cover
                raise AssertionError("VIP 试听必须抛 need_vip")
