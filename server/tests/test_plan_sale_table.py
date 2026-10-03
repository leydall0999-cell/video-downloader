# -*- coding: utf-8 -*-
"""守卫：档位「快速下架/上架」的表名解析（39号，2026-10-03）。

回归背景（真实事故）：
  前端界面的分段 id 是 dl / ai / cp，服务端表名是 download_plans / ai_plans /
  credit_packs。前端把分段 id 直接当 table 提交 → 后端只认表名 → 点「↓ 下架」
  必报「操作失败：未知套餐类别：dl」，整条下架链路不可用（截图实测）。

钉住：
  [A] 后端 _norm_plan_table：新旧命名都能归一化，非法值仍拒；
  [B] 前端两处模板都带 data-table，且请求优先读 data-table；
      并与后端别名表交叉核对（两张映射表必须一致）；
  [C] E2E：真实 TestClient 打 /api/admin/plans/{code}/sale，
      传旧命名 'dl' 必须成功 —— 这正是修复前会失败的入参；
  [D] 下架后 on_sale=False 落盘，且 /api/member/plans 不再出现该档。

运行：cd server && python tests/test_plan_sale_table.py
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="vdl_sale_table_")
os.environ["HOME"] = _TMP
os.makedirs(os.path.join(_TMP, ".video-downloader"), exist_ok=True)
os.environ["VDL_DATA_DIR"] = os.path.join(_TMP, ".video-downloader")
os.environ["VDL_CLOUD_LINK"] = "0"
os.environ["VDL_PLANS_CLOUD"] = "0"

HERE = pathlib.Path(__file__).resolve().parent
_SERVER_DIR = HERE.parent
_REPO = _SERVER_DIR.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import app as server_app                                            # noqa: E402
import auth_store                                                   # noqa: E402
from fastapi.testclient import TestClient                           # noqa: E402
from routers.admin import _SALE_TABLES, _norm_plan_table            # noqa: E402

client = TestClient(server_app.app, raise_server_exceptions=False)

APP_JS = _REPO / "web" / "app.js"

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  ⟵ {extra}" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


# ── [A] 后端归一化 ────────────────────────────────────────────────────────────
def test_norm_backend() -> None:
    print("\n[A] 后端表名归一化（新旧命名都认，非法仍拒）")
    for full in _SALE_TABLES:
        check(f"[表名] {full} 原样通过", _norm_plan_table(full) == full)
    check("[旧命名] dl → download_plans", _norm_plan_table("dl") == "download_plans")
    check("[旧命名] ai → ai_plans", _norm_plan_table("ai") == "ai_plans")
    check("[旧命名] cp → credit_packs", _norm_plan_table("cp") == "credit_packs")
    check("[别名] download_member → download_plans",
          _norm_plan_table("download_member") == "download_plans")
    check("[非售档位] cost 归一化为空（AI 积分成本不可上下架）",
          _norm_plan_table("cost") == "")
    check("[非法] bogus 拒绝", _norm_plan_table("bogus") == "")
    check("[空值] '' → ''（由 _find_plan_table 兜底）", _norm_plan_table("") == "")
    check("[空白] ' dl ' 去空格后仍识别", _norm_plan_table("  dl  ") == "download_plans")


# ── [B] 前端接线 + 两表一致 ───────────────────────────────────────────────────
def test_frontend_wiring() -> None:
    print("\n[B] 前端接线与映射一致性")
    src = APP_JS.read_text(encoding="utf-8")

    m = re.search(r"const _CAT_TABLE = \{([^}]*)\};", src)
    check("app.js 定义了 _CAT_TABLE", bool(m))
    if not m:
        return
    fe_map: dict[str, str] = {}
    for k, v in re.findall(r"(\w+)\s*:\s*'([^']+)'", m.group(1)):
        fe_map[k] = v
    check("_CAT_TABLE 三个分段齐全（dl/ai/cp）",
          set(fe_map) == {"dl", "ai", "cp"}, f"实际 {sorted(fe_map)}")

    # 前后端两张映射表必须一致，否则又是一次「界面能点、后端不认」
    for cat, table in fe_map.items():
        check(f"[跨端一致] 前端 {cat} → {table}，后端归一化结果相同",
              _norm_plan_table(cat) == table,
              f"后端得到 {_norm_plan_table(cat)!r}")
    check("[跨端一致] 前端 target 全在服务端白名单内",
          all(t in _SALE_TABLES for t in fe_map.values()))

    # 两处模板（现有档位 / 新增档位）都要带 data-table
    n_dt = src.count('data-table="${esc(_CAT_TABLE[cat] || cat)}"')
    check("两处模板的「下架/上架」按钮都带 data-table", n_dt == 2, f"实际 {n_dt} 处")

    # 请求必须优先读 data-table，不能再直接提交 data-cat
    check("请求体优先取 sale.dataset.table",
          "table: sale.dataset.table || _CAT_TABLE[sale.dataset.cat] || ''" in src)
    check("请求体不再直接提交 data-cat",
          "table: sale.dataset.cat || ''" not in src)
    check("_CAT_TABLE 定义早于点击处理器（无 TDZ 风险）",
          src.index("const _CAT_TABLE") < src.index("plansBox.addEventListener('click'"))


# ── [C]/[D] 真接口端到端 ─────────────────────────────────────────────────────
def _admin_headers() -> dict:
    uid = auth_store.create_user("saletest", "Saletest#2026") or "u_saletest"
    auth_store.set_user_admin(uid, True)
    return {"Authorization": "Bearer " + auth_store.issue_token(uid)}


CODE = "download_1day"


def test_sale_e2e() -> None:
    print("\n[C] 真接口：旧命名 'dl' 必须能下架（修复前此处报「未知套餐类别：dl」）")
    h = _admin_headers()

    r = client.post(f"/api/admin/plans/{CODE}/sale",
                    json={"on_sale": False, "table": "dl"}, headers=h)
    body = r.json()
    check("传 table='dl' → HTTP 200", r.status_code == 200, f"status={r.status_code}")
    check("ok=True（不再报未知套餐类别）", body.get("ok") is True, json.dumps(body, ensure_ascii=False)[:200])
    check("服务端回填真实表名", body.get("table") == "download_plans", str(body.get("table")))
    check("state.on_sale=False", (body.get("state") or {}).get("on_sale") is False)

    print("\n[C2] 其它入参形态")
    r2 = client.post(f"/api/admin/plans/{CODE}/sale",
                     json={"on_sale": False, "table": "download_plans"}, headers=h)
    check("完整表名可用", r2.json().get("ok") is True)
    r3 = client.post(f"/api/admin/plans/{CODE}/sale",
                     json={"on_sale": False}, headers=h)
    check("不带 table → _find_plan_table 兜底可用", r3.json().get("ok") is True,
          json.dumps(r3.json(), ensure_ascii=False)[:160])
    r4 = client.post(f"/api/admin/plans/{CODE}/sale",
                     json={"on_sale": False, "table": "bogus"}, headers=h)
    check("非法 table + 能定位到 code → 仍按 code 归属表处理（不报错）",
          r4.json().get("ok") is True and r4.json().get("table") == "download_plans",
          json.dumps(r4.json(), ensure_ascii=False)[:160])
    r4b = client.post("/api/admin/plans/no_such_plan_xyz/sale",
                      json={"on_sale": False, "table": "bogus"}, headers=h)
    check("既定位不到 code、table 又非法 → ok=False 且提示未知类别",
          r4b.json().get("ok") is False and "未知套餐类别" in (r4b.json().get("error") or ""),
          json.dumps(r4b.json(), ensure_ascii=False)[:160])
    r5 = client.post(f"/api/admin/plans/{CODE}/sale",
                     json={"on_sale": False, "table": "dl"})
    check("无鉴权 → 401", r5.status_code == 401, f"status={r5.status_code}")

    print("\n[D] 下架真落盘：接口回读 on_sale=False（前端据此隐藏该档）")
    mp = client.get("/api/member/plans").json()
    plans = ((mp.get("download_member") or {}).get("plans") or {})
    check("下架后 state.on_sale=False 已落到前台接口",
          (plans.get(CODE) or {}).get("state", {}).get("on_sale") is False,
          json.dumps((plans.get(CODE) or {}).get("state", {}), ensure_ascii=False)[:160])
    st = (plans.get(CODE) or {}).get("state", {})
    check("下架档 reason 标为「已下架」", st.get("reason") == "已下架", str(st.get("reason")))

    print("\n[D2] 传入错误表名也必须改到真实归属表（回归：曾把下载档写进 AI 表）")
    r6 = client.post(f"/api/admin/plans/{CODE}/sale",
                     json={"on_sale": True, "table": "ai"}, headers=h)
    check("传 table='ai' 但 code 属下载表 → 服务端回填 download_plans",
          r6.json().get("table") == "download_plans", str(r6.json().get("table")))
    mp2 = client.get("/api/member/plans").json()
    plans2 = ((mp2.get("download_member") or {}).get("plans") or {})
    check("重新上架后前台 on_sale 恢复 True",
          (plans2.get(CODE) or {}).get("state", {}).get("on_sale") is True,
          json.dumps((plans2.get(CODE) or {}).get("state", {}), ensure_ascii=False)[:160])
    check("错误表名未污染 AI 表", CODE not in ((mp2.get("ai_member") or {}).get("plans") or {}))

    # 复位：确保测试不留副作用（on_sale 默认 True 即回原状）
    client.post(f"/api/admin/plans/{CODE}/sale", json={"on_sale": True}, headers=h)


if __name__ == "__main__":
    test_norm_backend()
    test_frontend_wiring()
    test_sale_e2e()
    print("\n" + "=" * 64)
    if FAILS:
        print(f"❌ 表名解析守卫失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   - " + f)
        sys.exit(1)
    print("✅ 表名解析守卫全部通过（后端归一化 / 前端接线 / 真接口下架上架）")
