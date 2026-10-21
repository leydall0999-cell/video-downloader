#!/usr/bin/env python3
"""Cookie 池「未校验」治理：新快照继承同会话结论（2026-10-21）。

## 背景（线上取证）

后台 Cookie 池页面长期挂着一批「未校验」，用户问「这些怎么校验」。查线上
`cookie_pool_verify.json` 后发现不是「没点校验」，而是**校验之后又同步进来了
新快照**：

| 域 | 快照 | 状态 |
|---|---|---|
| xiaohongshu.com | 3 份 | 旧 2 份 valid，**最新那份 unknown**（页面按最新显示 ⇒ 未校验 3）|
| youtube.com | 3 份 | 3 份全 unknown，`verified_ts=0`，3 个 ts 全部晚于缓存 ts |

原因：浏览器每同步一次就多一份快照（Cookie 内容/时间不同、会话相同），而校验
缓存按 `entry_id` 存 ⇒ 新快照必然 miss。运营每看到一次同步就以为要多点一次校验，
而点完 2 分钟后又会有新快照进来。

## 改法

`_read_domain` 增加第二阶段：没有缓存结论的条目，若**同一 `_session_key`**
（身份 + 会话字段哈希）存在带 `verified_ts` 的结论，则继承该结论并标
`inherited=True`（detail 写明「同会话 X 分钟前已校验」）。

同时修一个 key 归一 bug：`_do_inject` 用请求里的原域名入缓存，而概览用根域读
（管理员粘贴常带 `www.`），导致刚注入的条目也显示「未校验」。

## 锁什么

1. 同会话新快照 → 继承 valid（不再显示「未校验」）；
2. 不同会话（换账号/SESSDATA 变了）→ **不继承**，仍 unknown（诚实）；
3. 静态已判 invalid（缺登录字段）→ 不被继承覆盖成 valid；
4. `www.` 域注入 → 缓存 key 落在根域，概览能立刻命中；
5. 真跑一次校验后 `inherited` 归 False（继承只是过渡态，不冒充「已验」）。

运行：cd server && VDL_CLOUD_LINK=0 python tests/test_cookie_verify_inherit.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

# 隔离 + 不联网：必须在 import 之前设好
os.environ["VDL_DATA_DIR"] = tempfile.mkdtemp(prefix="vdl_test_verify_inherit_")
os.environ["VDL_CLOUD_LINK"] = "0"

import cookie_pool as cp  # noqa: E402
import routers.admin_cookies as A  # noqa: E402

_FAILED: list[str] = []


def check(name: str, cond: bool, extra=None) -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else f"  → {extra!r}"))
    if not cond:
        _FAILED.append(name)


# bilibili 的会话字段是 (SESSDATA, bili_jct)：只改 buvid 等设备字段 ⇒ 同会话。
_OLD = "SESSDATA=sess-abc; bili_jct=jct-abc; buvid3=old-device"
_NEW = "SESSDATA=sess-abc; bili_jct=jct-abc; buvid3=new-device"
_OTHER = "SESSDATA=sess-zzz; bili_jct=jct-zzz; buvid3=other-device"
_GUEST = "buvid3=guest-device; buvid4=guest"


def _seed(cache_items: dict | None = None) -> None:
    """清空池与缓存，用最少的真实入池路径造数据。"""
    for f in Path(cp._POOL_DIR).glob("*.json"):
        f.unlink()
    A._save_cache(dict(cache_items or {}), int(time.time()))


def test_inherit_same_session() -> None:
    print("[1] 同会话新快照 ⇒ 继承已校验结论（不再挂「未校验」）")
    orig = cp.verify_cookie
    cp.verify_cookie = lambda d, h: True  # type: ignore[assignment]
    try:
        _seed()
        assert cp.add_cookie("bilibili.com", _OLD, source="sync")
        # add_cookie 的 ts 精度是「秒」：同秒写入两份快照会让「最新一份」
        # 退化成文件顺序（旧的在前），测不出继承。线上两份相差几分钟，这里等 1 秒。
        time.sleep(1.05)
        assert cp.add_cookie("bilibili.com", _NEW, source="sync")
        now = time.time()
        # 只给「旧那份」写结论：这正是线上出现「新快照未校验」的形态
        cache = {f"bilibili.com|{A._entry_id(_OLD)}": {
            "status": "valid", "detail": "站点确认已登录",
            "probe": A._PROBE_NET, "ts": int(now) - 120,
        }}
        A._save_cache(cache, int(now) - 120)
        row = A._read_domain("bilibili.com", now, A._load_cache()["items"])
        latest = row["items"][0]
        check("最新快照状态 = valid", latest["status"] == "valid", latest["status"])
        check("标记 inherited=True", latest["inherited"] is True, latest.get("inherited"))
        check("detail 写明同会话来源", "同会话" in str(latest["detail"]), latest["detail"])
        check("账号未校验计数归零", row["unknown"] == 0, row["unknown"])
        check("账号有效计数 = 2 份", row["valid"] == 2, row["valid"])
    finally:
        cp.verify_cookie = orig  # type: ignore[assignment]


def test_no_inherit_other_session() -> None:
    print("[2] 不同会话（SESSDATA 变了）⇒ 不继承，保持 unknown")
    orig = cp.verify_cookie
    cp.verify_cookie = lambda d, h: True  # type: ignore[assignment]
    try:
        _seed({f"bilibili.com|{A._entry_id(_OLD)}": {
            "status": "valid", "detail": "站点确认已登录",
            "probe": A._PROBE_NET, "ts": int(time.time()) - 120,
        }})
        assert cp.add_cookie("bilibili.com", _OTHER, source="sync")
        now = time.time()
        row = A._read_domain("bilibili.com", now, A._load_cache()["items"])
        item = row["items"][0]
        check("换会话的新快照不被继承", item["status"] == "unknown", item["status"])
        check("inherited 仍为 False", item["inherited"] is False, item.get("inherited"))
    finally:
        cp.verify_cookie = orig  # type: ignore[assignment]


def test_no_inherit_guest() -> None:
    print("[3] 缺登录字段（静态 invalid）⇒ 不被继承覆盖")
    orig = cp.verify_cookie
    cp.verify_cookie = lambda d, h: True  # type: ignore[assignment]
    try:
        _seed({f"bilibili.com|{A._entry_id(_OLD)}": {
            "status": "valid", "detail": "站点确认已登录",
            "probe": A._PROBE_NET, "ts": int(time.time()) - 60,
        }})
        assert cp.add_cookie("bilibili.com", _GUEST, source="sync")
        now = time.time()
        row = A._read_domain("bilibili.com", now, A._load_cache()["items"])
        guest = [x for x in row["items"] if x["parts"] == 2][0]
        check("游客快照仍判 invalid", guest["status"] == "invalid", guest["status"])
        check("游客 inherit 标记为 False", guest["inherited"] is False, guest.get("inherited"))
    finally:
        cp.verify_cookie = orig  # type: ignore[assignment]


def test_inject_key_normalized() -> None:
    print("[4] www. 域注入 ⇒ 缓存 key 归一，概览立刻命中")

    class _Req:
        class client:  # noqa: N801
            host = "127.0.0.1"

    orig = cp.verify_cookie
    cp.verify_cookie = lambda d, h: True  # type: ignore[assignment]
    orig_probe = A._probe
    # 注入走的是 _probe（B 站会真联网打 nav），这里换成固定结论，只验 key 归一
    A._probe = lambda d, h: {"status": "valid", "detail": "站点确认已登录",  # type: ignore[assignment]
                             "probe": A._PROBE_NET}
    try:
        _seed()
        # 直接调内部动作（绕开 require_admin）：验的是 key 归一，不是鉴权
        res = A._do_inject("www.bilibili.com", _OLD)
        check("注入成功", res.get("ok") is True, res)
        items = A._load_cache()["items"]
        check("缓存 key 落在根域",
              f"bilibili.com|{A._entry_id(_OLD)}" in items, list(items))
        now = time.time()
        row = A._read_domain("bilibili.com", now, items)
        check("概览读到结论（不是未校验）",
              row["items"] and row["items"][0]["status"] == "valid",
              row["items"][0]["status"] if row["items"] else None)
    finally:
        cp.verify_cookie = orig  # type: ignore[assignment]
        A._probe = orig_probe  # type: ignore[assignment]


def test_verify_overrides_inherited() -> None:
    print("[5] 真跑一次校验 ⇒ inherited 归 False（继承只是过渡态）")
    orig = cp.verify_cookie
    cp.verify_cookie = lambda d, h: True  # type: ignore[assignment]
    orig_probe = A._probe
    try:
        _seed()
        assert cp.add_cookie("bilibili.com", _OLD, source="sync")
        # add_cookie 的 ts 精度是「秒」：同秒写入两份快照会让「最新一份」
        # 退化成文件顺序（旧的在前），测不出继承。线上两份相差几分钟，这里等 1 秒。
        time.sleep(1.05)
        assert cp.add_cookie("bilibili.com", _NEW, source="sync")
        now = time.time()
        A._save_cache({f"bilibili.com|{A._entry_id(_OLD)}": {
            "status": "valid", "detail": "站点确认已登录",
            "probe": A._PROBE_NET, "ts": int(now) - 300,
        }}, int(now) - 300)
        inherited = A._read_domain("bilibili.com", now, A._load_cache()["items"])["items"][0]
        check("前置：新快照是继承态", inherited["inherited"] is True, inherited.get("inherited"))

        A._probe = lambda d, h: {"status": "valid", "detail": "站点确认已登录",  # type: ignore[assignment]
                                 "probe": A._PROBE_NET}
        r = A._do_verify("bilibili.com")
        check("校验真的打了 1 个会话", r.get("checked") == 1, r)
        now2 = time.time()
        after = A._read_domain("bilibili.com", now2, A._load_cache()["items"])["items"][0]
        check("校验后 inherited=False", after["inherited"] is False, after.get("inherited"))
        check("校验后 verified_ts 是本次", after["verified_ts"] >= int(now) - 2,
              after["verified_ts"])
    finally:
        cp.verify_cookie = orig  # type: ignore[assignment]
        A._probe = orig_probe  # type: ignore[assignment]


def test_session_key_failure_is_safe() -> None:
    print("[6] _session_key 抛异常 ⇒ 概览不 500，且不误继承")
    orig = cp.verify_cookie
    orig_sk = cp._session_key
    cp.verify_cookie = lambda d, h: True  # type: ignore[assignment]
    try:
        _seed({f"bilibili.com|{A._entry_id(_OLD)}": {
            "status": "valid", "detail": "站点确认已登录",
            "probe": A._PROBE_NET, "ts": int(time.time()) - 60,
        }})
        assert cp.add_cookie("bilibili.com", _NEW, source="sync")

        def _boom(host, header):  # noqa: ANN001
            raise ValueError("bad cookie")

        cp._session_key = _boom  # type: ignore[assignment]
        now = time.time()
        row = A._read_domain("bilibili.com", now, A._load_cache()["items"])
        item = row["items"][0]
        check("概览没抛异常", isinstance(row, dict) and "items" in row, type(row))
        check("异常条目不继承", item["inherited"] is False, item.get("inherited"))
        check("退化状态仍是 unknown", item["status"] == "unknown", item["status"])
    finally:
        cp.verify_cookie = orig  # type: ignore[assignment]
        cp._session_key = orig_sk  # type: ignore[assignment]


def main() -> int:
    test_inherit_same_session()
    test_no_inherit_other_session()
    test_no_inherit_guest()
    test_inject_key_normalized()
    test_verify_overrides_inherited()
    test_session_key_failure_is_safe()
    print()
    if _FAILED:
        print(f"❌ {len(_FAILED)} 项失败：" + "；".join(_FAILED))
        return 1
    print("🎉 未校验治理通过（同会话继承 / 换会话不继承 / 游客不被覆盖 / key 归一）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
