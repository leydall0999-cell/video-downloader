#!/usr/bin/env python3
"""App 内更新下载计数（8765 分发服务）的离线守卫（2026-10-13 接续待办 #5）。

背景：App 内自动更新从 8765 直链下载包/补丁，此前**不计入任何统计**。新增
`deploy/update_server.py`：分发的同时把包/补丁的 GET 计入下载统计文件
（bucket `app_update`，与官网下载分开计口径），形状与 web 侧 dl_stats 一致，
授权中心 stat_query 天然合并。

覆盖：
  ① 纯函数：countable（只计 .zip/.delta）/ is_app_download（只认 VDL-Update/ UA）/
     safe_rel_path（防路径穿越）；
  ② record：写入形状正确、by_day 90 天上限、原子落盘；
  ③ 本地 HTTP 冒烟：GET 包 → 计数 +1 且内容正确；HEAD → 不计数；浏览器 UA → 不计数；
     目录列表/穿越 → 404；缺文件 → 404。

零外部网络（只用 127.0.0.1 回环）；写盘只落 tempfile。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[2]

# 必须在 import 前设好（模块级常量在 import 时读 env）
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="vdl_upd_"))
_UPDATE_DIR = _TMP / "update"
_UPDATE_DIR.mkdir(parents=True, exist_ok=True)
(_UPDATE_DIR / "latest.json").write_text('{"version":"1.0.66"}', encoding="utf-8")
(_UPDATE_DIR / "VideoDownloader.app.zip").write_bytes(b"PK" + b"z" * 4096)
(_UPDATE_DIR / "patch.delta").write_bytes(b"d" * 128)
(_UPDATE_DIR / "index.html").write_text("<h1>vdl-update</h1>", encoding="utf-8")
_STATS = _TMP / "downloads.json"

import os  # noqa: E402
os.environ["VDL_UPDATE_DIR"] = str(_UPDATE_DIR)
os.environ["VDL_DL_STATS_PATH"] = str(_STATS)

_spec = importlib.util.spec_from_file_location(
    "vdl_update_server", REPO / "deploy" / "update_server.py")
upd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(upd)  # type: ignore[union-attr]

_PASS = _FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {detail}")


# ── ① 纯函数 ───────────────────────────────────────────────────────────────
def test_countable() -> None:
    check("zip → app_update/macos", upd.countable("VideoDownloader.app.zip") == ("app_update", "macos"))
    check("delta → app_update/macos", upd.countable("patch.delta") == ("app_update", "macos"))
    check("latest.json 不算下载", upd.countable("latest.json") is None)
    check(".sig 不算下载", upd.countable("latest.json.sig") is None)
    check("无扩展名不算", upd.countable("README") is None)


def test_is_app_download() -> None:
    check("App UA 命中", upd.is_app_download("VDL-Update/1.0") is True)
    check("浏览器 UA 不命中", upd.is_app_download("Mozilla/5.0 Chrome/120") is False)
    check("空 UA 不命中", upd.is_app_download("") is False)
    check("前缀必须完整（VDL-Update 后无斜杠不命中）",
          upd.is_app_download("VDL-UpdateX") is False)


def test_safe_rel_path() -> None:
    check("普通路径", upd.safe_rel_path("/VideoDownloader.app.zip") == "VideoDownloader.app.zip")
    check("URL 编码还原", upd.safe_rel_path("/a%20b.zip") == "a b.zip")
    check("穿越被拒", upd.safe_rel_path("/../../etc/passwd") is None)
    check("内嵌穿越被拒", upd.safe_rel_path("/sub/../../etc/passwd") is None)
    check("空路径被拒", upd.safe_rel_path("/") is None)
    check("反斜杠按分隔符处理且穿越被拒", upd.safe_rel_path("\\..\\..\\x") is None)


# ── ② record 落盘 ──────────────────────────────────────────────────────────
def test_record_shape_and_cap() -> None:
    if _STATS.exists():
        _STATS.unlink()
    upd.record("app_update", "macos")
    upd.record("app_update", "macos")
    data = json.loads(_STATS.read_text(encoding="utf-8"))
    slot = data["app_update"]["macos"]
    check("total 累加", slot["total"] == 2, str(slot))
    check("by_day 记当日", sum(slot["by_day"].values()) == 2, str(slot["by_day"]))
    check("带 updated_at", isinstance(data.get("updated_at"), (int, float)))
    # 90 天上限
    slot["by_day"] = {f"2020-01-{i:02d}": 1 for i in range(1, 32)}
    slot["by_day"].update({f"2021-01-{i:02d}": 1 for i in range(1, 32)})
    slot["by_day"].update({f"2022-01-{i:02d}": 1 for i in range(1, 32)})
    _STATS.write_text(json.dumps(data), encoding="utf-8")
    upd.record("app_update", "macos")
    kept = json.loads(_STATS.read_text(encoding="utf-8"))["app_update"]["macos"]["by_day"]
    check("by_day 裁剪到 ≤90 天", len(kept) <= 90, str(len(kept)))


# ── ③ 本地 HTTP 冒烟 ───────────────────────────────────────────────────────
def test_http_serving_and_counting() -> None:
    srv = upd.ThreadingHTTPServer(("127.0.0.1", 0), upd._Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    try:
        if _STATS.exists():
            _STATS.unlink()

        def _get(path, ua):
            req = urllib.request.Request(base + path, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()

        def _head(path, ua):
            req = urllib.request.Request(base + path, method="HEAD", headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, len(r.read())

        st, body = _get("/VideoDownloader.app.zip", "VDL-Update/1.0")
        check("GET 包 → 200 且内容一致", st == 200 and body == b"PK" + b"z" * 4096, str(st))
        check("GET 包（App UA）→ 计数 +1",
              json.loads(_STATS.read_text(encoding="utf-8"))["app_update"]["macos"]["total"] == 1)

        _head("/VideoDownloader.app.zip", "VDL-Update/1.0")
        check("HEAD → 不计数（预检不算下载）",
              json.loads(_STATS.read_text(encoding="utf-8"))["app_update"]["macos"]["total"] == 1)

        _get("/VideoDownloader.app.zip", "Mozilla/5.0 Chrome/120")
        check("浏览器 UA → 不计数",
              json.loads(_STATS.read_text(encoding="utf-8"))["app_update"]["macos"]["total"] == 1)

        st, _ = _get("/latest.json", "VDL-Update/1.0")
        check("latest.json 可取但不计数",
              st == 200 and json.loads(_STATS.read_text(encoding="utf-8"))["app_update"]["macos"]["total"] == 1)

        st, body = _get("/", "Mozilla/5.0")
        check("根路径回退 index.html（保留原 http.server 行为）",
              st == 200 and b"vdl-update" in body, f"{st} {body[:40]!r}")

        try:
            _get("/../../etc/passwd", "VDL-Update/1.0")
            check("穿越应 404", False, "未拒绝")
        except urllib.error.HTTPError as e:
            check("路径穿越 → 404", e.code == 404, str(e.code))
        try:
            _get("/nope.zip", "VDL-Update/1.0")
            check("缺文件应 404", False, "未拒绝")
        except urllib.error.HTTPError as e:
            check("缺文件 → 404", e.code == 404, str(e.code))
    finally:
        srv.shutdown()
        srv.server_close()


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
