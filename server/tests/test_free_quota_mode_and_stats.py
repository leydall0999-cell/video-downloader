#!/usr/bin/env python3
"""免费额度口径（mode）+ 安装/插件统计 的离线守卫（2026-10-13）。

覆盖三件事：
  ① 免费额度口径：effective_daily_limits 对 mode=off 的执行、脏值回退、
     save_plan_overrides 的清洗、GET credit-costs 下发 mode/modes（接线棘轮）；
  ② 心跳设备画像：heartbeat_remote 携带 meta、cloud_account 两处调用接线；
  ③ 授权中心统计聚合：stat_aggregate / _attach_device / _clean_device_meta
     —— 从 deploy/license_server.py 按函数抽取执行（不动真实 state）。

零网络、零用户数据；写盘只落 tempfile。
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys
import tempfile
import time
import types
from unittest.mock import patch

SERVER = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER))

import membership  # noqa: E402

_PASS = _FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {detail}")


# ── ① 免费额度口径 ──────────────────────────────────────────────────────────
def test_mode_off_zeroes_free_only() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_fqm_"))
    fake = {"free_quota": {"mode": "off"}}
    with patch.object(membership, "load_plan_overrides", return_value=fake):
        mem, free = membership.effective_daily_limits()
        check("mode=off → 免费每日全部归 0", all(v == 0 for v in free.values()), str(free))
        check("mode=off → 会员每日不受影响", mem == dict(membership.DAILY_QUOTA_LIMITS))
        check("mode=off → free_quota_mode() 返回 off", membership.free_quota_mode() == "off")
    fake2 = {"free_quota": {"mode": "on", "daily_free_limits": {"download": 7}}}
    with patch.object(membership, "load_plan_overrides", return_value=fake2):
        mem2, free2 = membership.effective_daily_limits()
        check("mode=on → 覆盖数字照常生效", free2.get("download") == 7, str(free2.get("download")))
    fake3 = {"free_quota": {"mode": "junk", "daily_free_limits": {"download": 7}}}
    with patch.object(membership, "load_plan_overrides", return_value=fake3):
        _, free3 = membership.effective_daily_limits()
        check("mode 脏值 → 回退 on（数字保留）", free3.get("download") == 7
              and membership.free_quota_mode() == "on")
    with patch.object(membership, "load_plan_overrides", return_value={}):
        check("未配置 mode → 默认 on", membership.free_quota_mode() == "on")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_save_overrides_sanitizes_mode() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_fqm_"))
    with patch.object(membership, "plan_override_path", return_value=tmp / "plans.json"):
        membership._PLAN_OVERRIDE_CACHE = None  # 隔离缓存
        out = membership.save_plan_overrides({"free_quota": {"mode": "JUNK", "daily_free_limits": {"download": 3}}})
        check("save_plan_overrides 丢弃非法 mode", "mode" not in (out.get("free_quota") or {}))
        out2 = membership.save_plan_overrides({"free_quota": {"mode": "off"}})
        check("save_plan_overrides 接受合法 off", (out2.get("free_quota") or {}).get("mode") == "off")
        out3 = membership.save_plan_overrides({"free_quota": {"mode": "on"}})
        check("save_plan_overrides 接受合法 on", (out3.get("free_quota") or {}).get("mode") == "on")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_get_credit_costs_exposes_mode() -> None:
    src = (SERVER / "routers" / "admin.py").read_text(encoding="utf-8")
    check("GET credit-costs 带生效 mode", '"mode": _M_free_quota_mode()' in src)
    check("GET credit-costs 带可选档位 modes", "list(_M_free_quota_modes())" in src)
    check("口径读取走独立包装（便于打桩）", "def _M_free_quota_mode" in src
          and "def _M_free_quota_modes" in src)
    # 三处执行点都吃 effective_daily_limits ⇒ mode=off 全链路生效
    check("membership 2051/2181 执行点沿用 effective_daily_limits",
          src.count("effective_daily_limits") >= 0
          and (SERVER / "membership.py").read_text(encoding="utf-8").count("effective_daily_limits()") >= 3)


# ── ② 心跳设备画像 ──────────────────────────────────────────────────────────
def test_heartbeat_meta_wiring() -> None:
    import license_client
    captured: dict = {}

    def fake_post(path, payload, *a, **k):
        captured["path"] = path
        captured["payload"] = payload
        return {"ok": True}

    with patch.object(license_client, "_post", fake_post):
        license_client.heartbeat_remote("tok", "fp", meta={"platform": "macos",
                                                           "ext": {"browser": "chrome"}})
        check("heartbeat 携带 meta", captured["payload"].get("meta", {}).get("platform") == "macos")
        license_client.heartbeat_remote("tok", "fp")
        check("不带 meta → 键省略（不是空对象）", "meta" not in captured["payload"])
        license_client.stat_query_remote("adm-tok")
        check("stat_query 打授权中心管理员端点", captured["path"] == "/api/license/stat_query"
              and captured["payload"] == {"token": "adm-tok"})
    ca = (SERVER / "routers" / "cloud_account.py").read_text(encoding="utf-8")
    check("两处心跳都带画像", ca.count("heartbeat_remote(token, fp, meta=_device_meta())") == 2,
          str(ca.count("meta=_device_meta()")))
    check("画像构造有 platform/ext 来源", "_device_meta" in ca and "current_platform" in ca
          and "extension_sync" in ca)


# ── ③ 授权中心：按函数抽取执行（不动真实 state）────────────────────────────
def _extract_fns(source: str, names: tuple[str, ...]) -> types.FunctionDef:
    tree = ast.parse(source)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            out[node.name] = node
    missing = set(names) - set(out)
    if missing:
        raise AssertionError(f"license_server 缺函数: {missing}")
    return out


def test_license_stat_functions() -> None:
    src = (SERVER.parent / "deploy" / "license_server.py").read_text(encoding="utf-8")
    fns = _extract_fns(src, ("stat_aggregate", "_users", "_clean_device_meta",
                             "_merge_device_meta", "_attach_device",
                             "_read_dl_stats"))
    ns = {"MAX_DEVICES": 2, "Any": object, "Optional": None,
          "dict": dict, "list": list, "set": set, "str": str, "float": float,
          "int": int, "isinstance": isinstance, "min": min,
          "time": time, "json": json, "Path": pathlib.Path}
    for fn in fns.values():
        exec(compile(ast.Module([fn], []), "license_server_fragment", "exec"), ns)

    now = 1_800_000_000.0
    state = {"users": {
        "a@x": {"devices": [
            {"fp": "d1", "name": "m1", "last_seen": now - 100, "first_seen": now - 100,
             "platform": "macos", "ext": {"browser": "chrome", "version": "1.2"}},
            {"fp": "d2", "name": "w1", "last_seen": now - 8 * 86400,
             "first_seen": now - 40 * 86400, "platform": "windows"},
        ]},
        "b@x": {"devices": [
            {"fp": "d3", "name": "old", "last_seen": now - 100},  # 无画像（旧设备）
        ]},
    }}
    st = ns["stat_aggregate"](state, now)
    check("安装按平台分组（旧设备归 unknown）",
          st["installs"].get("macos", {}).get("total") == 1
          and st["installs"].get("windows", {}).get("total") == 1
          and st["installs"].get("unknown", {}).get("total") == 1, json.dumps(st["installs"], ensure_ascii=False))
    check("今日新增只算 first_seen ≥ 当日零点",
          st["installs"]["macos"]["today"] == 1 and st["installs"]["windows"]["today"] == 0)
    check("近 7 天安装（windows 40 天前不算）", st["installs"]["windows"]["d7"] == 0
          and st["installs"]["macos"]["d7"] == 1)
    check("扩展按浏览器计数", st["ext"] == {"chrome": 1})
    check("活跃口径（7/30 天）", st["active"] == {"d7": 2, "d30": 3}
          and st["devices_total"] == 3)
    check("聚合含下载计数字段（本机无计数文件 → 空 dict）",
          isinstance(st.get("downloads"), dict))

    # _clean_device_meta 白名单
    cm = ns["_clean_device_meta"]({"platform": "MACOS", "app_version": "1.0.65",
                                   "ext": {"browser": "Chrome", "version": "2.0"},
                                   "hack": "x" * 999})
    check("画像清洗：platform 归一小写", cm.get("platform") == "macos")
    check("画像清洗：ext.browser 归一枚举", cm.get("ext", {}).get("browser") == "chrome")
    check("画像清洗：白名单外字段不落库", "hack" not in cm)

    # _attach_device：新设备带 first_seen + 画像；重复出现只刷新
    user = {"devices": []}
    ns["_attach_device"](user, {"fp": "d9", "name": "n", "meta": {"platform": "windows"}},
                         now)
    d9 = user["devices"][0]
    check("新设备记录 first_seen（= 安装时刻）", d9.get("first_seen") == now)
    check("新设备画像入库", d9.get("platform") == "windows")
    ns["_attach_device"](user, {"fp": "d9", "name": "n", "meta": {"platform": "windows",
                                                                  "ext": {"browser": "edge", "version": "3"}}},
                         now + 10)
    check("重复心跳只刷新不重复计数", len(user["devices"]) == 1)
    check("画像字段级刷新（后来才有 ext）", user["devices"][0].get("ext", {}).get("browser") == "edge")
    check("first_seen 不被心跳改写", user["devices"][0].get("first_seen") == now)

    # 接线棘轮：分发段有 stat_query；心跳分发传 meta
    check("授权中心分发含 stat_query 动作", 'action == "stat_query"' in src
          and "stat_aggregate(st, now)" in src)
    check("心跳分发把 meta 传进实现", "meta=(data.get(\"meta\")" in src)
    check("_attach_device 落 first_seen", '"first_seen": now' in src)
    # 🔴 布局棘轮（2026-10-13 用户定档「太乱太散，整合一下」）：安装/插件区块必须是
    #    **一张统一表**；容器不得挂 admin-stats-summary 网格类（那会把每张表排成
    #    横排挤扁）；表头固定写死，不允许 labelMap.th 式动态表头（undefined 根因）。
    app_js = (SERVER.parent / "web" / "app.js").read_text(encoding="utf-8")
    idx_html = (SERVER.parent / "web" / "index.html").read_text(encoding="utf-8")
    check("安装统计容器不再挂网格类（防多表横排）",
          'id="adminInstallStats" class="admin-stats-summary"' not in idx_html)
    check("不再用动态表头（undefined 根因）", "labelMap.th" not in app_js)
    check("统一表头五列就位",
          "<th>分类</th><th>平台 / 浏览器</th><th>累计</th><th>今日</th><th>近 7 天</th>" in app_js)


def main() -> int:
    print("▶ 免费额度口径 + 安装/插件统计 离线守卫")
    test_mode_off_zeroes_free_only()
    test_save_overrides_sanitizes_mode()
    test_get_credit_costs_exposes_mode()
    test_heartbeat_meta_wiring()
    test_license_stat_functions()
    print(f"\n通过 {_PASS} / 失败 {_FAIL}")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
