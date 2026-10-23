"""扩展把登录态交给本机 App 的接口 + Cookie 缓存根域回落（2026-10-18）。

覆盖的真实故障与契约：
  1. 浏览器扩展（`extension/cookie-harvest.js`）采集到登录态后 POST
     `/api/cookie/browser-push`，App 写进 `~/.videodownloader/cookies/`，之后解析
     自动带上 —— 这条路比 App 自己解密浏览器 Cookie 库更准（扩展读的是用户**此刻
     正在用**的 profile，多开/便携版/加密库都能拿到）。
  2. 只允许本机直连 + 扩展/本机来源：网页版（https://hanyuxz.top）或任意站点若能
     调它，就能借用户浏览器把伪造 Cookie 写进本机缓存，下次解析带着它们去请求目标站。
  3. 扩展按**根域**推送（bilibili.com），而解析时 host 是具体域
     （www.bilibili.com / api.bilibili.com）—— 缓存必须能按根域回落，否则扩展同步
     等于白做；但精确 host 的缓存必须优先（它含 host-only cookie，比根域快照更准）。

运行：cd server && python tests/test_cookie_browser_push.py
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

_SERVER_DIR = Path(__file__).resolve().parents[1]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import app as server_app  # noqa: E402
import cookie_cache  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_FAILED: list[str] = []


def check(name: str, cond: bool, extra=None) -> None:
    print(("  ✓ " if cond else "  ❌ ") + name + ("" if cond else f"  → {extra!r}"))
    if not cond:
        _FAILED.append(name)


@contextlib.contextmanager
def _env():
    """隔离本机缓存目录，并挡掉「写缓存 → 自动云备份」的后台线程（用例不联网）。"""
    d = Path(tempfile.mkdtemp(prefix="vdl_ckcache_"))
    fake = types.ModuleType("user_data_cloud")
    fake.schedule_autosync = lambda *a, **k: None
    with contextlib.ExitStack() as st:
        st.enter_context(patch.object(cookie_cache, "_COOKIE_CACHE_DIR", d))
        st.enter_context(patch.dict(sys.modules, {"user_data_cloud": fake}))
        yield d


def _post(payload, origin=None, local=True):
    c = TestClient(server_app.app)
    headers = {}
    if origin is not None:
        headers["origin"] = origin
    with patch.object(server_app, "_is_local_direct", return_value=local):
        return c.post("/api/cookie/browser-push", json=payload, headers=headers)


def case_local_push_writes_cache() -> None:
    with _env() as d:
        r = _post({"domain": "bilibili.com", "cookie": "SESSDATA=abc123def; buvid3=x", "platform": "bilibili"})
        check("本机直连推送 200", r.status_code == 200, (r.status_code, r.text))
        body = r.json()
        check("返回 saved=true 且带字节数", body.get("saved") is True and body.get("bytes", 0) > 0, body)
        f = d / "bilibili.com.json"
        check("缓存文件按域名落盘", f.exists(), str(list(d.glob("*"))))
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            check("缓存内容就是推来的 Cookie 头", data.get("header") == "SESSDATA=abc123def; buvid3=x", data)


def case_origin_gate() -> None:
    with _env():
        r = _post({"domain": "bilibili.com", "cookie": "SESSDATA=abc123def"}, origin="chrome-extension://abcdefghijklmnop")
        check("扩展来源放行", r.status_code == 200, (r.status_code, r.text))
        r2 = _post({"domain": "bilibili.com", "cookie": "SESSDATA=abc123def"}, origin="http://127.0.0.1:8321")
        check("本机页面来源放行", r2.status_code == 200, (r2.status_code, r2.text))
        r3 = _post({"domain": "bilibili.com", "cookie": "SESSDATA=abc123def"}, origin="https://hanyuxz.top")
        check("外部站点来源 403", r3.status_code == 403, (r3.status_code, r3.text))
        r4 = _post({"domain": "bilibili.com", "cookie": "SESSDATA=abc123def"}, origin="null")
        check("沙箱 iframe（Origin: null）403", r4.status_code == 403, (r4.status_code, r4.text))
        r5 = _post({"domain": "bilibili.com", "cookie": "SESSDATA=abc123def"}, local=False)
        check("非本机直连 403", r5.status_code == 403, (r5.status_code, r5.text))


def case_payload_validation() -> None:
    with _env() as d:
        for name, payload in [
            ("domain 缺失", {"cookie": "SESSDATA=abc123def"}),
            ("domain 注入（../etc）", {"domain": "../etc", "cookie": "SESSDATA=abc123def"}),
            ("domain 中文", {"domain": "哔哩哔哩.com", "cookie": "SESSDATA=abc123def"}),
            ("cookie 缺等号", {"domain": "bilibili.com", "cookie": "notacookie"}),
            ("cookie 含换行（头注入）", {"domain": "bilibili.com", "cookie": "SESSDATA=a\r\nX-Evil: 1"}),
        ]:
            r = _post(payload)
            check(f"{name} → 400", r.status_code == 400, (r.status_code, r.text))
        check("非法请求一个文件都没写", not list(d.glob("*.json")), list(d.glob("*.json")))


def case_root_domain_fallback() -> None:
    """扩展按根域推、解析按具体域取 —— 必须能命中；精确 host 仍优先。"""
    import downloader  # app 已加载它，这里只是拿模块对象来打桩

    with _env():
        with patch.object(downloader, "get_browser_cookie_header", return_value=None):
            r = _post({"domain": "bilibili.com", "cookie": "SESSDATA=root-level"})
            check("根域推送成功", r.status_code == 200, (r.status_code, r.text))

            got = cookie_cache.get_cached_cookie_header("www.bilibili.com")
            check("具体域回落命中根域缓存", got == "SESSDATA=root-level", got)
            got2 = cookie_cache.get_cached_cookie_header("api.bilibili.com")
            check("另一子域同样回落命中", got2 == "SESSDATA=root-level", got2)

            # 精确 host 缓存必须优先于根域
            cookie_cache._save("www.bilibili.com", "SESSDATA=host-only")
            got3 = cookie_cache.get_cached_cookie_header("www.bilibili.com")
            check("精确 host 缓存优先于根域", got3 == "SESSDATA=host-only", got3)

            # 过期缓存不回落（否则会拿一份 30 天前的失效登录态去请求）
            f = cookie_cache._cache_file("bilibili.com")
            old = json.loads(f.read_text(encoding="utf-8"))
            old["ts"] = int(time.time()) - cookie_cache._COOKIE_TTL - 60
            f.write_text(json.dumps(old), encoding="utf-8")
            cookie_cache._cache_file("www.bilibili.com").unlink()
            got4 = cookie_cache.get_cached_cookie_header("www.bilibili.com")
            check("根域缓存过期后不再回落", got4 is None, got4)


def main() -> int:
    print("== test_cookie_browser_push ==")
    case_local_push_writes_cache()
    case_origin_gate()
    case_payload_validation()
    case_root_domain_fallback()
    if _FAILED:
        print("\n失败 %d 项：%s" % (len(_FAILED), "; ".join(_FAILED)))
        return 1
    print("\n全部通过")
    return 0


def test_cookie_browser_push() -> None:
    """pytest 入口：整套 case 必须全绿（run_pytest_root 收集的就是它）。"""
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
