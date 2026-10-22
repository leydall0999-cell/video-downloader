"""解析需求榜「跨节点合并」源码棘轮（web-dev 专属）——2026-10-17。

背景：未知站解析失败默认落在 HK（非国内域名都转发对端），ops 页挂在 ECS，
只读本机日志 ≈ 空榜。故 ECS 端要合并对端数据。本测试钉住最容易改坏的五点：

  1. 内部令牌用 hmac.compare_digest（常量时间，防时序侧信道）；
  2. 带内部令牌的请求**不得触发合并**（否则 ECS→HK→ECS 无限递归互相拉）；
  3. 拉对端必须带非默认 UA（对端挂 CF 后，Python-urllib 默认 UA 会 403）；
  4. json 必须用本文件里的别名 `_json`（core.py 是 import json as _json，
     写成 json.loads 会 NameError 并被 except 静默吞掉 → 合并永远失效）；
  5. 合并口径（2026-10-23 起按周）：有 weeks 列表 → 取并集算周数；旧节点只回
     计数 → 退回相加；total/categories 累加、last_ts 取大。

运行：python tests/test_resolve_demand_merge.py   （纯源码检查，无需网络）
"""

import pathlib
import sys

SERVER = pathlib.Path(__file__).resolve().parent.parent
CORE = SERVER / "routers" / "core.py"
FAILS = []


def check(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main():
    src = CORE.read_text(encoding="utf-8")
    check("core.py 存在 resolve-demand 端点", "/api/admin/resolve-demand" in src)
    check("存在 _internal_token_ok 且用 hmac.compare_digest",
          "_internal_token_ok" in src and "hmac.compare_digest(got, tok)" in src)
    # 防递归：内部请求走 early-return 分支，不进入合并
    check("内部请求不触发合并（防递归护栏）",
          "if is_internal:" in src and "绝不再触发合并" in src)
    # UA：拉对端必须带非默认 UA
    check("拉对端带非默认 UA（CF 403 防护）",
          '"User-Agent": "VDL-Web/1.0"' in src)
    # json 别名：避免 NameError 被静默吞
    check("合并处用 _json.loads（不是 json.loads）",
          "_json.loads(resp.read()" in src and "data = json.loads(resp.read()" not in src)
    # 合并口径（按周）：同站点同周不重复计；旧节点无 weeks → 相加
    check("合并口径：周数取并集/回退相加、记录数相加、分类累加、最近时间取大",
          'cur["weeks_count"] = len(merged_weeks)' in src
          and 'cur["weeks_count"] = _demand_weeks(cur) + _demand_weeks(r)' in src
          and 'cur["total"] += int(r.get("total") or 0)' in src
          and 'cur["categories"][c] = cur["categories"].get(c, 0) + int(n or 0)' in src
          and 'if float(r.get("last_ts") or 0) >= float(cur.get("last_ts") or 0):' in src)
    check("对端不可达时明示 peer_ok=False（不假装有数据）",
          "return False, local_rows" in src and '"peer_ok": peer_ok' in src)

    print("🎉 跨节点合并守卫全部通过" if not FAILS else f"❌ 失败 {len(FAILS)} 项: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
