#!/usr/bin/env python3
"""云端侧 `POST /api/cookie/pool-admin` 契约（2026-10-21 事故）。

背景：桌面 App 后台「Cookie 池」页默认代理读**云端**那个池（桌面本机池通常是空的），
调的是 `POST <cloud>/api/cookie/pool-admin`。`routers/admin_cookies.py` 的 docstring
写明「同一份代码、两个部署角色（云端节点直接读写 / 桌面 App 代理）」，但 web 端
**一直没挂这份路由** ⇒ 桌面后台显示「云端不可达（HTTP 405）」，整页只剩空池。

本测试锁四件事：
  1. 令牌不对/缺失 → 403（这把钥匙本来就能写池，读池不能比它更松）；
  2. overview 返回运营需要的口径（summary/domains/injectable/verify_ttl/gaps），
     且**不带任何 Cookie 明文**；
  3. 未知 action → 400（不是 500，也不能静默当 overview）；
  4. inject 的坏输入走「return ok=False」而不是抛 500（后台要能显示原因）。

运行：cd server && python tests/test_cookie_pool_admin_cloud.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

# 测试隔离 + 固定令牌：必须在 import app 之前设好（app 启动时会读环境）
_TMP = tempfile.mkdtemp(prefix="vdl_test_pooladmin_")
os.environ["VDL_DATA_DIR"] = _TMP
os.environ["VDL_CLOUD_LINK"] = "0"
_TOKEN = "test-token-for-pool-admin"
os.environ["VDL_COOKIE_SYNC_TOKEN"] = _TOKEN

import cookie_pool as cp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import app as server_app  # noqa: E402

_FAILED: list[str] = []


def check(name: str, cond: bool, extra=None) -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else f"  → {extra!r}"))
    if not cond:
        _FAILED.append(name)


def _walk_keys(obj, out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(str(k))
            _walk_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_keys(v, out)


def test_pool_admin_contract() -> None:
    # 池目录指到临时目录，别碰真实池
    pool = Path(_TMP) / "pool"
    pool.mkdir(parents=True, exist_ok=True)
    cp._POOL_DIR = pool

    client = TestClient(server_app.app)
    url = "/api/cookie/pool-admin"

    print("[1] 鉴权：这把钥匙能写池，读池不得更松")
    check("缺令牌 403", client.post(url, json={"action": "overview"}).status_code == 403)
    check("错令牌 403", client.post(url, json={"token": "nope", "action": "overview"}).status_code == 403)

    print("[2] overview 口径与脱敏")
    r = client.post(url, json={"token": _TOKEN, "action": "overview"})
    check("正确令牌 200", r.status_code == 200, r.text[:200])
    d = r.json()
    check("ok=True", d.get("ok") is True, d)
    summary = d.get("summary") or {}
    for k in ("accounts", "entries", "valid", "invalid", "unknown"):
        check(f"summary 带 {k}", k in summary, summary)
    for k in ("domains", "injectable", "gaps", "verify_ttl", "checked_at", "pool_dir"):
        check(f"响应带 {k}", k in d, list(d))
    check("injectable 是 {domain,title} 结构",
          all(isinstance(x, dict) and "domain" in x and "title" in x
              for x in (d.get("injectable") or [])), d.get("injectable"))
    check("空池不报错（首次部署就是空池）", summary.get("entries") == 0, summary)

    keys: list[str] = []
    _walk_keys(d, keys)
    leaky = [k for k in keys if k.lower() in ("value", "cookie", "cookies", "header", "raw", "secret")]
    check("响应里没有任何 Cookie 明文字段", not leaky, leaky)

    print("[3] 未知动作 → 400（不能静默当 overview）")
    r = client.post(url, json={"token": _TOKEN, "action": "rm-rf"})
    check("未知动作 400", r.status_code == 400, r.status_code)

    print("[4] 坏输入走 ok=False，不抛 500")
    r = client.post(url, json={"token": _TOKEN, "action": "inject", "domain": "", "header": "a=b"})
    check("inject 空域名 200+ok=False", r.status_code == 200 and r.json().get("ok") is False, r.text[:200])
    r = client.post(url, json={"token": _TOKEN, "action": "inject",
                               "domain": "not-in-whitelist.example", "header": "a=b"})
    check("inject 非白名单域名 ok=False", r.status_code == 200 and r.json().get("ok") is False, r.text[:200])
    r = client.post(url, json={"token": _TOKEN, "action": "inject",
                               "domain": "bilibili.com", "header": "RELAYTEST=1"})
    check("inject 健康检查占位被拒", r.status_code == 200 and r.json().get("ok") is False, r.text[:200])
    r = client.post(url, json={"token": _TOKEN, "action": "delete", "domain": "", "ids": []})
    check("delete 空参数不炸", r.status_code == 200, r.text[:200])

    print("[5] 路由确实挂在 web 端（事故点：没挂 ⇒ 405）")
    # 🔴 别只扫 app.routes 的扁平 path：这个 FastAPI 版本把 include_router 包成
    # `fastapi.routing._IncludedRouter`（原 router 在 `.original_router`）+ 一个
    # Mount。第一版断言按扁平 path 找，直接误报「没挂载」。
    import routers.admin_cookies as _ac

    included = [getattr(rt, "original_router", None) for rt in server_app.app.routes]
    check("admin_cookies.router 已被 app 挂载", _ac.router in included,
          [type(rt).__name__ for rt in server_app.app.routes][:6])
    check("POST 不再 405", client.post(url, json={"token": _TOKEN, "action": "overview"}).status_code != 405)


def main() -> None:
    test_pool_admin_contract()
    print()
    if _FAILED:
        print(f"❌ 有失败：{len(_FAILED)} 项")
        for n in _FAILED:
            print("   -", n)
        raise SystemExit(1)
    print("🎉 云端 /api/cookie/pool-admin 契约全部通过（鉴权 / 口径 / 脱敏 / 坏输入 / 路由已挂载）")


if __name__ == "__main__":
    main()
