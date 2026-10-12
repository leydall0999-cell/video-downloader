#!/usr/bin/env python3
"""守卫：`/api/cookie/pull-diag` 必须校验令牌，且校验必须在重操作之前（2026-10-12）。

背景（安全盘点时实测）：该端点经 nginx/CF 暴露在公网，
`GET https://hanyuxz.top/api/cookie/pull-diag` **不带任何令牌就返回 200**；
而它默认 `mode=full` 会**同步经隧道拉取 VPS Cookie 并写入公共池** —— 属
「未鉴权的状态变更 + 重操作」端点：任何人可反复触发，写公共池、占出口带宽。

本守卫钉三件事（缺一即红）：
  ① 签名能拿到令牌（`token` 参数 + `Request`）；
  ② 令牌比对走既有环境变量（VDL_COOKIE_REFILL_TOKEN / VDL_COOKIE_SYNC_TOKEN），
     不匹配即 403 —— 与同文件 `/v1/resolve` 同口径；
  ③ **校验发生在重操作之前**（在 `import cn_tunnel` 之前）——
     只加校验但放到后面等于没加（重操作照跑）。

跑法：`python3 tests/test_cookie_pull_diag_auth.py`
"""
from __future__ import annotations

import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
# 可传入被测文件路径：变异测试在**副本**上注入缺陷后再跑本守卫（绝不动真源码）
APP = (pathlib.Path(sys.argv[1]) if len(sys.argv) > 1
       else HERE.parent / "app.py")

FAILS = 0
CHECKS = 0


def check(desc: str, cond: bool, extra: str = "") -> None:
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print(f"  ✅ {desc}")
    else:
        FAILS += 1
        print(f"  ❌ {desc} | {extra}")


def handler_body() -> str:
    """切出 cookie_pull_diag 的真源码（装饰器 → 下一个顶层 @app. 之前）。"""
    src = APP.read_text(encoding="utf-8")
    m = re.search(r'@app\.get\("/api/cookie/pull-diag"\)', src)
    if not m:
        return ""
    start = m.start()
    nxt = re.search(r"\n@app\.", src[m.end():])
    end = m.end() + (nxt.start() if nxt else len(src) - m.end())
    return src[start:end]


def main() -> int:
    body = handler_body()
    check("能定位到 pull-diag 处理器（锚点没被改名）", bool(body), "锚点未命中")
    if not body:
        print(f"\n通过 {CHECKS - FAILS} / 失败 {FAILS}")
        return 1

    check("签名接收 token（可经 ?token= 传入）", "token: str = \"\"" in body,
          body.splitlines()[0][:90])
    check("签名接收 Request（才能读 Authorization / X-Api-Key 头）",
          "request: Request" in body, body.splitlines()[0][:90])

    check("比对 VDL_COOKIE_REFILL_TOKEN（与 /v1/resolve 同源）",
          "VDL_COOKIE_REFILL_TOKEN" in body)
    check("比对 VDL_COOKIE_SYNC_TOKEN（兼容另一路令牌）",
          "VDL_COOKIE_SYNC_TOKEN" in body)
    check("不匹配即 403（不是「仅记录日志」）",
          re.search(r"status_code=403", body) is not None, "未见 403")
    check("同时支持 Authorization: Bearer 头",
          re.search(r'Authorization', body) is not None
          and re.search(r'bearer ', body, re.I) is not None)
    check("同时支持 X-Api-Key 头", "X-Api-Key" in body)

    # ③ 顺序：校验必须在重操作（import cn_tunnel / 写公共池）之前
    i_chk = body.find("status_code=403")
    i_heavy = body.find("import cn_tunnel")
    check("校验出现在重操作之前（顺序断言）",
          i_chk != -1 and i_heavy != -1 and i_chk < i_heavy,
          f"403@{i_chk} vs cn_tunnel@{i_heavy}")

    # 反回归：旧的无鉴权签名不得残留
    check("旧无鉴权签名已不存在（def cookie_pull_diag(mode: str = \"full\") -> dict:）",
          'def cookie_pull_diag(mode: str = "full") -> dict:' not in body,
          "旧签名仍在 ⇒ 鉴权未真正落到处理器上")

    print(f"\n通过 {CHECKS - FAILS} / 失败 {FAILS}")
    if FAILS:
        print("❌ pull-diag 鉴权守卫未通过")
        return 1
    print("🎉 /api/cookie/pull-diag 已要求令牌，且校验先于重操作")
    return 0


if __name__ == "__main__":
    sys.exit(main())
