"""A4 设置界面入口回归测试（2026-10-02）。

- _proxy_config_path：VDL_HOME 优先，默认 ~/.video-downloader/proxy.json；
- GET/POST /api/settings/proxy（TestClient 直连回环）：初始空、保存落盘、
  回读一致、清空移除键、非法 URL 400；
- 云端防护：带 X-Forwarded-For 的请求 GET/POST 都 403（多用户部署不许改全局代理）；
- _resolve_proxy 分流：youtube/googlevideo 命中 proxy.json["youtube"]，B 站不受影响。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = tempfile.mkdtemp()
os.environ["VDL_DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["VDL_HOME"] = str(Path(_TMP) / "vdhome")   # proxy.json 落点对齐测试目录

import app as app_mod  # noqa: E402
import downloader  # noqa: E402
from downloader import _proxy_config_path, _resolve_proxy  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app_mod.app, client=("127.0.0.1", 51000))  # 模拟桌面 webview 回环直连


def test_proxy_config_path_respects_vdl_home():
    p = _proxy_config_path()
    assert str(p).startswith(os.environ["VDL_HOME"]), p
    assert p.name == "proxy.json"


def test_get_initial_empty():
    r = client.get("/api/settings/proxy")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["source"] == "file" and d["youtube"] == ""


def test_save_readback_clear():
    r = client.post("/api/settings/proxy", json={"youtube": "socks5h://127.0.0.1:40000"})
    assert r.status_code == 200 and r.json()["ok"]
    raw = json.loads(_proxy_config_path().read_text(encoding="utf-8"))
    assert raw == {"youtube": "socks5h://127.0.0.1:40000"}, raw
    d = client.get("/api/settings/proxy").json()
    assert d["youtube"] == "socks5h://127.0.0.1:40000"

    # 清空 → 键被移除而不是留空串
    r = client.post("/api/settings/proxy", json={"youtube": ""})
    assert r.status_code == 200
    raw = json.loads(_proxy_config_path().read_text(encoding="utf-8"))
    assert "youtube" not in raw, raw


def test_invalid_url_400():
    r = client.post("/api/settings/proxy", json={"youtube": "ftp://nope"})
    assert r.status_code == 400, r.text


def test_forwarded_requests_blocked():
    # 云端多用户部署经 nginx 反代必带 X-Forwarded-For：不许任何远端用户改全局代理
    assert client.get("/api/settings/proxy", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
    assert client.post(
        "/api/settings/proxy", json={"youtube": "http://evil:1"},
        headers={"X-Forwarded-For": "1.2.3.4"},
    ).status_code == 403
    # 落盘未被污染
    raw = json.loads(_proxy_config_path().read_text(encoding="utf-8"))
    assert raw.get("youtube") != "http://evil:1", raw


def test_resolve_proxy_routing():
    _proxy_config_path().parent.mkdir(parents=True, exist_ok=True)
    _proxy_config_path().write_text(
        json.dumps({"youtube": "socks5h://9.9.9.9:1080"}), encoding="utf-8"
    )
    os.environ.pop("VDL_PROXY_YT", None)   # 环境变量优先级更高，测试先排除
    assert _resolve_proxy("www.youtube.com") == "socks5h://9.9.9.9:1080"
    assert _resolve_proxy("rr3---sn-x.googlevideo.com") == "socks5h://9.9.9.9:1080"
    assert _resolve_proxy("www.bilibili.com") != "socks5h://9.9.9.9:1080"


if __name__ == "__main__":
    test_proxy_config_path_respects_vdl_home()
    test_get_initial_empty()
    test_save_readback_clear()
    test_invalid_url_400()
    test_forwarded_requests_blocked()
    test_resolve_proxy_routing()
    print("test_proxy_settings: 6/6 OK")
