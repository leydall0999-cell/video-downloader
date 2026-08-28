"""Phase 1 物理隔离守门测试。

确保各功能模块已抽到独立 routers/<group>.py，且未残留在 server/app.py 中，
且 app.py 末尾确有 include_router 接线把它们挂进 app。
一旦有人把某个功能 handler 又写回 app.py、或删掉 include_router 接线，本测试立即红。

注意：app.py 里的 _<group>_rtr 按 env 开关（SUB_ENABLED 等）在 import 期条件性填充，
没开 env 时这些 router 是空的，运行时 app.routes 数量与具体路由随 import 顺序/env 浮动。
所以本测试用「静态源码 + router 模块自身路由」校验物理隔离与接线，不依赖运行时路由数，
避免 import 顺序 / env 导致的误报（也与 Phase3 后 core 路由抽进 routers/core.py 的解耦一致）。
"""
import os
import sys

SERVER = os.path.join(os.path.dirname(__file__), "..", "server")
sys.path.insert(0, os.path.abspath(SERVER))

import app as app_module  # noqa: E402  (触发 app.py 全量加载，确保 app 对象存在)

# group -> 该组内一个代表性 handler 函数名（必须只存在于路由器文件，不在 app.py）
GROUP_HANDLERS = {
    "commentary": "commentary_list",
    "convert": "convert_create",
    "crypto": "crypto_status",
    "fs": "fs_open",
    "library": "library_list",
    "llm": "llm_providers",
    "process": "process_run",
    "retention": "retention_config_get",
    "subscriptions": "subscriptions_list",
    "subtitles": "subtitles_list",
    "torrents": "torrents_list",
}

app_src = open(os.path.join(os.path.abspath(SERVER), "app.py"), encoding="utf-8").read()
core_src = open(os.path.join(os.path.abspath(SERVER), "routers", "core.py"), encoding="utf-8").read()


def test_all_feature_routers_present_and_registered():
    """每个功能组都必须：router 模块自身定义了路由 + handler 不残留在 app.py + app.py 有接线。"""
    for group, handler in GROUP_HANDLERS.items():
        mod = __import__(f"routers.{group}", fromlist=["router"])
        # 1) router 模块自身必须定义了路由（证明已抽到独立文件，而非留在 app.py）
        assert len(mod.router.routes) > 0, f"{group} router 无路由（模块未定义任何路由？）"
        # 2) 该组 handler 必须已从 app.py 移除（物理隔离，不能回潮）
        assert f"def {handler}" not in app_src, (
            f"隔离被破坏：{handler} 仍定义在 server/app.py（应只存在于 routers/{group}.py）"
        )
        # 3) app.py 必须确有 include_router 接线把它挂进 app（防止有人移除接线）
        assert f"app.include_router(_{group}_rtr.router)" in app_src, (
            f"接线缺失：app.py 未 include_router(_{group}_rtr.router)，{group} 模块未挂载"
        )


def test_core_handlers_stay_in_app_py():
    """核心下载链路（web 与 app 共享）必须仍通过 core router 挂在 app 上，不被误抽离。

    Phase3 后核心路由抽到 routers/core.py（@router.* 而非 @app.*），本测试校验：
    app.py 仍有 include_router(_core_rtr.router) 接线，且 core.py 真定义了这些核心路径。
    不依赖源码里的装饰器字面量，也不依赖运行时路由表，稳健且不受重构影响。
    """
    assert "app.include_router(_core_rtr.router)" in app_src, (
        "接线缺失：app.py 未 include_router(_core_rtr.router)，核心路由未挂进 app"
    )
    core_paths = ["/api/version", "/api/nodes", "/api/resolve", "/api/download", "/api/batch"]
    for p in core_paths:
        assert f"'{p}'" in core_src, f"核心路由 {p} 未在 routers/core.py 定义（不应被误抽离 app 链路）"
