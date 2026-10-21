#!/usr/bin/env python3
"""抖音 / 快手 的真验真探针（2026-10-21 实测突破）。

## 为什么之前是「未校验」

`test_cookie_probe_static.py` 那轮的结论是「抖音/快手没有可用登录态接口」，
依据是 `im/user/info` 回 `url doesn't match`、`rest/wd/common/user/info` 回 404。
**这个结论是错的**——换对接口后两个站都能免签名直连判登录态：

| 站 | 可用接口 | 关键判据 |
|---|---|---|
| 抖音 | `GET /aweme/v1/web/user/profile/self/` | `status_code=0`+`user` ⇒ 已登录；`status_code=8`+「用户未登录」⇒ 失效 |
| 快手 | `POST /graphql` `visionProfile` | `userProfile.profile.user_name` 有值 ⇒ 已登录 |

两处都**不需要** a_bogus / `__NS_sig3` 签名，裸请求直接回 JSON。

## 快手为什么必须成对看 result

同一时刻、同一台机、同一份 Cookie 实测三组对照：

    空 Cookie（真匿名）   → result=2,  userProfile=null
    带失效 Cookie         → result=21, userProfile=null   ← 与匿名不同
    已登录                → result=21 不会出现，userProfile 有实体

`2` 与 `21` 的差异是关键证据：它证明服务端**认出了**这份 Cookie 才回 21，
不是「压根没认到」被误读成失效。独立通道
`/pass/kuaishou/login/passToken` 回 `{"result":100110085,"error_msg":"sid不合法"}`
交叉印证。若只发 GraphQL 不看 passToken，服务端一旦改成不认无签名请求，
就会把「探针失效」误判成「用户 Cookie 失效」——这是本测试 [9] 锁的反向风险。

## 锁什么

1. 抖音 8 种响应（登录/未登录/其它码/HTTP 错误/非 JSON）各自的落点；
2. 快手 4 种 result + GraphQL 400（字段改名）的落点；
3. **交叉验证失败不得污染主结论**（[9]）；
4. **离线时不得把「有登录字段」判成无效**（[12]，防止探针不可用就滥发红标）。

运行：cd server && VDL_CLOUD_LINK=0 python tests/test_cookie_probe_douyin_kuaishou.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_test_probe_dyks_")
os.environ["VDL_CLOUD_LINK"] = "0"

import cookie_pool as cp  # noqa: E402
import routers.admin_cookies as A  # noqa: E402

_FAILED: list[str] = []


def check(name: str, cond: bool, extra=None) -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else f"  → {extra!r}"))
    if not cond:
        _FAILED.append(name)


LOGIN_DOUYIN = "sessionid=abc123; sid_tt=abc123; uid_tt=9988; login_time=1786961601363"
LOGIN_KUAISHOU = "userId=4548080451; passToken=tk-7788; kuaishou.server.webday7_st=xx"


class _Resp:
    def __init__(self, code: int, payload=None, text: str | None = None) -> None:
        self.status_code = code
        self._payload = payload
        if text is not None:
            self.text = text
        else:
            self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _Router:
    """按 URL 片段分派假响应；记录调用次数以便断言「有没有真的发出去」。"""

    def __init__(self, routes: dict[str, _Resp], boom: tuple[str, ...] = ()) -> None:
        self._routes = routes
        self._boom = boom
        self.calls: list[str] = []

    def _pick(self, url: str) -> _Resp:
        self.calls.append(url)
        for frag, resp in self._routes.items():
            if frag in url:
                return resp
        for frag in self._boom:
            if frag in url:
                raise ConnectionError("connection reset by peer")
        raise AssertionError("未预期的请求：" + url)

    def get(self, url, *a, **kw):  # noqa: ANN002, ANN003, ANN201
        return self._pick(url)

    def post(self, url, *a, **kw):  # noqa: ANN002, ANN003, ANN201
        return self._pick(url)


def _with(router, fn):
    orig = cp.requests
    cp.requests = router  # type: ignore[assignment]
    try:
        return fn()
    finally:
        cp.requests = orig  # type: ignore[assignment]


def _dy(status_code, user=None, msg="", http=200, raw=None):
    payload = {"status_code": status_code, "status_msg": msg, "user": user}
    if raw is not None:
        return _Resp(http, text=raw)
    return _Resp(http, payload)


def _ks(result, profile=None, http=200):
    return _Resp(http, {"data": {"visionProfile": {
        "result": result,
        "hostName": "public-bjzey-x.kwaidc.com",
        "userProfile": ({"ownerCount": {"fan": 1}, "profile": profile} if profile else None),
    }}})


def test_douyin() -> None:
    print("[1] 抖音：profile_self 的判据")
    r = _with(_Router({"/aweme/v1/web/user/profile/self/": _dy(
        0, {"nickname": "阿甲", "sec_uid": "MS4wLjAB"}),
        "/passport/": _Resp(404, text="")}), lambda: A._probe("douyin.com", LOGIN_DOUYIN))
    check("已登录 → valid", r.get("status") == "valid", r)
    check("detail 带昵称（后台能看到池里是谁的号）", r.get("detail") == "阿甲", r.get("detail"))
    check("标 net（真的连站了）", r.get("probe") == A._PROBE_NET, r.get("probe"))

    r = _with(_Router({"/aweme/v1/web/user/profile/self/": _dy(8, None, "用户未登录")}),
              lambda: A._probe("douyin.com", LOGIN_DOUYIN))
    check("status_code=8 → invalid", r.get("status") == "invalid", r)
    check("detail 点明要在抖音重新登录", "抖音重新登录" in str(r.get("detail")), r.get("detail"))

    r = _with(_Router({"/aweme/v1/web/user/profile/self/": _dy(0, None, "用户未登录")}),
              lambda: A._probe("douyin.com", LOGIN_DOUYIN))
    check("code 非 8 但 msg 含未登录 → invalid（文案兼容）",
          r.get("status") == "invalid", r)

    r = _with(_Router({"/aweme/v1/web/user/profile/self/": _dy(0, None, "success")}),
              lambda: A._probe("douyin.com", LOGIN_DOUYIN))
    check("code=0 但 user 为空 → unknown（不硬判 valid）",
          r.get("status") == "unknown", r)

    r = _with(_Router({"/aweme/v1/web/user/profile/self/": _dy(9999, None, "风控")}),
              lambda: A._probe("douyin.com", LOGIN_DOUYIN))
    check("未知 status_code → unknown", r.get("status") == "unknown", r)

    r = _with(_Router({"/aweme/v1/web/user/profile/self/": _dy(None, http=503)}),
              lambda: A._probe("douyin.com", LOGIN_DOUYIN))
    check("HTTP 503 → unknown（不当成失效）", r.get("status") == "unknown", r)
    check("detail 带 HTTP 码便于排查", "503" in str(r.get("detail")), r.get("detail"))

    r = _with(_Router({"/aweme/v1/web/user/profile/self/": _dy(None, http=200, raw="<html>滑块验证</html>")}),
              lambda: A._probe("douyin.com", LOGIN_DOUYIN))
    check("返回 HTML（风控页）→ unknown", r.get("status") == "unknown", r)


def test_kuaishou() -> None:
    print("[2] 快手：GraphQL visionProfile 的判据")
    ok_profile = {"user_name": "快手小王", "user_id": "4548080451", "headurl": "https://x"}
    router = _Router({
        "/graphql": _ks(1, ok_profile),
        "/pass/kuaishou/login/passToken": _Resp(200, {"result": 1}),
    })
    r = _with(router, lambda: A._probe("kuaishou.com", LOGIN_KUAISHOU))
    check("已登录 → valid", r.get("status") == "valid", r)
    check("detail 带昵称", r.get("detail") == "快手小王", r.get("detail"))
    check("走了 GraphQL", any("graphql" in u for u in router.calls), router.calls)

    router = _Router({
        "/graphql": _ks(21),
        "/pass/kuaishou/login/passToken": _Resp(
            200, {"result": 100110085, "error_msg": "sid不合法"}),
    })
    r = _with(router, lambda: A._probe("kuaishou.com", LOGIN_KUAISHOU))
    check("result=21 → invalid", r.get("status") == "invalid", r)
    check("detail 说明凭据失效", "凭据已失效" in str(r.get("detail")), r.get("detail"))
    check("detail 带 passToken 交叉验证结论",
          "sid 不合法" in str(r.get("detail")), r.get("detail"))
    check("detail 点明要在快手重新登录", "快手重新登录" in str(r.get("detail")), r.get("detail"))

    router = _Router({"/graphql": _ks(2)})
    r = _with(router, lambda: A._probe("kuaishou.com", LOGIN_KUAISHOU))
    check("result=2（真匿名）→ invalid", r.get("status") == "invalid", r)
    check("detail 说明是匿名请求", "匿名" in str(r.get("detail")), r.get("detail"))

    # [9] 关键反向风险：交叉验证请求挂了，不得把主结论丢掉
    router = _Router({"/graphql": _ks(21)}, boom=("/pass/kuaishou/login/passToken",))
    r = _with(router, lambda: A._probe("kuaishou.com", LOGIN_KUAISHOU))
    check("passToken 检查抛异常 → 主结论仍是 invalid", r.get("status") == "invalid", r)
    check("不把交叉验证失败写成「无法确认」",
          "无法联网确认" not in str(r.get("detail")), r.get("detail"))

    r = _with(_Router({"/graphql": _ks(1, None)}), lambda: A._probe("kuaishou.com", LOGIN_KUAISHOU))
    check("result=1 但 userProfile 为空 → 不当成 valid", r.get("status") in ("unknown", "invalid"), r)

    r = _with(_Router({"/graphql": _ks(777)}), lambda: A._probe("kuaishou.com", LOGIN_KUAISHOU))
    check("未知 result → unknown", r.get("status") == "unknown", r)

    r = _with(_Router({"/graphql": _Resp(400, {"errors": [
        {"message": 'Cannot query field "user_name" on type "VisionProfileProfile".'}]})}),
        lambda: A._probe("kuaishou.com", LOGIN_KUAISHOU))
    check("GraphQL 400（schema 改名）→ unknown 而非 invalid",
          r.get("status") == "unknown", r)
    check("detail 带 HTTP 码", "400" in str(r.get("detail")), r.get("detail"))


def test_offline_never_invalid() -> None:
    """离线/无 requests 时必须回落到静态判据，不能因为探针不可用就滥发红标。"""
    print("[3] 探针不可用时：有登录字段不得判 invalid")
    orig_verify = cp.verify_cookie
    orig_req = cp.requests
    cp.verify_cookie = lambda domain, header: None  # type: ignore[assignment]
    cp.requests = None  # type: ignore[assignment]
    try:
        for dom, header in (("douyin.com", LOGIN_DOUYIN),
                            ("kuaishou.com", LOGIN_KUAISHOU)):
            r = A._probe(dom, header)
            check(f"{dom} 离线 + 有登录字段 → unknown", r.get("status") == "unknown", r)
            check(f"{dom} 说明是无公开接口而非已失效",
                  "无公开验真接口" in str(r.get("detail")), r.get("detail"))
    finally:
        cp.verify_cookie = orig_verify  # type: ignore[assignment]
        cp.requests = orig_req  # type: ignore[assignment]


def main() -> int:
    test_douyin()
    test_kuaishou()
    test_offline_never_invalid()
    print()
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败：" + "；".join(_FAILED))
        return 1
    print("🎉 抖音/快手真探针判据全部通过（含交叉验证与离线反向约束）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
