#!/usr/bin/env python3
"""扩展「浏览器判定精确化」的离线守卫（2026-10-13 接续待办 #4）。

背景：设备画像的 `ext.browser` 原先只按**扩展加载目录路径**推断，而默认加载目录是
受管目录 `~/视频工坊浏览器扩展`（中性名、无浏览器特征）⇒ 几乎恒为 chromium；
且 Windows 路径是反斜杠，`"google/chrome"` 永远匹配不上。

修复：扩展发往本机的请求自带**浏览器的 User-Agent**，服务端记下最近一次的 UA，
优先据此判定浏览器；路径推断降为兜底并修正分隔符。

覆盖：
  ① browser_from_ua：各浏览器识别 + 顺序敏感（Edge/Opera 的 UA 含 "chrome/"）；
  ② browser_from_load_dir：Windows 反斜杠能认出 chrome；中性受管目录 → ""；
  ③ record_client_ua 落盘 + get_config 读回；
  ④ _device_meta：优先 UA > 加载目录 > 都不认得则不瞎填；
  ⑤ 授权中心 _clean_device_meta：白名单接受新浏览器、非法仍丢弃。

零网络、零用户数据；写盘只落 tempfile。
"""
from __future__ import annotations

import ast
import pathlib
import sys
import tempfile
from unittest.mock import patch

SERVER = pathlib.Path(__file__).resolve().parent.parent
REPO = SERVER.parent
sys.path.insert(0, str(SERVER))

import extension_sync as es  # noqa: E402
import routers.cloud_account as ca  # noqa: E402

_PASS = _FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}  {detail}")


# ── ① UA 判定 ──────────────────────────────────────────────────────────────
def test_browser_from_ua() -> None:
    cases = [
        ("Mozilla/5.0 (Macintosh) Chrome/120.0 Safari/537.36", "chrome"),
        ("Mozilla/5.0 Chrome/120.0 Safari/537.36 Edg/120.0", "edge"),
        ("Mozilla/5.0 Chrome/120.0 OPR/105.0", "opera"),
        ("Mozilla/5.0 Chrome/120.0 Vivaldi/6.5", "vivaldi"),
        ("Mozilla/5.0 YaBrowser/23.9 Chrome/120.0", "yandex"),
        ("Mozilla/5.0 Brave/1.60 Chrome/120.0", "brave"),
        ("Mozilla/5.0 (Windows) Firefox/121.0", "firefox"),
        ("Mozilla/5.0 (Macintosh) Version/17.0 Safari/605.1", "safari"),
        ("", ""),
        ("curl/8.4.0", ""),
    ]
    for ua, exp in cases:
        got = es.browser_from_ua(ua)
        check(f"UA→{exp or '(空)'}", got == exp, f"{got!r} != {exp!r}")
    # 顺序敏感：Edge 的 UA 也含 "chrome/" 与 "safari/"，必须判成 edge 而非 chrome
    check("Edge UA 不被误判为 chrome（顺序敏感）",
          es.browser_from_ua("Chrome/120 Safari/537.36 Edg/120") == "edge")


# ── ② 路径兜底（修 Windows 反斜杠）────────────────────────────────────────
def test_browser_from_load_dir() -> None:
    check("Windows 反斜杠路径 → chrome（旧实现恒落 chromium）",
          es.browser_from_load_dir(r"C:\Users\x\AppData\Local\Google\Chrome\User Data\Ext") == "chrome")
    check("macOS Chrome 路径 → chrome",
          es.browser_from_load_dir("/Users/x/Library/Application Support/Google/Chrome/Ext") == "chrome")
    check("Edge 路径 → edge",
          es.browser_from_load_dir("/Users/x/Library/Application Support/Microsoft Edge/Ext") == "edge")
    check("Brave 路径 → brave", es.browser_from_load_dir("/x/BraveSoftware/Brave-Browser/Ext") == "brave")
    check("中性受管目录 → 认不出（返回空，不再瞎填）",
          es.browser_from_load_dir("/Users/x/视频工坊浏览器扩展") == "")
    check("空路径 → 空", es.browser_from_load_dir("") == "")


# ── ③ UA 落盘 ──────────────────────────────────────────────────────────────
def test_record_client_ua_persists() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vdl_ua_"))
    with patch.object(es, "_config_path", return_value=tmp / "extension_sync.json"):
        es.record_client_ua("Mozilla/5.0 Chrome/120.0")
        check("记录后 get_config 读回 last_ua",
              es.get_config().get("last_ua") == "Mozilla/5.0 Chrome/120.0")
        es.record_client_ua("")
        check("空 UA 不改动", es.get_config().get("last_ua") == "Mozilla/5.0 Chrome/120.0")
        es.record_client_ua("Mozilla/5.0 Edg/120")
        check("再次记录覆盖", es.get_config().get("last_ua") == "Mozilla/5.0 Edg/120")


# ── ④ _device_meta 优先级 ──────────────────────────────────────────────────
def _meta_with(last_ua: str, load_dir: str) -> dict:
    with patch.object(es, "get_config",
                      return_value={"load_dir": load_dir, "auto": False, "last_ua": last_ua}), \
         patch.object(es, "read_version", return_value="1.2.3"):
        return ca._device_meta()


def test_device_meta_prefers_ua_over_neutral_dir() -> None:
    m = _meta_with("Mozilla/5.0 Chrome/120 Safari/537.36 Edg/120",
                   "/Users/x/视频工坊浏览器扩展")
    check("UA=Edge 压过中性加载目录 → edge", (m.get("ext") or {}).get("browser") == "edge", str(m))
    check("ext.version 仍带上", (m.get("ext") or {}).get("version") == "1.2.3")


def test_device_meta_falls_back_to_load_dir() -> None:
    m = _meta_with("", r"C:\Users\x\AppData\Local\Google\Chrome\Ext")
    check("无 UA → 退回加载目录（Windows 反斜杠也认出 chrome）",
          (m.get("ext") or {}).get("browser") == "chrome", str(m))


def test_device_meta_omits_browser_when_unknown() -> None:
    m = _meta_with("curl/8.0", "/Users/x/视频工坊浏览器扩展")
    ext = m.get("ext") or {}
    check("都认不出 → 不瞎填 chromium（browser 缺失）", "browser" not in ext, str(m))
    check("但版本仍保留", ext.get("version") == "1.2.3", str(m))


# ── ⑤ 授权中心白名单 ───────────────────────────────────────────────────────
def test_center_accepts_new_browsers() -> None:
    src = (REPO / "deploy" / "license_server.py").read_text(encoding="utf-8")
    fn = next((n for n in ast.parse(src).body
               if isinstance(n, ast.FunctionDef) and n.name == "_clean_device_meta"), None)
    assert fn is not None, "license_server 缺 _clean_device_meta"
    ns: dict = {"dict": dict, "str": str, "isinstance": isinstance}
    exec(compile(ast.Module([fn], []), "license_server_frag", "exec"), ns)
    clean = ns["_clean_device_meta"]
    for b in ("chrome", "edge", "brave", "chromium", "firefox", "safari",
              "opera", "vivaldi", "yandex"):
        out = clean({"ext": {"browser": b, "version": "1"}})
        check(f"白名单接受 {b}", (out.get("ext") or {}).get("browser") == b, str(out))
    out = clean({"ext": {"browser": "junk"}})
    check("非法浏览器仍被丢弃", "ext" not in out, str(out))


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
