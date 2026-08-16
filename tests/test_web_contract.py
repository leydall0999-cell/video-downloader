"""网页版契约冒烟测试（Web contract smoke test）。

这是「网页版 / App 多端并行开发」安全网的核心一环：
- 锁定网页版依赖的 /api/nodes 顶层结构（标量字段 + 功能组集合）。
- 锁定网页版最常用的核心路由（/api/version、/api/resolve）仍可正常响应。
- 断言网页版运行在 WEB profile 下（is_desktop()==False），并锁定 WEB 能力集 = 完整功能组。

任何后续对共享后端 / /api/nodes 的改动，只要破坏了网页版既有的契约或能力暴露，
本测试会立刻变红——哪怕改动来自 App / 其他端的开发会话。

运行：仓库根目录下 `.build_venv/bin/pytest tests/test_web_contract.py -q`
"""
import os
import sys
from pathlib import Path

# 显式锁定「网页版」profile，使测试结果确定、不受运行环境（是否打包）影响。
os.environ["VDL_PLATFORM"] = "web"
os.environ.setdefault("VDL_COMMENTARY_ENABLED", "false")

SERVER = str(Path(__file__).resolve().parent.parent / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

from fastapi.testclient import TestClient  # noqa: E402
import app as m  # noqa: E402
import platform_model as plat  # noqa: E402

client = TestClient(m.app)

# 网页版当前暴露的全部功能组（与 platform_model.NODE_GROUPS 一致）。
# 今后若要为网页版收窄能力，必须是有意为之并同步更新此处断言。
EXPECTED_NODE_GROUPS = set(plat.NODE_GROUPS)
SCALAR_FIELDS = {"region", "peer", "china_domains", "commentary_enabled", "ads_enabled", "authRequired"}


def test_web_profile_is_not_desktop():
    """网页版必须运行在 WEB profile 下——本测试其余断言都依赖此前提。"""
    assert plat.current_platform() == plat.Platform.WEB
    assert plat.is_desktop() is False
    # 网页版能力集当前 = 完整功能组（行为不变）；收窄需有意为之。
    assert plat.node_capabilities(plat.Platform.WEB) == EXPECTED_NODE_GROUPS


def test_nodes_web_contract():
    """/api/nodes 必须返回网页版依赖的全部标量字段与功能组。"""
    r = client.get("/api/nodes")
    assert r.status_code == 200, r.text
    body = r.json()

    missing_scalars = SCALAR_FIELDS - set(body.keys())
    assert not missing_scalars, f"/api/nodes 缺少网页版标量字段: {missing_scalars}"

    group_keys = set(body.keys()) - SCALAR_FIELDS
    missing_groups = EXPECTED_NODE_GROUPS - group_keys
    assert not missing_groups, f"/api/nodes 缺少网页版功能组: {missing_groups}"

    # 网页版未打包：library / subscriptions 等桌面功能默认关闭（enabled=False），
    # 但「字段」必须存在（前端据此决定是否渲染面板）。
    assert "enabled" in body["library"]
    assert "enabled" in body["subscriptions"]


def test_version_route_ok():
    """网页版核心路由 /api/version 必须可用。"""
    r = client.get("/api/version")
    assert r.status_code == 200, r.text


def test_resolve_validation_intact():
    """网页版核心路由 /api/resolve 必须仍按契约校验入参（缺 url 应被拒，而非 500）。"""
    r = client.post("/api/resolve", json={})
    assert r.status_code in (400, 422), r.text
