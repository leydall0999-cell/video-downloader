#!/usr/bin/env python3
"""Cookie 验真：无联网接口的站必须用「静态登录字段」给确定结论（2026-10-21）。

## 背景（实测取证，别再「待补探针」地空转）

后台「立即校验」对抖音/快手/小红书/TikTok 曾经是全量返回 unknown，耗时 0.1s——
看着像验过了，实际一条都没联网。查清后发现这些站**不存在可用的轻量登录态接口**：

| 站 | 尝试的接口 | 结果 |
|---|---|---|
| 抖音 | `aweme/v1/web/im/user/info` | 200 但 `"url doesn't match"`（要 a_bogus 签名）|
| 抖音 | `passport/web/account/info` | `error_code=16 该应用无权限` |
| 抖音 | `sso.douyin.com/check_login` | 返回滑块验证页 HTML |
| 小红书 | `edith.../v2/user/me` | **406**（要 x-s 签名）|
| 小红书 | `www.../v1/user/selfinfo` | 500 jarvis-gateway |
| 腾讯视频 | `access.video.qq.com/user/auth_check` | `ret=35004 can not find cmd` |
| 快手 | `rest/wd/common/user/info` | **404** |
| TikTok | `passport/web/account/info` | 国内机房直接连不上（ConnectionError）|

所以当时「加个探针」这条路的结论是：**不可行**，除非上 Playwright（30~50s/条，
不适合做常规校验）。

## 🔴 2026-10-21 后续更正：换对接口就能验（上表只证明「那些接口不行」）

上表的失败**不等于这两个站验不了**。当天晚些时候找到两个**免签名**入口：

| 站 | 可用接口 | 判据 |
|---|---|---|
| 抖音 | `GET /aweme/v1/web/user/profile/self/` | `status_code=8` +「用户未登录」⇒ 失效；`0` + `user` ⇒ 已登录（带 nickname）|
| 快手 | `POST /graphql` `visionProfile` | `userProfile.profile.user_name` 有值 ⇒ 已登录；`result=21` ⇒ 凭据失效 |

线上池实测：抖音 3 份、快手 5 份全部从「未校验」落成「无效」，说明列写明
「需重新登录」。**所以本文件的静态判据现在只是离线兜底**（`cp.requests` 不可用
时才会走到），联网路径由 `test_cookie_probe_douyin_kuaishou.py` 单独锁死。

别再拿本文件的旧结论去回答「抖音/快手验不了」——那已经被证伪了。

## 但「unknown」不等于没结论

登录了必然带 `sessionid` / `web_session` / `passToken` / `uin` / `skey` 之一。
**一个都没有 ⇒ 可确定是游客态**，这是确定性证据，不需要联网、也不该继续挂
「未校验」让运营以为「还能用」。本次改动就是把 `_probe` 的兜底分支接上
`_static_status`，让这类条目直接落 `invalid`。

反过来，有登录字段但无接口可问的，仍必须诚实保持 `unknown`——
2026-10-18 的教训：字段看着像登录态、实际早被服务端吊销的 Cookie 有一大批，
不能靠字段存在就发「有效」。

## 锁什么

1. 抖音/快手/小红书/TikTok 缺登录字段 → `invalid`，detail 点明「可确定未登录」；
2. 同上但含登录字段 → 保持 `unknown`，detail 不再是笼统的「无可用探针」，
   而是「无公开验真接口（需签名/需真实浏览器），无法联网确认」；
3. 已配结构校验表的站（YouTube）**不受影响**——仍走 `_structural_check` 判
   `valid`，且 detail 明确标注「未连站确认」，不得被误降级成 invalid。

运行：cd server && VDL_CLOUD_LINK=0 python tests/test_cookie_probe_static.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

# 隔离 + 不联网：必须在 import 之前设好
os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_test_probe_static_")
os.environ["VDL_CLOUD_LINK"] = "0"

import cookie_pool as cp  # noqa: E402
import routers.admin_cookies as A  # noqa: E402

_FAILED: list[str] = []


def check(name: str, cond: bool, extra=None) -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else f"  → {extra!r}"))
    if not cond:
        _FAILED.append(name)


# 游客态样本：有设备指纹/CSRF，但没有任何「登录后才存在」的字段
GUEST_DOUYIN = "ttwid=1%7Cabc; odin_tt=deadbeef; passport_csrf_token=xyz; msToken=abc"
# 登录态样本：只差一个 sessionid
LOGIN_DOUYIN = GUEST_DOUYIN + "; sessionid=abc123def; sid_tt=abc123def; uid_tt=9988"
LOGIN_KUAISHOU = "kuaishou.server.web_st=xx; userId=123456; passToken=tk-7788"
GUEST_KUAISHOU = "kuaishou.server.web_st=xx; did=web_deadbeef"
LOGIN_XHS = "a1=18abc; webId=deadbeef; web_session=040069aabbcc"
GUEST_TIKTOK = "tt_csrf_token=abc; ttwid=1%7Cabc; msToken=abc; tt_chain_token=xx"


def test_static_verdicts() -> None:
    print("[1] 无联网接口的站：缺登录字段 ⇒ 确定 invalid（不再挂「未校验」）")
    orig = cp.verify_cookie
    orig_req = cp.requests
    # 模拟「该站没有可用探针」+「离线」：yt-dlp 通用验真判不出来，
    # 小红书首页探针也发不出去（它的联网分支由 [2b] 单独覆盖）
    cp.verify_cookie = lambda domain, header: None  # type: ignore[assignment]
    cp.requests = None  # type: ignore[assignment]
    try:
        cases = [
            ("douyin.com", GUEST_DOUYIN),
            ("kuaishou.com", GUEST_KUAISHOU),
            ("tiktok.com", GUEST_TIKTOK),
        ]
        for dom, header in cases:
            r = A._probe(dom, header)
            check(f"{dom} 游客态 → invalid", r.get("status") == "invalid", r)
            check(
                f"{dom} detail 点明可确定未登录",
                "可确定未登录" in str(r.get("detail")),
                r.get("detail"),
            )

        print("[2] 有登录字段但无接口可问 ⇒ 保持 unknown（诚实：没验过就不是无效）")
        for dom, header in (
            ("douyin.com", LOGIN_DOUYIN),
            ("kuaishou.com", LOGIN_KUAISHOU),
            ("xiaohongshu.com", LOGIN_XHS),
        ):
            r = A._probe(dom, header)
            check(f"{dom} 含登录字段 → unknown", r.get("status") == "unknown", r)
            check(
                f"{dom} detail 说清「无公开验真接口」",
                "无公开验真接口" in str(r.get("detail")),
                r.get("detail"),
            )
    finally:
        cp.verify_cookie = orig  # type: ignore[assignment]
        cp.requests = orig_req  # type: ignore[assignment]


class _FakeResp:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _FakeRequests:
    """只实现 get()：小红书探针只用到它。"""

    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.calls = 0

    def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        return self._resp


def test_xhs_probe() -> None:
    """小红书首页探针（2026-10-21 新增）：解析 loggedIn / nickname / redId。

    真实请求在测试里不打（离线套件不许联网），用假响应锁住解析逻辑；
    真假响应样本取自线上实测：游客页 36KB 只有 loggedIn=false，登录页 206KB
    带 loggedIn=true + nickname + redId。
    """
    print("[2b] 小红书首页探针：loggedIn 解析（离线假响应）")
    orig_req = cp.requests
    try:
        # 登录态
        cp.requests = _FakeRequests(_FakeResp(  # type: ignore[assignment]
            200, '{"user":{"loggedIn":true,"nickname":"小红薯测试号","redId":"49729203795"}}'))
        r = A._probe("xiaohongshu.com", LOGIN_XHS)
        check("loggedIn=true → valid", r.get("status") == "valid", r)
        check("detail 带昵称", "小红薯测试号" in str(r.get("detail")), r.get("detail"))
        check("detail 带小红书号", "49729203795" in str(r.get("detail")), r.get("detail"))
        check("probe=net（确实是联网探针）", r.get("probe") == A._PROBE_NET, r.get("probe"))

        # 游客态：这才是最该拦住的情况（Cookie 里塞了假 web_session）
        cp.requests = _FakeRequests(_FakeResp(  # type: ignore[assignment]
            200, '{"user":{"loggedIn":false}}'))
        r = A._probe("xiaohongshu.com", LOGIN_XHS)
        check("loggedIn=false → invalid", r.get("status") == "invalid", r)

        # 页面结构变了：不能假装无效，必须留 unknown 等人工看
        cp.requests = _FakeRequests(_FakeResp(200, "<html>nothing here</html>"))  # type: ignore[assignment]
        r = A._probe("xiaohongshu.com", LOGIN_XHS)
        check("无 loggedIn 标记 → unknown（结构可能已变）", r.get("status") == "unknown", r)

        # 风控/5xx：也必须是 unknown，不能把有效 Cookie 误杀
        cp.requests = _FakeRequests(_FakeResp(461, "{}"))  # type: ignore[assignment]
        r = A._probe("xiaohongshu.com", LOGIN_XHS)
        check("HTTP 461 → unknown（不回 invalid）", r.get("status") == "unknown", r)

        # 探针打的是探索页，不是 edith（edith 要 x-s 签名，实测 406）
        fake = _FakeRequests(_FakeResp(200, '{"loggedIn":true}'))
        cp.requests = fake  # type: ignore[assignment]
        A._probe("xiaohongshu.com", LOGIN_XHS)
        check("探针命中一次", fake.calls == 1, fake.calls)
    finally:
        cp.requests = orig_req  # type: ignore[assignment]


def test_structural_not_downgraded() -> None:
    print("[3] 已配结构校验表的站不受影响（YouTube 有 SID ⇒ valid，且标明未连站确认）")
    header = (
        "SID=g.a000xyz; HSID=At7; SSID=AQ9; APISID=abc/def; "
        "SAPISID=ghi/jkl; __Secure-1PSID=g.a000xyz; LOGIN_INFO=AFmmF2sw"
    )
    r = A._probe("youtube.com", header)
    check("youtube 含 SID → valid", r.get("status") == "valid", r)
    check(
        "youtube detail 标明「未连站确认」",
        "未连站确认" in str(r.get("detail")),
        r.get("detail"),
    )
    check("youtube probe=struct（不许冒充联网验真）", r.get("probe") == A._PROBE_STRUCT, r.get("probe"))

    print("[4] 结构校验表本身没被改坏（qq.com 仍认 vus_session）")
    check(
        "qq.com 含 vus_session 命中结构表",
        cp._structural_check("qq.com", "vus_session=abc; video_platform=2") is True,
    )
    check(
        "qq.com 只有游客字段不命中",
        cp._structural_check("qq.com", "pac_uid=abc; uin_cookie=xx") is None,
    )


def main() -> int:
    test_static_verdicts()
    test_xhs_probe()
    test_structural_not_downgraded()
    print()
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败：" + "；".join(_FAILED))
        return 1
    print("🎉 静态判据全部通过（游客态下 invalid / 有字段保持 unknown / 结构表未受影响）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
