"""nivod.vip / pomo.mom yt-dlp 提取器单元测试（mock 网络）。

覆盖：
- NivodIE：player_aaaa JSON 平衡提取 → m3u8 直链；详情页 → 播放页跳转
- PomoIE：route1Data 手动模式 / searchKeyword 自动搜索 → play_url m3u8 提取
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "yt_dlp_plugins"))

from yt_dlp.utils import ExtractorError
import yt_dlp_plugins.extractor.nivod as nivod
import yt_dlp_plugins.extractor.pomo as pomo


# ── NivodIE ───────────────────────────────────────────────
_NIVOD_PLAY_HTML = """<!DOCTYPE html><html><head><title>仙逆详情介绍</title></head>
<body>
<script type="text/javascript">
var player_aaaa={"flag":"play","encrypt":0,"trysee":0,"points":0,
"link":"/niplay/44693-1-1/","link_next":"/niplay/44693-1-2/","link_pre":"",
"vod_data":{"vod_name":"仙逆","vod_actor":"王林,李慕婉","vod_pic":"https://img/pic.jpg"},
"url":"https://m3u8.vhmzy.com/videos/202504/13/x/index.m3u8",
"url_next":"https://m3u8.vhmzy.com/videos/next/index.m3u8",
"from":"ylzy","server":"no","note":"","id":44693,"sid":1,"nid":1};
</script>
</body></html>"""

_NIVOD_DETAIL_HTML = """<!DOCTYPE html><html><body>
<a href="/niplay/44693-1-1/">第1集</a>
<a href="/niplay/44693-1-2/">第2集</a>
</body></html>"""


def test_nivod_valid_url():
    ie = nivod.NivodIE()
    assert ie.suitable("https://www.nivod.vip/niplay/44693-1-1/")
    assert ie.suitable("https://www.nivod.vip/nivod/44693/")
    assert not ie.suitable("https://www.youtube.com/watch?v=x")


def test_nivod_extract_player_data():
    ie = nivod.NivodIE()
    data = ie._extract_player_data(_NIVOD_PLAY_HTML)
    assert data.get("url") == "https://m3u8.vhmzy.com/videos/202504/13/x/index.m3u8"
    assert data.get("vod_data", {}).get("vod_name") == "仙逆"


def test_nivod_play_page_extract(monkeypatch):
    ie = nivod.NivodIE()
    monkeypatch.setattr(ie, "_download_webpage", lambda *a, **kw: _NIVOD_PLAY_HTML)
    info = ie._real_extract("https://www.nivod.vip/niplay/44693-1-1/")
    assert info["title"] == "仙逆 第1集"
    assert info["formats"][0]["url"] == "https://m3u8.vhmzy.com/videos/202504/13/x/index.m3u8"
    assert info["formats"][0]["protocol"] == "m3u8_native"


def test_nivod_detail_page_jumps_to_play(monkeypatch):
    ie = nivod.NivodIE()
    calls = []

    def fake_download(url, *a, **kw):
        calls.append(url)
        return _NIVOD_PLAY_HTML if "niplay" in url else _NIVOD_DETAIL_HTML

    monkeypatch.setattr(ie, "_download_webpage", fake_download)
    info = ie._real_extract("https://www.nivod.vip/nivod/44693/")
    assert info["title"] == "仙逆 第1集"
    # 应请求了详情页 → 播放页（2 次）
    assert len(calls) == 2
    assert "niplay" in calls[1]


# ── PomoIE ───────────────────────────────────────────────
_POMO_MANUAL_HTML = """<!DOCTYPE html><html><head><title>玩具总动员5 Toy Story 5 - 在线播放</title></head>
<body><script>
const hasDbContent = true;
const route1Data = [{"name":"HD中字","url":"https://cdn1.example.com/v/abc/index.m3u8"},
                    {"name":"蓝光","url":"https://cdn2.example.com/v/xyz/index.m3u8"}];
const route2Data = [];
</script></body></html>"""

_POMO_SEARCH_HTML = """<!DOCTYPE html><html><head><title>玩具总动员5 Toy Story 5 - 在线播放</title></head>
<body><script>
const hasDbContent = false;
const route1Data = [];
const searchKeyword = "\\u73a9\\u5177\\u603b\\u52a8\\u5458";
</script></body></html>"""

_POMO_SEARCH_API = {
    "code": 200,
    "data": {
        "西瓜": [{"id": 86243, "name": "玩具总动员5",
                  "play_url": "HD中字$https://svip.xgplay17.com/play/x$$$HD中字$https://svip.xgplay17.com/2026-08-18/x/index.m3u8"}],
    },
}


def test_pomo_valid_url():
    ie = pomo.PomoIE()
    assert ie.suitable("https://pomo.mom/?plugin=plyr_player&gid=3603")
    assert ie.suitable("https://pomo.mom/")
    assert not ie.suitable("https://nivod.vip/nivod/1/")


def test_pomo_search_keyword_unicode_decode():
    ie = pomo.PomoIE()
    kw = ie._extract_search_keyword(_POMO_SEARCH_HTML)
    assert kw == "玩具总动员", f"JS unicode 转义应解码，实际 {kw!r}"


def test_pomo_manual_routes():
    ie = pomo.PomoIE()
    routes = ie._extract_routes(_POMO_MANUAL_HTML)
    assert len(routes) == 2
    assert routes[0] == ("HD中字", "https://cdn1.example.com/v/abc/index.m3u8")


def test_pomo_search_play_url_extraction(monkeypatch):
    ie = pomo.PomoIE()
    monkeypatch.setattr(ie, "_download_webpage",
                        lambda url, vid, **kw: _POMO_SEARCH_HTML if "api.php" not in url else json.dumps(_POMO_SEARCH_API))
    monkeypatch.setattr(ie, "_parse_src", lambda *a, **kw: "")
    routes = ie._search_routes("https://pomo.mom", "玩具总动员5")
    # play_url 里的 m3u8 应被直接提取（不依赖 parse API）
    assert any("index.m3u8" in u for _, u in routes), f"应提取 m3u8，实际 {routes}"


def test_pomo_real_extract_manual(monkeypatch):
    ie = pomo.PomoIE()
    monkeypatch.setattr(ie, "_download_webpage",
                        lambda url, vid, **kw: _POMO_MANUAL_HTML if "api.php" not in url else "{}")
    info = ie._real_extract("https://pomo.mom/?plugin=plyr_player&gid=3603")
    urls = [f["url"] for f in info["formats"]]
    assert "https://cdn1.example.com/v/abc/index.m3u8" in urls
    assert "https://cdn2.example.com/v/xyz/index.m3u8" in urls
    assert info["title"] == "玩具总动员5 Toy Story 5"
