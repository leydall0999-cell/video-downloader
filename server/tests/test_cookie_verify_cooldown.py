"""联网验真冷却（2026-10-21 快手「登着却一直无效」事故的回归锁）。

事故链：云端是机房 IP，用户 session 活在家宽浏览器里。后台每 5~10 分钟自动
全量校验一次 ⇒ 同一 session 在两个 IP 之间高频交替 ⇒ 平台风控作废会话 ⇒
用户重新登录又被踢，永远显示「无效」。

锁四件事：
1. 全量校验：6 小时内已校验过的同一份 Cookie 不再联网（skipped）
2. 全量校验：超过 6 小时会重新联网
3. 手动按域校验：10 分钟内不重复联网，超过则重新验
4. 新同步上来的 Cookie（无缓存结论）一定立刻校验——冷却不能误伤新登录
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routers.admin_cookies as A  # noqa: E402

FAILS: list[str] = []
PASS = 0


def check(cond: bool, label: str) -> None:
    global PASS
    if cond:
        PASS += 1
        print("  ok  ", label)
    else:
        FAILS.append(label)
        print("  FAIL", label)


class Harness:
    """接管联网与缓存，记录「真的打了几次平台」。"""

    def __init__(self, items: dict | None = None):
        self.calls: list[str] = []
        self.items = dict(items or {})
        self.saved: dict = {}
        self._orig = (A._collect_targets, A._probe, A._load_cache, A._save_cache)

    def __enter__(self):
        h = self

        def _collect(now):
            return h.targets

        def _probe(domain, header):
            h.calls.append(domain)
            return {"status": "valid", "detail": "stub", "probe": "net"}

        def _load():
            return {"items": dict(h.items), "ts": int(time.time())}

        def _save(items, ts):
            h.saved = dict(items)
            h.items = dict(items)

        A._collect_targets = _collect
        A._probe = _probe
        A._load_cache = _load
        A._save_cache = _save
        return self

    def __exit__(self, *exc):
        (A._collect_targets, A._probe, A._load_cache, A._save_cache) = self._orig
        return False


def items_for(domain: str, eid: str, age_sec: int) -> dict:
    return {f"{domain}|{eid}": {"status": "invalid", "detail": "旧结论",
                                "probe": "net", "ts": int(time.time()) - age_sec}}


print("[1] 全量校验：6 小时内不重复联网")
targets = [("kuaishou.com", ["eid1"], "passToken=abc; userId=1")]
with Harness(items_for("kuaishou.com", "eid1", 3600)) as h:
    h.targets = targets
    r = A._do_verify()
    check(h.calls == [], "1 小时内已验过 ⇒ 一次都不打平台")
    check(r.get("skipped") == 1, "返回 skipped=1")
    check(r.get("checked") == 0, "返回 checked=0")

print("[2] 全量校验：超过 6 小时重新联网")
with Harness(items_for("kuaishou.com", "eid1", 7 * 3600)) as h:
    h.targets = targets
    r = A._do_verify()
    check(h.calls == ["kuaishou.com"], "7 小时前验过 ⇒ 重新打一次")
    check(r.get("checked") == 1, "返回 checked=1")

print("[3] 手动按域校验：10 分钟冷却")
with Harness(items_for("kuaishou.com", "eid1", 60)) as h:
    h.targets = targets
    r = A._do_verify("kuaishou.com")
    check(h.calls == [], "1 分钟前刚验 ⇒ 手动也不重复打")
    check(r.get("skipped") == 1, "skipped=1")

with Harness(items_for("kuaishou.com", "eid1", 11 * 60)) as h:
    h.targets = targets
    r = A._do_verify("kuaishou.com")
    check(h.calls == ["kuaishou.com"], "11 分钟前验过 ⇒ 手动重新打")
    check(r.get("checked") == 1, "checked=1")

print("[4] 新 Cookie（无缓存）一定立刻校验")
with Harness({}) as h:
    h.targets = [("kuaishou.com", ["new_eid"], "passToken=xyz")]
    r = A._do_verify()
    check(h.calls == ["kuaishou.com"], "没有旧结论 ⇒ 立刻联网验")
    check(r.get("checked") == 1, "checked=1")

print("[5] 混合：一条新鲜、一条陈旧 ⇒ 只打陈旧那条")
mixed = {}
mixed.update(items_for("kuaishou.com", "fresh", 600))
mixed.update(items_for("kuaishou.com", "old", 8 * 3600))
with Harness(mixed) as h:
    h.targets = [("kuaishou.com", ["fresh"], "a=1"),
                 ("kuaishou.com", ["old"], "b=2")]
    r = A._do_verify()
    check(len(h.calls) == 1, "只打一次（陈旧那条）")
    check(r.get("skipped") == 1 and r.get("checked") == 1, "skipped=1 checked=1")

print("[6] 按会话冷却：同一次登录的多份快照共享冷却（eid 每次都变）")
import cookie_pool as cp  # noqa: E402

hdr_a = "passToken=TOKEN_A; userId=4548080451; did=web_x"
sk_a = cp._session_key("kuaishou.com", hdr_a)
old = {"kuaishou.com|snap1": {"status": "invalid", "detail": "旧",
                              "probe": "net", "ts": int(time.time()) - 120,
                              "sk": sk_a}}
with Harness(old) as h:
    # 同步又写了一份新快照（eid 不同，passToken 没变 ⇒ 同一会话）
    h.targets = [("kuaishou.com", ["snap2"], "passToken=TOKEN_A; userId=4548080451; did=web_x; ktrace=zzz")]
    r = A._do_verify()
    check(h.calls == [], "会话没变 ⇒ 冷却命中，不再打平台")
    check(r.get("skipped") == 1, "skipped=1")

print("[7] 重新登录（passToken 变）⇒ 立刻放行，不被冷却误伤")
with Harness(old) as h:
    h.targets = [("kuaishou.com", ["snap3"], "passToken=TOKEN_B_NEW; userId=4548080451")]
    r = A._do_verify()
    check(h.calls == ["kuaishou.com"], "passToken 变了 ⇒ 立刻联网验")
    check(r.get("checked") == 1, "checked=1")

print("[8] 写入结论时记住会话指纹（供下轮冷却用）")
with Harness({}) as h:
    h.targets = [("kuaishou.com", ["e1"], hdr_a)]
    A._do_verify()
    saved = h.saved.get("kuaishou.com|e1") or {}
    check(saved.get("sk") == sk_a, "落盘结论带 sk 字段且等于会话指纹")

print()
if FAILS:
    print("失败", len(FAILS), "项：")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("全部通过：%d 项" % PASS)
