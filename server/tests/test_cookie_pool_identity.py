"""cookie_pool 身份识别 / 入池验真 / 会话级裁剪 守卫（2026-10-18）。

覆盖的真实故障：
  B 站公共池累计 1905 条，DedeUserID 却只有 1 个；其中 1902 条是服务器无头
  浏览器每 20 分钟抓到的**未登录**游客快照（SESSDATA 已失效），而 get_cookie
  取「最新一条」⇒ 解析永远拿到废 Cookie，恒 412 靠 yt-dlp 兜底。
本用例用假 Cookie 复现三种必须被拦住的行为：
  1. 未登录 Cookie 不得入池（验真为 False 直接拒）；
  2. 同一账号反复抓取只留最近 N 条（按身份，不按整条哈希）；
  3. 剔除失效会话时按「会话变体」验真一次，绝不能逐条打 1905 次。
全程离线：verify_cookie 被替换，不碰网络。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_FAILED: list[str] = []


def check(name: str, cond: bool) -> None:
    print(("  ✓ " if cond else "  ❌ ") + name)
    if not cond:
        _FAILED.append(name)


def _load(pool_dir: str):
    """按指定池目录重新加载模块（_POOL_DIR 是模块级常量，必须在 import 前设 env）。"""
    os.environ["VDL_COOKIE_POOL_DIR"] = pool_dir
    os.environ.pop("VDL_COOKIE_ENC_KEY", None)   # 明文存储，便于断言
    spec = importlib.util.spec_from_file_location("cookie_pool_under_test",
                                                  _SERVER / "cookie_pool.py")
    m = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_SERVER))
    try:
        spec.loader.exec_module(m)
    finally:
        sys.path.pop(0)
    return m


def _bili(uid: str, sess: str, buvid: str) -> str:
    return ("SESSDATA=%s; bili_jct=jct-%s; DedeUserID=%s; buvid3=%s"
            % (sess, sess, uid, buvid))


def case_identity_and_session_grouping(tmp: str) -> None:
    m = _load(tmp)
    a = _bili("2092800535", "S1", "buvid-1")
    b = _bili("2092800535", "S1", "buvid-2")   # 同账号同会话、buvid 变了
    c = _bili("2092800535", "S2", "buvid-3")   # 同账号、会话变了
    d = _bili("9999999", "S9", "buvid-4")      # 另一个账号

    check("同一账号 buvid 变化 => 身份相同", m._identity("bilibili.com", a) == m._identity("bilibili.com", b))
    check("不同账号 => 身份不同", m._identity("bilibili.com", a) != m._identity("bilibili.com", d))
    check("同账号不同会话 => 会话变体不同", m._session_key("bilibili.com", a) != m._session_key("bilibili.com", c))
    check("同账号同会话 => 会话变体相同", m._session_key("bilibili.com", a) == m._session_key("bilibili.com", b))
    check("YouTube 也能识别身份", m._identity("youtube.com", "SID=x; SAPISID=sap; SSID=ss; HSID=hs") is not None)


def case_add_rejects_logged_out(tmp: str) -> None:
    m = _load(tmp)
    calls = {"n": 0}

    def fake_verify(domain, header):
        calls["n"] += 1
        return False                      # 站点明确回答「未登录」

    m.verify_cookie = fake_verify
    ok = m.add_cookie("bilibili.com", _bili("2092800535", "dead", "buvid-x"), source="vps-pull")
    check("未登录 Cookie 被拒绝入池", ok is False)
    check("拒绝前确实验过真", calls["n"] == 1)
    check("拒绝后池文件不存在/为空", not m._pool_file("bilibili.com").exists()
          or not m.get_cookie("bilibili.com"))


def case_add_keeps_only_newest_per_account(tmp: str) -> None:
    m = _load(tmp)
    m.verify_cookie = lambda domain, header: True
    for i in range(10):                  # 同一账号 10 次抓取（buvid 每次都变）
        m.add_cookie("bilibili.com", _bili("2092800535", "S1", "buvid-%d" % i), source="vps-pull")
    data = m._pool_file("bilibili.com")
    import json
    entries = json.loads(data.read_text())["cookies"]
    check("同账号 10 条 => 只留 %d 条" % m._MAX_PER_IDENTITY, len(entries) == m._MAX_PER_IDENTITY)
    ts = [e["ts"] for e in entries]
    check("保留的是最新几条（ts 递增、无旧条目）", ts == sorted(ts) and max(ts) >= min(ts))
    check("条目记录了身份标识", all(e.get("id") for e in entries))
    latest = m.get_cookie("bilibili.com") or ""
    check("get_cookie 取到的是最新那条（含最大 buvid 序号）", "buvid-9" in latest)


def case_add_keeps_multiple_accounts(tmp: str) -> None:
    m = _load(tmp)
    m.verify_cookie = lambda domain, header: True
    for uid in ("1001", "1002", "1003"):
        m.add_cookie("bilibili.com", _bili(uid, "S-" + uid, "b-" + uid), source="sync")
    check("三个不同账号都被保留",
          sum(1 for _ in __import__("json").loads(m._pool_file("bilibili.com").read_text())["cookies"]) == 3)


def case_prune_verifies_once_per_session(tmp: str) -> None:
    m = _load(tmp)
    import json
    import time
    now = int(time.time())
    # 200 条同会话（全死）+ 3 条另一会话（活），ts 必须在 TTL 内
    entries = []
    for i in range(200):
        entries.append({"header": _bili("2092800535", "DEAD", "b%d" % i),
                        "ts": now - 200 + i, "source": "vps-pull"})
    for i in range(3):
        entries.append({"header": _bili("2092800535", "ALIVE", "a%d" % i),
                        "ts": now - 100 + i, "source": "sync"})
    m._pool_file("bilibili.com").write_text(json.dumps({"cookies": entries}, ensure_ascii=False))

    seen: list[str] = []

    def fake_verify(domain, header):
        seen.append(header)
        return "ALIVE" in header

    m.verify_cookie = fake_verify
    left = m._prune_one("bilibili.com")
    check("失效会话整组被剔除、有效会话保留", left == 3)
    check("验真次数 = 会话变体数(2)，不是条目数(203)：" + str(len(seen)), len(seen) == 2)
    rest = json.loads(m._pool_file("bilibili.com").read_text())["cookies"]
    check("剩下的全是有效会话", all("ALIVE" in e["header"] for e in rest))


def case_verify_dispatch_prefers_bilibili_probe(tmp: str) -> None:
    m = _load(tmp)
    hit = {"n": 0}

    def fake_bili(header):
        hit["n"] += 1
        return True

    m.verify_bilibili = fake_bili
    check("B 站走专属验真（不是 yt-dlp 通用兜底）",
          m.verify_cookie("bilibili.com", "SESSDATA=x") is True and hit["n"] == 1)
    check("探针占位 RELAYTEST 被判无效",
          m.verify_cookie("youtube.com", "SID=RELAYTEST; HSID=RELAYTEST") is False)


def case_structural_check_login_fields(tmp: str) -> None:
    """守卫 _STRUCTURAL_FIELDS 的新增登录态字段（2026-10-20）。

    抖音/快手/TikTok/小红书没有联网探针，verify_cookie 在旧实现里会落到
    _verify_generic（yt-dlp 试解析），机房易被风控误判 False，把有效 Cookie
    拒之池外。改为命中白名单站「登录后才存在」的字段即判结构有效（True）。
    本用例确保：① 各新增域的登录字段确实命中 True；② 小红书 a1 这类
    游客也有字段单独出现时仍返回 None（绝不能当登录态）。
    """
    m = _load(tmp)
    # 抖音：任一登录字段命中即 True
    check("抖音 sessionid 命中结构校验",
          m._structural_check("douyin.com", "sessionid=abc") is True)
    check("抖音 sid_tt 命中结构校验",
          m._structural_check("douyin.com", "sid_tt=abc") is True)
    check("抖音 passport_csrf_token 命中结构校验",
          m._structural_check("douyin.com", "passport_csrf_token=abc") is True)
    check("抖音分享子域 iesdouyin 同样命中",
          m._structural_check("iesdouyin.com", "sessionid=abc") is True)
    # 快手
    check("快手 passToken 命中结构校验",
          m._structural_check("kuaishou.com", "passToken=abc") is True)
    check("快手 userId 命中结构校验",
          m._structural_check("kuaishou.com", "userId=abc") is True)
    check("快手 chenzhongtech 子域 userId 命中",
          m._structural_check("chenzhongtech.com", "userId=abc") is True)
    # TikTok
    check("TikTok sessionid 命中结构校验",
          m._structural_check("tiktok.com", "sessionid=abc") is True)
    # 小红书：web_session 是登录态，a1 只是游客也有
    check("小红书 web_session 命中结构校验",
          m._structural_check("xiaohongshu.com", "web_session=abc") is True)
    check("小红书仅 a1（游客）=> None，不当登录态",
          m._structural_check("xiaohongshu.com", "a1=guest; webId=xyz") is None)
    # 非白名单域仍返回 None（交给通用验真）
    check("非结构白名单域返回 None",
          m._structural_check("example.com", "foo=bar") is None)
    # 缺字段的已配置域也返回 None（游客态，不误判 False）
    check("抖音缺登录字段返回 None（游客态）",
          m._structural_check("douyin.com", "odin_tt=abc; ttwid=xyz") is None)


def case_baidu_member_first_ordering(tmp: str) -> None:
    """会员优先排序守卫（2026-10-28）。

    之前的真 bug：百度网盘按账号等级分流（会员明文直链、非会员 AES 密文），
    但 cookie 池没给百度登记身份字段 ⇒ 多账号被去重塌缩成「anon」单条，
    且永远取最新一条——若最新是非会员号，会员号被静默丢弃，解析必报红条。
    修复：① 百度按 BDUSS 区分账号（多号不再塌缩）；② 入池时探 vip_type 标 member；
    ③ get_cookies 把会员号排在最前。本用例锁住这三件事。
    """
    m = _load(tmp)
    m.verify_cookie = lambda domain, header: True   # 跳过站点验真，专注排序逻辑
    # 入池时 baidu_probe 探会员身份：按 BDUSS 前缀区分
    def fake_probe(header):
        if "BDUSS=M" in header:
            return (True, {"errno": 0, "vip_type": 2})   # 会员(SVIP)
        return (True, {"errno": 0, "vip_type": 0})        # 非会员
    m.baidu_probe = fake_probe
    non_member = "BDUSS=N1; STOKEN=n; SCRC=n"   # 非会员，先入池
    member = "BDUSS=M1; STOKEN=m; SCRC=m"       # 会员，后入池
    m.add_cookie("baidu.com", non_member, source="sync")
    m.add_cookie("baidu.com", member, source="sync")
    # 两个账号 BDUSS 不同 ⇒ 身份不同 ⇒ 不会被去重塌缩成单条
    pool = m.get_cookies("pan.baidu.com")
    check("百度两个账号都被保留（未塌缩）", len(pool) == 2)
    # 无论入池先后，会员号必须排在最前
    check("会员号排在最前（非会员不抢跑）", "BDUSS=M" in pool[0] and "BDUSS=N" in pool[1])
    # member 标记已随条目落盘（_save 白名单必须保留它）
    import json
    entries = json.loads(m._pool_file("baidu.com").read_text())["cookies"]
    m_entries = [e for e in entries if "BDUSS=M" in (e.get("header") or "")]
    check("会员条目落盘 member=True", any(e.get("member") is True for e in m_entries))
    check("非会员条目 member 非 True", all(e.get("member") is not True
          for e in entries if "BDUSS=N" in (e.get("header") or "")))


def case_baidu_same_bduss_distinct_stoken(tmp: str) -> None:
    """同 BDUSS、不同 STOKEN 必须留成两条（2026-10-28 真 bug 守卫）。

    之前的真 bug：`_SESSION_FIELDS` 没登记 baidu.com ⇒ 同 BDUSS 的两个快照被
    判成同一会话、只留最新那条；而实测里**最新的那条恰好只拿到密文**
    （4248 字节），旧的完整快照才拿得到明文直链 ⇒ 解析必报统一文案。
    修复：把 STOKEN 登记成百度的会话字段。
    """
    m = _load(tmp)
    m.verify_cookie = lambda domain, header: True
    m.baidu_probe = lambda header: (True, {"errno": 0, "vip_type": 2})
    full = "BDUSS=SAME; STOKEN=aaa; PANPSC=1; pcsett=1"
    partial = "BDUSS=SAME; STOKEN=bbb"
    m.add_cookie("baidu.com", full, source="cache")
    m.add_cookie("baidu.com", partial, source="browser")
    pool = m.get_cookies("pan.baidu.com")
    check("同 BDUSS 不同 STOKEN => 两条都保留", len(pool) == 2)
    check("两条 STOKEN 都在池里",
          any("STOKEN=aaa" in h for h in pool) and any("STOKEN=bbb" in h for h in pool))
    check("同 BDUSS 不同 STOKEN => 会话变体不同",
          m._session_key("baidu.com", full) != m._session_key("baidu.com", partial))
    check("同 BDUSS 同 STOKEN => 会话变体相同",
          m._session_key("baidu.com", partial)
          == m._session_key("baidu.com", "BDUSS=SAME; STOKEN=bbb; X=1"))


def main() -> int:
    print("== test_cookie_pool_identity ==")
    tmpdir = tempfile.mkdtemp(prefix="vdl_pool_test_")
    try:
        case_identity_and_session_grouping(tmpdir)
        shutil.rmtree(tmpdir, ignore_errors=True)

        for fn in (case_add_rejects_logged_out, case_add_keeps_only_newest_per_account,
                   case_add_keeps_multiple_accounts, case_prune_verifies_once_per_session,
                   case_verify_dispatch_prefers_bilibili_probe,
                   case_structural_check_login_fields,
                   case_baidu_member_first_ordering,
                   case_baidu_same_bduss_distinct_stoken):
            d = tempfile.mkdtemp(prefix="vdl_pool_test_")
            try:
                fn(d)
            finally:
                shutil.rmtree(d, ignore_errors=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    if _FAILED:
        print("\n失败 %d 项：%s" % (len(_FAILED), "; ".join(_FAILED)))
        return 1
    print("\n全部通过")
    return 0


def test_cookie_pool_identity() -> None:
    """pytest 入口：整套 case 必须全绿（run_pytest_root 收集的就是它）。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
