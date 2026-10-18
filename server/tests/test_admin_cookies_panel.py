"""后台「Cookie 池」管理页（2026-10-19）。

覆盖的真实契约：
  1. 概览**不联网**也必须正确：账号去重、每账号保存的 Cookie 项数、过期判定。
     这些是运营判断「要不要补 Cookie」的唯一依据，出错就会误删好 Cookie。
  2. 缓存里的「有效」不能盖过超过 30 天 TTL 的事实 —— 池的读取/裁剪都按 TTL 算，
     页面标绿等于骗人。
  3. 注入必须验真：一条死 Cookie 进池后 `get_cookie` 会取到它，整站解析跟着崩
     （2026-10-18 B 站 1902 条死 Cookie 恒 412 就是这条没守住）。
  4. 粘贴入口必须挡住换行注入：这条字符串会直接进 `Cookie:` 请求头。

运行：cd server && python tests/test_admin_cookies_panel.py
     或 pytest server/tests/test_admin_cookies_panel.py
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import cookie_pool as cp  # noqa: E402
import routers.admin_cookies as ac  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as server_app  # noqa: E402

_FAILED: list[str] = []


def check(name: str, cond: bool, extra=None) -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else f"  → {extra!r}"))
    if not cond:
        _FAILED.append(name)


@contextlib.contextmanager
def _pool(cookies_by_domain: dict[str, list[dict]]):
    """隔离池目录；返回 (pool_dir, client)。"""
    root = Path(tempfile.mkdtemp(prefix="vdl_admin_ck_"))
    pool = root / "cookie_pool"
    pool.mkdir()
    for domain, items in cookies_by_domain.items():
        (pool / f"{domain}.json").write_text(
            json.dumps({"cookies": items}, ensure_ascii=False), encoding="utf-8")
    with patch.object(cp, "_POOL_DIR", pool), \
         patch.object(ac, "require_admin", lambda request=None: None), \
         patch.object(ac, "_cloud_cfg", lambda: ("", "")):
        yield pool, TestClient(server_app.app)


def _item(header: str, ts: int | None = None, source: str = "sync") -> dict:
    return {"header": header, "ts": int(ts if ts is not None else time.time()), "source": source}


# ─── 用例 ───────────────────────────────────────────────────────────────────

def case_auth_required() -> None:
    print("\n[1] 每个端点都必须过超管鉴权网")
    # 这里要验的是「路由有没有挂鉴权」，不是 require_admin 自己的实现（另有用例覆盖）。
    # 用一堵必拒的墙替代：任何端点只要漏调 require_admin，就会返回 200 而不是 401。
    from fastapi import HTTPException

    def _deny(request=None):
        raise HTTPException(status_code=401, detail="未授权：需要超级用户权限")

    with _pool({}) as (_pool_dir, client):
        with patch.object(ac, "require_admin", _deny):
            calls = [
                ("GET", "/api/admin/cookies", None),
                ("POST", "/api/admin/cookies/verify", {}),
                ("POST", "/api/admin/cookies/inject", {"domain": "bilibili.com", "header": "a=1"}),
                ("POST", "/api/admin/cookies/delete", {"domain": "bilibili.com", "ids": ["x"]}),
            ]
            for method, url, payload in calls:
                r = (client.get(url) if method == "GET" else client.post(url, json=payload))
                check(f"{method} {url} 鉴权失败 → 401", r.status_code == 401, r.status_code)


def case_overview() -> None:
    print("\n[2] 概览：账号去重 / 项数 / 过期 / 缺登录字段")
    now = int(time.time())
    old = now - 40 * 24 * 3600          # 超过 30 天 TTL
    with _pool({
        "bilibili.com": [
            _item("SESSDATA=aaa; DedeUserID=111", now - 60),
            _item("SESSDATA=bbb; DedeUserID=111; buvid3=x", now - 30),  # 同账号第二份
            _item("SESSDATA=ccc; DedeUserID=222", now - 10),            # 另一个账号
        ],
        "youtube.com": [
            _item("SID=s; SAPISID=p; __Secure-3PSID=q", old),           # 过期
        ],
        "douyin.com": [
            _item("cna=x; buvid3=y"),                                   # 游客态：缺登录字段
        ],
    }) as (_pool_dir, client):
        r = client.get("/api/admin/cookies")
        check("GET 200", r.status_code == 200, r.status_code)
        d = r.json()
        by = {x["domain"]: x for x in d["domains"]}

        bili = by.get("bilibili.com") or {}
        check("B 站：2 个账号（同 DedeUserID 的 3 条快照不重复计）", bili.get("accounts") == 2, bili.get("accounts"))
        check("B 站：3 条快照", bili.get("entries") == 3, bili.get("entries"))
        check("B 站：每条显示 Cookie 项数", all(a["parts"] >= 2 for a in bili.get("items") or []),
              [a.get("parts") for a in bili.get("items") or []])
        check("B 站：同账号显示份数 + 最新项数", sorted(a["snapshots"] for a in bili.get("items") or []) == [1, 2],
              [a.get("snapshots") for a in bili.get("items") or []])
        check("B 站：未校验前状态为 unknown（不假报有效）",
              all(a["status"] == "unknown" for a in bili.get("items") or []),
              [a.get("status") for a in bili.get("items") or []])
        check("B 站：账号标签不暴露原始 ID",
              all("DedeUserID" not in a["label"] for a in bili.get("items") or []),
              [a.get("label") for a in bili.get("items") or []])
        check("空域不占屏（只列有货的域）", "chrqj.com" not in by, list(by))

        yt = by.get("youtube.com") or {}
        check("YouTube：超过 30 天 → invalid", yt.get("invalid") == 1, yt.get("invalid"))
        check("YouTube：过期原因写明", "30 天" in ((yt.get("items") or [{}])[0].get("detail") or ""),
              (yt.get("items") or [{}])[0].get("detail"))

        dy = by.get("douyin.com") or {}
        check("抖音：缺登录态字段 → invalid", dy.get("invalid") == 1, dy.get("invalid"))
        check("抖音：原因写明游客态", "游客" in ((dy.get("items") or [{}])[0].get("detail") or ""),
              (dy.get("items") or [{}])[0].get("detail"))

        check("summary 汇总有效/无效", d["summary"]["invalid"] == 2, d["summary"])
        check("gaps 列出没有有效 Cookie 的平台", set(d["gaps"]) >= {"youtube.com", "douyin.com"}, d["gaps"])
        check("注入下拉只列主平台", all(x["domain"] in ac._PRIMARY for x in d["injectable"]),
              [x["domain"] for x in d["injectable"]])


def case_cache_overrides() -> None:
    print("\n[3] 有效期缓存结论必须服从 30 天 TTL")
    now = int(time.time())
    with _pool({"bilibili.com": [_item("SESSDATA=aaa; DedeUserID=111", now - 40 * 24 * 3600)]}) as (pool_dir, client):
        (pool_dir.parent / "cookie_pool_verify.json").write_text(json.dumps({
            "ts": now,
            "items": {f"bilibili.com|{ac._entry_id('SESSDATA=aaa; DedeUserID=111')}": {
                "status": "valid", "detail": "缓存说有效", "probe": "net", "ts": now}},
        }), encoding="utf-8")
        d = client.get("/api/admin/cookies").json()
        bili = {x["domain"]: x for x in d["domains"]}["bilibili.com"]
        check("缓存 valid + 已过期 ⇒ 仍判 invalid", bili["invalid"] == 1, bili["valid"])
        check("并写明过期原因", "30 天" in bili["items"][0]["detail"], bili["items"][0]["detail"])


def case_unknown_keeps_hint() -> None:
    print("\n[3b] 联网给不出结论时，静态旁证不能丢（否则游客态被当成「还能用」）")
    now = int(time.time())
    h = "cna=x; buvid3=y"     # 抖音游客态：没有任何登录字段
    with _pool({"douyin.com": [_item(h)]}) as (pool_dir, client):
        (pool_dir.parent / "cookie_pool_verify.json").write_text(json.dumps({
            "ts": now,
            "items": {f"douyin.com|{ac._entry_id(h)}": {
                "status": "unknown", "detail": "无可用探针", "probe": "none", "ts": now}},
        }), encoding="utf-8")
        d = client.get("/api/admin/cookies").json()
        dy = {x["domain"]: x for x in d["domains"]}["douyin.com"]
        item = dy["items"][0]
        check("状态仍是 unknown（没验过就不判无效）", item["status"] == "unknown", item["status"])
        check("详情里补上「疑似未登录」旁证",
              "疑似未登录" in (item["detail"] or ""), item["detail"])


def case_verify_endpoint() -> None:
    print("\n[4] 联网校验：并发探针 + 写缓存 + 会话去重")
    now = int(time.time())
    calls: list[tuple[str, str]] = []

    def fake_probe(domain: str, header: str) -> dict:
        calls.append((domain, header))
        if "DedeUserID=111" in header:
            return {"status": "valid", "detail": "晚风短剧官", "probe": "net"}
        return {"status": "invalid", "detail": "站点判定：未登录", "probe": "net"}

    with _pool({
        "bilibili.com": [
            _item("SESSDATA=aaa; DedeUserID=111", now - 60),
            _item("SESSDATA=bbb; DedeUserID=111; buvid3=x", now - 30),  # 同 session? 不同 SESSDATA ⇒ 不同会话
            _item("SESSDATA=ccc; DedeUserID=222", now - 10),
        ],
    }) as (pool_dir, client):
        with patch.object(ac, "_probe", fake_probe):
            r = client.post("/api/admin/cookies/verify", json={})
        check("verify 200", r.status_code == 200, r.status_code)
        d = r.json()
        check("校验条数 = 独立会话数（不是条目数）", d["checked"] == len(calls) and d["checked"] >= 2,
              {"checked": d.get("checked"), "calls": len(calls)})
        check("返回有效/无效计数", d["valid"] >= 1 and d["invalid"] >= 1, d)
        cache = json.loads((pool_dir.parent / "cookie_pool_verify.json").read_text())
        check("结论写进了缓存文件", bool(cache.get("items")), cache)
        check("缓存带时间戳", int(cache.get("ts") or 0) > 0, cache.get("ts"))

        # 缓存生效后，概览能显示有效账号的昵称
        g = client.get("/api/admin/cookies").json()
        bili = {x["domain"]: x for x in g["domains"]}["bilibili.com"]
        valids = [a for a in bili["items"] if a["status"] == "valid"]
        check("概览读到缓存：至少一个账号有效", bool(valids), bili["items"])
        check("有效的那个账号带昵称", any("晚风短剧官" in (a["detail"] or "") for a in valids),
              [a["detail"] for a in valids])

        # 指定域校验：只打这一个站
        calls.clear()
        with patch.object(ac, "_probe", fake_probe):
            r2 = client.post("/api/admin/cookies/verify", json={"domain": "bilibili.com"})
        check("指定 domain 时只验该站", all(c[0] == "bilibili.com" for c in calls) and calls, calls)

    print("\n[4b] 同会话多份快照：结论必须回写到每一份（2026-10-19 线上踩过）")
    with _pool({"bilibili.com": [
        _item("SESSDATA=same; bili_jct=j; DedeUserID=333; buvid3=a", now - 100),
        _item("SESSDATA=same; bili_jct=j; DedeUserID=333; buvid3=b", now - 10),
    ]}) as (_pd2, client2):
        calls2: list[str] = []

        def fake2(domain: str, header: str) -> dict:
            calls2.append(header)
            return {"status": "valid", "detail": "测试昵称", "probe": "net"}

        with patch.object(ac, "_probe", fake2):
            client2.post("/api/admin/cookies/verify", json={"domain": "bilibili.com"})
        check("同会话只打一次平台（省请求 / 防风控）", len(calls2) == 1, len(calls2))
        g2 = client2.get("/api/admin/cookies").json()
        bili2 = {x["domain"]: x for x in g2["domains"]}["bilibili.com"]
        check("两份快照都拿到结论（不会因为只写最旧那份而显示未校验）",
              all(e["status"] == "valid" for a in bili2["items"] for e in a["entries"]),
              [(a["status"], [e["status"] for e in a["entries"]]) for a in bili2["items"]])
        check("最新那份带昵称", "测试昵称" in (bili2["items"][0]["detail"] or ""),
              bili2["items"][0]["detail"])


def case_inject() -> None:
    print("\n[5] 注入：验真闸门 / 白名单 / 换行注入")
    with _pool({}) as (pool_dir, client):
        # 站点判定无效 → 拒。
        # 同时把 add_cookie 内部的 verify_cookie 也钉成 False：真实实现会再拦一层，
        # 但用例不能依赖网络（断网时那层返回 None ⇒ 放行 ⇒ 断言假红）。
        with patch.object(ac, "_probe", lambda d, h: {"status": "invalid", "detail": "站点判定：未登录", "probe": "net"}), \
             patch.object(cp, "verify_cookie", lambda d, h: False):
            r = client.post("/api/admin/cookies/inject",
                            json={"domain": "bilibili.com", "header": "SESSDATA=dead; DedeUserID=9"})
        check("无效 Cookie 被拒", r.json().get("ok") is False and "无效" in r.json().get("error", ""), r.json())
        check("被拒时池里没写文件", not (pool_dir / "bilibili.com.json").exists())

        # 白名单外 → 拒
        with patch.object(ac, "_probe", lambda d, h: {"status": "valid", "detail": "", "probe": "net"}):
            r = client.post("/api/admin/cookies/inject",
                            json={"domain": "evil.example.com", "header": "a=1"})
        check("白名单外域名被拒", r.json().get("ok") is False, r.json())

        # 换行注入 → 拒
        r = client.post("/api/admin/cookies/inject",
                        json={"domain": "bilibili.com", "header": "SESSDATA=a\r\nX-Evil: 1"})
        check("带换行的粘贴被拒", r.json().get("ok") is False, r.json())

        # 正常注入（add_cookie 内部还会调 verify_cookie，这里打桩放行）
        with patch.object(ac, "_probe", lambda d, h: {"status": "valid", "detail": "晚风短剧官", "probe": "net"}), \
             patch.object(cp, "verify_cookie", lambda d, h: True):
            r = client.post("/api/admin/cookies/inject",
                            json={"domain": "bilibili.com",
                                  "header": "Cookie: SESSDATA=good; DedeUserID=111; buvid3=z"})
        body = r.json()
        check("有效 Cookie 注入成功", body.get("ok") is True, body)
        check("返回保存的 Cookie 项数", body.get("parts") == 3, body.get("parts"))
        saved = json.loads((pool_dir / "bilibili.com.json").read_text())
        check("池文件真的写入了", len(saved.get("cookies") or []) == 1, saved)
        check("来源标记为 admin",
              (saved["cookies"][0].get("source") == "admin"), saved["cookies"][0].get("source"))

        # 注入后再看概览：这条应该已经显示有效（不用等全量校验）
        g = client.get("/api/admin/cookies").json()
        bili = {x["domain"]: x for x in g["domains"]}["bilibili.com"]
        check("注入结果立刻可见（有效 + 昵称）",
              bili["valid"] == 1 and "晚风短剧官" in bili["items"][0]["detail"], bili)


def case_delete() -> None:
    print("\n[6] 按条目删除：只删指定 id")
    now = int(time.time())
    h1 = "SESSDATA=aaa; DedeUserID=111"
    h2 = "SESSDATA=ccc; DedeUserID=222"
    with _pool({"bilibili.com": [_item(h1, now), _item(h2, now)]}) as (pool_dir, client):
        r = client.post("/api/admin/cookies/delete",
                        json={"domain": "bilibili.com", "ids": [ac._entry_id(h1)]})
        check("delete 200", r.status_code == 200 and r.json().get("removed") == 1, r.json())
        saved = json.loads((pool_dir / "bilibili.com.json").read_text())
        left = [cp._decrypt_item(c) for c in saved.get("cookies") or []]
        check("删掉的是指定那条", h1 not in left and h2 in left, left)
        check("返回删除条数", r.json().get("removed") == 1, r.json())

        bad = client.post("/api/admin/cookies/delete", json={"domain": "bilibili.com", "ids": []})
        check("空 ids 被拒", bad.json().get("ok") is False, bad.json())


def case_normalize() -> None:
    print("\n[7] 粘贴格式兼容（Cookie 头 / 多行 / cURL）")
    check("`Cookie: a=1; b=2` 去前缀", ac._normalize_header("Cookie: a=1; b=2") == "a=1; b=2")
    check("多行 k=v 拼成一条", ac._normalize_header("a=1\nb=2\n") == "a=1; b=2")
    check("Copy as cURL 抽取 cookie 头",
          ac._normalize_header("curl 'https://x' -H 'cookie: c=3; d=4'") == "c=3; d=4")
    check("含非 k=v 行整条拒绝（防头注入）", ac._normalize_header("a=1\r\nX-Injected: yes") == "")
    check("没有 k=v 拒绝", ac._normalize_header("hello") == "")
    check("行尾分号容错", ac._normalize_header("a=1; b=2;") == "a=1; b=2")


def case_ui_wiring() -> None:
    print("\n[7b] 前端接线：每个按钮都必须真的绑上事件")
    # 2026-10-19 真机发现：「注入」按钮从来没绑 click（截图看不出来、接口单测也覆盖不到），
    # 点下去毫无反应。这里用静态断言守一道：新增按钮必须同时出现在 HTML 与绑定处。
    repo = Path(__file__).resolve().parents[2]
    js = (repo / "web" / "app.js").read_text(encoding="utf-8")
    html = (repo / "web" / "index.html").read_text(encoding="utf-8")
    for bid in ("adminCkVerify", "adminCkRefresh", "adminCkInjectBtn"):
        check(f"{bid} 有 addEventListener 绑定", f"$('{bid}').addEventListener" in js)
        check(f"{bid} 在 HTML 里存在", f'id="{bid}"' in html)
    # 每行的「补注入」只负责选平台 + 聚焦，真正提交仍走上面那个按钮
    check("平台块有 data-ck-inject 入口", "data-ck-inject=" in js)
    check("admin tab 有 Cookie 池入口", 'data-admin-view="cookies"' in html)


def case_cloud_proxy() -> None:
    print("\n[8] 桌面端默认代理云端池（本机池是空的，不能拿它冒充网页版的池）")
    now = int(time.time())
    with _pool({"bilibili.com": [_item("SESSDATA=local; DedeUserID=1", now)]}) as (pool_dir, client):
        cloud_overview = {
            "ok": True, "now": now, "pool_dir": "/root/.videodownloader/cookie_pool",
            "checked_at": now - 60, "check_age_sec": 60, "summary": {
                "accounts": 1, "entries": 2, "valid": 1, "invalid": 1, "unknown": 0, "broken": 0},
            "gaps": [], "domains": [{"domain": "youtube.com", "title": "YouTube", "accounts": 1,
                                     "entries": 2, "valid": 1, "invalid": 1, "unknown": 0,
                                     "broken": 0, "last_ts": now, "stale": False, "items": []}],
            "injectable": [],
        }
        with patch.object(ac, "_cloud_cfg", lambda: ("http://cloud.test", "tok")), \
             patch.object(ac, "_cloud_call", lambda action, payload=None, timeout=30: dict(cloud_overview)):
            d = client.get("/api/admin/cookies").json()
        check("默认(auto)走云端", d.get("source") == "cloud", d.get("source"))
        check("返回的是云端池的数据（不是本机那份）",
              (d["domains"][0]["domain"] == "youtube.com"), [x["domain"] for x in d["domains"]])
        check("带数据源标签供界面显示", "云端" in (d.get("source_label") or ""), d.get("source_label"))

        # 强制本机：不再走云端
        with patch.object(ac, "_cloud_cfg", lambda: ("http://cloud.test", "tok")), \
             patch.object(ac, "_cloud_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该走云端"))):
            d2 = client.get("/api/admin/cookies?source=local").json()
        check("source=local 时只读本机",
              d2.get("source") == "local" and d2["domains"][0]["domain"] == "bilibili.com",
              d2.get("source"))

        # auto + 云端挂 → 回退本机，但必须标注
        with patch.object(ac, "_cloud_cfg", lambda: ("http://cloud.test", "tok")), \
             patch.object(ac, "_cloud_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("连不上"))):
            d3 = client.get("/api/admin/cookies").json()
        check("云端不可达时回退本机", d3.get("source") == "local", d3.get("source"))
        check("回退要说清楚（不能让人以为看的是云端）",
              "回退" in (d3.get("source_label") or "") and "连不上" in (d3.get("cloud_error") or ""),
              {"label": d3.get("source_label"), "err": d3.get("cloud_error")})

        # 强制 cloud 且不可达 → 直接报错，不要偷偷给本机数据
        with patch.object(ac, "_cloud_cfg", lambda: ("http://cloud.test", "tok")), \
             patch.object(ac, "_cloud_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("连不上"))):
            d4 = client.get("/api/admin/cookies?source=cloud").json()
        check("source=cloud 且不可达 → ok=false（不伪装成功）", d4.get("ok") is False, d4)

        # 注入：云端失败时**不能**退而写本机（写了网页版也用不到，还会让人误以为成功）
        with patch.object(ac, "_cloud_cfg", lambda: ("http://cloud.test", "tok")), \
             patch.object(ac, "_cloud_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("连不上"))), \
             patch.object(cp, "verify_cookie", lambda d, h: True), \
             patch.object(cp, "add_cookie", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该写本机"))):
            r = client.post("/api/admin/cookies/inject",
                            json={"domain": "bilibili.com", "header": "SESSDATA=x; DedeUserID=1"})
        check("云端注入失败 → 中止，不偷偷写本机", r.json().get("ok") is False, r.json())


def case_pool_admin_token() -> None:
    print("\n[9] 云端入口 /api/cookie/pool-admin 只认共享令牌")
    now = int(time.time())
    with _pool({"bilibili.com": [_item("SESSDATA=a; DedeUserID=1", now)]}) as (_pool_dir, client):
        with patch.dict("os.environ", {"VDL_COOKIE_SYNC_TOKEN": "s3cret"}):
            bad = client.post("/api/cookie/pool-admin", json={"token": "nope", "action": "overview"})
            check("错令牌 → 403", bad.status_code == 403, bad.status_code)
            none = client.post("/api/cookie/pool-admin", json={"action": "overview"})
            check("不带令牌 → 403", none.status_code == 403, none.status_code)

            ok = client.post("/api/cookie/pool-admin", json={"token": "s3cret", "action": "overview"})
            check("正确令牌 → 200", ok.status_code == 200, ok.status_code)
            body = ok.json()
            check("返回概览结构", body.get("ok") is True and body.get("domains"), list(body)[:6])
            check("不含 Cookie 明文",
                  "SESSDATA=a" not in json.dumps(body, ensure_ascii=False), "leak")

            # 令牌校验必须在动作分发之前：错令牌不得触发删除
            with patch.object(ac, "_do_delete", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该执行"))):
                deny = client.post("/api/cookie/pool-admin",
                                   json={"token": "x", "action": "delete", "domain": "bilibili.com", "ids": ["a"]})
            check("错令牌连删除都不执行", deny.status_code == 403, deny.status_code)


def test_admin_cookies_panel() -> None:
    case_auth_required()
    case_overview()
    case_cache_overrides()
    case_unknown_keeps_hint()
    case_verify_endpoint()
    case_inject()
    case_delete()
    case_normalize()
    case_ui_wiring()
    case_cloud_proxy()
    case_pool_admin_token()
    print("\n" + ("✅ 全部通过" if not _FAILED else "❌ 有失败：%d 项" % len(_FAILED)))
    for n in _FAILED:
        print("   - " + n)
    assert not _FAILED, _FAILED


if __name__ == "__main__":
    test_admin_cookies_panel()
