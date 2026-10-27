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


def case_baidu_identity_distinguishes_accounts(tmp: str) -> None:
    m = _load(tmp)
    m.verify_cookie = lambda domain, header: True
    acct_a = "BDUSS=AAAA1111; STOKEN=sa; PTOKEN=pa"
    acct_b = "BDUSS=BBBB2222; STOKEN=sb; PTOKEN=pb"
    check("不同百度账号 BDUSS => 身份不同",
          m._identity("baidu.com", acct_a) != m._identity("baidu.com", acct_b))
    check("百度账号身份基于 BDUSS（非空）", m._identity("baidu.com", acct_a) is not None)
    m.add_cookie("baidu.com", acct_a, source="admin")
    m.add_cookie("baidu.com", acct_b, source="sync")
    cookies = m.get_cookies("baidu.com") or []
    check("两个不同百度账号都保留（不塌缩成 1）", len(cookies) == 2)
    bduss = set()
    for h in cookies:
        for p in h.split(";"):
            k, _, v = p.partition("=")
            if k.strip() == "BDUSS":
                bduss.add(v.strip())
    check("保留的两个账号 BDUSS 各异", bduss == {"AAAA1111", "BBBB2222"})


def case_baidu_member_first_ordering(tmp: str) -> None:
    """百度网盘 2026-10-28：换直链只有会员号（vip_type>0）才回明文直链，
    所以 get_cookies 必须「会员号优先、非会员号垫后」，与 ts 新旧无关；
    add_cookie 入池时要按 vip_type 打 member 标记并落盘。"""
    import json
    import time
    m = _load(tmp)
    now = int(time.time())

    # 1) 直接写池文件：会员号（member=True，ts 较旧）与非会员号（无标记，ts 较新）。
    #    无论 ts 新旧，会员号必须排在最前，非会员号仍保留。
    entries = [
        {"header": "BDUSS=NONMEMBER; STOKEN=s1", "ts": now, "source": "sync"},
        {"header": "BDUSS=MEMBEROLD; STOKEN=s2", "ts": now - 100, "source": "admin", "member": True},
    ]
    m._pool_file("baidu.com").write_text(json.dumps({"cookies": entries}, ensure_ascii=False))
    out = m.get_cookies("baidu.com") or []
    check("百度：会员号排在非会员号前面（无视 ts）",
          out and "MEMBEROLD" in out[0] and "NONMEMBER" in out[1])
    check("百度：非会员号仍被保留", len(out) == 2)

    # 2) 非会员号更新、会员号更旧，仍应会员优先。
    entries2 = [
        {"header": "BDUSS=NONNEW; STOKEN=s3", "ts": now, "source": "sync"},
        {"header": "BDUSS=MEMBEROLD2; STOKEN=s4", "ts": now - 500, "source": "admin", "member": True},
    ]
    m._pool_file("baidu.com").write_text(json.dumps({"cookies": entries2}, ensure_ascii=False))
    out2 = m.get_cookies("baidu.com") or []
    check("百度：会员号旧于非会员号时仍排在最前",
          out2 and "MEMBEROLD2" in out2[0] and "NONNEW" in out2[1])

    # 3) add_cookie 入池时按 vip_type 打 member 标记并落盘。
    m.verify_cookie = lambda domain, header: True

    def fake_probe(header):
        return (True, {"vip_type": 2 if "MEM" in header else 0})

    m.baidu_probe = fake_probe
    m.add_cookie("baidu.com", "BDUSS=MEMNEW; STOKEN=s5", source="admin")
    m.add_cookie("baidu.com", "BDUSS=NONNEW2; STOKEN=s6", source="sync")
    data = json.loads(m._pool_file("baidu.com").read_text())["cookies"]
    flag = {}
    for e in data:
        key = "MEMNEW" if "MEMNEW" in e["header"] else "NONNEW2"
        flag[key] = e.get("member", False)
    check("add_cookie 给会员号打 member=True", flag.get("MEMNEW") is True)
    check("add_cookie 给非会员号不打 member", flag.get("NONNEW2") is False)
    out3 = m.get_cookies("baidu.com") or []
    check("add_cookie 入池后会员号仍排在前", out3 and "MEMNEW" in out3[0])


def main() -> int:
    print("== test_cookie_pool_identity ==")
    tmpdir = tempfile.mkdtemp(prefix="vdl_pool_test_")
    try:
        case_identity_and_session_grouping(tmpdir)
        shutil.rmtree(tmpdir, ignore_errors=True)

        for fn in (case_add_rejects_logged_out, case_add_keeps_only_newest_per_account,
                   case_add_keeps_multiple_accounts, case_prune_verifies_once_per_session,
                   case_verify_dispatch_prefers_bilibili_probe,
                   case_baidu_identity_distinguishes_accounts,
                   case_baidu_member_first_ordering):
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
