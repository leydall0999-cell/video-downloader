"""Phase 1 物理隔离守门测试。

确保各功能模块已抽到独立 routers/<group>.py，且未残留在 server/app.py 中。
一旦有人把某个功能 handler 又写回 app.py，本测试立即红。
"""
import ast
import os
import sys

SERVER = os.path.join(os.path.dirname(__file__), "..", "server")
sys.path.insert(0, os.path.abspath(SERVER))

import app as app_module  # noqa: E402  (触发 app.py 全量加载 + 挂载)
from fastapi.testclient import TestClient  # noqa: E402

# group -> 该组内一个代表性 handler 函数名（必须只存在于路由器文件，不在 app.py）
GROUP_HANDLERS = {
    "archive": "archive_config_get",
    "baidu_dlink": "baidu_dlink",
    "cloud": "cloud_providers",
    "commentary": "commentary_list",
    "convert": "convert_create",
    "crypto": "crypto_status",
    "fs": "fs_open",
    "library": "library_list",
    "llm": "llm_providers",
    "pcs": "pcs_status",
    "process": "process_run",
    "retention": "retention_config_get",
    "subscriptions": "subscriptions_list",
    "subtitles": "subtitles_list",
    "torrents": "torrents_list",
}

app_src = open(os.path.join(os.path.abspath(SERVER), "app.py"), encoding="utf-8").read()


def test_all_feature_routers_present_and_registered():
    """每个功能组都应作为独立 router 挂载，且注册了 >0 条路由。"""
    client = TestClient(app_module.app)
    for group, handler in GROUP_HANDLERS.items():
        mod = __import__(f"routers.{group}", fromlist=["router"])
        assert len(mod.router.routes) > 0, f"{group} router 无路由"
        # 该组 handler 必须已从 app.py 移除（物理隔离）
        assert f"def {handler}" not in app_src, (
            f"隔离被破坏：{handler} 仍定义在 server/app.py（应只存在于 routers/{group}.py）"
        )
    # 总路由数应大于核心路由与各 router 之和（粗略守门，防止整体挂载丢失）
    assert len(client.app.routes) > 30


def test_core_handlers_stay_in_app_py():
    """核心下载链路（web 与 app 共享）必须仍留在 app.py，不被误抽走。"""
    core_decorators = [
        '@app.get("/api/version")',
        '@app.get("/api/nodes")',
        '@app.post("/api/resolve")',
        '@app.post("/api/download")',
        '@app.post("/api/batch")',
    ]
    for deco in core_decorators:
        assert deco in app_src, f"核心路由 {deco} 不应被抽出 app.py"
