#!/usr/bin/env python3
"""免费额度口径（free_quota.mode）上云 的离线守卫（2026-10-13 接续待办 #2）。

背景：`free_quota.mode`（on/off）此前只存本机 plans.json ⇒ 换机/重装后缓存被重建，
口径回默认 on。修复：把 mode 也下发到授权中心（free_quota_set），并从
`/api/license/plans` 的 free_quota 字段读回。

覆盖：
  ① 授权中心 free_quota_impl：mode 的 设置/清除/不传 三态语义、非法值 400、
     与 cloud_lifetime 互不干扰；plans_payload 带出 free_quota；
  ② 客户端 free_quota_set_remote：只把**提供了的**键放进 payload（不传 ≠ 传 null）；
  ③ 客户端口径优先级：本机 > 云端 > 默认 on；mode=off 经云端也能把免费归 0；
  ④ push_free_quota_to_cloud：把 mode 一起下发，并主动刷新缓存。

零网络、零用户数据；授权中心函数按 AST 抽取执行（不动真实 state）。
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import types
from unittest.mock import patch

SERVER = pathlib.Path(__file__).resolve().parent.parent
REPO = SERVER.parent
sys.path.insert(0, str(SERVER))

import membership  # noqa: E402
import license_client  # noqa: E402

_PASS = _FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {detail}")


# ── ① 授权中心 free_quota_impl（AST 抽取执行）────────────────────────────────
class _ApiError(Exception):
    def __init__(self, status: int, code: str, msg: str = ""):
        super().__init__(msg or code)
        self.status = status
        self.code = code


def _license_ns():
    src = (REPO / "deploy" / "license_server.py").read_text(encoding="utf-8")
    want = ("_free_quota_overrides", "free_quota_impl")
    tree = ast.parse(src)
    fns = {n.name: n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name in want}
    assert set(want) <= set(fns), f"license_server 缺函数: {set(want) - set(fns)}"
    sentinel = object()
    ns = {"Any": object, "dict": dict, "str": str, "int": int,
          "isinstance": isinstance, "TypeError": TypeError, "ValueError": ValueError,
          "_UNSET": sentinel, "ApiError": _ApiError,
          "_save_state": lambda st: None,
          "effective_cloud_limits": lambda st: {}}
    for fn in fns.values():
        exec(compile(ast.Module([fn], []), "license_server_frag", "exec"), ns)
    return ns, sentinel


def test_center_free_quota_mode_semantics() -> None:
    ns, _s = _license_ns()
    fqi = ns["free_quota_impl"]
    fqo = ns["_free_quota_overrides"]

    st: dict = {}
    fqi(st, 1.0, mode="off")
    check("mode=off 落库", fqo(st).get("mode") == "off", json.dumps(fqo(st)))
    fqi(st, 2.0, mode="OFF")          # 大小写归一
    check("mode 归一小写", fqo(st).get("mode") == "off")
    fqi(st, 3.0)                       # 不传 → 不动
    check("不传 mode → 不动（仍是 off）", fqo(st).get("mode") == "off")
    fqi(st, 4.0, mode=None)            # 显式清除
    check("mode=null → 清除覆盖", "mode" not in fqo(st))
    fqi(st, 5.0, mode="on")
    check("mode=on 落库", fqo(st).get("mode") == "on")

    # 非法值 → 400，且不写脏
    before = dict(fqo(st))
    try:
        fqi(st, 6.0, mode="junk")
        check("非法 mode 应拒绝", False, "未抛 ApiError")
    except _ApiError as e:
        check("非法 mode → ApiError 400", e.status == 400, str(e.status))
    check("非法 mode 不写脏", fqo(st) == before)

    # 只传 cloud_lifetime 不得动 mode（互不干扰）
    fqi(st, 7.0, cloud_lifetime=5)
    check("只传 cloud_lifetime → mode 原样保留", fqo(st).get("mode") == "on"
          and fqo(st).get("cloud_lifetime") == 5, json.dumps(fqo(st)))
    # 只传 mode 不得动 cloud_lifetime
    fqi(st, 8.0, mode="off")
    check("只传 mode → cloud_lifetime 原样保留", fqo(st).get("cloud_lifetime") == 5
          and fqo(st).get("mode") == "off", json.dumps(fqo(st)))
    # 同时传：一起落库
    fqi(st, 9.0, cloud_lifetime=None, mode=None)
    check("同时传 null → 两项都清", fqo(st) == {}, json.dumps(fqo(st)))


def test_center_plans_payload_exposes_free_quota() -> None:
    src = (REPO / "deploy" / "license_server.py").read_text(encoding="utf-8")
    check("plans_payload 带出 free_quota 字段", '"free_quota": _free_quota_overrides(st)' in src)
    check("dispatch 把 mode 透传给 free_quota_impl", 'mode=(data["mode"] if "mode" in data else _UNSET)' in src)


# ── ② 客户端 free_quota_set_remote：只发提供了的键 ──────────────────────────
def test_client_payload_omits_unset_keys() -> None:
    cap: dict = {}
    with patch.object(license_client, "_post",
                      lambda path, payload, *a, **k: cap.update(path=path, payload=payload) or {"ok": True}):
        license_client.free_quota_set_remote("tok", mode="off")
        check("只给 mode → payload 只含 token+mode",
              cap["payload"] == {"token": "tok", "mode": "off"}, str(cap["payload"]))
        license_client.free_quota_set_remote("tok", cloud_lifetime=3)
        check("只给 cloud_lifetime → 不含 mode",
              cap["payload"] == {"token": "tok", "cloud_lifetime": 3}, str(cap["payload"]))
        license_client.free_quota_set_remote("tok", mode=None)
        check("显式 null → 键在 payload 里（=清除）",
              cap["payload"] == {"token": "tok", "mode": None}, str(cap["payload"]))
        license_client.free_quota_set_remote("tok")
        check("什么都不传 → payload 只有 token（不动任何项）",
              cap["payload"] == {"token": "tok"}, str(cap["payload"]))


# ── ③ 客户端口径优先级：本机 > 云端 > 默认 on ──────────────────────────────
def test_mode_precedence_local_over_cloud() -> None:
    with patch.object(membership, "load_plan_overrides", return_value={"free_quota": {"mode": "on"}}), \
         patch.object(membership, "cloud_free_quota_overrides", return_value={"mode": "off"}):
        check("本机 on 压过云端 off（改完即时生效）", membership.free_quota_mode() == "on")
    with patch.object(membership, "load_plan_overrides", return_value={"free_quota": {"mode": "off"}}), \
         patch.object(membership, "cloud_free_quota_overrides", return_value={"mode": "on"}):
        check("本机 off 压过云端 on", membership.free_quota_mode() == "off")


def test_mode_falls_back_to_cloud_then_default() -> None:
    with patch.object(membership, "load_plan_overrides", return_value={"free_quota": {}}), \
         patch.object(membership, "cloud_free_quota_overrides", return_value={"mode": "off"}):
        check("本机未设 → 用云端 off（换机/重装恢复）", membership.free_quota_mode() == "off")
    with patch.object(membership, "load_plan_overrides", return_value={}), \
         patch.object(membership, "cloud_free_quota_overrides", return_value={}):
        check("本机与云端都未设 → 默认 on", membership.free_quota_mode() == "on")
    # 云端脏值 → 回默认
    with patch.object(membership, "load_plan_overrides", return_value={}), \
         patch.object(membership, "cloud_free_quota_overrides", return_value={"mode": "junk"}):
        check("云端脏值 → 默认 on", membership.free_quota_mode() == "on")


def test_cloud_mode_off_zeroes_free_limits() -> None:
    """本机没设、云端 off ⇒ effective_daily_limits 免费档归 0、会员档不受影响。"""
    with patch.object(membership, "load_plan_overrides", return_value={}), \
         patch.object(membership, "cloud_free_quota_overrides", return_value={"mode": "off"}):
        mem, free = membership.effective_daily_limits()
        check("云端 off → 免费每日全归 0", all(v == 0 for v in free.values()), str(free))
        check("云端 off → 会员每日不受影响", mem == dict(membership.DAILY_QUOTA_LIMITS))


def test_cloud_free_quota_reader_respects_switch() -> None:
    # 关掉云 → 直接返回 {}
    with patch.dict("os.environ", {"VDL_PLANS_CLOUD": "0"}):
        check("VDL_PLANS_CLOUD=0 → 不读云端", membership.cloud_free_quota_overrides() == {})
    # 开云：从缓存读回
    with patch.dict("os.environ", {"VDL_PLANS_CLOUD": "1"}), \
         patch.object(membership, "_license_api", return_value="http://x/api/license/plans"), \
         patch.object(membership, "cloud_plan_overrides", return_value={}):
        membership._CLOUD_PLANS_CACHE["free_quota"] = {"mode": "off"}
        check("开云 → 从同一次拉取的缓存读回 free_quota",
              membership.cloud_free_quota_overrides() == {"mode": "off"})


# ── ④ push_free_quota_to_cloud：mode 一起下发 + 刷缓存 ─────────────────────
def test_push_sends_mode_and_refreshes_cache() -> None:
    cap: dict = {}
    fake_admin = types.SimpleNamespace(_license_admin_token=lambda: "adm")
    with patch.object(membership, "_license_api", return_value="http://x/api/license/free_quota_set"), \
         patch.dict(sys.modules, {"admin_store": fake_admin}), \
         patch.object(membership, "load_plan_overrides",
                      return_value={"free_quota": {"mode": "off", "cloud_lifetime": {"cloud_commentary": 3}}}), \
         patch.object(license_client, "free_quota_set_remote",
                      lambda token, **kw: cap.update(token=token, kw=kw) or
                      {"ok": True, "free_quota_overrides": {"mode": "off", "cloud_lifetime": {"cloud_commentary": 3}},
                       "effective_cloud_limits": {}}):
        r = membership.push_free_quota_to_cloud()
        check("下发成功", r.get("ok") is True, str(r))
        check("mode 随之下发", cap["kw"].get("mode") == "off", str(cap["kw"]))
        check("cloud_lifetime 一并下发", cap["kw"].get("cloud_lifetime") == {"cloud_commentary": 3}, str(cap["kw"]))
        check("成功后刷新缓存（即时一致）",
              membership._CLOUD_PLANS_CACHE.get("free_quota", {}).get("mode") == "off")


# ── ⑤ 每日额度必须下发（2026-10-20 实测：后台把「生成二维码」改成免费 2 次，
#      网页版仍按代码默认 20 显示/放行 —— 根因是推送只发了 cloud_lifetime/mode，
#      daily_free_limits / daily_member_limits 从未上云；网页端只读云端。）────────
def test_push_sends_daily_limits() -> None:
    cap: dict = {}
    fake_admin = types.SimpleNamespace(_license_admin_token=lambda: "adm")
    fq = {"mode": "on",
          "daily_free_limits": {"share_qr": 2, "share_page": 2},
          "daily_member_limits": {"share_qr": 200, "share_page": 200}}
    with patch.object(membership, "_license_api", return_value="http://x/api/license/free_quota_set"), \
         patch.dict(sys.modules, {"admin_store": fake_admin}), \
         patch.object(membership, "load_plan_overrides", return_value={"free_quota": fq}), \
         patch.object(license_client, "free_quota_set_remote",
                      lambda token, **kw: cap.update(token=token, kw=kw) or
                      {"ok": True, "free_quota_overrides": fq, "effective_cloud_limits": {}}):
        r = membership.push_free_quota_to_cloud()
        check("下发成功", r.get("ok") is True, str(r))
        check("daily_free_limits 一并下发",
              cap["kw"].get("daily_free_limits") == {"share_qr": 2, "share_page": 2}, str(cap["kw"]))
        check("daily_member_limits 一并下发",
              cap["kw"].get("daily_member_limits") == {"share_qr": 200, "share_page": 200}, str(cap["kw"]))


def test_license_client_payload_carries_daily_limits() -> None:
    sent: dict = {}

    def _fake_post(path, payload, base_url, timeout, opener=None):
        sent.update(path=path, payload=payload)
        return {"ok": True}

    with patch.object(license_client, "_post", _fake_post):
        license_client.free_quota_set_remote("adm",
                                             daily_free_limits={"share_qr": 2},
                                             daily_member_limits={"share_qr": 200},
                                             base_url="http://x")
    check("走 free_quota_set 端点", str(sent.get("path") or "").endswith("/free_quota_set"), str(sent.get("path")))
    check("payload 带 daily_free_limits",
          sent["payload"].get("daily_free_limits") == {"share_qr": 2}, str(sent.get("payload")))
    check("payload 带 daily_member_limits",
          sent["payload"].get("daily_member_limits") == {"share_qr": 200}, str(sent.get("payload")))
    check("未传的项不塞进 payload（中心不动该项）",
          "cloud_lifetime" not in sent["payload"] and "mode" not in sent["payload"],
          str(sent.get("payload")))


def test_push_skips_when_no_override() -> None:
    with patch.object(membership, "_license_api", return_value="http://x/api/license/free_quota_set"), \
         patch.dict(sys.modules, {"admin_store": types.SimpleNamespace(_license_admin_token=lambda: "adm")}), \
         patch.object(membership, "load_plan_overrides", return_value={"free_quota": {}}):
        r = membership.push_free_quota_to_cloud()
        check("无覆盖 → no_override 且不报错", r.get("ok") is True and r.get("reason") == "no_override", str(r))


if __name__ == "__main__":  # pragma: no cover
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    sys.exit(1 if (failed or _FAIL) else 0)
