"""MV3 浏览器扩展回传链路测试（2026-09-27，★12 对标 DataTool 的扩展能力）。

扩展（extension/）通过 POST /api/sniffer/send 把嗅探到的媒体流发给桌面端：
  - add_manual 必须接受并保存 cookie（带页面 Cookie 的直链才能下签名资源）
    与 source（'extension'，区别于悬浮球兜底的 'manual'）
  - item 只进 picked 队列（桌面端 picked 轮询自动建任务），不进 items 列表
  - cookie 截断 8192（与 app.py DownloadRequest.cookie 的 max_length 对齐）
  - 缺 url → 400

运行：cd server && python tests/test_sniffer_extension_ingest.py
"""
import os
import sys
from unittest.mock import patch

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cdp_sniffer import SNIFFER  # noqa: E402


def _client():
    return TestClient(server_app.app)


def _clean_picked():
    """跑前清空全局单例的 picked 队列（add_manual 直接压栈）。"""
    SNIFFER.take_picked()


def test_send_accepts_cookie_and_source():
    _clean_picked()
    c = _client()
    r = c.post("/api/sniffer/send", json={
        "url": "https://cdn.douyin.com/aweme/video.mp4?is_play_url=1",
        "mime": "video/mp4",
        "referer": "https://www.douyin.com/discover",
        "page_url": "https://www.douyin.com/discover",
        "page_title": "抖音精选",
        "cookie": "ttwid=1%7Cabctest; SESSION_ID=hamster",
        "source": "extension",
    })
    assert r.status_code == 200, f"send 非 200: {r.status_code} {r.text}"
    body = r.json()
    assert body.get("ok") is True and body.get("picked") is True
    rows = SNIFFER.take_picked()
    assert len(rows) == 1, "picked 队列应有且仅有 1 条"
    it = rows[0]
    assert it["cookie"] == "ttwid=1%7Cabctest; SESSION_ID=hamster", "cookie 必须原样保存"
    assert it["source"] == "extension", "source 必须标记为 extension"
    assert it["kind"] == "media" and it["referer"].startswith("https://www.douyin.com")
    print("✅ send 接受 cookie/source 并入 picked 队列")


def test_picked_via_http_and_not_in_items():
    _clean_picked()
    c = _client()
    c.post("/api/sniffer/send", json={"url": "https://c/x/master.m3u8"})
    items = c.get("/api/sniffer/items?limit=100").json()["items"]
    assert all("master.m3u8" not in (i.get("url") or "") for i in items), \
        "manual/extension 提交项不进 items 列表（那是 CDP 嗅探的展示区）"
    picked = c.get("/api/sniffer/picked").json()["items"]
    assert len(picked) == 1 and picked[0]["kind"] == "playlist", "picked 经 HTTP 出队应含该清单"
    again = c.get("/api/sniffer/picked").json()["items"]
    assert again == [], "picked 取走即出队（幂等消费）"
    print("✅ picked 经 HTTP 出队且 items 列表不受污染")


def test_cookie_truncated_to_8192():
    _clean_picked()
    c = _client()
    big = "k=v;" * 3000   # 15000 字符
    c.post("/api/sniffer/send", json={"url": "https://c/a.mp4", "cookie": big})
    it = SNIFFER.take_picked()[0]
    assert len(it["cookie"]) == 8192, f"cookie 应截断到 8192，实际 {len(it['cookie'])}"
    print("✅ 超长 cookie 截断 8192（对齐 DownloadRequest.max_length）")


def test_missing_url_400():
    _clean_picked()
    c = _client()
    r = c.post("/api/sniffer/send", json={"mime": "video/mp4"})
    assert r.status_code == 400, f"缺 url 应 400，实际 {r.status_code}"
    assert SNIFFER.take_picked() == []
    print("✅ 缺 url 返回 400")


def test_default_source_manual():
    _clean_picked()
    c = _client()
    c.post("/api/sniffer/send", json={"url": "https://c/b.mp4"})
    it = SNIFFER.take_picked()[0]
    assert it["source"] == "manual", "未带 source 时应回落 manual（悬浮球兜底语义不变）"
    assert it["cookie"] == "", "不带 cookie 时字段为空串而非缺失"
    print("✅ 缺省 source=manual、cookie 空串（悬浮球兜底行为不回归）")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            with patch.dict(os.environ, {"VDL_LOGIN_GATE": "0"}):
                fn()
        except AssertionError as exc:
            failed += 1
            print(f"❌ {fn.__name__}: {exc}")
    print(f"\n{'✅ 全部通过' if not failed else '❌ 存在失败'}：{len(fns) - failed}/{len(fns)}")
    sys.exit(1 if failed else 0)
