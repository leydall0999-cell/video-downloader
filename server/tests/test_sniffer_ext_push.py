#!/usr/bin/env python3
"""浏览器扩展「自动嗅探直推」回归测试（2026-10-01，纯离线）。

背景：扩展（MV3 webRequest）本就自动嗅探，但条目只存在扩展本地；桌面面板
列表读的是服务端 items 库 → 永远空，用户以为「没有嗅探到」。
链路：扩展每嗅到新条目 POST /api/sniffer/ext-push → SNIFFER.add_ext_items()
→ 并入同一 items 库 → 面板实时显示、点「下载」直接建任务（带 referer/cookie）。

运行：
    cd server && python tests/test_sniffer_ext_push.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试隔离：绝不写用户家目录
os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_test_extpush_")

import cdp_sniffer  # noqa: E402
from routers import sniffer as rs  # noqa: E402


def _item(url, **kw):
    d = {"url": url, "mime": "", "referer": "https://v.example/watch",
         "page_url": "https://v.example/watch", "page_title": "测试页",
         "cookie": "sid=abc"}
    d.update(kw)
    return d


def test_ext_push_flow():
    s = cdp_sniffer.SNIFFER
    before = len(s.items(limit=200))
    # 1) 合法媒体直链：入库、source=ext、kind 由服务端 classify_media 判定
    added = s.add_ext_items([_item("https://cdn.example/video/abc.mp4", mime="video/mp4")])
    assert added == 1, f"合法 mp4 应回库 1 条: {added}"
    rows = s.items(limit=200)
    assert len(rows) == before + 1
    it = next(r for r in rows if r["url"].endswith("abc.mp4"))
    assert it["source"] == "ext", f"source 应为 ext: {it}"
    assert it["kind"] == "media", f"mp4 应判为 media: {it}"
    assert it.get("cookie") == "sid=abc", f"cookie 应随条目保留（防盗链）: {it}"
    assert it.get("page_title") == "测试页"
    # 2) 重复推送同一 URL：去重，只加 count，不新增行
    s.add_ext_items([_item("https://cdn.example/video/abc.mp4", mime="video/mp4")])
    rows2 = s.items(limit=200)
    assert len(rows2) == before + 1, "重复 URL 不应新增行"
    it2 = next(r for r in rows2 if r["url"].endswith("abc.mp4"))
    assert it2["count"] == 2, f"重复推只加 count: {it2}"
    # 3) m3u8 清单：判为 playlist（一等公民）
    s.add_ext_items([_item("https://cdn.example/live/index.m3u8", mime="application/vnd.apple.mpegurl")])
    it3 = next(r for r in s.items(limit=200) if r["url"].endswith("index.m3u8"))
    assert it3["kind"] == "playlist", f"m3u8 应判为 playlist: {it3}"
    # 4) 脏数据：非 http URL / 与媒体无关的 mime → 丢弃不报错
    added4 = s.add_ext_items([
        "not-a-dict",
        _item("ftp://x/y.mp4"),
        _item("https://cdn.example/logo.png", mime="image/png"),
        _item("https://cdn.example/api.json", mime="application/json"),
    ])
    assert added4 == 0, f"脏数据应全部丢弃: {added4}"
    # 5) 服务端判不出但扩展带 kind_hint（如特殊扩展名直链）→ 采信 hint
    s.add_ext_items([_item("https://cdn.example/media/playback9", mime="", kind_hint="media")])
    it5 = next(r for r in s.items(limit=200) if r["url"].endswith("playback9"))
    assert it5["kind"] == "media", f"无 mime 时应采信 kind_hint: {it5}"
    # 6) 路由层直调：正常 dict 载荷返回 ok+added
    r = rs.sniffer_ext_push({"items": [_item("https://cdn.example/video/def.mp4", mime="video/mp4")]})
    assert r.get("ok") is True and r.get("added") == 1, f"路由层直调失败: {r}"
    # 7) 路由层容错：空/畸形载荷不抛异常
    r2 = rs.sniffer_ext_push({})
    assert r2.get("ok") is True and r2.get("added") == 0
    r3 = rs.sniffer_ext_push({"items": "garbage"})
    assert r3.get("ok") is True and r3.get("added") == 0
    print("✅ ext-push：入库/去重/playlist/脏数据/kind_hint/路由直调/容错 全部正确")


def test_ext_page_items():
    """视频**页面**链接条目（2026-10-01）：YouTube 等 UMP/SABR 站点推页面，
    桌面端点「下载」交给 yt-dlp 解析。页面 URL 本身判不出媒体 → 必须靠 kind_hint。"""
    s = cdp_sniffer.SNIFFER
    added = s.add_ext_items([_item("https://www.youtube.com/watch?v=abc12345678",
                                   mime="text/html", kind_hint="page")])
    assert added == 1, f"页面条目应入库: {added}"
    it = next((r for r in s.items(limit=200) if "youtube.com/watch" in r["url"]), None)
    assert it is not None, "页面条目未出现在 items 里"
    assert it["kind"] == "page", f"kind 应为 page: {it}"
    assert it["source"] == "ext"
    # 已知媒体 URL 即便误带 page hint，也应被 classify_media 优先判成 media（不被 hint 带偏）
    s.add_ext_items([_item("https://cdn.example/real.mp4", mime="video/mp4", kind_hint="page")])
    it2 = next(r for r in s.items(limit=200) if r["url"].endswith("real.mp4"))
    assert it2["kind"] == "media", f"媒体判定优先于 page hint: {it2}"
    # 未知 hint 仍被丢弃（白名单外不外泄）
    assert s.add_ext_items([_item("https://example.com/x", mime="", kind_hint="whatever")]) == 0
    print("✅ ext-push 页面条目：入库/优先级/未知 hint 丢弃 全部正确")


if __name__ == "__main__":
    test_ext_push_flow()
    test_ext_page_items()
    print("🎉 扩展自动推送回归测试全部通过")
