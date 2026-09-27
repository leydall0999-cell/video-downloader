"""CDP 浏览器嗅探离线测试（2026-09-27）。

覆盖：媒体判定、分片聚合与清单取代、CDP 消息解析（假消息流，无真实 ws）、
outbox 回收、悬浮球脚本关键符号、referer 透传下载链路。
全程无网络、无浏览器。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from cdp_sniffer import (  # noqa: E402
    _BALL_JS,
    _OUTBOX_READ_JS,
    CDPSniffer,
    classify_media,
)


# ---------------------------------------------------------------------------
# classify_media：判定矩阵
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,mime,expect", [
    ("https://cdn.x.com/live/master.m3u8?sign=abc", "application/vnd.apple.mpegurl", "playlist"),
    ("https://cdn.x.com/live.m3u8", "", "playlist"),
    ("https://v.com/dash.mpd", "application/dash+xml", "playlist"),
    ("https://v.com/M3U8-upper", "", ""),          # 大小写后缀不命中（路径无真后缀）
    ("https://upos.b.com/v/30080.m4s?e=sig", "video/mp4", "segment"),
    ("https://cdn.x.com/seg-1.ts", "", "segment"),
    ("https://v.com/full.mp4", "video/mp4", "media"),
    ("https://v.com/a.m4a", "audio/mp4", "media"),
    ("https://v.com/x.webm", "video/webm", "media"),
    ("https://v.com/subs.vtt", "text/vtt", ""),
    ("https://v.com/api.json", "application/json", ""),
    ("https://v.com/", "text/html", ""),
    ("https://v.com/img.jpg", "image/jpeg", ""),
    ("https://googlevideo.com/videoplayback?expire=1", "", ""),   # 无后缀签名 URL
    ("https://v.com/full.mp4", "text/html", ""),                  # mime 权威：错报 html 一律过滤
])
def test_classify_media(url, mime, expect):
    assert classify_media(url, mime) == expect


# ---------------------------------------------------------------------------
# _register：去重 / 分片聚合 / 清单取代占位 / 环形上限
# ---------------------------------------------------------------------------

def _fresh_sniffer() -> CDPSniffer:
    s = CDPSniffer()
    return s


def test_register_dedup_counts():
    s = _fresh_sniffer()
    for _ in range(3):
        s._register(url="https://c/x.m3u8?a=1", mime="", kind="playlist",
                    referer="https://p/", page_url="https://p/watch", page_title="t")
    rows = s.items()
    assert len(rows) == 1 and rows[0]["count"] == 3


def test_register_playlist_replaces_segment_placeholder():
    s = _fresh_sniffer()
    s._register(url="https://c/seg1.m4s?x=1", mime="video/mp4", kind="segment",
                referer="", page_url="https://p/watch", page_title="t")
    s._register(url="https://c/seg2.m4s?x=2", mime="video/mp4", kind="segment",
                referer="", page_url="https://p/watch", page_title="t")
    rows = s.items()
    assert len(rows) == 1 and rows[0]["kind"] == "segment" and rows[0]["count"] == 2
    # 清单到手 → 分片占位被取代
    s._register(url="https://c/master.m3u8", mime="application/vnd.apple.mpegurl",
                kind="playlist", referer="https://p/", page_url="https://p/watch",
                page_title="t")
    rows = s.items()
    assert len(rows) == 1 and rows[0]["kind"] == "playlist"


def test_register_segment_kept_per_page():
    s = _fresh_sniffer()
    s._register(url="https://c/seg.m4s", mime="", kind="segment",
                referer="", page_url="https://p/1", page_title="a")
    s._register(url="https://d/seg.m4s", mime="", kind="segment",
                referer="", page_url="https://p/2", page_title="b")
    assert len(s.items()) == 2


def test_register_ring_limit():
    s = _fresh_sniffer()
    for i in range(230):
        s._register(url=f"https://c/{i}.mp4", mime="video/mp4", kind="media",
                    referer="", page_url="https://p/", page_title="t")
    assert len(s.items(limit=500)) == 200
    urls = [r["url"] for r in s.items(limit=500)]
    assert "https://c/30.mp4" in urls and "https://c/0.mp4" not in urls  # 弹最旧 30 条


# ---------------------------------------------------------------------------
# _on_browser_message：CDP 假消息流（browser 级 flatten 会话）
# ---------------------------------------------------------------------------

def _make_session(s: CDPSniffer):
    pages: dict = {}
    pendings: dict = {}
    async def _noop_enable(ws, sid):    # 真发送走 ws，单测只需 stub
        return None
    s._enable_page = _noop_enable
    s._spawn = lambda coro: coro.close()
    return pages, pendings


def test_on_message_request_response_flow():
    s = _fresh_sniffer()
    pages, pendings = _make_session(s)
    s._on_browser_message({"method": "Target.attachedToTarget", "params": {
        "sessionId": "s1",
        "targetInfo": {"type": "page", "title": "页面A", "url": "https://p/watch"},
    }}, pages, pendings, None)
    assert "s1" in pages
    s._on_browser_message({"sessionId": "s1", "method": "Network.requestWillBeSent", "params": {
        "requestId": "r1",
        "documentURL": "https://p/watch",
        "request": {"url": "https://c/master.m3u8?sig=1",
                    "headers": {"Referer": "https://p/watch", "User-Agent": "UA"}},
    }}, pages, pendings, None)
    s._on_browser_message({"sessionId": "s1", "method": "Network.responseReceived", "params": {
        "requestId": "r1",
        "response": {"url": "https://c/master.m3u8?sig=1",
                     "mimeType": "application/vnd.apple.mpegurl"},
    }}, pages, pendings, None)
    rows = s.items()
    assert len(rows) == 1
    assert rows[0]["referer"] == "https://p/watch"   # 防盗链关键
    assert rows[0]["page_url"] == "https://p/watch"
    assert rows[0]["page_title"] == "页面A"


def test_on_message_ignores_non_page_and_non_media():
    s = _fresh_sniffer()
    pages, pendings = _make_session(s)
    # 非 page target（扩展/浏览器 UI）不建 pages 条目，其事件被忽略
    s._on_browser_message({"method": "Target.attachedToTarget", "params": {
        "sessionId": "ext", "targetInfo": {"type": "service_worker"},
    }}, pages, pendings, None)
    s._on_browser_message({"sessionId": "ext", "method": "Network.requestWillBeSent", "params": {
        "requestId": "r9", "documentURL": "", "request": {"url": "https://c/a.mp4", "headers": {}},
    }}, pages, pendings, None)
    assert s.items() == []
    # page 上的非媒体请求
    s._on_browser_message({"method": "Target.attachedToTarget", "params": {
        "sessionId": "s2", "targetInfo": {"type": "page", "title": "t", "url": "https://p/"},
    }}, pages, pendings, None)
    s._on_browser_message({"sessionId": "s2", "method": "Network.requestWillBeSent", "params": {
        "requestId": "r2", "documentURL": "https://p/",
        "request": {"url": "https://p/api.json", "headers": {}},
    }}, pages, pendings, None)
    s._on_browser_message({"sessionId": "s2", "method": "Network.responseReceived", "params": {
        "requestId": "r2", "response": {"url": "https://p/api.json",
                                        "mimeType": "application/json"},
    }}, pages, pendings, None)
    assert s.items() == []


def test_on_message_collects_outbox_response():
    s = _fresh_sniffer()
    pages, pendings = _make_session(s)
    s._on_browser_message({"id": 42, "result": {"result": {"value": json.dumps([
        {"url": "https://c/full.mp4", "referer": "https://p/", "mime": "video/mp4",
         "page_url": "https://p/watch"}])}}}, pages, pendings, None)
    picked = s.take_picked()
    assert len(picked) == 1 and picked[0]["url"] == "https://c/full.mp4"
    assert s.take_picked() == []                      # take 即清空


def test_on_message_pending_memory_cap():
    s = _fresh_sniffer()
    pages, pendings = _make_session(s)
    s._on_browser_message({"method": "Target.attachedToTarget", "params": {
        "sessionId": "s3", "targetInfo": {"type": "page", "title": "t", "url": "https://p/"},
    }}, pages, pendings, None)
    for i in range(900):
        s._on_browser_message({"sessionId": "s3", "method": "Network.requestWillBeSent", "params": {
            "requestId": f"r{i}", "documentURL": "https://p/",
            "request": {"url": f"https://c/{i}.m3u8", "headers": {}},
        }}, pages, pendings, None)
    assert len(pendings["s3"]) <= 600


# ---------------------------------------------------------------------------
# 手动提交（悬浮球兜底）与 status
# ---------------------------------------------------------------------------

def test_add_manual_and_status():
    s = _fresh_sniffer()
    item = s.add_manual({"url": "https://c/x.m3u8", "referer": "https://p/",
                         "mime": "application/vnd.apple.mpegurl",
                         "page_url": "https://p/"})
    assert item["kind"] == "playlist"
    assert s.take_picked()[0]["url"] == "https://c/x.m3u8"
    st = s.status()
    assert st["state"] == "idle" and st["port"] == 0
    with pytest.raises(ValueError):
        s.add_manual({"url": ""})


def test_items_latest_first():
    s = _fresh_sniffer()
    s._register(url="https://c/1.mp4", mime="video/mp4", kind="media",
                referer="", page_url="", page_title="")
    s._register(url="https://c/2.mp4", mime="video/mp4", kind="media",
                referer="", page_url="", page_title="")
    assert s.items()[0]["url"] == "https://c/2.mp4"


# ---------------------------------------------------------------------------
# 悬浮球脚本关键符号（注入前静态断言）
# ---------------------------------------------------------------------------

def test_ball_js_key_symbols():
    assert "window.__VDL_SNIFF" in _BALL_JS
    assert "attachShadow" in _BALL_JS                 # Shadow DOM 防样式污染
    assert "outbox.push" in _BALL_JS                  # 点击经 outbox 回传（不走页面 fetch，绕 CSP）
    assert "setItems" in _BALL_JS                     # 嗅探端推送列表
    assert "splice(0)" in _OUTBOX_READ_JS             # 读即取走，避免重复
    assert "fetch(" not in _BALL_JS                   # 确保无页面内 fetch


# ---------------------------------------------------------------------------
# referer 透传下载链路（task → _download_options）
# ---------------------------------------------------------------------------

def test_download_options_injects_sniffer_referer():
    import downloader
    from tasks import DownloadTask

    task = DownloadTask(id="0" * 16, url="https://upos.bilivideo.com/v/30080.m4s?e=x",
                        title="t", platform="嗅探", quality="best",
                        referer="https://www.bilibili.com/video/BV1xx")
    opts = downloader._download_options(task, "best", _ProgressStub(), cookie="", proxy="")
    assert opts["http_headers"]["Referer"] == "https://www.bilibili.com/video/BV1xx"


def test_download_options_without_referer_keeps_old_behavior():
    import downloader
    from tasks import DownloadTask

    task = DownloadTask(id="0" * 16, url="https://www.bilibili.com/video/BV1GJ411x7h7",
                        title="t", platform="bilibili", quality="best", referer="")
    opts = downloader._download_options(task, "best", _ProgressStub(), cookie="", proxy="")
    assert opts["http_headers"]["Referer"] == "https://www.bilibili.com/"


class _ProgressStub:
    """_download_options 只把 reporter 塞进 hooks，不调用它。"""

    def __call__(self, *a, **k):  # pragma: no cover
        raise AssertionError("reporter 不应被调用")

    on_postprocess = None


# ---------------------------------------------------------------------------
# store.create referer 字段
# ---------------------------------------------------------------------------

def test_store_create_persists_referer():
    """沙盒拦 /tmp 下 mkdir（sitecustomize），改用仓库内临时目录。"""
    import shutil

    from tasks import TaskStore

    root = Path(__file__).resolve().parent / "_tmp_sniff_tasks"
    shutil.rmtree(root, ignore_errors=True)
    store = TaskStore(root / "tasks")
    task = store.create(url="https://c/x.mp4", title="t", platform="嗅探",
                        quality="原始", referer="https://p/watch")
    assert task.referer == "https://p/watch"
    assert store.get(task.id).referer == "https://p/watch"
    shutil.rmtree(root, ignore_errors=True)
