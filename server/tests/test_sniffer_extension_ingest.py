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


def test_send_id_and_ack_roundtrip():
    """2026-09-27 用户实测：扩展点「下载」→ 桌面端没反应，扩展却显示「已发送 ✓」。

    /api/sniffer/send 只是入队（必然 200）；真正建任务在桌面端进程里，未登录 /
    不支持 / 超配额都会失败 —— 必须有一条回执通道把结果送回扩展，否则扩展只能
    谎报成功。这里锁住：send 回 send_id、picked 条目带同一 send_id、
    result 先 pending 再按桌面端写回变成 error(带原因)/ok。
    """
    _clean_picked()
    c = _client()
    r = c.post("/api/sniffer/send", json={"url": "https://c/a.mp4"})
    assert r.status_code == 200
    sid = r.json().get("send_id")
    assert sid, "send 必须回 send_id（扩展据它轮询回执）"

    assert c.get("/api/sniffer/result", params={"send_id": sid}).json()["state"] == "pending", \
        "桌面端还没处理时应为 pending"
    assert c.get("/api/sniffer/result", params={"send_id": "nosuchid"}).json()["state"] == "unknown"

    rows = SNIFFER.take_picked()
    assert len(rows) == 1 and rows[0]["send_id"] == sid, "picked 条目必须带同一 send_id（桌面端据它写回执）"

    w = c.post("/api/sniffer/send-result",
               json={"send_id": sid, "ok": False, "message": "请先登录账号后再使用该功能"})
    assert w.status_code == 200 and w.json().get("ok") is True
    got = c.get("/api/sniffer/result", params={"send_id": sid}).json()
    assert got["state"] == "error" and "登录" in got["message"], \
        "失败原因必须原样回到扩展（用户才知道为什么没反应）"

    c.post("/api/sniffer/send-result", json={"send_id": sid, "ok": True, "message": ""})
    assert c.get("/api/sniffer/result", params={"send_id": sid}).json()["state"] == "ok"
    print("✅ send_id 回执往返：pending → error(带原因) → ok")


def test_picked_poll_records_desktop_login_state():
    """桌面端登录态信号：只有带有效令牌的桌面端轮询 picked 才能写入；匿名轮询不得污染。

    这是 2026-09-28 修复的核心不变量：普通浏览器（Chrome）打开 127.0.0.1:8321 也会
    加载 desktop-app.js 并匿名轮询 picked，旧实现会把信号覆盖成 False → 扩展误报「未登录」。
    修复后 mark_desktop_auth(None) 是 no-op，信号维持上次有效值，过期才回落 None。
    """
    _clean_picked()
    # 单例跨用例共存：先清掉可能由其它用例写下的信号，保证断言与执行顺序无关
    SNIFFER._desktop_auth = None  # noqa: SLF001
    c = _client()
    # 1) 没有任何轮询过 → 未知（None），不能谎报未登录
    assert c.get("/api/sniffer/status").json()["desktop_logged_in"] is None, \
        "没有桌面端轮询过时必须未知（None），不能谎报未登录"
    # 2) 匿名轮询（无 Bearer）→ 不得写入 False（本次修复点：防污染）
    c.get("/api/sniffer/picked")
    assert c.get("/api/sniffer/status").json()["desktop_logged_in"] is None, \
        "匿名轮询不得把登录态覆盖成 False（2026-09-28 修复点）"
    # 3) 桌面端已登录（带令牌轮询过）→ 信号为 True
    SNIFFER.mark_desktop_auth("u_real_desktop")  # 等价于路由器解析到有效令牌后调用
    assert c.get("/api/sniffer/status").json()["desktop_logged_in"] is True, \
        "带有效令牌的桌面端轮询后应为 True"
    # 4) 之后即便有匿名轮询，也不得把 True 偷改成 False/None（Chrome 伪装成 App 的场景）
    c.get("/api/sniffer/picked")
    assert c.get("/api/sniffer/status").json()["desktop_logged_in"] is True, \
        "已登录状态下，匿名轮询不得污染信号（修复点）"
    print("✅ picked 轮询：匿名不污染，仅带令牌桌面端可写入登录态")


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
