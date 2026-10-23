"""Cookie 自动同步：`/api/cookie/sync/auto` 与 `/api/cookie/sync/from-local` 的契约。

背景（2026-10-21 用户反馈「浏览器登录了快手，怎么没自动获取」）
------------------------------------------------------------------
1. `from-local` 的 docstring 写着「上报到公共池」，实现却只有 `add_cookie()` ——
   只写本机池、根本不上云。调用方看到 `added/verified` 全 true 会以为完事，
   实测云端池毫无变化。现在改成「有云端配置就顺手推一次」，并用 `cloud` 字段
   如实回报（本机成功不得因云端失败被判成失败）。
2. 上云原本只有前端 30 分钟一次的定时器，而每轮要逐域实时解密浏览器（14 站
   16.7s）⇒ 用户登录完只能干等。新增 auto 端点：先比对浏览器 Cookies 库的
   mtime/size（ms 级），没变化直接 skipped，有变化才解密 + 推送。

这两个行为都必须锁死，否则下次改动很容易退回「看着成功其实没上云」或
「每 2 分钟白跑一次全量解密」。

运行：cd server && VDL_CLOUD_LINK=0 ../.build_venv/bin/python tests/test_cookie_sync_auto.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app as server_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_PASS = 0
_FAIL: list[str] = []


def check(name: str, cond: bool, detail=None) -> None:
    global _PASS
    if cond:
        _PASS += 1
        print(f"  PASS  {name}")
    else:
        _FAIL.append(name)
        print(f"  FAIL  {name}" + (f"  <<< {detail}" if detail is not None else ""))


def _client() -> TestClient:
    # 这些端点只认环回来源；TestClient 默认 host 是 "testclient" 会被 403 拦掉。
    return TestClient(server_app.app, client=("127.0.0.1", 51234))


def test_route_registered() -> None:
    print("[1] auto 端点已注册（事故点：没注册 ⇒ 404/405）")
    c = _client()
    with patch.object(server_app, "_cloud_sync_config", return_value=("", "")):
        r = c.post("/api/cookie/sync/auto", json={})
    check("POST /api/cookie/sync/auto 不是 404/405",
          r.status_code not in (404, 405), r.status_code)


def test_requires_cloud_config() -> None:
    print("[2] 未配置云端 ⇒ 400 且说清怎么配（不能假装成功）")
    c = _client()
    with patch.object(server_app, "_cloud_sync_config", return_value=("", "")):
        r = c.post("/api/cookie/sync/auto", json={})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check("状态码 400", r.status_code == 400, r.status_code)
    check("提示里含配置项名", "VDL_COOKIE_SYNC" in str(body.get("detail", "")), body)


def test_signature_detects_change() -> None:
    print("[3] 签名只反映 mtime/size 变化（登录会写库 ⇒ 必须能感知）")
    sig1 = server_app._cookie_db_signature()
    check("签名非空（本机至少一个浏览器 Cookies 库）", bool(sig1), len(sig1))
    check("签名值形如 mtime:size",
          all(isinstance(v, str) and ":" in v for v in sig1.values()),
          list(sig1.items())[:2])
    sig2 = server_app._cookie_db_signature()
    check("连续两次一致（未操作浏览器时不该抖动）", sig1 == sig2)


def test_skip_when_unchanged() -> None:
    print("[4] 签名未变 ⇒ skipped，且不再解密/推送（这是 2 分钟轮询的成本前提）")
    c = _client()
    calls = {"n": 0}

    def fake_sync(url, token):
        calls["n"] += 1
        return [{"domain": "kuaishou.com", "pushed": True}]

    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "cookie_sync_state.json"
        with patch.object(server_app, "_COOKIE_WATCH_STATE", state), \
             patch.object(server_app, "_cloud_sync_config", return_value=("http://x", "t")), \
             patch.object(server_app, "_sync_domains_to_cloud", side_effect=fake_sync):
            r1 = c.post("/api/cookie/sync/auto", json={})
            b1 = r1.json()
            check("首轮未跳过", b1.get("skipped") is False, b1)
            check("首轮确实推送了一次", calls["n"] == 1, calls["n"])
            check("推送后写入签名快照", state.exists())

            r2 = c.post("/api/cookie/sync/auto", json={})
            b2 = r2.json()
            check("次轮 skipped=True", b2.get("skipped") is True, b2)
            check("次轮没有重复解密推送", calls["n"] == 1, calls["n"])

            # 模拟「用户刚登录某站」：Cookies 库变了 ⇒ 签名快照过期 ⇒ 必须重推
            state.write_text(json.dumps({"fake": "0:0"}), encoding="utf-8")
            r3 = c.post("/api/cookie/sync/auto", json={})
            b3 = r3.json()
            check("签名变化后重新推送", b3.get("skipped") is False and calls["n"] == 2, b3)


def test_push_failure_keeps_state() -> None:
    print("[5] 推送全失败不落盘签名（下次要能重试，不能把变更永久跳过）")
    c = _client()

    def all_fail(url, token):
        return [{"domain": "kuaishou.com", "pushed": False, "reason": "boom"}]

    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "cookie_sync_state.json"
        state.write_text(json.dumps({"old": "1:1"}), encoding="utf-8")
        before = state.read_text(encoding="utf-8")
        with patch.object(server_app, "_COOKIE_WATCH_STATE", state), \
             patch.object(server_app, "_cloud_sync_config", return_value=("http://x", "t")), \
             patch.object(server_app, "_sync_domains_to_cloud", side_effect=all_fail):
            r = c.post("/api/cookie/sync/auto", json={})
        b = r.json()
        check("返回 ok 但 pushed=0", b.get("ok") is True and b.get("pushed") == 0, b)
        check("签名快照未被覆盖（下一轮仍会重试）",
              state.read_text(encoding="utf-8") == before)


def test_partial_failure_keeps_state() -> None:
    """部分成功 + 部分失败 ⇒ 不写签名（2026-10-21 连推压测实测踩到的坑）。

    原判定是 `if pushed or not prev`，只要有一个站推成功就写签名。实测一轮里
    10 站有登录态、7 站撞 429，pushed=3 就把签名写了 ⇒ 库不再变化时那 7 站
    永远不会补推。这里的断言就是「有失败就必须留待重试」。
    """
    print("[5b] 部分失败不落盘签名（失败的域下一轮要能补推）")
    c = _client()

    def mixed(url, token):
        return [
            {"domain": "bilibili.com", "pushed": True},
            {"domain": "kuaishou.com", "pushed": False, "reason": "HTTP Error 429: Too Many Requests"},
        ]

    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "cookie_sync_state.json"
        state.write_text(json.dumps({"old": "1:1"}), encoding="utf-8")
        before = state.read_text(encoding="utf-8")
        with patch.object(server_app, "_COOKIE_WATCH_STATE", state), \
             patch.object(server_app, "_cloud_sync_config", return_value=("http://x", "t")), \
             patch.object(server_app, "_sync_domains_to_cloud", side_effect=mixed):
            r = c.post("/api/cookie/sync/auto", json={})
        b = r.json()
        check("pushed=1 且 failed=1", b.get("pushed") == 1 and b.get("failed") == 1, b)
        check("retry_next=True（告诉前端这轮没推全）", b.get("retry_next") is True, b)
        check("签名快照未被覆盖（失败域下轮还能补推）",
              state.read_text(encoding="utf-8") == before)


def test_no_login_is_not_failure() -> None:
    """「本机未登录」是正常状态，不得让签名永远写不上（否则每轮全量解密）。

    必须和 [5b] 成对：只堵失败、不误伤「没登录这个站」，否则用户没登录
    instagram 这种站就会让 auto 每 2 分钟白跑一次 14 站解密。
    """
    print("[5c] 「本机未登录」不算失败 ⇒ 照常写签名（避免每轮白跑）")
    c = _client()

    def with_absent(url, token):
        return [
            {"domain": "bilibili.com", "pushed": True},
            {"domain": "gifshow.com", "pushed": False, "reason": server_app._COOKIE_NO_LOGIN},
        ]

    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "cookie_sync_state.json"
        with patch.object(server_app, "_COOKIE_WATCH_STATE", state), \
             patch.object(server_app, "_cloud_sync_config", return_value=("http://x", "t")), \
             patch.object(server_app, "_sync_domains_to_cloud", side_effect=with_absent):
            r = c.post("/api/cookie/sync/auto", json={})
            b = r.json()
            check("failed=0（未登录不计入失败）", b.get("failed") == 0, b)
            check("写入了签名快照", state.exists())
            r2 = c.post("/api/cookie/sync/auto", json={})
            check("下一轮即 skipped", r2.json().get("skipped") is True, r2.json())


def test_from_local_pushes_to_cloud() -> None:
    print("[6] from-local 写本机池后同时上云（原缺陷：只写本机却自称已上报）")
    c = _client()
    pushed = {"n": 0}

    def fake_push(domain, header, url, token):
        pushed["n"] += 1
        return {"ok": True, "verified": True}

    with patch.object(server_app.downloader, "get_browser_cookie_header",
                      return_value="passToken=x; userId=1"), \
         patch("cookie_pool.verify_cookie", return_value=None), \
         patch("cookie_pool.add_cookie", return_value=True), \
         patch.object(server_app, "_cloud_sync_config", return_value=("http://x", "t")), \
         patch.object(server_app, "_push_cookie_to_cloud", side_effect=fake_push):
        r = c.post("/api/cookie/sync/from-local", json={"domain": "kuaishou.com"})
    b = r.json()
    check("本机池写入成功", b.get("ok") is True and b.get("added") is True, b)
    check("确实调了一次云端推送", pushed["n"] == 1, pushed["n"])
    check("响应带 cloud 字段如实回报", isinstance(b.get("cloud"), dict) and b["cloud"].get("pushed") is True, b)


def test_from_local_cloud_failure_not_fatal() -> None:
    print("[7] 云端推送失败不得把「本机已成功」判成失败")
    c = _client()

    def boom(domain, header, url, token):
        raise RuntimeError("cloud down")

    with patch.object(server_app.downloader, "get_browser_cookie_header",
                      return_value="passToken=x"), \
         patch("cookie_pool.verify_cookie", return_value=None), \
         patch("cookie_pool.add_cookie", return_value=True), \
         patch.object(server_app, "_cloud_sync_config", return_value=("http://x", "t")), \
         patch.object(server_app, "_push_cookie_to_cloud", side_effect=boom):
        r = c.post("/api/cookie/sync/from-local", json={"domain": "kuaishou.com"})
    b = r.json()
    check("仍是 200 + ok=True", r.status_code == 200 and b.get("ok") is True, (r.status_code, b))
    check("cloud.pushed=False 且带原因",
          b.get("cloud", {}).get("pushed") is False and b.get("cloud", {}).get("reason"), b.get("cloud"))


def test_from_local_no_cloud_config() -> None:
    print("[8] 未配置云端时 from-local 行为不变（cloud=None，不报错）")
    c = _client()
    with patch.object(server_app.downloader, "get_browser_cookie_header",
                      return_value="passToken=x"), \
         patch("cookie_pool.verify_cookie", return_value=None), \
         patch("cookie_pool.add_cookie", return_value=True), \
         patch.object(server_app, "_cloud_sync_config", return_value=("", "")):
        r = c.post("/api/cookie/sync/from-local", json={"domain": "kuaishou.com"})
    b = r.json()
    check("200 且 cloud 为 null", r.status_code == 200 and b.get("cloud") is None, b)


def test_refreshes_local_snapshot() -> None:
    """同步/单域读取时顺手刷快照：否则 30 天 TTL 的旧快照会挡住新登录态。

    这条是「明明同步成功却还是未登录」的直接防线：解析时 cookie_cache 快照
    优先级高于本机池，不刷新它，本机池写得再对也不生效。
    """
    print("[9] 拿到新鲜 header 时刷新本机快照（旧快照不得挡住重新登录）")
    import cookie_cache

    c = _client()
    saved: list[tuple[str, str]] = []

    with patch.object(server_app.downloader, "get_browser_cookie_header",
                      return_value="passToken=fresh; userId=1"), \
         patch("cookie_pool.verify_cookie", return_value=None), \
         patch("cookie_pool.add_cookie", return_value=True), \
         patch.object(server_app, "_cloud_sync_config", return_value=("", "")), \
         patch.object(cookie_cache, "_save", side_effect=lambda h, v: saved.append((h, v))):
        c.post("/api/cookie/sync/from-local", json={"domain": "kuaishou.com"})
    check("from-local 刷新了该域快照",
          saved == [("kuaishou.com", "passToken=fresh; userId=1")], saved)

    saved.clear()
    # _sync_domains_to_cloud 自身：用真实实现，只把一个域喂进去
    import cookie_pool
    with patch.object(cookie_pool, "_root_domains", return_value={"kuaishou.com"}), \
         patch.object(server_app.downloader, "get_browser_cookie_header",
                      return_value="passToken=fresh2"), \
         patch.object(server_app, "_push_cookie_to_cloud",
                      return_value={"ok": True, "verified": True}), \
         patch.object(cookie_cache, "_save", side_effect=lambda h, v: saved.append((h, v))):
        out = server_app._sync_domains_to_cloud("http://x", "t")
    check("全量上云时也刷新了快照", saved == [("kuaishou.com", "passToken=fresh2")], saved)
    check("并照常返回逐域结果", out and out[0].get("pushed") is True, out)


if __name__ == "__main__":
    test_route_registered()
    test_requires_cloud_config()
    test_signature_detects_change()
    test_skip_when_unchanged()
    test_push_failure_keeps_state()
    test_partial_failure_keeps_state()
    test_no_login_is_not_failure()
    test_from_local_pushes_to_cloud()
    test_from_local_cloud_failure_not_fatal()
    test_from_local_no_cloud_config()
    test_refreshes_local_snapshot()
    print(f"\n{'=' * 46}\n通过 {_PASS} 项" + (f"，失败 {len(_FAIL)}: {_FAIL}" if _FAIL else "，全部通过"))
    sys.exit(1 if _FAIL else 0)
